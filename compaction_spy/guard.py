"""Path guards: refuse writes under the protected OpenClaw state dir.

The live store under ``~/.openclaw`` (or ``$OPENCLAW_STATE_DIR``) must never be
written to by this tool. Every write target (snapshot --to, audit --out, temp
dirs) is checked here, with symlinks resolved first so that a symlink pointing
into the protected root is also refused. ``audit`` / ``census`` additionally
refuse to *read* a --db under the protected root: only ``snapshot`` may read
from there, via a read-only URI connection.
"""

from __future__ import annotations

import os
from pathlib import Path


class GuardError(Exception):
    """A path violates the read-only / no-write policy."""


def protected_roots(extra_root: Path | None = None) -> list[Path]:
    """The roots this tool must never write under (and mostly never read)."""
    roots = [Path.home() / ".openclaw"]
    env = os.environ.get("OPENCLAW_STATE_DIR")
    if env:
        roots.append(Path(env))
    if extra_root is not None:
        roots.append(extra_root)
    # Resolve without requiring existence; do not create anything.
    return [r.expanduser().resolve() for r in roots]


def _is_under(path: Path, root: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved == root or root in resolved.parents


def check_write_path(target: Path, *, roots: list[Path] | None = None) -> Path:
    """Refuse any write target under a protected root. Returns the resolved path.

    Resolution follows symlinks, so a symlink that points inside the protected
    root is refused even if the symlink itself lives elsewhere. Nothing is
    created or modified by this check.
    """
    roots = roots if roots is not None else protected_roots()
    resolved = target.expanduser().resolve()
    for root in roots:
        if _is_under(resolved, root):
            raise GuardError(
                f"refusing to write under protected root {root}: {target} resolves to {resolved}"
            )
    return resolved


def check_read_db(db: Path, *, roots: list[Path] | None = None) -> Path:
    """Refuse to read a --db that lives under the protected root (live store).

    Only ``snapshot`` reads the live store, and it does so read-only. Audit and
    census must operate on snapshots.
    """
    roots = roots if roots is not None else protected_roots()
    resolved = db.expanduser().resolve()
    for root in roots:
        if _is_under(resolved, root):
            raise GuardError(
                f"refusing to read a database under protected root {root}: {db}. "
                "Take a snapshot first with 'compaction-spy snapshot'."
            )
    return resolved
