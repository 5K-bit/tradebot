"""
The signal score. The two worked examples from the strategy document are
encoded directly — they are what proves the six categories are SUMMED, not
multiplied, and that hard rules outrank the total.
"""
import pytest

import scoring
from scoring import (MIN_SCORE, REQUIRED_TRIGGER_SCORE, ScoreBreakdown,
                     execution_score, rr_score, session_score, signal_object)
from setups import TREND_PULLBACK, SetupCandidate


# --- the spec's own worked examples ----------------------------------------
def test_spec_example_execution_failure_is_a_hold():
    """
    Spec: regime 25, setup 20, trigger 20, RR 15, execution 0, session 10.
    "Total mathematically = 90 ... BUT execution filter failed ... HOLD, NOT BUY."
    """
    b = ScoreBreakdown(regime=25, setup=20, trigger=20, reward_risk=15,
                       execution=0, session=10)
    assert b.total == 90, "categories must be summed, not multiplied"
    b.blockers.append("spread filter failed")
    assert b.executable is False
    assert b.classification == scoring.STRONG          # scores high...
    # ...and still does not trade.


def test_spec_signal_object_example_totals_95():
    """Spec's signal object: 25+20+20+12+10+8 = 95, matching "score_total": 95."""
    b = ScoreBreakdown(regime=25, setup=20, trigger=20, reward_risk=12,
                       execution=10, session=8)
    assert b.total == 95
    assert b.executable is True


def test_maximum_is_exactly_100():
    b = ScoreBreakdown(regime=25, setup=20, trigger=20, reward_risk=15,
                       execution=10, session=10)
    assert b.total == 100


def test_multiplication_would_be_absurd():
    """Guard the interpretation: the product of the maxima is 15 million."""
    maxima = [25, 20, 20, 15, 10, 10]
    product = 1
    for m in maxima:
        product *= m
    assert product != 100 and sum(maxima) == 100


# --- classification bands ---------------------------------------------------
@pytest.mark.parametrize("total,expected", [
    (0, scoring.HOLD), (59, scoring.HOLD),
    (60, scoring.WATCH), (79, scoring.WATCH),
    (80, scoring.VALID), (89, scoring.VALID),
    (90, scoring.STRONG), (100, scoring.STRONG),
])
def test_classification_bands(total, expected):
    b = ScoreBreakdown(regime=total)     # single category, just to reach a total
    assert b.total == total
    assert b.classification == expected


# --- category 4: reward / risk ---------------------------------------------
@pytest.mark.parametrize("rr,points", [(3.0, 15), (2.5, 15), (2.4, 12), (2.0, 12), (1.9, 0)])
def test_rr_bands_trend(rr, points):
    assert rr_score("trend", rr)[0] == points


@pytest.mark.parametrize("rr,points", [(2.5, 15), (2.0, 15), (1.9, 12), (1.5, 12), (1.4, 0)])
def test_rr_bands_range(rr, points):
    assert rr_score("range", rr)[0] == points


def test_breakout_uses_the_trend_bands():
    assert rr_score("breakout", 2.0)[0] == 12
    assert rr_score("breakout", 1.9)[0] == 0


# --- category 5: execution --------------------------------------------------
def test_execution_bands():
    median, hard_max = 0.00010, 0.00030
    assert execution_score(0.00008, median, hard_max)[0] == 10
    assert execution_score(0.00010, median, hard_max)[0] == 10
    assert execution_score(0.00012, median, hard_max)[0] == 7
    assert execution_score(0.00020, median, hard_max)[0] == 4
    assert execution_score(0.00040, median, hard_max)[0] == 0


def test_execution_without_history_is_not_a_free_ten():
    points, why = execution_score(0.00010, float("nan"), 0.00030)
    assert points == 7 and "no median" in why


# --- category 6: session / volatility --------------------------------------
def test_session_bands():
    assert session_score(0.0010, 0.0010)[0] == 10
    assert session_score(0.0018, 0.0010)[0] == 7
    assert session_score(0.0030, 0.0010)[0] == 4
    assert session_score(0.0004, 0.0010)[0] == 4


def test_news_blackout_zeroes_the_session_score():
    points, why = session_score(0.0010, 0.0010, news_clear=False)
    assert points == 0 and "news" in why


def test_unhealthy_data_zeroes_the_session_score():
    assert session_score(0.0010, 0.0010, data_healthy=False)[0] == 0


# --- hard rules -------------------------------------------------------------
def candidate(rr_target=1.1050):
    return SetupCandidate(TREND_PULLBACK, "buy", 1.1000, 1.0980, rr_target, 1.0980,
                          location_score=20, location_reason="at structure",
                          trigger_score=20, trigger_reason="confirmed")


def build(**over):
    args = dict(regime_points=25, regime_reason="strong trend",
                candidate=candidate(), setup_class="trend",
                spread=0.00008, median_spread=0.00010, max_spread=0.00030,
                atr_m5=0.0010, median_atr_m5=0.0010)
    args.update(over)
    return scoring.build(**args)


def test_full_house_is_executable():
    b = build()
    assert b.total >= MIN_SCORE
    assert b.executable is True
    assert not b.blockers


def test_trigger_below_20_blocks_regardless_of_total():
    c = candidate()
    c.trigger_score = 10
    b = build(candidate=c)
    assert b.executable is False
    assert any("trigger" in x for x in b.blockers)


def test_failed_spread_blocks_regardless_of_total():
    b = build(spread=0.00090)          # beyond the hard maximum
    assert b.execution == 0
    assert b.executable is False


def test_rr_below_minimum_blocks():
    b = build(candidate=candidate(rr_target=1.1015))    # RR 0.75
    assert b.reward_risk == 0
    assert b.executable is False


def test_news_blackout_blocks():
    b = build(news_clear=False)
    assert b.session == 0 and b.executable is False


def test_score_below_threshold_blocks():
    # 0 + 20 + 20 + 12 + 10 + 10 = 72, under the 80 needed to execute.
    b = build(regime_points=0)
    assert b.total == 72
    assert b.total < MIN_SCORE
    assert b.executable is False
    assert any("below the 80" in x for x in b.blockers)


def test_required_trigger_score_constant_matches_spec():
    assert REQUIRED_TRIGGER_SCORE == 20
    assert MIN_SCORE == 80.0


# --- the signal object ------------------------------------------------------
def test_signal_object_shape():
    b = build()
    obj = signal_object("EURUSD", candidate(), "bullish_trend", b, 0.01)
    for key in ("symbol", "strategy", "direction", "regime", "scores",
                "score_total", "entry", "stop", "target", "rr", "risk_pct",
                "hard_gates_passed", "signal", "reason"):
        assert key in obj, f"signal object missing {key}"
    assert obj["signal"] == "BUY"
    assert obj["direction"] == "LONG"
    assert obj["hard_gates_passed"] is True
    assert obj["scores"]["trigger"] == 20
    assert sum(obj["scores"].values()) == obj["score_total"]


def test_signal_object_says_hold_when_blocked():
    b = build(spread=0.00090)
    obj = signal_object("EURUSD", candidate(), "bullish_trend", b, 0.01)
    assert obj["signal"] == "HOLD"
    assert obj["hard_gates_passed"] is False
    assert obj["blockers"]


def test_signal_object_direction_for_a_short():
    c = SetupCandidate(TREND_PULLBACK, "sell", 1.1000, 1.1020, 1.0950, 1.1020,
                       location_score=20, trigger_score=20)
    obj = signal_object("USDJPY", c, "bearish_trend", build(candidate=c), 0.01)
    assert obj["direction"] == "SHORT"
    assert obj["signal"] in ("SELL", "HOLD")


# --- executable(): each clause must hold independently ----------------------
# These construct a breakdown directly, with NO blockers, so that removing any
# single clause from executable() is visible. Going through build() would add a
# blocker and mask which clause actually did the work.
def clean(**over):
    args = dict(regime=25, setup=20, trigger=20, reward_risk=15,
                execution=10, session=10)
    args.update(over)
    return ScoreBreakdown(**args)


def test_executable_requires_a_full_trigger_on_its_own():
    b = clean(trigger=10)
    assert b.blockers == []
    assert b.total == 90, "still a high score"
    assert b.executable is False, "trigger below 20 must block by itself"


def test_executable_requires_nonzero_execution_on_its_own():
    b = clean(execution=0)
    assert b.blockers == []
    assert b.total == 90
    assert b.executable is False, "a failed spread filter must block by itself"


def test_executable_requires_the_threshold_on_its_own():
    b = clean(regime=0, setup=10, reward_risk=12, session=4)   # 0+10+20+12+10+4 = 56
    assert b.blockers == []
    assert b.total == 56
    assert b.executable is False, "below the threshold must block by itself"


def test_executable_requires_nonzero_rr_on_its_own():
    b = clean(reward_risk=0)
    assert b.blockers == []
    assert b.executable is False


def test_a_clean_full_house_is_executable():
    b = clean()
    assert b.total == 100 and b.executable is True


def test_custom_min_score_is_honoured():
    b = clean(min_score=101)
    assert b.total == 100 and b.executable is False
