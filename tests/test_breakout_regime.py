"""
BREAKOUT as a fourth H1 regime.

Rule 1 of the strategy document defines bullish / bearish / range / unclear and
gives no H1 conditions for a breakout regime. This classifies one by applying
the document's own M15 breakout structure test to H1 bars. It is checked AFTER
Rule 1 by default, so it can only rescue a bar that was already unclear.
"""
import pytest
import scenarios as S

from regime import BREAKOUT, RANGE, TREND_DOWN, TREND_UP, UNDEFINED, RegimeConfig, classify, regime_score

OFF = RegimeConfig()
ON = RegimeConfig(breakout_enabled=True)


def test_disabled_by_default():
    assert RegimeConfig().breakout_enabled is False
    assert classify(S.h1_breakout_regime(), OFF).state == UNDEFINED


def test_fires_when_enabled():
    r = classify(S.h1_breakout_regime(), ON)
    assert r.state == BREAKOUT
    assert r.is_breakout
    assert r.direction == "buy"
    assert "broken" in r.reason


@pytest.mark.parametrize("name,series,expected", [
    ("uptrend", S.h1_uptrend(), TREND_UP),
    ("downtrend", S.h1_downtrend(), TREND_DOWN),
    ("range", S.h1_true_range(), RANGE),
])
def test_enabling_it_never_steals_a_rule_1_regime(name, series, expected):
    """Additive by construction: Rule 1 wins every bar it can classify."""
    assert classify(series, OFF).state == expected
    assert classify(series, ON).state == expected


def test_precedence_before_lets_it_win():
    cfg = RegimeConfig(breakout_enabled=True, breakout_precedence="before")
    r = classify(S.h1_breakout_regime(), cfg)
    assert r.state == BREAKOUT


def test_requires_an_expansion_candle():
    cfg = RegimeConfig(breakout_enabled=True, breakout_expansion=99.0)
    assert classify(S.h1_breakout_regime(), cfg).state == UNDEFINED


def test_requires_the_retest_when_configured():
    cfg = RegimeConfig(breakout_enabled=True, breakout_retest_atr=0.0001)
    assert classify(S.h1_breakout_regime(), cfg).state == UNDEFINED


def test_retest_can_be_waived():
    no_retest = S.h1_breakout_regime(retest=False)
    assert classify(no_retest, ON).state == UNDEFINED
    cfg = RegimeConfig(breakout_enabled=True, breakout_requires_retest=False)
    assert classify(no_retest, cfg).state == BREAKOUT


def test_quiet_market_is_not_a_breakout():
    assert classify(S.h1_true_range(), ON).state != BREAKOUT


def test_breakout_scores_as_a_valid_regime():
    r = classify(S.h1_breakout_regime(), ON)
    points, why = regime_score(r, ON)
    assert points == 20
    assert "breakout regime" in why


def test_precedence_decides_when_a_market_is_both():
    """
    On a market that is simultaneously a Rule 1 trend and a fresh break, the
    default 'after' precedence must leave it a TREND. This is the whole point
    of checking BREAKOUT last.
    """
    series = S.h1_trend_with_breakout()
    assert classify(series, OFF).state == TREND_UP, "fixture is not a Rule 1 trend"

    from indicators import atr
    from regime import _detect_breakout
    assert _detect_breakout(series, ON, float(atr(series, 14)[-1])) is not None, \
        "fixture does not contain a detectable breakout"

    assert classify(series, ON).state == TREND_UP, \
        "BREAKOUT stole a bar Rule 1 had already classified"

    before = RegimeConfig(breakout_enabled=True, breakout_precedence="before")
    assert classify(series, before).state == BREAKOUT, \
        "precedence 'before' should let the breakout win"
