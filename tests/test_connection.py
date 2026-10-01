"""
A dropped MT5 terminal must not end the process with positions open and
nothing managing them.
"""
import pytest

import fake_mt5
from mt5_connector import MT5Connector


def test_healthy_link_needs_no_reinitialize(market, conn):
    before = market.init_calls
    assert conn.ensure_connected() is True
    assert market.init_calls == before, "re-initialized a healthy connection"


def test_detects_a_dropped_terminal(market, conn):
    market.terminal_up = False
    assert conn.is_connected() is False


def test_reconnects_when_the_terminal_returns(market, conn):
    market.terminal_up = False
    before = market.init_calls
    market.can_initialize = True

    assert conn.ensure_connected() is True
    assert market.init_calls > before
    assert conn.is_connected() is True


def test_gives_up_gracefully_when_terminal_stays_down(market, conn):
    market.terminal_up = False
    market.can_initialize = False
    assert conn.ensure_connected() is False          # returns, does not raise


def test_symbol_cache_cleared_on_reconnect(market, conn):
    conn.ensure_symbol("EURUSD")
    assert "EURUSD" in conn._selected

    market.terminal_up = False
    market.can_initialize = True
    conn.ensure_connected()
    assert conn._selected == set(), "stale Market Watch cache survived a reconnect"


def test_connect_raises_clearly_when_account_info_is_none(market):
    market.account_info_none = True
    with pytest.raises(RuntimeError, match="account_info"):
        MT5Connector().connect(quiet=True)


def test_connects_with_no_credentials_at_all(market, monkeypatch):
    """
    The normal way to run against a terminal you are already signed into.
    No password in the environment, nothing to lapse.
    """
    for var in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        monkeypatch.delenv(var, raising=False)
    c = MT5Connector()
    assert c.login is None
    c.connect(quiet=True)
    assert c.is_connected()


def test_lapsed_credentials_fall_back_to_the_terminal_session(market, capsys):
    """
    A demo account that has expired must not stop the bot running against a
    terminal that is signed in and connected.
    """
    market.init_rejects_credentials = True
    MT5Connector().connect()
    out = capsys.readouterr().out
    assert "credentials rejected" in out
    assert "already has open" in out


def test_raises_clearly_when_both_routes_fail(market):
    market.can_initialize = False
    with pytest.raises(RuntimeError, match="preflight.py"):
        MT5Connector().connect(quiet=True)


def test_error_mentions_both_attempts(market):
    market.can_initialize = False
    try:
        MT5Connector().connect(quiet=True)
    except RuntimeError as e:
        assert "with credentials" in str(e) and "without them" in str(e)


def test_connect_raises_on_failed_initialize(market):
    market.can_initialize = False
    with pytest.raises(RuntimeError, match="initialize"):
        MT5Connector().connect(quiet=True)


def test_symbol_selected_before_candles_are_requested(market, conn):
    market.closed_prices = [1.1 + i * 0.0001 for i in range(60)]
    market.selected.clear()
    conn.get_candles("EURUSD", fake_mt5.TIMEFRAME_M15, count=200)
    assert "EURUSD" in market.selected


def test_symbol_selection_is_cached(market, conn):
    market.closed_prices = [1.1 + i * 0.0001 for i in range(60)]
    market.selected.clear()
    for _ in range(5):
        conn.get_candles("EURUSD", fake_mt5.TIMEFRAME_M15, count=200)
    assert market.selected.count("EURUSD") == 1
