"""compaction-spy CLI (typer + rich).

Commands: snapshot, census, audit, memory. Every write target is checked by
guard.check_write_path; census/audit refuse to read a --db under the protected
root. A cost abort exits non-zero with a clear message and writes NO report
files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console

from . import census as census_mod
from . import memory_mode, report, snapshot
from .audit import run_audit
from .cost import CostAbort, CostGuard, PriceConfig
from .guard import GuardError, check_read_db, check_write_path
from .judge import BedrockJudge, JudgeClient, StubJudge

app = typer.Typer(help="Offline, read-only provenance auditor for OpenClaw compaction summaries.")
console = Console()


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=code)


def _make_judge(kind: str) -> JudgeClient:
    if kind == "stub":
        return StubJudge()
    if kind == "bedrock":
        return BedrockJudge()
    _fail(f"unknown judge '{kind}' (use 'bedrock' or 'stub')")
    raise AssertionError("unreachable")


@app.command("snapshot")
def snapshot_cmd(
    to: Path = typer.Option(..., "--to", help="Private destination dir (chmod 700)."),
    agent: str | None = typer.Option(None, "--agent", help="Agent id under the state dir."),
    source: Path | None = typer.Option(
        None, "--source", help="Explicit source DB path (overrides --agent)."
    ),
) -> None:
    """Consistent read-only snapshot via the sqlite3 backup API."""
    if source is None:
        if agent is None:
            _fail("provide --agent <id> or --source <path>")
        assert agent is not None
        source = snapshot.agent_db_path(agent)
    try:
        dest = snapshot.take_snapshot(source, to, name=(f"{agent}.sqlite" if agent else None))
    except GuardError as ex:
        _fail(str(ex))
        return
    except snapshot.SnapshotError as ex:
        _fail(str(ex))
        return
    console.print(f"snapshot written: [bold]{dest}[/bold] (integrity ok, marker sidecar written)")


@app.command("census")
def census_cmd(
    db: list[Path] = typer.Option(..., "--db", help="Snapshot DB path(s)."),
    json_out: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """Count compactions by store/format/month, deduped by summary hash."""
    try:
        dbs = [check_read_db(d) for d in db]
    except GuardError as ex:
        _fail(str(ex))
        return
    for d in dbs:
        if not d.exists():
            _fail(f"database not found: {d}")
    result = census_mod.census(dbs)
    data = census_mod.census_summary(result)
    if json_out:
        console.print_json(json.dumps(data))
    else:
        console.print(f"unique compactions: [bold]{data['unique_compactions']}[/bold]")
        console.print(f"by store: {data['by_store']}")
        console.print(f"by format: {data['by_format']}")
        console.print(f"by month: {data['by_month']}")
        for skipped in data["skipped_dbs"]:
            console.print(f"[yellow]skipped[/yellow] {skipped['store']}: {skipped['reason']}")


@app.command("audit")
def audit_cmd(
    db: list[Path] = typer.Option(..., "--db", help="Snapshot DB path(s)."),
    out: Path = typer.Option(..., "--out", help="Private output dir (chmod 700)."),
    agent: str | None = typer.Option(None, "--agent", help="Audit only this store name."),
    since: str | None = typer.Option(None, "--since", help="ISO timestamp lower bound."),
    limit: int | None = typer.Option(None, "--limit", help="Audit at most N compactions."),
    max_usd: float = typer.Option(15.0, "--max-usd", help="Hard spend ceiling."),
    judge: str = typer.Option(
        "bedrock", "--judge", help="bedrock (default) or stub (offline test/demo judge)."
    ),
    no_fullctx: bool = typer.Option(False, "--no-fullctx", help="Skip full-context confirm."),
    include_text: bool = typer.Option(
        False, "--include-text", help="Also write entries.private.jsonl (private out dir only)."
    ),
    cleanup: bool = typer.Option(
        True,
        "--cleanup/--no-cleanup",
        help="After a successful run, delete snapshot DBs that carry this tool's marker sidecar "
        "(never arbitrary files).",
    ),
    price_input: float = typer.Option(3.0, "--price-input", help="USD per M input tokens."),
    price_output: float = typer.Option(15.0, "--price-output", help="USD per M output tokens."),
) -> None:
    """Full provenance audit pipeline against snapshot DBs."""
    try:
        dbs = [check_read_db(d) for d in db]
        out_dir = check_write_path(out)
    except GuardError as ex:
        _fail(str(ex))
        return
    for d in dbs:
        if not d.exists():
            _fail(f"database not found: {d}")
    judge_client = _make_judge(judge)
    guard = CostGuard(
        max_usd, PriceConfig(input_per_mtok=price_input, output_per_mtok=price_output)
    )
    try:
        result = run_audit(
            dbs,
            judge_client,
            max_usd,
            limit=limit,
            since=since,
            agent=agent,
            no_fullctx=no_fullctx,
            guard=guard,
        )
    except CostAbort as ex:
        # Clean abort: non-zero exit, clear message, NO report files written.
        _fail(f"cost budget abort: {ex} (no report written)", code=2)
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    data = report.build_report_data(result)
    (out_dir / "report.json").write_text(json.dumps(data, indent=1))
    (out_dir / "report.md").write_text(report.render_markdown(data))
    if include_text:
        (out_dir / "entries.private.jsonl").write_text(report.private_entries_jsonl(result))
    console.print(
        f"audited {data['compactions_audited']} compactions, "
        f"{data['entries_judged']} entries; no source found: {data['non_sourced_pct']}%; "
        f"spent ${data['cost']['spent_usd']}"
    )
    console.print(f"report: {out_dir / 'report.md'}")
    if cleanup:
        removed = [d for d in dbs if snapshot.cleanup_snapshot(d)]
        kept = [d for d in dbs if d not in removed and d.exists()]
        if removed:
            console.print(f"cleanup: removed {len(removed)} marked snapshot(s)")
        if kept:
            console.print(
                f"cleanup: kept {len(kept)} file(s) without a compaction-spy snapshot marker"
            )


@app.command("memory")
def memory_cmd(
    memory_md: Path = typer.Option(..., "--memory-md", help="Path to MEMORY.md."),
    git_restore: Path = typer.Option(
        ..., "--git-restore", help="Git repo holding workspace/memory daily logs."
    ),
    sample: int = typer.Option(40, "--sample", help="Number of claims to sample."),
    judge: str = typer.Option("bedrock", "--judge", help="bedrock or stub."),
    seed: int = typer.Option(11, "--seed", help="Sampling seed."),
) -> None:
    """Secondary mode: MEMORY.md vs daily logs. Self-consistency, NOT ground truth."""
    import random

    if not memory_md.exists():
        _fail(f"MEMORY.md not found: {memory_md}")
    console.print(f"[yellow]{memory_mode.SELF_CONSISTENCY_NOTE}[/yellow]")
    judge_client = _make_judge(judge)
    claims = memory_mode.parse_claims(memory_md.read_text(errors="replace"))
    random.Random(seed).shuffle(claims)
    claims = claims[:sample]
    tmp_parent = os.environ.get("TMPDIR")
    with memory_mode.restored_daily_logs(
        git_restore, Path(tmp_parent) if tmp_parent else None
    ) as logs_dir:
        docs = memory_mode.build_log_chunks(logs_dir)
        if not docs:
            _fail("no daily logs found in the working tree or git history")
        from rank_bm25 import BM25Okapi

        from .retrieval import tokenize

        bm = BM25Okapi([tokenize(t) for _d, t in docs])
        from collections import Counter

        counts: Counter[str] = Counter()
        for claim in claims:
            verdict, _usage = memory_mode.check_claim(claim, docs, bm, judge_client)
            counts[str(verdict["label"])] += 1
    console.print(f"claims sampled: {len(claims)}, log chunks: {len(docs)}")
    console.print(f"labels: {dict(counts)}")
    console.print(f"[yellow]{memory_mode.SELF_CONSISTENCY_NOTE}[/yellow]")


if __name__ == "__main__":
    app()
