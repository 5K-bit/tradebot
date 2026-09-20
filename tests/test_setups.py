"""
The six entry patterns. Each test drives a synthetic market built to satisfy
(or just miss) one specific clause of the spec.
"""
import pytest
import scenarios as S

import setups
from regime import classify
from setups import (BREAKOUT_RETEST, RANGE_REVERSION, TREND_PULLBACK,
                    MIN_RR, SetupCandidate, SetupConfig)

CFG = SetupConfig()


def detect(name, m15, m5, h1):
    return setups.detect(name, m15, m5, classify(h1), CFG)


# --- A / B: trend pullback --------------------------------------------------
def test_a_trend_pullback_long_fires():
    c, why = detect(TREND_PULLBACK, S.m15_pullback_to_ema(),
                    S.m5_full_long_trigger(), S.h1_uptrend())
    assert c is not None, why
    assert c.direction == "buy"
    assert c.trigger_score == 20
    assert c.stop_price < c.entry_price < c.target_price
    assert c.rr == pytest.approx(2.0, abs=0.01)


def test_b_trend_pullback_short_fires():
    c, why = detect(TREND_PULLBACK, S.m15_pullback_to_ema(down=True),
                    S.m5_full_short_trigger(), S.h1_downtrend())
    assert c is not None, why
    assert c.direction == "sell"
    assert c.trigger_score == 20
    assert c.stop_price > c.entry_price > c.target_price


def test_entry_sits_beyond_the_trigger_candle():
    """SPEC: entry = trigger_high + 0.05 * ATR_M5, i.e. a stop entry."""
    m5 = S.m5_full_long_trigger()
    c, _ = detect(TREND_PULLBACK, S.m15_pullback_to_ema(), m5, S.h1_uptrend())
    assert c.entry_price > float(m5["high"][-1])


def test_pullback_too_far_from_structure_is_rejected():
    """SPEC: valid only when distance_to_structure <= 0.25 * ATR_M15."""
    tight = SetupConfig(pullback_structure_atr=0.0001)
    c, why = setups.detect(TREND_PULLBACK, S.m15_pullback_to_ema(),
                           S.m5_full_long_trigger(), classify(S.h1_uptrend()), tight)
    assert c is None
    assert "pullback limit" in why


def test_trend_pullback_needs_a_trending_regime():
    c, why = detect(TREND_PULLBACK, S.m15_pullback_to_ema(),
                    S.m5_full_long_trigger(), S.h1_true_range())
    assert c is None and "trending regime" in why


def test_incomplete_m5_trigger_does_not_produce_a_full_score():
    """No higher low -> trigger scores 10, and the spec forbids executing."""
    flat_m5 = S.from_closes([1.0950 + i * 0.00002 for i in range(40)], wick=0.00002)
    c, why = detect(TREND_PULLBACK, S.m15_pullback_to_ema(), flat_m5, S.h1_uptrend())
    assert c is None or c.trigger_score < 20


# --- C / D: range reversion -------------------------------------------------
def test_c_range_reversion_long_fires():
    c, why = detect(RANGE_REVERSION, S.m15_range_structure(),
                    S.m5_range_rejection_long(), S.h1_true_range())
    assert c is not None, why
    assert c.direction == "buy"
    assert c.trigger_score == 20
    assert c.rr >= MIN_RR[RANGE_REVERSION]


def test_range_reversion_needs_a_ranging_regime():
    c, why = detect(RANGE_REVERSION, S.m15_range_structure(),
                    S.m5_range_rejection_long(), S.h1_uptrend())
    assert c is None and "ranging regime" in why


def test_range_long_requires_rsi_below_35():
    strict = SetupConfig(rsi_long_max=1.0)
    c, why = setups.detect(RANGE_REVERSION, S.m15_range_structure(),
                           S.m5_range_rejection_long(), classify(S.h1_true_range()), strict)
    assert c is None and "RSI" in why


def test_range_long_requires_the_bottom_zone():
    narrow = SetupConfig(range_zone_pct=0.0001)
    c, why = setups.detect(RANGE_REVERSION, S.m15_range_structure(),
                           S.m5_range_rejection_long(), classify(S.h1_true_range()), narrow)
    assert c is None and "outer" in why


def test_range_target_is_toward_the_far_side():
    c, _ = detect(RANGE_REVERSION, S.m15_range_structure(),
                  S.m5_range_rejection_long(), S.h1_true_range())
    assert c.target_price > c.entry_price
    assert c.stop_price < c.entry_price


# --- E / F: breakout + retest ----------------------------------------------
def test_e_breakout_retest_long_fires():
    c, why = detect(BREAKOUT_RETEST, S.m15_breakout_then_retest(),
                    S.m5_full_long_trigger(), S.h1_uptrend())
    assert c is not None, why
    assert c.direction == "buy"
    assert c.trigger_score == 20


def test_breakout_needs_an_expansion_candle():
    """SPEC: breakout candle range >= 1.20 * median of the previous 20."""
    strict = SetupConfig(breakout_expansion=99.0)
    c, why = setups.detect(BREAKOUT_RETEST, S.m15_breakout_then_retest(),
                           S.m5_full_long_trigger(), classify(S.h1_uptrend()), strict)
    assert c is None and "median" in why


def test_retest_must_come_back_close_to_the_level():
    strict = SetupConfig(retest_distance_atr=0.0001)
    c, why = setups.detect(BREAKOUT_RETEST, S.m15_breakout_then_retest(),
                           S.m5_full_long_trigger(), classify(S.h1_uptrend()), strict)
    assert c is None and "retest" in why


def test_no_breakout_means_no_candidate():
    quiet = S.from_closes([1.0950 + 0.00001 * (i % 3) for i in range(80)], wick=0.00005)
    c, why = detect(BREAKOUT_RETEST, quiet, S.m5_full_long_trigger(), S.h1_uptrend())
    assert c is None and "no qualifying breakout" in why


# --- shared behaviour -------------------------------------------------------
def test_rr_minimums_match_the_spec():
    assert MIN_RR[TREND_PULLBACK] == 2.0
    assert MIN_RR[BREAKOUT_RETEST] == 2.0
    assert MIN_RR[RANGE_REVERSION] == 1.5


def test_candidate_rr_math():
    c = SetupCandidate(TREND_PULLBACK, "buy", 1.1000, 1.0980, 1.1050, 1.0980)
    assert c.risk_distance == pytest.approx(0.0020)
    assert c.reward_distance == pytest.approx(0.0050)
    assert c.rr == pytest.approx(2.5)
    assert c.meets_min_rr()


def test_candidate_below_min_rr_is_rejected():
    c = SetupCandidate(TREND_PULLBACK, "buy", 1.1000, 1.0980, 1.1020, 1.0980)
    assert c.rr == pytest.approx(1.0)
    assert not c.meets_min_rr()


def test_zero_risk_candidate_does_not_divide_by_zero():
    c = SetupCandidate(TREND_PULLBACK, "buy", 1.1000, 1.1000, 1.1050, 1.1000)
    assert c.rr == 0.0 and not c.meets_min_rr()


def test_unknown_setup_name():
    c, why = detect("moon_phase", S.m15_pullback_to_ema(),
                    S.m5_full_long_trigger(), S.h1_uptrend())
    assert c is None and "unknown setup" in why


def test_short_history_is_handled_not_crashed():
    tiny = S.from_closes([1.10, 1.11, 1.12], wick=0.0001)
    for name in setups.ALL_SETUPS:
        c, why = detect(name, tiny, tiny, S.h1_uptrend())
        assert c is None and why


def test_config_round_trips_from_yaml():
    cfg = setups.from_config({"setup_params": {"trigger_bars": 5, "rsi_long_max": 25.0}})
    assert cfg.trigger_bars == 5
    assert cfg.rsi_long_max == 25.0
    assert cfg.pullback_structure_atr == 0.25     # spec default preserved


# --- gaps found by mutation testing ----------------------------------------
def test_pullback_closing_beyond_ema50_invalidates_the_setup():
    """SPEC: 'INVALIDATE SETUP IF M15 closes below EMA50'."""
    m15 = S.m15_pullback_to_ema()
    deep = m15.copy()
    # Drive the last close far below anything the EMA50 could be.
    deep["close"][-1] = float(deep["close"][-1]) - 0.05
    deep["low"][-1] = deep["close"][-1] - 0.0001
    c, why = detect(TREND_PULLBACK, deep, S.m5_full_long_trigger(), S.h1_uptrend())
    assert c is None
    assert "EMA50" in why or "pullback invalidated" in why


def test_trigger_needs_the_three_bar_break_not_just_a_higher_low():
    """
    A higher low with no 3-bar break is 'forming', not confirmed. The setup
    still reports a candidate; what matters is that its trigger cannot reach
    20, because the spec forbids executing below that.
    """
    c, why = detect(TREND_PULLBACK, S.m15_pullback_to_ema(),
                    S.m5_higher_low_no_break(), S.h1_uptrend())
    assert c is None or c.trigger_score < 20, why
    if c:
        assert "forming" in c.trigger_reason


def test_trigger_needs_the_higher_low_not_just_the_break():
    c, why = detect(TREND_PULLBACK, S.m15_pullback_to_ema(),
                    S.m5_break_no_higher_low(), S.h1_uptrend())
    assert c is None or c.trigger_score < 20, why
    if c:
        assert "higher low no" in c.trigger_reason


def test_failed_breakout_is_rejected():
    """SPEC: price closing back inside the old range makes the setup invalid."""
    c, why = detect(BREAKOUT_RETEST, S.m15_failed_breakout(),
                    S.m5_full_long_trigger(), S.h1_uptrend())
    assert c is None
    assert "failed breakout" in why or "retest" in why
