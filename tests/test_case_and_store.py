"""B1: TAIL segmentation by firstKeptEntryId, serial zstd decode, grouping,
census dedupe-by-hash and malformed-DB skipping."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import zstandard

from compaction_spy import store
from compaction_spy.case import build_case
from compaction_spy.census import census
from tests.fixtures.build_fixture_db import TAIL_COMPLETION


def _alpha_sessions(fixture_dbs: dict[str, Path]) -> dict[str, list]:
    return store.load_sessions(fixture_dbs["alpha"])


def test_tail_segmentation_by_first_kept_entry_id(fixture_dbs: dict[str, Path]) -> None:
    evs = _alpha_sessions(fixture_dbs)["sess-fixture-0001"]
    case = build_case("alpha", "sess-fixture-0001", "comp-0001", evs)
    tail = [m for m in case.span if m.segment == "TAIL"]
    summarized = [m for m in case.span if m.segment == "SUMMARIZED"]
    # firstKeptEntryId = evt-0005: exactly the completion message is the tail.
    assert len(tail) == 1 and TAIL_COMPLETION in tail[0].text
    assert len(summarized) == 4
    assert case.first_compaction


def test_second_compaction_chains_from_previous_first_kept(
    fixture_dbs: dict[str, Path],
) -> None:
    evs = _alpha_sessions(fixture_dbs)["sess-fixture-0001"]
    case = build_case("alpha", "sess-fixture-0001", "comp-0002", evs)
    assert not case.first_compaction
    assert "## Open TODOs" in case.prev_summary
    # Span starts at the previous compaction's firstKeptEntryId (evt-0005).
    assert TAIL_COMPLETION in case.span[0].text
    assert len(case.span) == 3


def test_roles_typed_and_tool_call_args_flattened(fixture_dbs: dict[str, Path]) -> None:
    evs = _alpha_sessions(fixture_dbs)["sess-fixture-0001"]
    case = build_case("alpha", "sess-fixture-0001", "comp-0001", evs)
    roles = [m.role for m in case.span]
    assert roles == ["USER", "TOOL", "ASSISTANT", "ASSISTANT", "TOOL"]
    assert any("[toolCall frob_rotate]" in m.text and "widget-staging" in m.text for m in case.span)


def test_zstd_rows_round_trip(fixture_dbs: dict[str, Path]) -> None:
    events = list(store.iter_events(fixture_dbs["alpha"]))
    assert len(events) >= 9  # nothing lost to the zstd/plain alternation
    assert any(e.get("type") == "compaction" for _s, _q, e in events)


def test_decode_refuses_non_main_thread() -> None:
    dctx = zstandard.ZstdDecompressor()
    payload = zstandard.ZstdCompressor().compress(b'{"type": "message"}')
    errors: list[Exception] = []

    def worker() -> None:
        try:
            store.decode_event(None, payload, dctx)
        except RuntimeError as ex:
            errors.append(ex)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors and "serially on the main thread" in str(errors[0])
    # And on the main thread it works.
    assert store.decode_event(None, payload, dctx) == {"type": "message"}


def test_decode_skips_malformed_json() -> None:
    dctx = zstandard.ZstdDecompressor()
    assert store.decode_event("not json {", None, dctx) is None
    assert store.decode_event(None, None, dctx) is None
    assert store.decode_event('"a bare string"', None, dctx) is None


def test_census_dedupes_by_summary_hash(fixture_dbs: dict[str, Path]) -> None:
    result = census([fixture_dbs["alpha"], fixture_dbs["beta"]])
    # alpha: comp-0001, comp-0002, legacy, dupA; beta's dupB has the same
    # summary text as dupA, so it dedupes away.
    assert len(result.compactions) == 4
    assert result.by_format() == {"sg5": 3, "legacy": 1}


def test_census_skips_malformed_db(fixture_dbs: dict[str, Path]) -> None:
    result = census([fixture_dbs["alpha"], fixture_dbs["broken"]])
    assert [name for name, _reason in result.skipped] == ["broken"]
    assert len(result.compactions) == 4


def test_session_keys(fixture_dbs: dict[str, Path]) -> None:
    keys = store.session_keys(fixture_dbs["alpha"])
    assert keys["sess-fixture-0001"] == "key-sess-fixture-0001"


def test_connect_ro_cannot_write(fixture_dbs: dict[str, Path]) -> None:
    conn = store.connect_ro(fixture_dbs["alpha"])
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("insert into session_windows values ('x', 'y')")
    conn.close()
