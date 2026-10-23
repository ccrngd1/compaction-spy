"""Cost estimation and the hard --max-usd guard.

Three checkpoints, all from character counts (chars/4 ≈ tokens, matching the
spike's accounting). Stage-1 pre-run: estimate all retrieval requests and abort
before any call if that alone exceeds the budget. Stage-2 pre-check: once the
retrieval stage has decided which entries need full-context confirmation, the
built full-context requests are estimated and checked against the budget minus
actual spend so far, before any full-context call. Mid-run: track reported
usage; before each call, abort if the running total plus a worst-case next call
could exceed the budget. An abort exits non-zero and writes no report.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# Defaults for Sonnet via Bedrock; configurable.
PRICE_INPUT_PER_MTOK = 3.0
PRICE_OUTPUT_PER_MTOK = 15.0
CHARS_PER_TOKEN = 4


class CostAbort(Exception):
    """Raised when the budget would be exceeded; the run stops with no report."""


@dataclass
class PriceConfig:
    input_per_mtok: float = PRICE_INPUT_PER_MTOK
    output_per_mtok: float = PRICE_OUTPUT_PER_MTOK
    chars_per_token: int = CHARS_PER_TOKEN


def estimate_usd(
    request_chars: list[int],
    max_output_tokens: int,
    prices: PriceConfig | None = None,
) -> float:
    """Pre-run estimate: chars/4 input tokens plus max output tokens per call."""
    p = prices or PriceConfig()
    input_tokens = sum(c // p.chars_per_token for c in request_chars)
    output_tokens = max_output_tokens * len(request_chars)
    return input_tokens * p.input_per_mtok / 1e6 + output_tokens * p.output_per_mtok / 1e6


class CostGuard:
    """Thread-safe running-total guard around judge calls."""

    def __init__(self, max_usd: float, prices: PriceConfig | None = None) -> None:
        self.max_usd = max_usd
        self.prices = prices or PriceConfig()
        self._lock = threading.Lock()
        self._spent_usd = 0.0

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._spent_usd

    def check_estimate(self, request_chars: list[int], max_output_tokens: int) -> float:
        """Stage-1: abort before any call if the retrieval stage exceeds the budget."""
        est = estimate_usd(request_chars, max_output_tokens, self.prices)
        if est > self.max_usd:
            raise CostAbort(
                f"retrieval stage estimated cost ${est:.2f} exceeds "
                f"--max-usd ${self.max_usd:.2f}; no requests were made"
            )
        return est

    def check_stage2_estimate(self, request_chars: list[int], max_output_tokens: int) -> float:
        """Stage-2: abort before any full-context call if spend so far plus the
        full-context stage estimate exceeds the budget."""
        est = estimate_usd(request_chars, max_output_tokens, self.prices)
        with self._lock:
            spent = self._spent_usd
        if spent + est > self.max_usd:
            raise CostAbort(
                f"full-context stage estimated cost ${est:.2f} plus ${spent:.2f} "
                f"already spent would exceed --max-usd ${self.max_usd:.2f}; "
                "no full-context requests were made"
            )
        return est

    def check_before_call(self, request_chars: int, max_output_tokens: int) -> None:
        """Abort if this call's worst case could push the total over budget."""
        call_cost = estimate_usd([request_chars], max_output_tokens, self.prices)
        with self._lock:
            if self._spent_usd + call_cost > self.max_usd:
                raise CostAbort(
                    f"running total ${self._spent_usd:.2f} plus next call "
                    f"(~${call_cost:.2f}) would exceed --max-usd ${self.max_usd:.2f}"
                )

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        cost = (
            input_tokens * self.prices.input_per_mtok / 1e6
            + output_tokens * self.prices.output_per_mtok / 1e6
        )
        with self._lock:
            self._spent_usd += cost
