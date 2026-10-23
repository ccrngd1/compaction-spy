"""Census of compactions across snapshot stores (ported from ``census4.py``).

Counts by store / format (safeguard vs legacy) / month, deduplicated by summary
hash. Malformed DBs are skipped and reported, not fatal.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import parse, store


@dataclass
class CompactionRef:
    store: str
    session_id: str
    seq: int
    compaction_id: str
    timestamp: str
    tokens_before: int | None
    first_kept_entry_id: str | None
    fmt: str  # "sg5" | "legacy"
    summary_hash: str


@dataclass
class CensusResult:
    compactions: list[CompactionRef] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (store, reason)

    def by_store(self) -> Counter[str]:
        return Counter(c.store for c in self.compactions)

    def by_format(self) -> Counter[str]:
        return Counter(c.fmt for c in self.compactions)

    def by_month(self) -> Counter[str]:
        return Counter((c.timestamp or "")[:7] for c in self.compactions)


def census(dbs: list[Path]) -> CensusResult:
    """Scan stores for compaction events, dedupe by summary hash."""
    result = CensusResult()
    seen: dict[str, CompactionRef] = {}
    for db in sorted(dbs):
        name = db.stem
        try:
            events = list(store.iter_events(db))
        except (sqlite3.Error, Exception) as ex:  # noqa: BLE001 - malformed DBs are skipped
            result.skipped.append((name, str(ex)[:200]))
            continue
        for sid, seq, e in events:
            if e.get("type") != "compaction":
                continue
            summary = e.get("summary") or ""
            h = parse.summary_hash(summary)
            if h in seen:
                continue
            seen[h] = CompactionRef(
                store=name,
                session_id=sid,
                seq=seq,
                compaction_id=str(e.get("id") or ""),
                timestamp=e.get("timestamp") or "",
                tokens_before=e.get("tokensBefore"),
                first_kept_entry_id=e.get("firstKeptEntryId"),
                fmt="sg5" if parse.is_safeguard_format(summary) else "legacy",
                summary_hash=h,
            )
    result.compactions = list(seen.values())
    return result


def census_summary(result: CensusResult) -> dict[str, Any]:
    return {
        "unique_compactions": len(result.compactions),
        "by_store": dict(result.by_store()),
        "by_format": dict(result.by_format()),
        "by_month": dict(sorted(result.by_month().items())),
        "skipped_dbs": [{"store": s, "reason": r} for s, r in result.skipped],
    }
