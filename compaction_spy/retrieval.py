"""BM25 evidence selection (ported from ``spike_sjc_v2.py``).

Typed 300-char windows over 1500-char parent chunks; top-3 windows per entry,
round-robin across entries, dedup by parent hash, 70k-char total budget.
"""

from __future__ import annotations

import hashlib
import re

from rank_bm25 import BM25Okapi

from .case import SpanMessage
from .parse import Entry

EVIDENCE_BUDGET = 70_000
PARENT_SIZE = 1500
WINDOW_STEP = 250
WINDOW_SIZE = 300
TOP_PER_ENTRY = 3

_TOKEN = re.compile(r"[A-Za-z0-9_./:-]+")


def tokenize(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


def passages(span: list[SpanMessage]) -> list[tuple[str, str, str]]:
    """(typed-role-tag, window, parent) triples over the span.

    The role tag carries the segment, matching the spike's passage typing:
    e.g. 'TOOL" segment="SUMMARIZED'. It is interpolated into the passage XML
    attribute by the judge prompt builder.
    """
    out: list[tuple[str, str, str]] = []
    for msg in span:
        tag = f'{msg.role}" segment="{msg.segment}'
        text = msg.text
        for pstart in range(0, max(len(text), 1), PARENT_SIZE):
            parent = text[pstart : pstart + PARENT_SIZE]
            for w in range(0, max(len(parent), 1), WINDOW_STEP):
                out.append((tag, parent[w : w + WINDOW_SIZE], parent))
    return out


def select_evidence(
    entries: list[Entry],
    span: list[SpanMessage],
    budget: int = EVIDENCE_BUDGET,
) -> list[tuple[str, str]]:
    """Pick (role_tag, parent_chunk) evidence under the character budget."""
    pool = passages(span)
    if not pool:
        return []
    bm = BM25Okapi([tokenize(p[1]) for p in pool])
    ranks: list[list[int]] = []
    for entry in entries:
        scores = bm.get_scores(tokenize(entry.text))
        ranks.append(sorted(range(len(pool)), key=lambda i: -scores[i])[:TOP_PER_ENTRY])
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()
    used = 0
    for r in range(TOP_PER_ENTRY):
        for rank in ranks:
            if r >= len(rank):
                continue
            role, _window, parent = pool[rank[r]]
            h = hashlib.md5(parent.encode()).hexdigest()
            if h in seen:
                continue
            if used + len(parent) > budget:
                return picked
            seen.add(h)
            used += len(parent)
            picked.append((role, parent))
    return picked
