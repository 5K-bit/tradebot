"""
The vault log is the record of what the money actually did, so it must carry
the real fill — not the quote the bot sized against.
"""
from pathlib import Path

import fake_mt5
import trader


def open_a_trade(market, conn, risk, config, vault, prices, cross_bar):
    market.closed_prices = prices[:cross_bar]
    market.forming_price = prices[cross_bar - 1]
    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, {}, conn.account_info(), 0)
    return Path(vault).read_text()


def test_logs_the_fill_not_the_quote(market, conn, risk, config, vault, prices, cross_bar):
    log = open_a_trade(market, conn, risk, config, vault, prices, cross_bar)
    quoted = market.orders[0]["price"]
    filled = quoted + market.slippage
    assert f"fill={filled}" in log
    assert f"quoted {quoted}" in log
    assert filled != quoted, "fixture should exercise slippage"


def test_logs_the_broker_ticket_and_volume(market, conn, risk, config, vault, prices, cross_bar):
    log = open_a_trade(market, conn, risk, config, vault, prices, cross_bar)
    assert f"ticket={market.positions[0].ticket}" in log
    assert "lots=0.5" in log


def test_stop_loss_is_attached_to_the_order(market, conn, risk, config, vault, prices, cross_bar):
    open_a_trade(market, conn, risk, config, vault, prices, cross_bar)
    order = market.orders[0]
    assert order.get("sl", 0) > 0, "order went out with no stop-loss"
    assert order.get("tp", 0) > 0
    assert order["sl"] < order["price"] < order["tp"], "buy stop/target on the wrong side"


def test_prices_rounded_to_symbol_digits(market, conn, risk, config, vault, prices, cross_bar):
    open_a_trade(market, conn, risk, config, vault, prices, cross_bar)
    order = market.orders[0]
    assert order["sl"] == round(order["sl"], 5)
    assert order["tp"] == round(order["tp"], 5)


def test_zero_stop_price_is_not_silently_dropped(market, conn):
    """`if sl_price:` would drop a 0.0 stop; `is not None` keeps it."""
    market.closed_prices = [1.1000]
    conn.market_order("EURUSD", 0.1, "buy", sl_price=0.0, tp_price=0.0)
    assert "sl" in market.orders[0], "a 0.0 stop-loss was silently dropped"


def test_max_open_positions_is_logged_not_silent(market, conn, risk, config, vault,
                                                 prices, cross_bar):
    market.closed_prices = prices[:cross_bar]
    market.forming_price = prices[cross_bar - 1]
    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, {}, conn.account_info(), 3)   # already at the cap
    assert "max open positions reached" in Path(vault).read_text()
    assert market.orders == []
