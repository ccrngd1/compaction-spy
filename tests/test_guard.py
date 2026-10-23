"""B1: the no-write guard, including the symlink case and the default root."""

from __future__ import annotations

from pathlib import Path

import pytest

from compaction_spy.guard import GuardError, check_read_db, check_write_path, protected_roots


@pytest.fixture()
def protected(tmp_path: Path) -> Path:
    root = tmp_path / "state" / ".openclaw"
    root.mkdir(parents=True)
    return root


def test_write_inside_protected_root_refused(protected: Path) -> None:
    with pytest.raises(GuardError, match="refusing to write"):
        check_write_path(protected / "snapshots", roots=[protected])


def test_write_to_root_itself_refused(protected: Path) -> None:
    with pytest.raises(GuardError):
        check_write_path(protected, roots=[protected])


def test_write_outside_allowed(protected: Path, tmp_path: Path) -> None:
    ok = tmp_path / "private-out"
    assert check_write_path(ok, roots=[protected]) == ok.resolve()


def test_symlink_into_protected_root_refused(protected: Path, tmp_path: Path) -> None:
    # A symlink that lives OUTSIDE the protected root but points INSIDE it.
    link = tmp_path / "innocent-looking-dir"
    link.symlink_to(protected / "agents")
    with pytest.raises(GuardError, match="resolves to"):
        check_write_path(link / "out", roots=[protected])


def test_default_root_is_home_openclaw_without_creating_anything() -> None:
    # Uses the real default (Path.home()/.openclaw) but never creates it.
    target = Path.home() / ".openclaw" / "agents" / "demo" / "out"
    existed_before = (Path.home() / ".openclaw").exists()
    with pytest.raises(GuardError):
        check_write_path(target)
    assert (Path.home() / ".openclaw").exists() == existed_before


def test_env_state_dir_added_to_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    alt = tmp_path / "alt-state"
    alt.mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(alt))
    assert alt.resolve() in protected_roots()
    with pytest.raises(GuardError):
        check_write_path(alt / "report-out")


def test_read_db_under_protected_root_refused(protected: Path) -> None:
    live = protected / "agents" / "demo" / "agent" / "openclaw-agent.sqlite"
    with pytest.raises(GuardError, match="snapshot"):
        check_read_db(live, roots=[protected])


def test_read_db_outside_allowed(tmp_path: Path, protected: Path) -> None:
    db = tmp_path / "snap.sqlite"
    assert check_read_db(db, roots=[protected]) == db.resolve()
