"""B1: cost guard — pre-run estimate abort and mid-run abort."""

from __future__ import annotations

import pytest

from compaction_spy.cost import CostAbort, CostGuard, PriceConfig, estimate_usd


def test_estimate_from_char_counts() -> None:
    # 4M chars -> 1M input tokens -> $3; plus 1000 output tokens -> $0.015.
    est = estimate_usd([4_000_000], max_output_tokens=1000)
    assert est == pytest.approx(3.015)


def test_estimate_abort_before_any_call() -> None:
    guard = CostGuard(max_usd=0.01)
    with pytest.raises(CostAbort, match="no requests were made"):
        guard.check_estimate([400_000], max_output_tokens=8000)


def test_estimate_within_budget_passes() -> None:
    guard = CostGuard(max_usd=10.0)
    est = guard.check_estimate([4000, 4000], max_output_tokens=100)
    assert 0 < est < 10


def test_stage2_estimate_abort_counts_spend_so_far() -> None:
    guard = CostGuard(max_usd=1.0)
    guard.record_usage(input_tokens=300_000, output_tokens=0)  # $0.90 spent in stage 1
    # $0.15 more would exceed; stage 1 alone would have passed this plan.
    with pytest.raises(CostAbort, match="full-context stage"):
        guard.check_stage2_estimate([40_000], max_output_tokens=8000)


def test_stage2_estimate_within_remaining_budget_passes() -> None:
    guard = CostGuard(max_usd=1.0)
    guard.record_usage(input_tokens=100_000, output_tokens=0)  # $0.30 spent
    est = guard.check_stage2_estimate([4_000], max_output_tokens=100)
    assert 0 < est < 0.7


def test_midrun_abort_when_next_call_could_exceed() -> None:
    guard = CostGuard(max_usd=0.50)
    guard.record_usage(input_tokens=100_000, output_tokens=10_000)  # $0.45 spent
    with pytest.raises(CostAbort, match="would exceed"):
        guard.check_before_call(request_chars=40_000, max_output_tokens=8000)


def test_midrun_allows_call_within_budget() -> None:
    guard = CostGuard(max_usd=5.0)
    guard.record_usage(input_tokens=1000, output_tokens=100)
    guard.check_before_call(request_chars=4000, max_output_tokens=100)


def test_running_total_tracks_reported_usage() -> None:
    guard = CostGuard(max_usd=10.0)
    guard.record_usage(input_tokens=1_000_000, output_tokens=0)
    assert guard.spent_usd == pytest.approx(3.0)
    guard.record_usage(input_tokens=0, output_tokens=100_000)
    assert guard.spent_usd == pytest.approx(4.5)


def test_price_config_is_configurable() -> None:
    cheap = PriceConfig(input_per_mtok=1.0, output_per_mtok=5.0)
    assert estimate_usd([4_000_000], 0, cheap) == pytest.approx(1.0)
