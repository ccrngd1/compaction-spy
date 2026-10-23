"""Report rendering: aggregates plus opaque entry refs only.

Privacy posture: report.md / report.json never contain entry text or judge
rationales (rationales echo private content). Flagged entries are referenced as
``store/session-hash/compaction-id/section/index``. Entry text is written only
to ``entries.private.jsonl`` inside the private --out dir when --include-text
is passed. The wording "no source found" is mandatory; see the banned-words
test for the terms this tool must never emit.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .audit import AuditResult, EntryResult
from .judge import SOURCED_LABELS

LABEL_DISPLAY = {
    "TOOL": "SOURCED:TOOL",
    "USER": "SOURCED:USER",
    "PREV": "SOURCED:PREV",
    "TAIL": "SOURCED:TAIL",
    "AGENT_ONLY": "AGENT_ONLY",
    "UNSUPPORTED": "UNSUPPORTED",
    "CONTRADICTED": "CONTRADICTED",
}


def _entry_rows(result: AuditResult) -> list[EntryResult]:
    return [er for cr in result.cases for er in cr.entry_results]


def build_report_data(result: AuditResult) -> dict[str, Any]:
    """Aggregate JSON-serializable report data; no entry text, no rationales."""
    rows = _entry_rows(result)
    by_label: Counter[str] = Counter()
    by_section: dict[str, Counter[str]] = defaultdict(Counter)
    by_store: dict[str, Counter[str]] = defaultdict(Counter)
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    by_boundary: dict[str, Counter[str]] = defaultdict(Counter)
    stale = 0
    flagged_refs: list[str] = []
    unjudged = 0
    boundary_of = {
        (cr.case.store, cr.case.compaction_id): "first" if cr.case.first_compaction else "later"
        for cr in result.cases
    }
    for er in rows:
        item = er.final_item
        if item is None:
            unjudged += 1
            continue
        label = LABEL_DISPLAY.get(item.label, item.label)
        by_label[label] += 1
        by_section[er.section][label] += 1
        by_store[er.store][label] += 1
        by_kind[item.kind][label] += 1
        by_boundary[boundary_of.get((er.store, er.compaction_id), "first")][label] += 1
        if item.stale:
            stale += 1
        if item.label not in SOURCED_LABELS:
            flagged_refs.append(er.ref)

    id_total = sum(cr.idcheck.total for cr in result.cases)
    id_found = sum(cr.idcheck.found for cr in result.cases)
    judged = sum(by_label.values())
    non_sourced = sum(
        v for k, v in by_label.items() if k in ("AGENT_ONLY", "UNSUPPORTED", "CONTRADICTED")
    )
    return {
        "note": (
            "This report measures provenance, not truth. Entries with no source "
            "found may still be correct."
        ),
        "compactions_audited": len(result.cases),
        "entries_judged": judged,
        "entries_unjudged": unjudged,
        "judge_errors": result.judge_errors,
        "unparsed_legacy": result.unparsed_legacy,
        "skipped_dbs": [{"store": s, "reason": r} for s, r in result.skipped_dbs],
        "labels": dict(by_label),
        "non_sourced_total": non_sourced,
        "non_sourced_pct": round(100 * non_sourced / judged, 1) if judged else 0.0,
        "by_section": {k: dict(v) for k, v in by_section.items()},
        "by_store": {k: dict(v) for k, v in by_store.items()},
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
        "by_boundary": {k: dict(v) for k, v in by_boundary.items()},
        "stale_entries": stale,
        "identifier_check": {
            "total": id_total,
            "verbatim_found": id_found,
            "rate_pct": round(100 * id_found / id_total, 1) if id_total else 100.0,
        },
        "cost": {
            "estimated_usd": round(result.estimated_usd, 4),
            "stage2_estimated_usd": round(result.stage2_estimated_usd, 4),
            "spent_usd": round(result.spent_usd, 4),
        },
        "flagged_entry_refs": flagged_refs,
    }


def render_markdown(data: dict[str, Any]) -> str:
    """Human-readable aggregate report. Same privacy rules as the JSON."""
    lines = [
        "# CompactionSpy audit report",
        "",
        f"> {data['note']}",
        "",
        f"- Compactions audited: **{data['compactions_audited']}**",
        f"- Entries judged: **{data['entries_judged']}** "
        f"(unjudged: {data['entries_unjudged']}, judge errors: {data['judge_errors']})",
        f"- Legacy / unparsed summaries: {data['unparsed_legacy']}",
        f"- No source found (AGENT_ONLY + UNSUPPORTED + CONTRADICTED): "
        f"**{data['non_sourced_total']}** ({data['non_sourced_pct']}%)",
        f"- Stale entries (done in the kept tail, still listed): {data['stale_entries']}",
        f"- Identifier check: {data['identifier_check']['verbatim_found']}"
        f"/{data['identifier_check']['total']} verbatim "
        f"({data['identifier_check']['rate_pct']}%)",
        f"- Cost: retrieval stage estimated ${data['cost']['estimated_usd']}, "
        f"full-context stage estimated ${data['cost']['stage2_estimated_usd']}, "
        f"spent ${data['cost']['spent_usd']}",
        "",
        "## Labels",
        "",
        "| Label | Count |",
        "|---|---|",
    ]
    for label, count in sorted(data["labels"].items()):
        lines.append(f"| {label} | {count} |")
    for title, key in [
        ("By section", "by_section"),
        ("By store", "by_store"),
        ("By kind", "by_kind"),
        ("By boundary", "by_boundary"),
    ]:
        lines += ["", f"## {title}", ""]
        for group, counts in sorted(data[key].items()):
            total = sum(counts.values())
            non_sourced = sum(
                v for k, v in counts.items() if k in ("AGENT_ONLY", "UNSUPPORTED", "CONTRADICTED")
            )
            pct = round(100 * non_sourced / total, 1) if total else 0.0
            lines.append(f"- **{group}**: n={total}, no source found: {non_sourced} ({pct}%)")
    lines += [
        "",
        "## Flagged entry refs (opaque)",
        "",
        "Format: `store/session-hash/compaction-id/section/index`. "
        "Use `--include-text` for a private per-entry file.",
        "",
    ]
    lines += [f"- `{ref}`" for ref in data["flagged_entry_refs"]]
    if data["skipped_dbs"]:
        lines += ["", "## Skipped databases", ""]
        lines += [f"- {d['store']}: {d['reason']}" for d in data["skipped_dbs"]]
    lines.append("")
    return "\n".join(lines)


def private_entries_jsonl(result: AuditResult) -> str:
    """Per-entry private lines for --include-text (entry text + rationales)."""
    out = []
    for er in _entry_rows(result):
        item = er.final_item
        out.append(
            json.dumps(
                {
                    "ref": er.ref,
                    "section": er.section,
                    "text": er.entry_text,
                    "label": LABEL_DISPLAY.get(item.label, item.label) if item else None,
                    "kind": item.kind if item else None,
                    "stale": item.stale if item else None,
                    "conf": item.conf if item else None,
                    "why": item.why if item else None,
                    "retrieval_label": er.retrieval.label if er.retrieval else None,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(out) + "\n"
