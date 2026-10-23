"""Full-context confirmation prompt building (ported from ``fullctx.py`` spike).

Entries the retrieval judge did not label sourced are re-judged with the full
history span visible: 4k chars per message (middle elided), 420k chars total,
truncating the earliest history first.
"""

from __future__ import annotations

from .case import Case
from .parse import Entry

CAP_MSG = 4_000
CAP_TOTAL = 420_000


def build_history(case: Case, cap_msg: int = CAP_MSG, cap_total: int = CAP_TOTAL) -> str:
    parts = []
    for msg in case.span:
        text = msg.text
        if len(text) > cap_msg:
            text = text[: cap_msg // 2] + "\n[... truncated ...]\n" + text[-cap_msg // 2 :]
        parts.append(f'<msg type="{msg.role}" segment="{msg.segment}">\n{text}\n</msg>')
    hist = "\n".join(parts)
    if len(hist) > cap_total:
        hist = "[... earliest history truncated ...]\n" + hist[-cap_total:]
    return hist


def build_fullctx_prompt(case: Case, flagged: list[tuple[int, Entry]]) -> str:
    """User prompt for the full-context re-judge of flagged entries.

    ``flagged`` pairs the entry's original index with the entry; the judge is
    asked about index positions 0..len(flagged)-1 in this prompt's own order.
    """
    hist = build_history(case)
    items = "\n".join(f"{i}. [{e.section}] {e.text}" for i, (_orig, e) in enumerate(flagged))
    prev = case.prev_summary[:12000] or "(none)"
    return (
        f"<previous_summary>\n{prev}\n</previous_summary>\n\n"
        f"<full_history>\n{hist}\n</full_history>\n\n"
        f"<summary_entries>\n{items}\n</summary_entries>"
    )
