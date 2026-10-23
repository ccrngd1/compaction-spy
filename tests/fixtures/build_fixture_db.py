"""Build synthetic SQLite fixture stores matching the real OpenClaw schema.

Schema (from the spikes): transcript_events(session_id, seq, event_json,
event_zstd, created_at) and session_windows(session_id, session_key). Events
are a mix of zstd-compressed and plain JSON. All text is obviously synthetic:
frobnicator / widget-staging scenarios, session ids like ``sess-fixture-0001``.

Usage: ``python build_fixture_db.py <out_dir>`` or import ``build_fixture_store``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import zstandard

# --- synthetic content ------------------------------------------------------

TOOL_OUTPUT_FROBNICATOR = (
    "frobnicator config check: widget-staging reports rotation pending; "
    "config file `frob.toml` currently at revision 7."
)
USER_TURN_ROTATE = (
    "please rotate the frobnicator config on widget-staging before the demo "
    "and keep the sprocket cache warm"
)
ASSISTANT_PLAN = "I will rotate the frobnicator config next and then re-verify the sprocket cache."
TAIL_COMPLETION = (
    "rotation finished: frobnicator config on widget-staging now at revision 8, sprocket cache warm"
)

# Identifier present verbatim in history; one deliberately absent.
ID_PRESENT = "frob.toml"
ID_ABSENT = "missing-gadget.cfg"

SUMMARY_FIRST = f"""## Decisions
- Decided to rotate the frobnicator config on widget-staging
  before the demo window opens.

## Open TODOs
- Rotate the frobnicator config on widget-staging (still pending at the cut).

## Constraints/Rules
- Keep the sprocket cache warm during any rotation.

## Pending user asks
- none

## Exact identifiers
- Config file `{ID_PRESENT}` at revision 7.
- Phantom reference `{ID_ABSENT}` reported by nobody.
"""

SUMMARY_SECOND = """## Decisions
- Continue monitoring the widget-staging frobnicator after rotation.

## Open TODOs
- n/a

## Constraints/Rules
- none

## Pending user asks
- Confirm the sprocket cache stayed warm overnight.

## Exact identifiers
- none
"""

SUMMARY_LEGACY = (
    "The agent discussed frobnicator maintenance in free-form prose without "
    "any structured sections. Legacy format summary for counting only."
)

# Reused verbatim in two sessions/stores to exercise dedupe-by-hash.
SUMMARY_DUPLICATE = """## Decisions
- Standardize gizmo polishing cadence on widget-staging.

## Open TODOs
- Polish the gizmo rack.

## Constraints/Rules
- none

## Pending user asks
- none

## Exact identifiers
- none
"""


def _msg(role: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _tool_call_msg(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": name, "arguments": args}],
        },
    }


def _compaction(cid: str, summary: str, first_kept: str | None, ts: str) -> dict[str, Any]:
    return {
        "type": "compaction",
        "id": cid,
        "summary": summary,
        "tokensBefore": 90000,
        "firstKeptEntryId": first_kept,
        "timestamp": ts,
    }


def _with_id(event: dict[str, Any], eid: str) -> dict[str, Any]:
    return {**event, "id": eid}


def two_compaction_session() -> list[dict[str, Any]]:
    """A session with two compactions chained by firstKeptEntryId.

    Includes USER / TOOL / ASSISTANT messages, a tool call with arguments, a
    STALE setup (the rotate TODO is completed only in the kept tail), one
    identifier present verbatim and one absent.
    """
    return [
        _with_id(_msg("user", USER_TURN_ROTATE), "evt-0001"),
        _with_id(_msg("toolResult", TOOL_OUTPUT_FROBNICATOR), "evt-0002"),
        _with_id(_msg("assistant", ASSISTANT_PLAN), "evt-0003"),
        _with_id(
            _tool_call_msg("frob_rotate", {"target": "widget-staging", "config": ID_PRESENT}),
            "evt-0004",
        ),
        # Kept tail of compaction 1 starts here: the TODO completes only in the tail.
        _with_id(_msg("toolResult", TAIL_COMPLETION), "evt-0005"),
        _compaction("comp-0001", SUMMARY_FIRST, "evt-0005", "2026-09-05T10:00:00Z"),
        _with_id(_msg("user", "thanks, keep watching the frobnicator dial"), "evt-0006"),
        _with_id(_msg("assistant", "watching the widget-staging frobnicator dial now"), "evt-0007"),
        _compaction("comp-0002", SUMMARY_SECOND, "evt-0007", "2026-09-06T10:00:00Z"),
    ]


def legacy_session() -> list[dict[str, Any]]:
    return [
        _with_id(_msg("user", "tell me about frobnicator upkeep"), "evt-1001"),
        _compaction("comp-1001", SUMMARY_LEGACY, None, "2026-08-01T09:00:00Z"),
    ]


def duplicate_session(cid: str) -> list[dict[str, Any]]:
    return [
        _with_id(_msg("user", "set a gizmo polishing cadence on widget-staging"), f"evt-{cid}-1"),
        _compaction(f"comp-{cid}", SUMMARY_DUPLICATE, None, "2026-09-10T12:00:00Z"),
    ]


def write_store(
    db_path: Path, sessions: dict[str, list[dict[str, Any]]], *, zstd_every_other: bool = True
) -> Path:
    """Write a fixture store; alternate rows between zstd and plain JSON."""
    cctx = zstandard.ZstdCompressor()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "create table transcript_events ("
            "session_id text, seq integer, event_json text, event_zstd blob, created_at text)"
        )
        conn.execute("create table session_windows (session_id text, session_key text)")
        row = 0
        for sid, events in sessions.items():
            conn.execute("insert into session_windows values (?, ?)", (sid, f"key-{sid}"))
            for seq, event in enumerate(events):
                raw = json.dumps(event)
                if zstd_every_other and row % 2 == 1:
                    conn.execute(
                        "insert into transcript_events values (?, ?, NULL, ?, ?)",
                        (sid, seq, cctx.compress(raw.encode()), "2026-09-01T00:00:00Z"),
                    )
                else:
                    conn.execute(
                        "insert into transcript_events values (?, ?, ?, NULL, ?)",
                        (sid, seq, raw, "2026-09-01T00:00:00Z"),
                    )
                row += 1
        conn.commit()
    finally:
        conn.close()
    return db_path


def build_fixture_store(out_dir: Path) -> dict[str, Path]:
    """Build the full fixture set. Returns name -> path.

    - ``alpha.sqlite``: two-compaction session + legacy session + duplicate summary.
    - ``beta.sqlite``: the same duplicate summary in another store (dedupe).
    - ``broken.sqlite``: deliberately malformed, for census skip.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    alpha = write_store(
        out_dir / "alpha.sqlite",
        {
            "sess-fixture-0001": two_compaction_session(),
            "sess-fixture-0002": legacy_session(),
            "sess-fixture-0003": duplicate_session("dupA"),
        },
    )
    beta = write_store(
        out_dir / "beta.sqlite",
        {"sess-fixture-0004": duplicate_session("dupB")},
    )
    broken = out_dir / "broken.sqlite"
    broken.write_bytes(b"this is not a sqlite database, deliberately malformed")
    return {"alpha": alpha, "beta": beta, "broken": broken}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the synthetic fixture SQLite stores.")
    parser.add_argument("out_dir", type=Path, help="Directory to write the fixture stores into.")
    args = parser.parse_args()
    built = build_fixture_store(args.out_dir)
    for name, path in built.items():
        print(name, path)
