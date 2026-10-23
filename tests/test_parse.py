"""B1: section parser — continuation lines, none/n/a, legacy detection, dedupe hash."""

from compaction_spy import parse
from tests.fixtures.build_fixture_db import SUMMARY_FIRST, SUMMARY_LEGACY, SUMMARY_SECOND


def test_parses_sections_and_bullets() -> None:
    entries = parse.parse_entries(SUMMARY_FIRST)
    sections = {e.section for e in entries}
    assert sections == {"Decisions", "Open TODOs", "Constraints/Rules", "Exact identifiers"}
    # "none" placeholder in Pending user asks is skipped
    assert not [e for e in entries if e.section == "Pending user asks"]


def test_continuation_lines_joined() -> None:
    entries = parse.parse_entries(SUMMARY_FIRST)
    decision = next(e for e in entries if e.section == "Decisions")
    assert "before the demo window opens" in decision.text
    assert "\n" not in decision.text


def test_none_and_na_skipped() -> None:
    entries = parse.parse_entries(SUMMARY_SECOND)
    assert {e.section for e in entries} == {"Decisions", "Pending user asks"}


def test_numbered_and_star_bullets() -> None:
    summary = "## Decisions\n1. first synthetic decision\n* second synthetic decision\n"
    entries = parse.parse_entries(summary)
    assert [e.text for e in entries] == ["first synthetic decision", "second synthetic decision"]


def test_unknown_section_ignored() -> None:
    summary = "## Mystery Section\n- ignored bullet\n## Decisions\n- kept bullet\n"
    entries = parse.parse_entries(summary)
    assert [e.text for e in entries] == ["kept bullet"]


def test_legacy_format_detection() -> None:
    assert parse.is_safeguard_format(SUMMARY_FIRST)
    assert not parse.is_safeguard_format(SUMMARY_LEGACY)
    assert parse.parse_entries(SUMMARY_LEGACY) == []


def test_summary_hash_dedupe_key() -> None:
    assert parse.summary_hash(SUMMARY_FIRST) == parse.summary_hash(SUMMARY_FIRST)
    assert parse.summary_hash(SUMMARY_FIRST) != parse.summary_hash(SUMMARY_SECOND)


def test_entry_indices_are_positional() -> None:
    entries = parse.parse_entries(SUMMARY_FIRST)
    assert [e.index for e in entries] == list(range(len(entries)))
