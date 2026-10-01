"""
Rule 1 — market regime, exactly as the strategy document states it:

    BULLISH  EMA20>EMA50 AND ADX>=20 AND (EMA20-EMA50) >= 0.25*ATR
    BEARISH  EMA20<EMA50 AND ADX>=20 AND (EMA50-EMA20) >= 0.25*ATR
    RANGE    ADX<18 AND |EMA20-EMA50| <= 0.20*ATR
    else     UNCLEAR -> HOLD
"""
import numpy as np
import scenarios as S

import regime as regime_mod
from regime import (RANGE, TREND_DOWN, TREND_UP, UNDEFINED, RegimeConfig,
                    classify, regime_score)


def test_uptrend_is_bullish_trend():
    r = classify(S.h1_uptrend())
    assert r.state == TREND_UP
    assert r.direction == "buy"
    assert r.is_trend and r.is_clear


def test_downtrend_is_bearish_trend():
    r = classify(S.h1_downtrend())
    assert r.state == TREND_DOWN
    assert r.direction == "sell"


def test_mean_reverting_noise_is_a_range():
    r = classify(S.h1_true_range())
    assert r.state == RANGE
    assert r.adx < 18
    assert r.direction is None


def test_adx_below_threshold_is_not_a_trend():
    """ADX 19 with wide EMAs satisfies neither rule — that gap IS the spec."""
    cfg = RegimeConfig(trend_adx_min=200.0)       # nothing can be a trend
    r = classify(S.h1_uptrend(), cfg)
    assert r.state == UNDEFINED
    assert "neither" in r.reason


def test_trend_needs_ema_separation_not_just_adx():
    """A high ADX with EMAs on top of each other is not a trend."""
    cfg = RegimeConfig(trend_separation_atr=99.0, range_adx_max=0.0)
    r = classify(S.h1_uptrend(), cfg)
    assert r.state == UNDEFINED


def test_range_needs_compression_not_just_low_adx():
    cfg = RegimeConfig(range_separation_atr=0.0, trend_adx_min=999.0)
    r = classify(S.h1_true_range(), cfg)
    assert r.state == UNDEFINED


def test_not_enough_history_is_unclear():
    short = S.h1_uptrend(n=20)
    r = classify(short)
    assert r.state == UNDEFINED
    assert "need" in r.reason


def test_reason_is_always_populated():
    for series in (S.h1_uptrend(), S.h1_downtrend(), S.h1_true_range(), S.h1_uptrend(n=10)):
        assert classify(series).reason


def test_atr_and_separation_are_exposed_for_scoring():
    r = classify(S.h1_uptrend())
    assert r.atr > 0 and not np.isnan(r.atr)
    assert r.separation_in_atr > 0
    assert r.close > 0


# --- category 1 of the signal score ----------------------------------------
def test_strong_trend_scores_25():
    r = classify(S.h1_uptrend())
    points, why = regime_score(r)
    assert points == 25
    assert "strong" in why


def test_moderate_trend_scores_20():
    r = classify(S.h1_uptrend())
    cfg = RegimeConfig(strong_adx=999.0)          # never "strong"
    points, why = regime_score(r, cfg)
    assert points == 20
    assert "moderate" in why


def test_unclear_regime_scores_zero():
    r = classify(S.h1_uptrend(n=10))
    assert regime_score(r)[0] == 0


def test_range_scores_within_band():
    r = classify(S.h1_true_range())
    points, _ = regime_score(r)
    assert points in (20, 25)


def test_spec_thresholds_are_the_defaults():
    cfg = regime_mod.from_config({})
    assert cfg.trend_adx_min == 20.0
    assert cfg.trend_separation_atr == 0.25
    assert cfg.range_adx_max == 18.0
    assert cfg.range_separation_atr == 0.20


def test_thresholds_come_from_config():
    cfg = regime_mod.from_config({"regime": {"trend_adx_min": 30.0, "range_adx_max": 12.0}})
    assert cfg.trend_adx_min == 30.0
    assert cfg.range_adx_max == 12.0


def test_bearish_trend_also_needs_ema_separation():
    """Both branches enforce the 0.25*ATR gap — not just the bullish one."""
    cfg = RegimeConfig(trend_separation_atr=99.0, range_adx_max=0.0)
    assert classify(S.h1_downtrend(), cfg).state == UNDEFINED


def test_bearish_trend_also_needs_the_adx_minimum():
    cfg = RegimeConfig(trend_adx_min=999.0, range_adx_max=0.0)
    assert classify(S.h1_downtrend(), cfg).state == UNDEFINED
