"""H1 regime classification: trending, ranging, or explicitly neither."""
import numpy as np

import regime as regime_mod
from conftest import ranging_prices, trending_prices
from regime import RANGE, TREND_DOWN, TREND_UP, UNDEFINED, RegimeConfig, classify


def candles(closes):
    closes = np.asarray(closes, dtype=float)
    a = np.zeros(len(closes), dtype=[("high", "<f8"), ("low", "<f8"), ("close", "<f8")])
    a["close"], a["high"], a["low"] = closes, closes + 0.4, closes - 0.4
    return a


def test_uptrend_classified_as_trend_up():
    r = classify(candles(trending_prices()))
    assert r.state == TREND_UP
    assert r.is_trend and not r.is_range
    assert r.direction == "buy"
    assert r.ema_fast > r.ema_slow


def test_downtrend_classified_as_trend_down():
    r = classify(candles(list(reversed(trending_prices()))))
    assert r.state == TREND_DOWN
    assert r.direction == "sell"
    assert r.ema_fast < r.ema_slow


def test_quiet_oscillation_classified_as_range():
    # Force a clearly-ranging read by widening the range threshold above the
    # ADX this series produces, rather than hunting for a magic price series.
    cfg = RegimeConfig(adx_trend_min=90.0, adx_range_max=80.0)
    r = classify(candles(ranging_prices()), cfg)
    assert r.state == RANGE
    assert r.is_range and not r.is_trend
    assert r.direction is None


def test_between_thresholds_is_undefined_not_a_guess():
    """ADX in the dead band is neither trend nor range, and must not trade."""
    # The synthetic trend reads ADX 100, so bracket it: too weak to be a trend
    # under a 101 threshold, too strong to be a range under a 50 one.
    cfg = RegimeConfig(adx_trend_min=101.0, adx_range_max=50.0)
    r = classify(candles(trending_prices()), cfg)
    assert r.state == UNDEFINED
    assert "between" in r.reason


def test_not_enough_history_is_undefined():
    r = classify(candles(np.arange(20) * 1.0))
    assert r.state == UNDEFINED
    assert "need" in r.reason


def test_reason_is_always_populated():
    for series in (trending_prices(), ranging_prices(), list(np.arange(10) * 1.0)):
        assert classify(candles(series)).reason


def test_atr_is_carried_through_for_stop_sizing():
    r = classify(candles(trending_prices()))
    assert r.atr > 0 and not np.isnan(r.atr)


def test_thresholds_come_from_config():
    cfg = regime_mod.from_config({"regime": {"adx_trend_min": 30.0, "adx_range_max": 15.0,
                                             "ema_fast": 8, "ema_slow": 21}})
    assert cfg.adx_trend_min == 30.0
    assert cfg.adx_range_max == 15.0
    assert cfg.ema_fast == 8 and cfg.ema_slow == 21


def test_config_defaults_are_the_conventional_wilder_levels():
    cfg = regime_mod.from_config({})
    assert (cfg.adx_trend_min, cfg.adx_range_max) == (25.0, 20.0)
