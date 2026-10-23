"""Audit pipeline: cases -> id check -> retrieval judge -> full-context confirm.

Order of operations is fixed by the thread-safety constraint: ALL events are
loaded and zstd-decoded serially first (store.py enforces main-thread decode),
then only the judge calls run in a thread pool. The cost guard aborts before
any call if the stage-1 (retrieval) estimate exceeds the budget, again between
stages if actual spend plus the stage-2 (full-context) estimate would exceed
it, and mid-run (cancelling pending futures) if the next call could exceed it.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import fullctx as fc
from .case import Case, build_case
from .census import CensusResult, census
from .cost import CostGuard
from .idcheck import IdCheckResult, check_case
from .judge import (
    FULLCTX_SYSTEM,
    RETRIEVAL_SYSTEM,
    SOURCED_LABELS,
    JudgeClient,
    JudgeError,
    JudgeItem,
    build_retrieval_prompt,
)
from .parse import Entry, parse_entries
from .retrieval import select_evidence
from .store import load_sessions

MAX_TOKENS_RETRIEVAL = 8000
MAX_TOKENS_FULLCTX = 6000
JUDGE_THREADS = 6


def session_hash(session_id: str) -> str:
    """Opaque session ref for reports: never expose raw session ids."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


@dataclass
class EntryResult:
    store: str
    session_hash: str
    compaction_id: str
    section: str
    index: int
    entry_text: str  # private; only written with --include-text
    retrieval: JudgeItem | None = None
    full: JudgeItem | None = None

    @property
    def final_label(self) -> str | None:
        item = self.full or self.retrieval
        return item.label if item else None

    @property
    def final_item(self) -> JudgeItem | None:
        return self.full or self.retrieval

    @property
    def ref(self) -> str:
        return f"{self.store}/{self.session_hash}/{self.compaction_id}/{self.section}/{self.index}"


@dataclass
class CaseResult:
    case: Case
    entries: list[Entry]
    idcheck: IdCheckResult
    entry_results: list[EntryResult] = field(default_factory=list)
    judge_errors: int = 0  # counted, never silently dropped


@dataclass
class AuditResult:
    cases: list[CaseResult] = field(default_factory=list)
    unparsed_legacy: int = 0
    skipped_dbs: list[tuple[str, str]] = field(default_factory=list)
    estimated_usd: float = 0.0
    stage2_estimated_usd: float = 0.0
    spent_usd: float = 0.0
    judge_errors: int = 0


def build_cases(census_result: CensusResult, dbs: list[Path]) -> tuple[list[CaseResult], int]:
    """Serial phase: load sessions, build cases, parse entries, run id check."""
    by_store = {db.stem: db for db in dbs}
    cases: list[CaseResult] = []
    unparsed = 0
    needed: dict[str, set[str]] = {}
    for ref in census_result.compactions:
        if ref.fmt != "sg5":
            unparsed += 1
            continue
        needed.setdefault(ref.store, set()).add(ref.session_id)
    sessions_by_store = {
        name: {
            sid: evs for sid, evs in load_sessions(by_store[name]).items() if sid in needed[name]
        }
        for name in needed
    }
    for ref in census_result.compactions:
        if ref.fmt != "sg5":
            continue
        evs = sessions_by_store[ref.store].get(ref.session_id, [])
        if not evs:
            continue
        case = build_case(ref.store, ref.session_id, ref.compaction_id, evs)
        entries = parse_entries(case.summary)
        if not entries:
            unparsed += 1
            continue
        cases.append(CaseResult(case=case, entries=entries, idcheck=check_case(case)))
    return cases, unparsed


def _retrieval_prompt(cr: CaseResult) -> str:
    evidence = select_evidence(cr.entries, cr.case.span)
    return build_retrieval_prompt(cr.entries, evidence, cr.case.prev_summary)


def run_audit(
    dbs: list[Path],
    judge: JudgeClient,
    max_usd: float,
    *,
    limit: int | None = None,
    since: str | None = None,
    agent: str | None = None,
    no_fullctx: bool = False,
    guard: CostGuard | None = None,
    threads: int = JUDGE_THREADS,
) -> AuditResult:
    """Full pipeline. Raises CostAbort (budget) — callers must not write reports then."""
    census_result = census(dbs)
    if agent:
        census_result.compactions = [c for c in census_result.compactions if c.store == agent]
    if since:
        census_result.compactions = [
            c for c in census_result.compactions if c.timestamp and c.timestamp >= since
        ]
    if limit is not None:
        census_result.compactions = census_result.compactions[:limit]

    cases, unparsed = build_cases(census_result, dbs)
    result = AuditResult(unparsed_legacy=unparsed, skipped_dbs=census_result.skipped)

    guard = guard or CostGuard(max_usd)

    # Serial phase done (all zstd decoded). Build all retrieval prompts and
    # pre-check the whole plan against the budget before any call.
    prompts = [(cr, _retrieval_prompt(cr)) for cr in cases]
    result.estimated_usd = guard.check_estimate(
        [len(RETRIEVAL_SYSTEM) + len(p) for _cr, p in prompts], MAX_TOKENS_RETRIEVAL
    )

    def judge_retrieval(cr: CaseResult, prompt: str) -> None:
        guard.check_before_call(len(RETRIEVAL_SYSTEM) + len(prompt), MAX_TOKENS_RETRIEVAL)
        items, usage = judge.judge(RETRIEVAL_SYSTEM, prompt, len(cr.entries), MAX_TOKENS_RETRIEVAL)
        guard.record_usage(usage.input_tokens, usage.output_tokens)
        by_i = {item.i: item for item in items}
        sh = session_hash(cr.case.session_id)
        for entry in cr.entries:
            cr.entry_results.append(
                EntryResult(
                    store=cr.case.store,
                    session_hash=sh,
                    compaction_id=cr.case.compaction_id,
                    section=entry.section,
                    index=entry.index,
                    entry_text=entry.text,
                    retrieval=by_i.get(entry.index),
                )
            )

    _run_threaded(
        [(judge_retrieval, (cr, p)) for cr, p in prompts],
        [cr for cr, _p in prompts],
        threads,
    )

    if not no_fullctx:
        fullctx_jobs: list[tuple[CaseResult, list[tuple[int, Entry]], str]] = []
        for cr in cases:
            flagged = [
                (er.index, cr.entries[_entry_pos(cr.entries, er.index)])
                for er in cr.entry_results
                if er.retrieval is not None and er.retrieval.label not in SOURCED_LABELS
            ]
            if not flagged:
                continue
            prompt = fc.build_fullctx_prompt(cr.case, flagged)
            fullctx_jobs.append((cr, flagged, prompt))

        # Stage-2 pre-check: all full-context prompts are built; abort before
        # any full-context call if spend so far + this stage's estimate exceeds
        # the budget.
        result.stage2_estimated_usd = guard.check_stage2_estimate(
            [len(FULLCTX_SYSTEM) + len(p) for _cr, _fl, p in fullctx_jobs], MAX_TOKENS_FULLCTX
        )

        def judge_fullctx(cr: CaseResult, flagged: list[tuple[int, Entry]], prompt: str) -> None:
            guard.check_before_call(len(FULLCTX_SYSTEM) + len(prompt), MAX_TOKENS_FULLCTX)
            items, usage = judge.judge(FULLCTX_SYSTEM, prompt, len(flagged), MAX_TOKENS_FULLCTX)
            guard.record_usage(usage.input_tokens, usage.output_tokens)
            by_pos = {item.i: item for item in items}
            by_index = {er.index: er for er in cr.entry_results}
            for pos, (orig_index, _entry) in enumerate(flagged):
                if pos in by_pos and orig_index in by_index:
                    by_index[orig_index].full = by_pos[pos]

        _run_threaded(
            [(judge_fullctx, (cr, fl, p)) for cr, fl, p in fullctx_jobs],
            [cr for cr, _fl, _p in fullctx_jobs],
            threads,
        )

    result.cases = cases
    result.judge_errors = sum(cr.judge_errors for cr in cases)
    result.spent_usd = guard.spent_usd
    return result


def _entry_pos(entries: list[Entry], index: int) -> int:
    for pos, e in enumerate(entries):
        if e.index == index:
            return pos
    raise KeyError(index)


def _run_threaded(
    jobs: list[tuple],  # type: ignore[type-arg]
    owners: list[CaseResult],
    threads: int,
) -> None:
    """Run judge jobs in a pool. JudgeError -> counted bucket; CostAbort cancels
    all pending futures and re-raises (abort = no report)."""
    if not jobs:
        return
    from .cost import CostAbort

    with cf.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(fn, *args): owner for (fn, args), owner in zip(jobs, owners, strict=True)
        }
        abort: CostAbort | None = None
        for fut in cf.as_completed(futures):
            try:
                fut.result()
            except JudgeError:
                futures[fut].judge_errors += 1
            except CostAbort as ex:
                abort = ex
                for other in futures:
                    other.cancel()
                break
        if abort is not None:
            raise abort
