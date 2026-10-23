"""B1: label-schema validation and retry/backoff on a stubbed Bedrock client."""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from compaction_spy.judge import (
    BedrockJudge,
    JudgeError,
    JudgeSchemaError,
    StubJudge,
    parse_judge_text,
    validate_items,
)


def _ok_payload(n: int = 2) -> dict:
    return {
        "items": [
            {"i": i, "label": "TOOL", "kind": "FACT", "stale": False, "conf": 0.9, "why": "w"}
            for i in range(n)
        ]
    }


def _converse_response(payload: dict) -> dict:
    return {
        "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
        "usage": {"inputTokens": 100, "outputTokens": 50},
    }


class FakeBedrockClient:
    """Explicit opt-in stub; conftest forbids real boto3 clients."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0

    def converse(self, **kwargs: object) -> dict:
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _throttle() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )


def test_validate_items_accepts_good_payload() -> None:
    items = validate_items(_ok_payload(), 2)
    assert [it.label for it in items] == ["TOOL", "TOOL"]


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"i": 0, "label": "MADE_UP", "kind": "FACT", "conf": 0.5}]},
        {"items": [{"i": 0, "label": "TOOL", "kind": "WRONG", "conf": 0.5}]},
        {"items": [{"i": 5, "label": "TOOL", "kind": "FACT", "conf": 0.5}]},
        {"items": [{"i": 0, "label": "TOOL", "kind": "FACT", "conf": 2.0}]},
        {"items": ["not a dict"]},
        {"wrong": []},
    ],
)
def test_validate_items_rejects_bad_payloads(payload: dict) -> None:
    with pytest.raises(JudgeSchemaError):
        validate_items(payload, 2)


def test_parse_judge_text_extracts_embedded_json() -> None:
    text = "Here is my answer:\n" + json.dumps(_ok_payload(1)) + "\nthanks"
    assert len(parse_judge_text(text, 1)) == 1


def test_parse_judge_text_rejects_non_json() -> None:
    with pytest.raises(JudgeSchemaError):
        parse_judge_text("no json here", 1)


def test_retry_backoff_throttle_then_success() -> None:
    client = FakeBedrockClient([_throttle(), _throttle(), _converse_response(_ok_payload())])
    sleeps: list[float] = []
    judge = BedrockJudge(client=client, sleep=sleeps.append)
    items, usage = judge.judge("sys", "user", 2, 1000)
    assert client.calls == 3
    assert len(items) == 2
    assert usage.input_tokens == 100
    # Exponential-ish backoff: 5*(attempt+1) + jitter.
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_retry_exhaustion_raises_judge_error() -> None:
    client = FakeBedrockClient([_throttle()] * 10)
    judge = BedrockJudge(client=client, sleep=lambda _s: None)
    with pytest.raises(JudgeError, match="after 4 attempts"):
        judge.judge("sys", "user", 1, 1000)
    assert client.calls == 4


def test_schema_invalid_output_retried_then_counted() -> None:
    bad = _converse_response({"items": [{"i": 0, "label": "NOPE", "kind": "FACT", "conf": 0.5}]})
    client = FakeBedrockClient([bad, _converse_response(_ok_payload(1))])
    judge = BedrockJudge(client=client, sleep=lambda _s: None)
    items, _usage = judge.judge("sys", "user", 1, 1000)
    assert client.calls == 2 and items[0].label == "TOOL"


def test_schema_invalid_exhaustion_is_judge_error_not_silent() -> None:
    bad = _converse_response({"items": [{"i": 0, "label": "NOPE", "kind": "FACT", "conf": 0.5}]})
    client = FakeBedrockClient([bad] * 10)
    judge = BedrockJudge(client=client, sleep=lambda _s: None)
    with pytest.raises(JudgeError):
        judge.judge("sys", "user", 1, 1000)


def test_stub_judge_echoes_entry_text_in_why() -> None:
    stub = StubJudge()
    user = "<summary_entries>\n0. [Decisions] rotate the frobnicator\n</summary_entries>"
    items, _ = stub.judge("sys", user, 1, 1000)
    assert "rotate the frobnicator" in items[0].why


def test_real_boto3_client_is_forbidden_by_conftest() -> None:
    with pytest.raises(RuntimeError, match="stubbed client"):
        BedrockJudge()
