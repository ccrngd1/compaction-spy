"""Pipeline-level tests: STALE visibility, fullctx promotion, judge-error bucket,
mid-run abort cancelling pending work, memory mode restore + cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from compaction_spy.audit import run_audit
from compaction_spy.cost import CostAbort, CostGuard
from compaction_spy.judge import JudgeError, JudgeItem, JudgeUsage, StubJudge
from compaction_spy.memory_mode import parse_claims, restored_daily_logs
from compaction_spy.report import build_report_data


class SequencedJudge:
    """Opt-in fake: returns canned labels, can fail specific calls."""

    def __init__(self, label: str = "AGENT_ONLY", fail_calls: set[int] | None = None) -> None:
        self.label = label
        self.fail_calls = fail_calls or set()
        self.calls = 0

    def judge(
        self, system: str, user: str, n_entries: int, max_tokens: int = 8000
    ) -> tuple[list[JudgeItem], JudgeUsage]:
        self.calls += 1
        if self.calls in self.fail_calls:
            raise JudgeError("synthetic judge failure")
        items = [
            JudgeItem(i=i, label=self.label, kind="PLAN", stale=True, conf=0.8, why="w")
            for i in range(n_entries)
        ]
        return items, JudgeUsage(input_tokens=10, output_tokens=10)

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, JudgeUsage]:
        return "{}", JudgeUsage()


def test_fullctx_promotes_flagged_entries(fixture_dbs: dict[str, Path]) -> None:
    class PromotingJudge(SequencedJudge):
        def judge(
            self, system: str, user: str, n_entries: int, max_tokens: int = 8000
        ) -> tuple[list[JudgeItem], JudgeUsage]:
            self.calls += 1
            label = "TOOL" if "<full_history>" in user else "UNSUPPORTED"
            items = [
                JudgeItem(i=i, label=label, kind="FACT", stale=False, conf=0.9, why="w")
                for i in range(n_entries)
            ]
            return items, JudgeUsage(input_tokens=10, output_tokens=10)

    judge = PromotingJudge()
    result = run_audit([fixture_dbs["alpha"]], judge, max_usd=100)
    data = build_report_data(result)
    # Every retrieval flag was re-judged sourced with full context.
    assert data["labels"] == {"SOURCED:TOOL": data["entries_judged"]}
    assert data["flagged_entry_refs"] == []


def test_no_fullctx_keeps_retrieval_labels(fixture_dbs: dict[str, Path]) -> None:
    judge = SequencedJudge(label="UNSUPPORTED")
    result = run_audit([fixture_dbs["alpha"]], judge, max_usd=100, no_fullctx=True)
    data = build_report_data(result)
    assert set(data["labels"]) == {"UNSUPPORTED"}
    assert data["stale_entries"] == data["entries_judged"]


def test_judge_errors_are_counted_not_dropped(fixture_dbs: dict[str, Path]) -> None:
    judge = SequencedJudge(fail_calls={1})
    result = run_audit([fixture_dbs["alpha"]], judge, max_usd=100, no_fullctx=True)
    assert result.judge_errors == 1
    data = build_report_data(result)
    assert data["judge_errors"] == 1


def test_midrun_abort_raises_cost_abort(fixture_dbs: dict[str, Path]) -> None:
    guard = CostGuard(max_usd=1.0)
    guard.record_usage(input_tokens=330_000, output_tokens=0)  # $0.99 pre-spent
    judge = StubJudge()
    with pytest.raises(CostAbort):
        run_audit([fixture_dbs["alpha"]], judge, max_usd=1.0, guard=guard, no_fullctx=True)


def test_stub_judge_end_to_end_mixes_labels(fixture_dbs: dict[str, Path]) -> None:
    result = run_audit([fixture_dbs["alpha"]], StubJudge(), max_usd=100)
    data = build_report_data(result)
    assert data["compactions_audited"] == 3
    assert data["entries_judged"] >= 8
    assert "SOURCED:TOOL" in data["labels"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )


def test_memory_restore_from_git_history_and_cleanup(tmp_path: Path) -> None:
    repo = tmp_path / "workspace-repo"
    logs = repo / "workspace" / "memory"
    logs.mkdir(parents=True)
    _git(repo, "init", "-q")
    keep = logs / "2026-09-01.md"
    pruned = logs / "2026-03-01.md"
    keep.write_text("# log\n- polished the gizmo rack on widget-staging\n")
    pruned.write_text("# log\n- rotated the frobnicator config (synthetic old log)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add logs")
    pruned.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prune old log")

    restored_path: Path | None = None
    with restored_daily_logs(repo, tmp_path) as logs_dir:
        restored_path = logs_dir
        names = {p.name for p in logs_dir.glob("*.md")}
        assert names == {"2026-09-01.md", "2026-03-01.md"}
        assert "frobnicator" in (logs_dir / "2026-03-01.md").read_text()
    # Temp dir deleted on exit.
    assert restored_path is not None and not restored_path.exists()


def test_memory_restore_cleans_up_on_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo2"
    (repo / "workspace" / "memory").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "workspace" / "memory" / "2026-09-02.md").write_text("- synthetic note\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "log")
    captured: Path | None = None
    with pytest.raises(RuntimeError, match="synthetic"):
        with restored_daily_logs(repo, tmp_path) as logs_dir:
            captured = logs_dir
            raise RuntimeError("synthetic failure")
    assert captured is not None and not captured.exists()


def test_parse_claims_filters_short_bullets() -> None:
    md = "# Section\n- short\n- " + ("a long synthetic memory bullet about gizmos " * 2)
    claims = parse_claims(md)
    assert len(claims) == 1 and claims[0].section == "Section"
