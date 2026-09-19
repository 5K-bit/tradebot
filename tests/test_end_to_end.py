"""
Drives the real main() loop against the fake terminal, including a mid-run
terminal outage, and checks what ends up in the vault log and state file.
"""
import json

import pytest

import trader
from conftest import crossing_prices


def run_main(tmp_path, monkeypatch, market, cycles=40, symbols=("EURUSD", "GBPUSD"),
             kill_terminal_at=8, revive_terminal_at=10, start_bar=64):
    cfg = {
        "symbols": list(symbols),
        "timeframe": "M15",
        "poll_seconds": 30,
        "pip": {"size": 0.0001, "value_per_lot": 10},
        "stops": {"stop_loss_pips": 20, "take_profit_pips": 40},
        "risk": {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03,
                 "max_open_positions": 3, "max_lot_size": 1.0,
                 "broker_utc_offset_hours": 0},
        "state": {"path": str(tmp_path / "state.json")},
        "vault": {"log_path": str(tmp_path / "trades.md")},
    }
    monkeypatch.setattr(trader, "load_config", lambda path="config.yaml": cfg)

    prices = crossing_prices()
    market.closed_prices = prices[:start_bar]
    market.forming_price = prices[start_bar]

    n = {"cycles": 0}

    def fake_sleep(_seconds):
        n["cycles"] += 1
        bars = min(start_bar + n["cycles"] // 3, len(prices) - 1)
        market.closed_prices = prices[:bars]
        market.forming_price = prices[bars] + 0.00003 * (n["cycles"] % 3)
        if kill_terminal_at and n["cycles"] == kill_terminal_at:
            market.terminal_up = False
            market.can_initialize = False
        if revive_terminal_at and n["cycles"] == revive_terminal_at:
            market.can_initialize = True
        if n["cycles"] >= cycles:
            raise KeyboardInterrupt

    monkeypatch.setattr(trader.time, "sleep", fake_sleep)
    trader.main()

    return {
        "cycles": n["cycles"],
        "log": (tmp_path / "trades.md").read_text(),
        "state": json.loads((tmp_path / "state.json").read_text()),
    }


def test_full_run_survives_a_terminal_outage(tmp_path, monkeypatch, market):
    out = run_main(tmp_path, monkeypatch, market)

    assert out["cycles"] == 40, "loop exited early"
    assert "Lathe trader started" in out["log"]
    assert "Ctrl+C" in out["log"], "did not shut down cleanly"
    assert "ERROR" not in out["log"], out["log"]
    assert "CYCLE ERROR" not in out["log"], out["log"]


def test_full_run_opens_one_position_per_symbol(tmp_path, monkeypatch, market):
    out = run_main(tmp_path, monkeypatch, market)

    opens = [o for o in market.orders if "position" not in o]
    assert len(opens) == 2, f"expected one entry per symbol, got {len(opens)}"
    assert {o["symbol"] for o in opens} == {"EURUSD", "GBPUSD"}
    assert out["log"].count("OPENED") == 2
    # 40 polls, 2 orders: the churn is gone.
    assert len(market.orders) == 2


def test_full_run_persists_state(tmp_path, monkeypatch, market):
    out = run_main(tmp_path, monkeypatch, market)

    assert set(out["state"]["last_bar_time"]) == {"EURUSD", "GBPUSD"}
    assert out["state"]["halted"] is False
    assert out["state"]["equity_at_day_start"] == 10_000.0
    assert out["state"]["trading_day"]


def test_kill_switch_halts_a_live_run(tmp_path, monkeypatch, market):
    """Drop equity 4% mid-run; no further orders should be sent."""
    prices = crossing_prices()
    market.closed_prices = prices[:52]
    market.forming_price = prices[52]

    cfg = {
        "symbols": ["EURUSD"], "timeframe": "M15", "poll_seconds": 30,
        "pip": {"size": 0.0001, "value_per_lot": 10},
        "stops": {"stop_loss_pips": 20, "take_profit_pips": 40},
        "risk": {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03,
                 "max_open_positions": 3, "max_lot_size": 1.0,
                 "broker_utc_offset_hours": 0},
        "state": {"path": str(tmp_path / "state.json")},
        "vault": {"log_path": str(tmp_path / "trades.md")},
    }
    monkeypatch.setattr(trader, "load_config", lambda path="config.yaml": cfg)

    n = {"c": 0}

    def fake_sleep(_s):
        n["c"] += 1
        if n["c"] == 2:
            market.equity = 9_600.0                 # -4%: past the 3% limit
        bars = min(52 + n["c"], len(prices) - 1)
        market.closed_prices = prices[:bars]
        market.forming_price = prices[bars]
        if n["c"] >= 30:
            raise KeyboardInterrupt

    monkeypatch.setattr(trader.time, "sleep", fake_sleep)
    trader.main()

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["halted"] is True
    assert market.orders == [], "traded after the kill switch tripped"


def test_state_file_is_not_rewritten_when_nothing_changes(tmp_path, monkeypatch, market):
    """The cursor is only persisted when it actually moves."""
    writes = {"n": 0}
    from state import JsonState
    original = JsonState._save

    def counting_save(self):
        writes["n"] += 1
        return original(self)

    monkeypatch.setattr(JsonState, "_save", counting_save)
    run_main(tmp_path, monkeypatch, market, cycles=30, symbols=("EURUSD",))

    # ~10 candle closes across 30 cycles, plus the day baseline — far fewer
    # than one write per poll.
    assert writes["n"] < 20, f"state written {writes['n']} times in 30 cycles"


def test_main_validates_config_before_trading(tmp_path, monkeypatch, market):
    """A bad config must stop the bot at startup, not on the first live signal."""
    cfg = {
        "symbols": ["EURUSD", "USDJPY"],          # USDJPY with no pip override
        "timeframe": "M15", "poll_seconds": 30,
        "pip": {"size": 0.0001, "value_per_lot": 10},
        "stops": {"stop_loss_pips": 20, "take_profit_pips": 40},
        "risk": {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03,
                 "max_open_positions": 3, "max_lot_size": 1.0,
                 "broker_utc_offset_hours": 0},
        "state": {"path": str(tmp_path / "state.json")},
        "vault": {"log_path": str(tmp_path / "trades.md")},
    }
    monkeypatch.setattr(trader, "load_config", lambda path="config.yaml": cfg)
    monkeypatch.setattr(trader.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(ValueError, match="USDJPY"):
        trader.main()

    assert market.orders == [], "traded despite an invalid config"
    assert market.init_calls == 0, "connected to the terminal before validating config"
