"""
The full decision pipeline against real setup detectors:

    session -> hard gates -> H1 regime -> M15 setup -> M5 trigger
            -> stop validation -> spread rule -> score >= 80 -> order

Entries are pending stop orders, not market orders: the spec enters beyond the
trigger candle and cancels if price does not reach it within 3 M5 candles.
"""
from datetime import datetime, timedelta

import scenarios as S

import setups
from conftest import IN_SESSION_UTC, feed_candles, read_log

NOW = datetime.fromisoformat(IN_SESSION_UTC)
SESSION = "2026-01-14"


def evaluate(lathe, conn, symbol="EURUSD", now=NOW, session=SESSION):
    return lathe.evaluate(symbol, session, conn.account_info(), now)


def pending(market):
    return [o for o in market.orders if o.get("action") == 5]


# --- the happy path ---------------------------------------------------------
def test_trend_pullback_produces_a_pending_entry(lathe, conn, trending_setup, vault):
    evaluate(lathe, conn)
    orders = pending(trending_setup)
    assert len(orders) == 1, read_log(vault)
    o = orders[0]
    assert o["type"] == 4                       # BUY_STOP
    assert o["sl"] < o["price"] < o["tp"]
    assert o["expiration"] > NOW.timestamp()


def test_signal_object_is_logged(lathe, conn, trending_setup, vault):
    import json
    evaluate(lathe, conn)
    line = [x for x in read_log(vault).splitlines() if "SIGNAL {" in x]
    assert line, read_log(vault)
    obj = json.loads(line[0].split("SIGNAL ", 1)[1])
    assert obj["symbol"] == "EURUSD"
    assert obj["signal"] == "BUY"
    assert obj["scores"]["trigger"] == 20
    assert sum(obj["scores"].values()) == obj["score_total"] >= 80
    assert obj["hard_gates_passed"] is True
    assert obj["reason"]


def test_position_is_sized_from_risk(lathe, conn, trending_setup):
    evaluate(lathe, conn)
    assert pending(trending_setup)[0]["volume"] > 0


# --- gates that must block --------------------------------------------------
def test_outside_the_session_nothing_is_evaluated(lathe, conn, trending_setup, vault):
    lathe.evaluate("EURUSD", None, conn.account_info(), NOW)
    assert pending(trending_setup) == []
    assert "outside the trading session" in read_log(vault)


def test_unclear_regime_holds(lathe, conn, market, vault):
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    lathe.regime_cfg.trend_adx_min = 999.0      # nothing qualifies as a trend
    lathe.regime_cfg.range_adx_max = 0.0        # ...or as a range
    evaluate(lathe, conn)
    assert pending(market) == []
    assert "neither the trend nor the range rule" in read_log(vault)


def test_wide_spread_holds(lathe, conn, trending_setup, vault):
    trending_setup.spread = 0.0020               # far beyond any limit
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    assert "spread" in read_log(vault)


def test_one_concurrent_trade(lathe, conn, trending_setup, vault):
    evaluate(lathe, conn)
    assert len(pending(trending_setup)) == 1
    lathe.bar_cursor.clear()
    evaluate(lathe, conn, symbol="USDJPY")
    assert "concurrent" in read_log(vault)
    assert len(pending(trending_setup)) == 1


def test_session_trade_limit(lathe, conn, trending_setup, vault):
    for _ in range(3):
        lathe.protection.record_trade_opened(SESSION)
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    assert "session trade limit reached (3/3)" in read_log(vault)


def test_consecutive_loss_cooldown_blocks(lathe, conn, trending_setup, vault):
    lathe.protection.record_trade_closed(-10.0, SESSION)
    lathe.protection.record_trade_closed(-10.0, SESSION)
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    assert "consecutive losses" in read_log(vault)


def test_account_kill_switch_blocks(lathe, conn, trending_setup, vault):
    lathe.protection.observe_equity(10_000.0)
    lathe.protection.observe_equity(8_500.0)     # -15%
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    assert "account drawdown" in read_log(vault)


def test_stop_outside_atr_bounds_blocks(lathe, conn, trending_setup, vault):
    lathe.protection.cfg.stop_max_atr_multiple = 0.0001
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    assert "ATR_M15" in read_log(vault)


def test_score_threshold_blocks(lathe, conn, trending_setup, vault):
    lathe.min_score = 999
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    assert "below the" in read_log(vault)


# --- bar gating -------------------------------------------------------------
def test_one_decision_per_setup_bar(lathe, conn, trending_setup, vault):
    for _ in range(10):
        evaluate(lathe, conn)
    assert len(pending(trending_setup)) == 1


def test_no_trade_bars_are_also_gated(lathe, conn, market, vault):
    """Ten polls on a bar that produces no candidate must log one decision."""
    feed_candles(market, h1=S.h1_true_range(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    for _ in range(10):
        evaluate(lathe, conn)
    assert read_log(vault).count("HOLD") == 1


# --- pending order lifecycle -----------------------------------------------
def test_expired_entry_order_is_cancelled(lathe, conn, trending_setup, vault):
    evaluate(lathe, conn)
    assert len(conn.pending_orders()) == 1
    lathe.expire_stale_orders(NOW + timedelta(hours=2))
    assert conn.pending_orders() == []
    assert "ORDER-EXPIRED" in read_log(vault)


def test_live_order_is_not_cancelled_early(lathe, conn, trending_setup):
    evaluate(lathe, conn)
    lathe.expire_stale_orders(NOW + timedelta(seconds=30))
    assert len(conn.pending_orders()) == 1


def test_working_order_counts_against_concurrency(lathe, conn, trending_setup, vault):
    evaluate(lathe, conn)
    lathe.bar_cursor.clear()
    evaluate(lathe, conn, symbol="USDJPY")
    assert "concurrent" in read_log(vault)


# --- scoping ----------------------------------------------------------------
def test_foreign_positions_are_invisible(lathe, conn, trending_setup):
    trending_setup.add_foreign_position(symbol="EURUSD", ticket=777, magic=0)
    evaluate(lathe, conn)
    assert len(pending(trending_setup)) == 1
    assert any(p.ticket == 777 for p in trending_setup.positions)


def test_trade_metadata_recorded(lathe, conn, trending_setup):
    evaluate(lathe, conn)
    ticket = str(pending(trending_setup) and conn.pending_orders()[0].ticket)
    assert ticket in lathe.trade_meta
    assert lathe.trade_meta[ticket]["setup"] == setups.TREND_PULLBACK
    assert lathe.trade_meta[ticket]["session_key"] == SESSION


def test_spread_rule_blocks_even_within_the_hard_maximum(lathe, conn, trending_setup, vault):
    """
    The spec's spread rule is separate from the hard pip ceiling: a spread well
    inside max_spread_pips still fails if it is above 1.5x the recent median,
    or above 10% of ATR_M5.
    """
    base = trending_setup.spread
    for _ in range(30):
        lathe.protection.observe_spread(base)        # establish a tight median
    trending_setup.spread = base * 3                 # 3x median, still tiny in pips
    evaluate(lathe, conn)
    assert pending(trending_setup) == []
    log = read_log(vault)
    assert "median" in log or "ATR_M5" in log


def test_spread_at_the_median_is_allowed(lathe, conn, trending_setup):
    for _ in range(30):
        lathe.protection.observe_spread(trending_setup.spread)
    evaluate(lathe, conn)
    assert len(pending(trending_setup)) == 1
