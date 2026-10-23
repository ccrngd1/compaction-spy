"""B3: the three banned words never appear in templates, outputs, or the repo.

The words are assembled from fragments here so they never appear literally in
the repo (the hygiene test enforces that too). Required wording instead:
"no source found".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from compaction_spy import judge, memory_mode, report
from compaction_spy.cli import app
from tests.fixtures.build_fixture_db import build_fixture_store

BANNED = [
    "INVEN" + "TED",
    "fabri" + "cated",
    "halluci" + "nated",
]

REPO = Path(__file__).resolve().parents[1]


def _assert_clean(text: str, where: str) -> None:
    low = text.lower()
    for word in BANNED:
        assert word.lower() not in low, f"banned word in {where}"


def test_prompts_and_label_constants_clean() -> None:
    _assert_clean(judge.RETRIEVAL_SYSTEM, "retrieval system prompt")
    _assert_clean(judge.FULLCTX_SYSTEM, "fullctx system prompt")
    _assert_clean(memory_mode.MEMORY_SYSTEM, "memory system prompt")
    _assert_clean(" ".join(judge.VALID_LABELS), "labels")
    _assert_clean(" ".join(report.LABEL_DISPLAY.values()), "display labels")


def test_rendered_reports_clean(tmp_path: Path) -> None:
    dbs = build_fixture_store(tmp_path / "stores")
    out = tmp_path / "out"
    runner = CliRunner()
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
            "--no-cleanup",
            "--include-text",
        ],
    )
    assert result.exit_code == 0, result.output
    _assert_clean(result.output, "audit CLI output")
    for name in ("report.md", "report.json", "entries.private.jsonl"):
        _assert_clean((out / name).read_text(), name)


def test_census_output_clean(tmp_path: Path) -> None:
    dbs = build_fixture_store(tmp_path / "stores")
    runner = CliRunner()
    result = runner.invoke(app, ["census", "--db", str(dbs["alpha"])])
    assert result.exit_code == 0
    _assert_clean(result.output, "census CLI output")


def test_no_banned_words_in_tracked_files() -> None:
    """The banned words must not appear literally anywhere in the repo."""
    files = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for rel in files:
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        _assert_clean(content, rel)
