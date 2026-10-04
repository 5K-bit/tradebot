"""
PAPER / BACKTEST must not reach the broker, and LIVE must be opted into twice.

The guard lives on the connector rather than in the caller, so there is exactly
one place an order can escape and one place to test.
"""
import copy

import pytest
import scenarios as S
from datetime import datetime

import config_schema as cs
import trader
from conftest import IN_SESSION_UTC, feed_candles, read_log
from state import JsonState

NOW = datetime.fromisoformat(IN_SESSION_UTC)


def bot_in_mode(config, conn, tmp_path, mode, enabled=False):
    cfg = copy.deepcopy(config)
    cfg["safety"] = {"default_mode": mode, "live_trading_enabled": enabled}
    return trader.Lathe(cfg, conn, JsonState(str(tmp_path / "s.json")))


@pytest.mark.parametrize("mode", ["PAPER", "BACKTEST"])
def test_non_live_modes_send_nothing(config, conn, market, tmp_path, mode):
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    bot = bot_in_mode(config, conn, tmp_path, mode)
    assert bot.conn.dry_run is True

    bot.evaluate("EURUSD", "2026-01-14", conn.account_info(), NOW)
    assert market.orders == [], f"{mode} mode sent an order to the broker"
    # ...but the decision was still made and recorded.
    assert "SIGNAL" in read_log(bot.vault)


def test_live_without_the_enable_flag_sends_nothing(config, conn, market, tmp_path):
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    bot = bot_in_mode(config, conn, tmp_path, "LIVE", enabled=False)
    assert bot.mode == cs.MODE_PAPER
    assert bot.conn.dry_run is True

    bot.evaluate("EURUSD", "2026-01-14", conn.account_info(), NOW)
    assert market.orders == [], "LIVE without the second switch reached the broker"


def test_live_with_the_enable_flag_does_send(config, conn, market, tmp_path):
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    bot = bot_in_mode(config, conn, tmp_path, "LIVE", enabled=True)
    assert bot.conn.dry_run is False

    bot.evaluate("EURUSD", "2026-01-14", conn.account_info(), NOW)
    assert [o for o in market.orders if o.get("action") == 5], "LIVE sent nothing"


def test_paper_result_looks_like_a_real_one(conn, market):
    """The rest of the engine must not need to know which mode it is in."""
    market.closed_prices = [1.1000]
    conn.dry_run = True
    result = conn.pending_stop_order("EURUSD", 0.5, "buy", 1.1050, 1.1030, 1.1090)
    assert result.order > 0
    assert result.volume == 0.5
    assert result.price == 1.1050
    assert market.orders == []


def test_paper_blocks_every_order_sending_call(conn, market):
    market.closed_prices = [1.1000]
    conn.dry_run = True
    pos = market.add_foreign_position(symbol="EURUSD", ticket=555, magic=20260917)

    conn.market_order("EURUSD", 0.1, "buy", sl_price=1.09, tp_price=1.12)
    conn.pending_stop_order("EURUSD", 0.1, "buy", 1.105, 1.103, 1.109)
    conn.modify_stop(pos, 1.0950)
    conn.close_position(pos)
    assert market.orders == [], "a call path bypassed the dry-run guard"


def test_mode_is_recorded_on_the_signal(config, conn, market, tmp_path):
    import json
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    bot = bot_in_mode(config, conn, tmp_path, "PAPER")
    bot.evaluate("EURUSD", "2026-01-14", conn.account_info(), NOW)
    line = [x for x in read_log(bot.vault).splitlines() if "SIGNAL {" in x][0]
    assert json.loads(line.split("SIGNAL ", 1)[1])["mode"] == "PAPER"


def test_filling_mode_and_slippage_reach_the_connector(config, conn, tmp_path):
    """preflight reports what the broker accepts; the bot must be able to use it."""
    cfg = copy.deepcopy(config)
    cfg["execution"] = {"filling_mode": "FOK", "max_slippage_points": 35}
    bot = trader.Lathe(cfg, conn, JsonState(str(tmp_path / "s.json")))
    assert bot.conn.filling_mode == "FOK"
    assert bot.conn.deviation == 35


def test_auto_filling_follows_what_the_symbol_allows(conn, market):
    import fake_mt5
    market.closed_prices = [1.1000]
    conn.filling_mode = "auto"
    for mask, expected in ((2, fake_mt5.ORDER_FILLING_IOC),
                           (1, fake_mt5.ORDER_FILLING_FOK),
                           (0, fake_mt5.ORDER_FILLING_RETURN)):
        market.filling_mask = mask
        market.orders.clear()
        conn._selected.clear()
        conn.market_order("EURUSD", 0.1, "buy", sl_price=1.09, tp_price=1.12)
        assert market.orders[0]["type_filling"] == expected


# --- the account the orders actually reach -----------------------------------
# The MetaTrader5 package attaches to whichever account the terminal has open,
# so the account is a property of the terminal, not of this config. These cover
# the guard that stops a terminal slip putting real money behind the orders.

def norm_for(config, mode, enabled, require_demo=None):
    cfg = copy.deepcopy(config)
    cfg["safety"] = {"default_mode": mode, "live_trading_enabled": enabled}
    if require_demo is not None:
        cfg["safety"]["require_demo_account"] = require_demo
    return cs.normalise(cfg)


def test_live_mode_refuses_a_real_money_account(config):
    norm = norm_for(config, "LIVE", True)
    assert norm.require_demo is True, "the guard should default to on"
    with pytest.raises(cs.ConfigError) as e:
        cs.check_account(norm, is_live_account=True)
    assert "REAL" in str(e.value)


def test_live_mode_accepts_a_demo_account(config):
    cs.check_account(norm_for(config, "LIVE", True), is_live_account=False)


def test_the_guard_can_be_turned_off_on_purpose(config):
    norm = norm_for(config, "LIVE", True, require_demo=False)
    cs.check_account(norm, is_live_account=True)


@pytest.mark.parametrize("mode,enabled", [
    ("PAPER", False),
    ("BACKTEST", False),
    ("LIVE", False),      # LIVE without the second switch degrades to PAPER
])
def test_a_mode_that_sends_nothing_does_not_care_which_account(config, mode, enabled):
    """No order leaves, so the account is irrelevant and must not block startup."""
    cs.check_account(norm_for(config, mode, enabled), is_live_account=True)


@pytest.mark.parametrize("trade_mode,expected", [
    (0, False),    # DEMO
    (1, False),    # CONTEST
    (2, True),     # REAL
])
def test_the_connector_reports_the_account_type(conn, market, trade_mode, expected):
    market.trade_mode = trade_mode
    assert conn.is_live_account() is expected


def test_an_unreadable_account_is_treated_as_live(conn, market):
    """
    Fails closed. The mistake that costs money is calling a real account a
    demo, so anything unreadable counts as real.
    """
    market.account_info_none = True
    assert conn.is_live_account() is True


def test_an_account_with_no_trade_mode_is_treated_as_live(conn):
    class Bare:
        login = 1
    assert conn._is_live(Bare()) is True
