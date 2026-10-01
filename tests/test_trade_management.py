"""
Structural ATR stop, break-even at +1R, trailing from +1.5R, 2R target.
The property that matters most: a stop may only ever move in the trade's
favour, so a retrace can never widen the risk accepted at entry.
"""
import pytest

from trade_management import (ManagementConfig, next_stop, r_multiple,
                              structural_stop, target_from_r)

CFG = ManagementConfig()
ENTRY, SWING, ATR = 1.1000, 1.0980, 0.0010
STOP = 1.0975          # swing 1.0980 padded by 0.5 * ATR
R = ENTRY - STOP       # 0.0025


def test_structural_stop_pads_beyond_the_swing():
    assert structural_stop("buy", SWING, ATR, CFG) == pytest.approx(1.0975)
    assert structural_stop("sell", 1.1020, ATR, CFG) == pytest.approx(1.1025)


def test_pad_multiple_is_configurable():
    wide = ManagementConfig(atr_stop_multiple=2.0)
    assert structural_stop("buy", SWING, ATR, wide) == pytest.approx(1.0980 - 0.0020)


def test_target_is_two_r():
    assert target_from_r("buy", ENTRY, STOP, CFG) == pytest.approx(ENTRY + 2 * R)
    assert target_from_r("sell", ENTRY, 1.1025, CFG) == pytest.approx(ENTRY - 2 * 0.0025)


def test_r_multiple_measures_progress():
    assert r_multiple("buy", ENTRY, STOP, ENTRY) == pytest.approx(0.0)
    assert r_multiple("buy", ENTRY, STOP, ENTRY + R) == pytest.approx(1.0)
    assert r_multiple("buy", ENTRY, STOP, ENTRY - R) == pytest.approx(-1.0)
    assert r_multiple("sell", ENTRY, 1.1025, ENTRY - 0.0025) == pytest.approx(1.0)


def test_stop_unchanged_below_one_r():
    stop, reason = next_stop("buy", ENTRY, STOP, STOP, ENTRY + 0.4 * R, CFG)
    assert stop == STOP and reason is None


def test_break_even_at_one_r():
    stop, reason = next_stop("buy", ENTRY, STOP, STOP, ENTRY + 1.0 * R, CFG)
    assert stop == pytest.approx(ENTRY)
    assert "break-even" in reason


def test_break_even_offset_covers_costs():
    cfg = ManagementConfig(breakeven_offset_r=0.1)
    stop, _ = next_stop("buy", ENTRY, STOP, STOP, ENTRY + 1.0 * R, cfg)
    assert stop == pytest.approx(ENTRY + 0.1 * R)


def test_trailing_starts_at_one_and_a_half_r():
    below, reason_below = next_stop("buy", ENTRY, STOP, ENTRY, ENTRY + 1.49 * R, CFG)
    assert reason_below is None                     # still at break-even
    above, reason_above = next_stop("buy", ENTRY, STOP, ENTRY, ENTRY + 1.6 * R, CFG)
    assert "trailing" in reason_above
    assert above > below


def test_trail_sits_one_r_behind_price():
    price = ENTRY + 2.4 * R
    stop, _ = next_stop("buy", ENTRY, STOP, ENTRY, price, CFG)
    assert stop == pytest.approx(price - CFG.trail_distance_r * R)


def test_stop_never_loosens_on_a_retrace():
    trailed, _ = next_stop("buy", ENTRY, STOP, ENTRY, ENTRY + 2.4 * R, CFG)
    after, reason = next_stop("buy", ENTRY, STOP, trailed, ENTRY + 1.6 * R, CFG)
    assert after == trailed, "a retrace widened the stop"
    assert reason is None


def test_short_side_mirrors_exactly():
    entry, stop = 1.1000, 1.1025
    r = stop - entry
    be, reason = next_stop("sell", entry, stop, stop, entry - 1.0 * r, CFG)
    assert be == pytest.approx(entry) and "break-even" in reason

    trailed, _ = next_stop("sell", entry, stop, entry, entry - 2.4 * r, CFG)
    assert trailed == pytest.approx(entry - 2.4 * r + CFG.trail_distance_r * r)
    assert trailed < stop

    after, _ = next_stop("sell", entry, stop, trailed, entry - 1.6 * r, CFG)
    assert after == trailed


def test_zero_risk_distance_is_a_noop():
    stop, reason = next_stop("buy", ENTRY, ENTRY, ENTRY, ENTRY + 0.01, CFG)
    assert stop == ENTRY and reason is None
