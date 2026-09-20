"""Indicator math. Everything downstream reads these values, so they are
checked against hand-computed numbers rather than against themselves."""
import numpy as np
import pytest

from indicators import adx, atr, ema, rma, swing_high, swing_low


def candles(high, low, close):
    a = np.zeros(len(high), dtype=[("high", "<f8"), ("low", "<f8"), ("close", "<f8")])
    a["high"], a["low"], a["close"] = high, low, close
    return a


def test_ema_matches_hand_calculation():
    # period 3 -> seed = SMA(1,2,3) = 2, alpha = 2/(3+1) = 0.5
    e = ema([1, 2, 3, 4, 5], 3)
    assert np.isnan(e[1])
    assert e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(0.5 * 4 + 0.5 * 2)
    assert e[4] == pytest.approx(0.5 * 5 + 0.5 * 3)


def test_ema_is_nan_before_warmup_not_truncated():
    """Full-length output keeps indicator values aligned to their candles."""
    e = ema([1, 2, 3, 4, 5], 3)
    assert len(e) == 5
    assert np.isnan(e[:2]).all()


def test_rma_is_wilder_not_simple_mean():
    r = rma([1, 2, 3, 4, 5], 3)
    assert r[2] == pytest.approx(2.0)
    assert r[3] == pytest.approx((2 * 2 + 4) / 3)
    # A simple rolling mean would give 4.0 here; Wilder gives less.
    assert r[4] == pytest.approx((r[3] * 2 + 5) / 3)
    assert r[4] < 4.0


def test_atr_seeds_from_first_bar_range():
    c = candles([10, 11, 12], [9, 10, 11], [9.5, 10.5, 11.5])
    a = atr(c, 2)
    assert a[1] == pytest.approx(1.25)
    assert a[2] == pytest.approx((1.25 * 1 + 1.5) / 2)


def test_short_input_returns_all_nan():
    assert np.isnan(ema([1, 2], 5)).all()
    assert np.isnan(rma([1, 2], 5)).all()


def test_adx_is_high_in_a_trend_and_low_in_chop():
    n = 80
    up = candles(np.arange(n) + 2.0, np.arange(n) * 1.0, np.arange(n) + 1.0)
    a, plus, minus = adx(up, 14)
    assert a[-1] > 25
    assert plus[-1] > minus[-1]

    rng = np.random.default_rng(7)
    base = 100 + rng.normal(0, 0.3, n).cumsum() * 0.05
    chop = candles(base + 0.5, base - 0.5, base)
    a2, _, _ = adx(chop, 14)
    assert a2[-1] < 25


def test_adx_direction_flips_on_a_downtrend():
    n = 80
    down = candles(np.arange(n, 0, -1) + 2.0, np.arange(n, 0, -1) * 1.0,
                   np.arange(n, 0, -1) + 1.0)
    _, plus, minus = adx(down, 14)
    assert minus[-1] > plus[-1]


def test_adx_stays_within_bounds_and_warms_up():
    n = 80
    up = candles(np.arange(n) + 2.0, np.arange(n) * 1.0, np.arange(n) + 1.0)
    a, _, _ = adx(up, 14)
    finite = a[~np.isnan(a)]
    assert ((finite >= 0) & (finite <= 100)).all()
    assert np.isnan(a[10]), "ADX must not report a value before it is defined"


def test_adx_refuses_short_input():
    c = candles([1, 2, 3], [0, 1, 2], [1, 2, 3])
    a, p, m = adx(c, 14)
    assert np.isnan(a).all() and np.isnan(p).all() and np.isnan(m).all()


def test_swing_high_and_low():
    peak = candles([1, 2, 5, 2, 1, 2, 3], [1, 2, 5, 2, 1, 2, 3], [1, 2, 5, 2, 1, 2, 3])
    assert swing_high(peak, lookback=2) == (2, 5.0)

    trough = candles([5, 4, 1, 4, 5, 4, 3], [5, 4, 1, 4, 5, 4, 3], [5, 4, 1, 4, 5, 4, 3])
    assert swing_low(trough, lookback=2) == (2, 1.0)


def test_swing_needs_confirmation_bars_on_the_right():
    """A high at the very last bar is not yet a swing — it could still be exceeded."""
    rising = candles([1, 2, 3, 4, 9], [1, 2, 3, 4, 9], [1, 2, 3, 4, 9])
    assert swing_high(rising, lookback=2) is None


def test_swing_returns_none_when_no_structure():
    flat = candles([1] * 8, [1] * 8, [1] * 8)
    assert swing_high(flat, lookback=2) is None
    assert swing_low(flat, lookback=2) is None
