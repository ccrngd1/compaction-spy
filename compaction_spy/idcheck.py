"""Deterministic identifier check (ported from the spike ``idcheck.py``).

Backticked tokens and URLs in the summary are searched verbatim in the raw
pre-compaction events (including tool-call arguments, via the raw event JSON).
This is free and runs before any judge. A token not found verbatim gets the
ID_UNVERIFIED flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .case import Case

IDENTIFIER_PATTERN = re.compile(r'`([^`\s]{4,})`|(https?://[^\s)>\]`"]+)')


def extract_identifiers(text: str) -> list[str]:
    """Backtick tokens (>=4 chars, no spaces) and http(s) URLs, trailing punctuation stripped."""
    out = []
    for m in IDENTIFIER_PATTERN.finditer(text):
        token = (m.group(1) or m.group(2)).rstrip(".,;:")
        out.append(token)
    return out


@dataclass
class IdCheckResult:
    total: int
    found: int
    missing: list[str]

    @property
    def rate(self) -> float:
        return self.found / self.total if self.total else 1.0


def check_case(case: Case) -> IdCheckResult:
    """Verbatim-search every summary identifier in the case's raw history."""
    hay = (
        "\n".join(m.text for m in case.span)
        + "\n"
        + case.prev_summary
        + "\n"
        + "\n".join(json.dumps(e, ensure_ascii=False) for e in case.raw_events)
    )
    total = found = 0
    missing: list[str] = []
    for token in extract_identifiers(case.summary):
        total += 1
        if token in hay or token.replace("\\", "") in hay:
            found += 1
        else:
            missing.append(token)
    return IdCheckResult(total=total, found=found, missing=missing)
