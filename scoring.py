"""
scoring.py — the signal score, per LATHE SETUP RULES + SIGNAL SCORE FORMULA v1.

    1. Regime quality      0-25
    2. Setup location      0-20
    3. Entry trigger       0-20
    4. Reward / risk       0-15
    5. Execution quality   0-10
    6. Session / volatility 0-10
                          -----
                            100

The six categories are SUMMED. The spec renders the formula with asterisks
between the terms, but those are mangled bullet points: the categories max at
25+20+20+15+10+10 = 100 only when added, and both worked examples in the spec
confirm it — 25+20+20+15+0+10 = 90 ("total mathematically = 90", where the
product would be 0) and 25+20+20+12+10+8 = 95 matching that example's
"score_total": 95.

Hard rules that the score cannot override:

    - every hard gate must pass
    - trigger_score must be exactly 20
    - execution_score of 0 (spread filter failed) blocks the trade
    - RR below the setup's minimum invalidates it
    - total must be >= 80

A trade that scores 90 with a failed execution filter is a HOLD, not a BUY.
"""
from dataclasses import dataclass, field

MIN_SCORE = 80.0
REQUIRED_TRIGGER_SCORE = 20

HOLD = "HOLD"
WATCH = "WATCH"
VALID = "VALID SIGNAL"
STRONG = "STRONG VALID SIGNAL"

# Reward:risk bands, by setup class (spec category 4).
RR_BANDS = {
    "trend": ((2.5, 15), (2.0, 12)),
    "breakout": ((2.5, 15), (2.0, 12)),
    "range": ((2.0, 15), (1.5, 12)),
}


@dataclass
class ScoreBreakdown:
    regime: int = 0
    setup: int = 0
    trigger: int = 0
    reward_risk: int = 0
    execution: int = 0
    session: int = 0
    reasons: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    min_score: float = MIN_SCORE

    @property
    def total(self) -> int:
        return (self.regime + self.setup + self.trigger
                + self.reward_risk + self.execution + self.session)

    def as_dict(self) -> dict:
        return {"regime": self.regime, "setup": self.setup, "trigger": self.trigger,
                "reward_risk": self.reward_risk, "execution": self.execution,
                "session": self.session}

    @property
    def classification(self) -> str:
        t = self.total
        if t >= 90:
            return STRONG
        if t >= 80:
            return VALID
        if t >= 60:
            return WATCH
        return HOLD

    @property
    def executable(self) -> bool:
        """Score alone is never enough — see the hard rules in the module docstring."""
        return (not self.blockers
                and self.trigger == REQUIRED_TRIGGER_SCORE
                and self.execution > 0
                and self.reward_risk > 0
                and self.total >= self.min_score)


def rr_score(setup_class: str, rr: float) -> tuple[int, str]:
    """Category 4. Returns 0 when RR is below the setup's minimum, which
    invalidates the setup outright."""
    bands = RR_BANDS.get(setup_class, RR_BANDS["trend"])
    for threshold, points in bands:
        if rr >= threshold:
            return points, f"RR {rr:.2f} >= {threshold}"
    minimum = bands[-1][0]
    return 0, f"RR {rr:.2f} below the {minimum} minimum for a {setup_class} setup"


def execution_score(spread: float, median_spread: float,
                    max_spread: float) -> tuple[int, str]:
    """
    Category 5, from the spread relative to its recent median.

    10  spread <= median
     7  spread <= 1.25 * median
     4  above that but within the hard maximum
     0  hard maximum exceeded -> no trade at any score
    """
    if spread > max_spread:
        return 0, f"spread {spread:.5f} exceeds the hard maximum {max_spread:.5f}"
    if median_spread <= 0 or median_spread != median_spread:      # unknown median
        return 7, f"spread {spread:.5f} within limits (no median history yet)"
    if spread <= median_spread:
        return 10, f"spread {spread:.5f} at or below median {median_spread:.5f}"
    if spread <= 1.25 * median_spread:
        return 7, f"spread {spread:.5f} <= 1.25x median {median_spread:.5f}"
    return 4, f"spread {spread:.5f} above 1.25x median but within the maximum"


def session_score(atr: float, median_atr: float, news_clear: bool = True,
                  data_healthy: bool = True) -> tuple[int, str]:
    """
    Category 6. Volatility is judged against its own recent median, since what
    counts as healthy differs between the two pairs and across the session.

    The band edges are not given in the spec and are configurable — see
    SessionScoreConfig.
    """
    if not news_clear:
        return 0, "inside a high-impact news blackout"
    if not data_healthy:
        return 0, "data or execution environment unhealthy"
    if median_atr <= 0 or median_atr != median_atr or atr != atr:
        return 7, "normal conditions (no volatility history yet)"

    ratio = atr / median_atr
    if 0.8 <= ratio <= 1.5:
        return 10, f"healthy volatility ({ratio:.2f}x median ATR)"
    if 0.6 <= ratio <= 2.0:
        return 7, f"normal volatility ({ratio:.2f}x median ATR)"
    return 4, f"unusual volatility ({ratio:.2f}x median ATR)"


def build(*, regime_points: int, regime_reason: str,
          candidate, setup_class: str,
          spread: float, median_spread: float, max_spread: float,
          atr_m5: float, median_atr_m5: float,
          news_clear: bool = True, data_healthy: bool = True,
          min_score: float = MIN_SCORE) -> ScoreBreakdown:
    """Assemble the full breakdown for one candidate."""
    b = ScoreBreakdown(min_score=min_score)

    b.regime = regime_points
    b.reasons.append(regime_reason)

    b.setup = candidate.location_score
    b.reasons.append(candidate.location_reason)

    b.trigger = candidate.trigger_score
    b.reasons.append(candidate.trigger_reason)

    b.reward_risk, rr_why = rr_score(setup_class, candidate.rr)
    b.reasons.append(rr_why)

    b.execution, exec_why = execution_score(spread, median_spread, max_spread)
    b.reasons.append(exec_why)

    b.session, session_why = session_score(atr_m5, median_atr_m5, news_clear, data_healthy)
    b.reasons.append(session_why)

    if b.trigger != REQUIRED_TRIGGER_SCORE:
        b.blockers.append(f"entry trigger not fully confirmed (scored {b.trigger}/20)")
    if b.execution == 0:
        b.blockers.append("spread filter failed")
    if b.reward_risk == 0:
        b.blockers.append(rr_why)
    if b.session == 0:
        b.blockers.append(session_why)
    if b.total < min_score:
        b.blockers.append(f"score {b.total} below the {min_score:.0f} threshold")
    return b


def signal_object(symbol: str, candidate, regime_state: str,
                  breakdown: ScoreBreakdown, risk_pct: float) -> dict:
    """The record the strategy asks every evaluated setup to return."""
    direction = "LONG" if candidate.direction == "buy" else "SHORT"
    signal = ("BUY" if candidate.direction == "buy" else "SELL") \
        if breakdown.executable else HOLD
    return {
        "symbol": symbol,
        "strategy": candidate.setup,
        "direction": direction,
        "regime": regime_state,
        "scores": breakdown.as_dict(),
        "score_total": breakdown.total,
        "classification": breakdown.classification,
        "entry": candidate.entry_price,
        "stop": candidate.stop_price,
        "target": candidate.target_price,
        "rr": round(candidate.rr, 2),
        "risk_pct": risk_pct,
        "hard_gates_passed": not breakdown.blockers,
        "signal": signal,
        "reason": list(breakdown.reasons),
        "blockers": list(breakdown.blockers),
    }
