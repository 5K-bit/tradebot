"""
The bot must only ever see and close positions it opened itself.

The original bug: positions_get() returns every position on the account, so a
manual trade blocked the bot's entries, counted toward max_open_positions, and
got closed whenever the strategy said "close". The MAGIC tag was written on
orders but never read back.
"""
from pathlib import Path

import pytest

import fake_mt5
import trader
from mt5_connector import MAGIC
from conftest import find_close_bar


def test_foreign_position_is_invisible(market, conn):
    market.add_foreign_position(symbol="EURUSD", ticket=777, magic=0)
    assert conn.open_positions() == []
    assert conn.open_positions(symbol="EURUSD") == []


def test_magic_none_still_sees_everything(market, conn):
    market.add_foreign_position(symbol="EURUSD", ticket=777, magic=0)
    assert len(conn.open_positions(magic=None)) == 1


def test_other_ea_position_is_invisible(market, conn):
    """A different EA's magic number is just as foreign as a manual trade."""
    market.add_foreign_position(symbol="EURUSD", ticket=888, magic=99999)
    assert conn.open_positions() == []


def test_close_signal_does_not_touch_a_manual_trade(market, conn, risk, config, vault, prices):
    market.add_foreign_position(symbol="EURUSD", ticket=777, magic=0)
    close_bar = find_close_bar(prices)

    market.closed_prices = prices[:close_bar]
    market.forming_price = prices[close_bar - 1]
    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, {}, conn.account_info(), 0)

    assert any(p.ticket == 777 for p in market.positions), "manual trade was closed"
    assert not any(o.get("position") == 777 for o in market.orders)


def test_close_position_refuses_a_foreign_ticket(market, conn):
    foreign = market.add_foreign_position(ticket=777, magic=0)
    with pytest.raises(RuntimeError, match="refusing to close"):
        conn.close_position(foreign)
    assert market.orders == [], "an order was sent for a foreign position"


def test_orders_carry_the_bot_magic(market, conn, risk, config, vault, prices, cross_bar):
    market.closed_prices = prices[:cross_bar]
    market.forming_price = prices[cross_bar - 1]
    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, {}, conn.account_info(), 0)
    assert market.orders[0]["magic"] == MAGIC


def test_bot_can_close_its_own_position(market, conn, risk, config, vault, prices):
    """The flip side: our own position must still close normally."""
    close_bar = find_close_bar(prices)
    market.closed_prices = prices[:close_bar]
    market.forming_price = prices[close_bar - 1]

    ours = market.add_foreign_position(symbol="EURUSD", ticket=555, magic=MAGIC)
    assert conn.open_positions() == [ours]

    open_count = trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                                   vault, {}, conn.account_info(), 1)
    assert not any(p.ticket == 555 for p in market.positions), "own position was not closed"
    assert open_count == 0
    assert "CLOSED EURUSD ticket=555" in Path(vault).read_text()


def test_manual_trade_does_not_block_entries(market, conn, risk, config, vault,
                                             prices, cross_bar):
    """A manual EURUSD trade must not make the bot think it is already in."""
    market.add_foreign_position(symbol="EURUSD", ticket=777, magic=0)
    market.closed_prices = prices[:cross_bar]
    market.forming_price = prices[cross_bar - 1]

    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, {}, conn.account_info(), 0)
    opens = [o for o in market.orders if "position" not in o]
    assert len(opens) == 1, "manual trade blocked the bot's entry"
