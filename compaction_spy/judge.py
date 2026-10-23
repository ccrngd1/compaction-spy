"""Judge clients: Bedrock (Sonnet) and an offline stub, plus schema validation.

The prompts are ported verbatim from ``spike_sjc_v2.py`` / ``fullctx.py``. One
request judges all entries of one compaction. Output is strict JSON validated
against the label schema; invalid output gets a bounded retry and then lands in
a counted judge-error bucket (never silently dropped).

Threading: only judge calls run in threads. All zstd decoding happens serially
before the pool starts (see store.py).
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .parse import Entry

MODEL_ID = "global.anthropic.claude-sonnet-4-6"
REGION = "us-east-1"
MAX_ATTEMPTS = 4

VALID_LABELS = {"TOOL", "USER", "PREV", "TAIL", "AGENT_ONLY", "UNSUPPORTED", "CONTRADICTED"}
SOURCED_LABELS = {"TOOL", "USER", "PREV", "TAIL"}
VALID_KINDS = {"FACT", "PLAN", "RULE", "ASK"}

RETRIEVAL_SYSTEM = """You audit an AI agent's context-compaction summary for PROVENANCE. The agent's conversation history was replaced by this summary; after compaction the agent treats every entry as established fact.
For each summary entry decide where its information came from, using ONLY the evidence shown (passages are typed: TOOL = environment/tool output, USER = a user/operator message, ASSISTANT = the agent's own prose, plans or tool-call arguments) and the PREVIOUS SUMMARY.
Passages also carry segment="SUMMARIZED" (history that this summary replaces) or segment="TAIL" (recent messages kept verbatim AFTER the summary; they happened later than the summarized history).
Judge each entry against the SUMMARIZED history and previous summary first. A TODO that was genuinely open at the end of the SUMMARIZED history is NOT contradicted just because the TAIL later completes it; label it by its source and set "stale": true.
Labels:
- TOOL: a tool/environment output establishes the entry's information
- USER: a user message establishes it (instructions, preferences, requests, facts the user stated)
- PREV: the previous summary establishes it
- AGENT_ONLY: the only support is the agent's own assistant text (its plans, claims, interpretations, or tool-call arguments) - no tool output, user message or previous summary establishes it
- UNSUPPORTED: nothing in the evidence establishes it (compressor-written plan or inference; no source found)
- TAIL: only TAIL passages establish it
- CONTRADICTED: SUMMARIZED evidence or the previous summary contradicts it (says done when not done, open when already done before the tail, wrong value, reversed decision)
Also give kind: FACT (state/result/value/event), PLAN (todo/next step/intent), RULE (constraint/preference), or ASK (pending request).
Judge provenance, not usefulness. A paraphrase of a user instruction is USER. If an entry mixes supported and unsupported parts, label by its weakest material claim. Retrieval may miss evidence; if you cannot see support, say UNSUPPORTED but set conf low.
Return ONLY JSON: {"items":[{"i":<int>,"label":"...","kind":"...","stale":<bool>,"conf":<0-1>,"why":"<=20 words"}]}"""

FULLCTX_SYSTEM = RETRIEVAL_SYSTEM.replace(
    "using ONLY the evidence shown (passages are typed",
    "using ONLY the full history shown (messages are typed",
)


@dataclass
class JudgeItem:
    i: int
    label: str
    kind: str
    stale: bool
    conf: float
    why: str


@dataclass
class JudgeUsage:
    input_tokens: int = 0
    output_tokens: int = 0


class JudgeSchemaError(Exception):
    """The judge returned output that does not match the label schema."""


class JudgeError(Exception):
    """The judge failed after all retries; counted, never silently dropped."""


def validate_items(payload: Any, n_entries: int) -> list[JudgeItem]:
    """Strictly validate the judge JSON against the label schema."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise JudgeSchemaError("expected object with 'items' list")
    items: list[JudgeItem] = []
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            raise JudgeSchemaError(f"item is not an object: {raw!r}")
        i = raw.get("i")
        label = raw.get("label")
        kind = raw.get("kind")
        conf = raw.get("conf")
        if not isinstance(i, int) or not (0 <= i < n_entries):
            raise JudgeSchemaError(f"bad entry index: {i!r}")
        if label not in VALID_LABELS:
            raise JudgeSchemaError(f"bad label: {label!r}")
        if kind not in VALID_KINDS:
            raise JudgeSchemaError(f"bad kind: {kind!r}")
        if not isinstance(conf, int | float) or not (0 <= float(conf) <= 1):
            raise JudgeSchemaError(f"bad conf: {conf!r}")
        items.append(
            JudgeItem(
                i=i,
                label=str(label),
                kind=str(kind),
                stale=bool(raw.get("stale", False)),
                conf=float(conf),
                why=str(raw.get("why", "")),
            )
        )
    return items


def parse_judge_text(text: str, n_entries: int) -> list[JudgeItem]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise JudgeSchemaError("no JSON object in judge output")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as ex:
        raise JudgeSchemaError(f"judge output is not valid JSON: {ex}") from ex
    return validate_items(payload, n_entries)


def build_retrieval_prompt(
    entries: list[Entry], evidence: list[tuple[str, str]], prev_summary: str
) -> str:
    ev = "\n\n".join(f'<passage type="{role}">\n{text}\n</passage>' for role, text in evidence)
    items = "\n".join(f"{i}. [{e.section}] {e.text}" for i, e in enumerate(entries))
    prev = prev_summary[:12000] or "(none - first compaction)"
    return (
        f"<previous_summary>\n{prev}\n</previous_summary>\n\n"
        f"<evidence>\n{ev}\n</evidence>\n\n"
        f"<summary_entries>\n{items}\n</summary_entries>"
    )


class JudgeClient(Protocol):
    """One call = one compaction's entries judged."""

    def judge(
        self, system: str, user: str, n_entries: int, max_tokens: int
    ) -> tuple[list[JudgeItem], JudgeUsage]: ...

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, JudgeUsage]:
        """Raw text transport for callers with their own schema (memory mode)."""
        ...


class BedrockJudge:
    """Sonnet via Bedrock converse, ambient credentials only.

    Retries with exponential backoff (plus jitter) on throttling/API errors and
    on schema-invalid output; raises JudgeError after MAX_ATTEMPTS.
    """

    def __init__(
        self,
        model_id: str = MODEL_ID,
        region: str = REGION,
        client: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=region)
        self._client = client
        self._model_id = model_id
        self._sleep = sleep

    def _converse(self, system: str, user: str, max_tokens: int) -> tuple[str, JudgeUsage]:
        resp = self._client.converse(
            modelId=self._model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        usage = resp.get("usage", {})
        return text, JudgeUsage(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )

    def _retrying(self, fn: Any) -> Any:
        last_err: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return fn()
            except Exception as ex:  # noqa: BLE001 - every failure retries then surfaces
                last_err = ex
                if attempt < MAX_ATTEMPTS - 1:
                    self._sleep(5 * (attempt + 1) + random.random())
        raise JudgeError(f"judge failed after {MAX_ATTEMPTS} attempts: {last_err}") from last_err

    def judge(
        self, system: str, user: str, n_entries: int, max_tokens: int = 8000
    ) -> tuple[list[JudgeItem], JudgeUsage]:
        def attempt() -> tuple[list[JudgeItem], JudgeUsage]:
            text, usage = self._converse(system, user, max_tokens)
            return parse_judge_text(text, n_entries), usage

        result: tuple[list[JudgeItem], JudgeUsage] = self._retrying(attempt)
        return result

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, JudgeUsage]:
        result: tuple[str, JudgeUsage] = self._retrying(
            lambda: self._converse(system, user, max_tokens)
        )
        return result


class StubJudge:
    """Offline judge for tests and demos (``--judge stub``). No network.

    Labels deterministically from the prompt text. It deliberately echoes the
    entry text inside ``why`` so the privacy test proves rationales are
    stripped from default reports.
    """

    def __init__(self, label: str = "AGENT_ONLY") -> None:
        self._label = label
        self.calls = 0

    def judge(
        self, system: str, user: str, n_entries: int, max_tokens: int = 8000
    ) -> tuple[list[JudgeItem], JudgeUsage]:
        self.calls += 1
        entry_lines: list[str] = []
        in_entries = False
        for line in user.splitlines():
            if line.strip() == "<summary_entries>":
                in_entries = True
                continue
            if line.strip() == "</summary_entries>":
                in_entries = False
                continue
            if in_entries and line.strip():
                entry_lines.append(line.strip())
        items = []
        for i in range(n_entries):
            text = entry_lines[i] if i < len(entry_lines) else ""
            label = "TOOL" if i % 2 == 0 else self._label
            items.append(
                JudgeItem(
                    i=i,
                    label=label,
                    kind="FACT",
                    stale=False,
                    conf=0.9,
                    # Deliberate echo of private entry text: the privacy test
                    # asserts this never reaches report.md / report.json.
                    why=f"stub rationale echoing: {text[:80]}",
                )
            )
        usage = JudgeUsage(input_tokens=len(user) // 4, output_tokens=20 * n_entries)
        return items, usage

    def complete(self, system: str, user: str, max_tokens: int = 400) -> tuple[str, JudgeUsage]:
        self.calls += 1
        text = json.dumps(
            {
                "label": "SUPPORTED",
                "claim_type": "STATE",
                "conf": 0.9,
                "evidence_date": None,
                "why": "stub rationale echoing: " + user[:80],
            }
        )
        return text, JudgeUsage(input_tokens=len(user) // 4, output_tokens=40)
