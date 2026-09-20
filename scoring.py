"""
scoring.py — the signal score gate.

The strategy requires `signal score >= 80` before any entry.

STATUS: NOT CONFIGURED — the spec states the threshold but not the function.
A threshold without a formula cannot be evaluated, so score() refuses and the
trade is rejected. This is the single gate every trade passes through, so
guessing at it would silently decide which trades you take.

To make scoring live, implement score() to return 0-100. What it needs:

  - the component factors (e.g. regime alignment, distance from the EMA,
    ADX strength, spread, time-of-session, proximity to structure,
    candle confirmation quality, RR headroom)
  - each factor's weight, and how it maps to points
  - whether any factor is a veto (scores 0 regardless of the rest)
  - whether the score is comparable across the three setups, or whether each
    setup has its own scale

Until then the threshold is enforced against a score that cannot be produced,
so nothing trades.
"""
MIN_SCORE = 80.0


class ScoreNotConfigured(Exception):
    """Raised when scoring is required but no formula has been defined."""


def score(candidate, regime, context: dict) -> tuple[float | None, str]:
    """
    Returns (score, reason). A None score means the trade cannot be evaluated
    and must be rejected.
    """
    return None, (
        "signal score is not defined — scoring.py has the threshold (>= 80) "
        "but no formula. See its docstring for what the formula needs. No trade."
    )


def passes(value: float | None, minimum: float = MIN_SCORE) -> bool:
    return value is not None and value >= minimum
