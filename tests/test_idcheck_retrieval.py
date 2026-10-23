"""B1: identifier regex + verbatim check; BM25 evidence budget."""

from __future__ import annotations

from pathlib import Path

from compaction_spy import store
from compaction_spy.case import SpanMessage, build_case
from compaction_spy.idcheck import check_case, extract_identifiers
from compaction_spy.parse import Entry
from compaction_spy.retrieval import select_evidence
from tests.fixtures.build_fixture_db import ID_ABSENT, ID_PRESENT


def test_identifier_regex_backticks_and_urls() -> None:
    text = (
        "see `frob.toml` and `widget-staging-conf` plus https://example.invalid/docs, "
        "short `abc` skipped, spaced `a b` skipped."
    )
    ids = extract_identifiers(text)
    assert "frob.toml" in ids
    assert "widget-staging-conf" in ids
    assert "https://example.invalid/docs" in ids
    assert "abc" not in ids  # under 4 chars
    assert not any(" " in i for i in ids)


def test_identifier_trailing_punctuation_stripped() -> None:
    ids = extract_identifiers("go to https://example.invalid/page. now")
    assert ids == ["https://example.invalid/page"]


def test_idcheck_present_and_absent(fixture_dbs: dict[str, Path]) -> None:
    evs = store.load_sessions(fixture_dbs["alpha"])["sess-fixture-0001"]
    case = build_case("alpha", "sess-fixture-0001", "comp-0001", evs)
    result = check_case(case)
    assert result.total == 2
    assert result.found == 1
    assert result.missing == [ID_ABSENT]
    assert ID_PRESENT not in result.missing


def test_select_evidence_respects_budget() -> None:
    span = [SpanMessage(role="TOOL", segment="SUMMARIZED", text="widget " * 2000)]
    entries = [Entry(section="Decisions", text="widget plan", index=0)]
    picked = select_evidence(entries, span, budget=3000)
    assert picked
    assert sum(len(t) for _r, t in picked) <= 3000


def test_select_evidence_empty_span() -> None:
    assert select_evidence([Entry(section="Decisions", text="x", index=0)], []) == []


def test_evidence_carries_role_and_segment_tag() -> None:
    span = [SpanMessage(role="USER", segment="TAIL", text="rotate the frobnicator config")]
    entries = [Entry(section="Open TODOs", text="rotate the frobnicator config", index=0)]
    picked = select_evidence(entries, span)
    assert picked and picked[0][0] == 'USER" segment="TAIL'
