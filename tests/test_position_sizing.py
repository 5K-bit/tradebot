"""
Lot sizing must respect the broker's volume rules and never exceed the
configured risk. The original code rounded to 2dp, which could round UP past
risk_per_trade_pct and ignored volume_min/volume_step entirely.
"""
import pytest

from risk_manager import RiskConfig, RiskManager

RM = RiskManager(RiskConfig(0.01, 0.03, 3, 1.0, 0.0))


def size(equity, stop=1.0980, entry=1.1000, **kw):
    kw.setdefault("volume_min", 0.01)
    kw.setdefault("volume_step", 0.01)
    kw.setdefault("volume_max", 100.0)
    return RM.position_size(equity, entry, stop, 10, 0.0001, **kw)


def test_basic_one_percent_over_twenty_pips():
    # 1% of 10,000 = $100 risk; 20 pips * $10/pip/lot = $200 per lot -> 0.50 lots
    assert size(10_000) == pytest.approx(0.50)


def test_floors_to_step_never_rounds_up():
    # 1% of 7,777 = $77.77 -> 0.3888 lots. Rounding gives 0.39 (over-risk); floor gives 0.38.
    assert size(7_777) == pytest.approx(0.38)


def test_result_has_no_float_dust():
    lots = size(7_777)
    assert lots == round(lots, 2)
    assert repr(lots) == "0.38"


def test_respects_a_coarse_volume_step():
    assert size(10_000, volume_step=0.10, volume_min=0.10) == pytest.approx(0.50)
    assert size(7_777, volume_step=0.10, volume_min=0.10) == pytest.approx(0.30)


def test_max_lot_size_ceiling_applies():
    assert size(1_000_000) == pytest.approx(1.0)


def test_broker_volume_max_applies():
    assert size(1_000_000, volume_max=0.30) == pytest.approx(0.30)


def test_below_broker_minimum_raises():
    with pytest.raises(ValueError, match="below the broker minimum"):
        size(50)


def test_below_minimum_when_min_exceeds_step():
    """Some brokers set volume_min above volume_step (e.g. min 0.10, step 0.01).

    Flooring alone does not catch that case — 0.05 lots is a whole number of
    steps but still untradeable — so the minimum must be checked separately.
    """
    with pytest.raises(ValueError, match="below the broker minimum 0.1"):
        size(1_000, volume_min=0.10, volume_step=0.01)


def test_size_at_exactly_the_minimum_is_allowed():
    assert size(2_000, volume_min=0.10, volume_step=0.01) == pytest.approx(0.10)


def test_no_stop_loss_still_refused():
    with pytest.raises(ValueError, match="stop-loss is required"):
        RM.position_size(10_000, 1.1000, 1.1000, 10, 0.0001)


def test_zero_volume_step_raises():
    with pytest.raises(ValueError, match="volume_step"):
        size(10_000, volume_step=0)


def test_sizing_never_exceeds_configured_risk():
    """Property check: across many equities, realised risk never exceeds 1%."""
    for equity in range(500, 200_000, 971):
        try:
            lots = size(equity)
        except ValueError:
            continue                       # too small to trade at all — fine
        risk_dollars = lots * 20 * 10      # lots * stop pips * value per pip per lot
        assert risk_dollars <= equity * 0.01 + 1e-9, f"over-risked at equity={equity}"


def test_max_open_positions_cap():
    assert RM.can_open_new_position(2) is True
    assert RM.can_open_new_position(3) is False
