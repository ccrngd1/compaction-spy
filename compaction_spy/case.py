"""Case building: the history span each compaction summary replaced.

Ported from the spike ``spike_sjc_v2.py`` (``text_of``, ``build_case``).

The span runs from the previous compaction's ``firstKeptEntryId`` (or the
session start) up to this compaction event. Messages at or after the current
compaction's ``firstKeptEntryId`` are the kept TAIL; earlier ones are the
SUMMARIZED history the summary replaced. Each message is typed USER / TOOL /
ASSISTANT (tool-call arguments count as ASSISTANT text — the agent wrote them).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .store import Event

ROLE_MAP = {"user": "USER", "toolResult": "TOOL", "assistant": "ASSISTANT"}


@dataclass
class SpanMessage:
    role: str  # USER / TOOL / ASSISTANT / OTHER
    segment: str  # SUMMARIZED / TAIL
    text: str


@dataclass
class Case:
    """Everything the judges need for one compaction."""

    store: str
    session_id: str
    compaction_id: str
    timestamp: str
    summary: str
    prev_summary: str
    span: list[SpanMessage] = field(default_factory=list)
    first_compaction: bool = True
    raw_events: list[Event] = field(default_factory=list)  # pre-compaction, for the id check


def text_of(content: Any) -> str:
    """Flatten a message content field to text, including tool-call args."""
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            out.append(c.get("text", ""))
        elif c.get("type") == "toolCall":
            out.append(f"[toolCall {c.get('name')}] " + json.dumps(c.get("arguments"))[:4000])
    return "\n".join(out)


def build_case(
    store: str, session_id: str, compaction_id: str, evs: list[tuple[int, Event]]
) -> Case:
    """Build the audit case for one compaction within its session's events.

    ``evs`` is the full ordered (seq, event) list for the session. Mirrors the
    spike: span start = previous compaction's firstKeptEntryId (or just after
    the previous compaction if that id is not found, or session start); the
    TAIL begins at the current compaction's firstKeptEntryId.
    """
    idx = next(
        i
        for i, (_s, e) in enumerate(evs)
        if e.get("type") == "compaction" and e.get("id") == compaction_id
    )
    comp = evs[idx][1]
    prev_i = max(
        (i for i in range(idx) if evs[i][1].get("type") == "compaction"),
        default=None,
    )
    start = 0
    prev_summary = ""
    if prev_i is not None:
        prev = evs[prev_i][1]
        prev_summary = prev.get("summary") or ""
        fk_prev = prev.get("firstKeptEntryId")
        start = next(
            (i for i in range(prev_i) if evs[i][1].get("id") == fk_prev),
            prev_i + 1,
        )
    fk_cur = comp.get("firstKeptEntryId")
    tail_i = next((i for i in range(start, idx) if evs[i][1].get("id") == fk_cur), idx)

    span: list[SpanMessage] = []
    for i, (_seq, e) in enumerate(evs[start:idx], start):
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        text = text_of(msg.get("content"))
        if not text.strip():
            continue
        role = ROLE_MAP.get(msg.get("role"), "OTHER")
        segment = "TAIL" if i >= tail_i else "SUMMARIZED"
        span.append(SpanMessage(role=role, segment=segment, text=text))

    return Case(
        store=store,
        session_id=session_id,
        compaction_id=compaction_id,
        timestamp=comp.get("timestamp") or "",
        summary=comp.get("summary") or "",
        prev_summary=prev_summary,
        span=span,
        first_compaction=prev_i is None,
        raw_events=[e for _s, e in evs[:idx]],
    )
