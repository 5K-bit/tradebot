"""
setups.py — the three entry patterns from LATHE ADAPTIVE SESSION STRATEGY v1.

    1. Trend Pullback
    2. Range Reversion
    3. Breakout + Retest

Setup identified on M15, entry refined on M5.

STATUS: NOT CONFIGURED — the strategy document names these three setups but
does not define them, and a setup name is not a rule. Rather than invent
entry logic for a bot that trades real money, each detector below refuses and
says exactly what it needs. The bot therefore takes no trades until these are
filled in: consistent with the house rule that missing data means no trade.

To make a setup live, implement its detector to return a SetupCandidate. The
surrounding machinery — session gating, regime filter, protection, sizing,
RR validation, trade management, logging — is complete and tested, so each
detector is a self-contained drop-in.

What each detector needs from you, concretely:

  Trend Pullback
    - what counts as a pullback (touch/close beyond EMA20? a % or ATR
      retracement of the prior leg? a Fibonacci band?)
    - how deep is too deep (the level past which the trend is void)
    - what confirms resumption on M5 (engulfing close? break of the pullback's
      high? momentum cross?)
    - where the structural stop anchors (last M15 swing, or the pullback low)

  Range Reversion
    - how the range boundaries are established (N-bar high/low? Donchian
      period? Bollinger band?)
    - how close to the boundary an entry may be taken
    - what invalidates the range (a close beyond it, or a close beyond it by
      some ATR margin?)
    - whether the target is the mid-range or the opposite boundary

  Breakout + Retest
    - what level qualifies as the breakout level (session high/low? prior
      range boundary? swing structure?)
    - what counts as a break (close beyond? beyond by an ATR multiple?)
    - the retest tolerance, and how many bars the retest may take before the
      signal expires
    - what confirms the retest held on M5
"""
from dataclasses import dataclass

TREND_PULLBACK = "trend_pullback"
RANGE_REVERSION = "range_reversion"
BREAKOUT_RETEST = "breakout_retest"

ALL_SETUPS = (TREND_PULLBACK, RANGE_REVERSION, BREAKOUT_RETEST)

# Minimum reward:risk per the strategy spec.
MIN_RR = {
    TREND_PULLBACK: 2.0,
    BREAKOUT_RETEST: 2.0,
    RANGE_REVERSION: 1.5,
}


@dataclass
class SetupCandidate:
    """A potential trade, before scoring and risk sizing."""
    setup: str
    direction: str          # "buy" or "sell"
    entry_price: float
    stop_price: float
    target_price: float
    structure_level: float  # the swing/boundary the stop is anchored to
    notes: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def reward_distance(self) -> float:
        return abs(self.target_price - self.entry_price)

    @property
    def rr(self) -> float:
        risk = self.risk_distance
        return self.reward_distance / risk if risk > 0 else 0.0

    def meets_min_rr(self) -> bool:
        return self.rr >= MIN_RR.get(self.setup, 2.0)


class SetupNotConfigured(Exception):
    """Raised when a setup is selected but its rules have not been defined."""


def detect(setup: str, m15_candles, m5_candles, regime, cfg: dict):
    """
    Returns (SetupCandidate | None, reason).

    A None candidate with a reason is a normal "no trade here" outcome, not an
    error — the reason is logged so every skipped bar is accounted for.
    """
    if setup not in ALL_SETUPS:
        return None, f"unknown setup {setup!r}"

    detector = _DETECTORS[setup]
    return detector(m15_candles, m5_candles, regime, cfg)


def _not_configured(name: str):
    def detector(m15_candles, m5_candles, regime, cfg):
        return None, (
            f"setup '{name}' has no rules defined — see the docstring in "
            f"setups.py for exactly what it needs. No trade."
        )
    detector.not_configured = True
    return detector


_DETECTORS = {
    TREND_PULLBACK: _not_configured(TREND_PULLBACK),
    RANGE_REVERSION: _not_configured(RANGE_REVERSION),
    BREAKOUT_RETEST: _not_configured(BREAKOUT_RETEST),
}


def configured_setups() -> tuple:
    """Which setups actually have rules. Empty until you define them."""
    return tuple(name for name, fn in _DETECTORS.items()
                 if not getattr(fn, "not_configured", False))


def any_configured() -> bool:
    return len(configured_setups()) > 0
