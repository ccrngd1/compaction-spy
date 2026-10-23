"""Parse safeguard compaction summaries into entries.

Ported from the spike ``spike_sjc_v2.py:parse_entries``. The safeguard format
has five ``## <heading>`` sections; each bullet (with its indented continuation
lines) is one entry. "none" / "n/a" / "no prior history" placeholders are
skipped. Summaries without any of the five headings are legacy free-form and
are counted as unparsed, not parsed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SECTIONS = [
    "Decisions",
    "Open TODOs",
    "Constraints/Rules",
    "Pending user asks",
    "Exact identifiers",
]

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_BULLET = re.compile(r"^([-*]|\d+\.)\s+")
_PLACEHOLDER = re.compile(r"^(none|n/a|no prior history)\.?$", re.I)


@dataclass
class Entry:
    section: str
    text: str
    index: int  # position within the summary's parsed entry list


def is_safeguard_format(summary: str) -> bool:
    """The spike census keyed on the Open TODOs heading to detect the format."""
    return "## Open TODOs" in summary


def summary_hash(summary: str) -> str:
    """Dedupe key: sha1 of the raw summary text (matches the spike census)."""
    return hashlib.sha1(summary.encode()).hexdigest()


def parse_entries(summary: str) -> list[Entry]:
    """Split a safeguard summary into per-bullet entries.

    Continuation lines (indented, non-bullet) are appended to the previous
    bullet. Text outside the five known sections is ignored.
    """
    entries: list[tuple[str, str]] = []
    section: str | None = None
    for line in summary.splitlines():
        m = _HEADING.match(line)
        if m:
            heading = m.group(1).strip()
            section = heading if heading in SECTIONS else None
            continue
        if section is None:
            continue
        stripped = line.strip()
        if _BULLET.match(stripped):
            entries.append((section, _BULLET.sub("", stripped)))
        elif stripped and entries and entries[-1][0] == section and line.startswith((" ", "\t")):
            entries[-1] = (section, entries[-1][1] + " " + stripped)
    return [
        Entry(section=s, text=t, index=i)
        for i, (s, t) in enumerate(e for e in entries if e[1] and not _PLACEHOLDER.match(e[1]))
    ]
