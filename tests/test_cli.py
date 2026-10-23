"""B2 (privacy) and B4 (census + audit offline, cost abort) via typer's CliRunner."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from compaction_spy.cli import app
from tests.fixtures.build_fixture_db import (
    SUMMARY_DUPLICATE,
    SUMMARY_FIRST,
    SUMMARY_SECOND,
    build_fixture_store,
)

runner = CliRunner()

# Every bullet's text from the fixture summaries: none may leak into reports.
FIXTURE_ENTRY_STRINGS = [
    "rotate the frobnicator config on widget-staging",
    "Keep the sprocket cache warm",
    "Config file",
    "Phantom reference",
    "Continue monitoring the widget-staging frobnicator",
    "Confirm the sprocket cache stayed warm",
    "Standardize gizmo polishing cadence",
    "Polish the gizmo rack",
]
STUB_RATIONALE_MARKER = "stub rationale echoing"


def _audit(dbs: dict[str, Path], out: Path, *extra: str) -> object:
    args = [
        "audit",
        "--db",
        str(dbs["alpha"]),
        "--db",
        str(dbs["beta"]),
        "--out",
        str(out),
        "--judge",
        "stub",
        "--no-cleanup",
        *extra,
    ]
    return runner.invoke(app, args)


def test_census_runs_offline(fixture_dbs: dict[str, Path]) -> None:
    result = runner.invoke(
        app,
        [
            "census",
            "--db",
            str(fixture_dbs["alpha"]),
            "--db",
            str(fixture_dbs["beta"]),
            "--db",
            str(fixture_dbs["broken"]),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"unique_compactions": 4' in result.output.replace("\n", "")
    assert "broken" in result.output


def test_census_refuses_db_under_protected_root(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    root = tmp_path / ".openclaw"
    (root / "agents").mkdir(parents=True)
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(root))
    db = root / "agents" / "live.sqlite"
    db.write_bytes(b"")
    result = runner.invoke(app, ["census", "--db", str(db)])
    assert result.exit_code != 0
    assert "snapshot" in result.output.lower()


def test_audit_passes_with_generous_budget(fixture_dbs: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _audit(fixture_dbs, out, "--max-usd", "100")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert (out / "report.md").exists()
    assert (out / "report.json").exists()
    data = json.loads((out / "report.json").read_text())
    assert data["compactions_audited"] == 3  # two in sess-0001 + one deduped duplicate
    assert data["entries_judged"] > 0
    assert data["identifier_check"]["total"] == 2
    assert data["identifier_check"]["verbatim_found"] == 1
    assert oct(os.stat(out).st_mode & 0o777) == oct(0o700)


def test_audit_cost_abort_is_clean(fixture_dbs: dict[str, Path], tmp_path: Path) -> None:
    """B4: tiny budget -> non-zero exit, clear message, NO report files."""
    out = tmp_path / "out-abort"
    result = _audit(fixture_dbs, out, "--max-usd", "0.01")
    assert result.exit_code != 0  # type: ignore[attr-defined]
    assert "cost budget abort" in result.output  # type: ignore[attr-defined]
    assert "no report written" in result.output  # type: ignore[attr-defined]
    assert not (out / "report.md").exists()
    assert not (out / "report.json").exists()
    assert not (out / "entries.private.jsonl").exists()


def test_privacy_default_report_has_no_entry_text_or_rationales(
    fixture_dbs: dict[str, Path], tmp_path: Path
) -> None:
    """B2: no fixture entry string and no stub rationale in report.md/report.json."""
    out = tmp_path / "out-priv"
    result = _audit(fixture_dbs, out, "--max-usd", "100")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    for name in ("report.md", "report.json"):
        content = (out / name).read_text()
        for fragment in FIXTURE_ENTRY_STRINGS:
            assert fragment not in content, f"{fragment!r} leaked into {name}"
        assert STUB_RATIONALE_MARKER not in content, f"rationale leaked into {name}"
    # Opaque refs are present instead, and session ids are hashed.
    data = json.loads((out / "report.json").read_text())
    assert data["flagged_entry_refs"]
    assert all("sess-fixture" not in ref for ref in data["flagged_entry_refs"])


def test_include_text_writes_private_file_only_in_out(
    fixture_dbs: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "out-text"
    result = _audit(fixture_dbs, out, "--max-usd", "100", "--include-text")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    private = out / "entries.private.jsonl"
    assert private.exists()
    content = private.read_text()
    assert "frobnicator" in content  # entry text IS in the private file
    assert STUB_RATIONALE_MARKER in content  # and rationales too
    # ... but still not in the default reports.
    assert STUB_RATIONALE_MARKER not in (out / "report.md").read_text()
    # And nowhere else: only inside --out.
    strays = [p for p in tmp_path.rglob("entries.private.jsonl") if not p.is_relative_to(out)]
    assert strays == []


def test_audit_refuses_out_under_protected_root(
    fixture_dbs: dict[str, Path], tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    root = tmp_path / ".openclaw"
    root.mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(root))
    result = runner.invoke(
        app,
        ["audit", "--db", str(fixture_dbs["alpha"]), "--out", str(root / "out"), "--judge", "stub"],
    )
    assert result.exit_code != 0
    assert "refusing to write" in result.output


def test_audit_cleanup_deletes_only_marked_snapshots(tmp_path: Path) -> None:
    dbs = build_fixture_store(tmp_path / "snap")
    from compaction_spy.snapshot import write_marker

    write_marker(dbs["alpha"], Path("/synthetic/source.sqlite"))
    out = tmp_path / "out-clean"
    result = runner.invoke(
        app,
        [
            "audit",
            "--db",
            str(dbs["alpha"]),
            "--db",
            str(dbs["beta"]),
            "--out",
            str(out),
            "--judge",
            "stub",
            "--max-usd",
            "100",
            "--cleanup",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not dbs["alpha"].exists()  # marked -> deleted
    assert dbs["beta"].exists()  # unmarked user file -> kept


def test_snapshot_fixture_source_and_marker(tmp_path: Path) -> None:
    dbs = build_fixture_store(tmp_path / "src")
    dest = tmp_path / "private"
    result = runner.invoke(app, ["snapshot", "--source", str(dbs["alpha"]), "--to", str(dest)])
    assert result.exit_code == 0, result.output
    snap = dest / "alpha.sqlite"
    assert snap.exists()
    assert (dest / "alpha.sqlite.compaction-spy-snapshot.json").exists()
    assert oct(os.stat(dest).st_mode & 0o777) == oct(0o700)


def test_snapshot_refuses_to_under_protected_root(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    root = tmp_path / ".openclaw"
    root.mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(root))
    dbs = build_fixture_store(tmp_path / "src2")
    result = runner.invoke(
        app, ["snapshot", "--source", str(dbs["alpha"]), "--to", str(root / "snaps")]
    )
    assert result.exit_code != 0
    assert "refusing to write" in result.output


def test_snapshot_malformed_source_leaves_no_partial(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"deliberately not sqlite")
    dest = tmp_path / "dest"
    result = runner.invoke(app, ["snapshot", "--source", str(bad), "--to", str(dest)])
    assert result.exit_code != 0
    assert list(dest.glob("*.partial")) == []
    assert list(dest.glob("*.sqlite")) == []


def test_fixture_summaries_are_safeguard_format() -> None:
    # Sanity: fixture format matches what the parser expects.
    for s in (SUMMARY_FIRST, SUMMARY_SECOND, SUMMARY_DUPLICATE):
        assert "## Open TODOs" in s
