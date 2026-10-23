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
