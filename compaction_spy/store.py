"""Snapshot reading: serial zstd decode, event loading, session grouping.

Ported from the spike ``loadtx.py``. Schema (real OpenClaw agent store):

- ``transcript_events(session_id, seq, event_json, event_zstd, created_at)``
  where exactly one of ``event_json`` / ``event_zstd`` is populated per row.
- ``session_windows(session_id, session_key)``.

zstandard's decompressor is NOT thread-safe (the spike segfaulted when it was
shared across threads), so all decoding happens serially in the calling thread
before any judge threads start. ``decode_event`` asserts it runs on the main
thread to make that guarantee testable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import zstandard

Event = dict[str, Any]

_main_thread_only = True


def _decompressor() -> zstandard.ZstdDecompressor:
    return zstandard.ZstdDecompressor()


def decode_event(
    event_json: str | None, event_zstd: bytes | None, dctx: zstandard.ZstdDecompressor
) -> Event | None:
    """Decode one transcript_events row into an event dict (None if malformed).

    Must run serially on the main thread: zstandard decompressors are not
    thread-safe. Enforced here so a unit test can prove decode never happens
    inside the judge thread pool.
    """
    if _main_thread_only and threading.current_thread() is not threading.main_thread():
        raise RuntimeError("zstd decode must run serially on the main thread")
    raw = event_json
    if raw is None:
        if event_zstd is None:
            return None
        raw = dctx.decompress(event_zstd).decode("utf-8", "replace")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def connect_ro(db: Path) -> sqlite3.Connection:
    """Open a SQLite file read-only via URI; never creates or writes the file."""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def iter_events(db: Path) -> Iterator[tuple[str, int, Event]]:
    """Yield (session_id, seq, event) in session/seq order, decoding serially."""
    dctx = _decompressor()
    conn = connect_ro(db)
    try:
        cursor = conn.execute(
            "select session_id, seq, event_json, event_zstd, created_at "
            "from transcript_events order by session_id, seq"
        )
        for sid, seq, ej, ez, _ts in cursor:
            event = decode_event(ej, ez, dctx)
            if event is None:
                continue
            yield sid, seq, event
    finally:
        conn.close()


def session_keys(db: Path) -> dict[str, str]:
    """Map session_id -> session_key from session_windows (empty if absent)."""
    conn = connect_ro(db)
    try:
        rows = conn.execute("select session_id, session_key from session_windows")
        return {r[0]: r[1] for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def load_sessions(db: Path) -> dict[str, list[tuple[int, Event]]]:
    """Load all events grouped by session, decoded serially, ordered by seq."""
    sessions: dict[str, list[tuple[int, Event]]] = defaultdict(list)
    for sid, seq, event in iter_events(db):
        sessions[sid].append((seq, event))
    return dict(sessions)
