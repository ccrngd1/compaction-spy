"""Consistent read-only snapshotting of an agent store.

Only this module may read under the protected root, and only via a
``mode=ro`` URI connection plus the sqlite3 backup API. The copy goes to a
temp file in the destination dir, is integrity-checked, then atomically
renamed. On busy/locked/integrity failure the partial file is removed and a
clear error raised. A marker sidecar is written next to each snapshot so that
``audit --cleanup`` can later delete only files this tool created.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .guard import check_write_path
from .store import connect_ro

MARKER_SUFFIX = ".compaction-spy-snapshot.json"


class SnapshotError(Exception):
    pass


def _octet_length(value: object) -> int | None:
    """Python stand-in for SQLite's ``octet_length()`` (native since 3.43)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8"))


def ensure_sql_functions(conn: sqlite3.Connection) -> None:
    """Register functions that newer OpenClaw schemas use in CHECK constraints.

    Live ``transcript_events`` has a CHECK that uses ``octet_length()``. Older
    linked SQLite builds (e.g. 3.40) lack it, so ``PRAGMA integrity_check`` on
    an exact copy fails with "unknown function". Registered only when the
    native function is missing.
    """
    try:
        conn.execute("select octet_length('a')").fetchone()
    except sqlite3.OperationalError:
        conn.create_function("octet_length", 1, _octet_length, deterministic=True)


def agent_db_path(agent_id: str, state_root: Path | None = None) -> Path:
    root = state_root if state_root is not None else Path.home() / ".openclaw"
    return root / "agents" / agent_id / "agent" / "openclaw-agent.sqlite"


def marker_path(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.name + MARKER_SUFFIX)


def write_marker(snapshot: Path, source: Path) -> None:
    marker_path(snapshot).write_text(
        json.dumps({"tool": "compaction-spy", "source": str(source), "snapshot": snapshot.name})
    )


def has_marker(snapshot: Path) -> bool:
    return marker_path(snapshot).exists()


def cleanup_snapshot(snapshot: Path) -> bool:
    """Delete a snapshot DB only if it carries our marker. Never touches other files."""
    if not has_marker(snapshot):
        return False
    snapshot.unlink(missing_ok=True)
    marker_path(snapshot).unlink(missing_ok=True)
    return True


def take_snapshot(
    source: Path, dest_dir: Path, name: str | None = None, *, roots: list[Path] | None = None
) -> Path:
    """Snapshot ``source`` into ``dest_dir`` (guarded, chmod 700), atomically."""
    dest_dir = check_write_path(dest_dir, roots=roots)
    if not source.exists():
        raise SnapshotError(f"source database not found: {source}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    final = dest_dir / (name or source.name)
    partial = dest_dir / (final.name + ".partial")
    try:
        src_conn = connect_ro(source)
        try:
            dst_conn = sqlite3.connect(partial)
            try:
                ensure_sql_functions(dst_conn)
                src_conn.backup(dst_conn)
                row = dst_conn.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise SnapshotError(f"integrity check failed on snapshot of {source}: {row}")
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, SnapshotError) as ex:
        partial.unlink(missing_ok=True)
        if isinstance(ex, SnapshotError):
            raise
        raise SnapshotError(
            f"snapshot of {source} failed (busy, locked or malformed): {ex}"
        ) from ex
    partial.rename(final)
    write_marker(final, source)
    return final
