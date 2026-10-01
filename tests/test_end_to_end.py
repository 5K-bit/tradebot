"""
Drives the real main() loop against the fake terminal: a session opening and
closing, a terminal outage, the kill switches, and what ends up in the trade
log and state file.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta

import pytest

import scenarios as S

import trader
from conftest import IN_SESSION_UTC, feed_candles

START = datetime.fromisoformat(IN_SESSION_UTC)


def install_setup(monkeypatch, score=None):
    """The real detectors are live now — nothing to install."""
    return None


def run_main(config, monkeypatch, market, cycles=12, clock_start=START,
             minutes_per_cycle=20, on_cycle=None):
    """Run main() for N cycles, advancing a fake clock, then Ctrl+C out."""
    monkeypatch.setattr(trader, "load_config", lambda path="config.yaml": config)
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())

    n = {"c": 0}
    clock = {"now": clock_start}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(trader, "datetime", FakeDateTime)

    def fake_sleep(_seconds):
        n["c"] += 1
        clock["now"] = clock["now"] + timedelta(minutes=minutes_per_cycle)
        market.now_ts = clock["now"].timestamp()
        if on_cycle:
            on_cycle(n["c"], market)
        if n["c"] >= cycles:
            raise KeyboardInterrupt

    monkeypatch.setattr(trader.time, "sleep", fake_sleep)
    trader.main()

    from conftest import read_log
    return {
        "cycles": n["c"],
        "log": read_log(config["vault"]["log_path"]),
        "state": json.loads(Path(config["state"]["path"]).read_text()),
    }


def test_runs_clean(config, monkeypatch, market):
    out = run_main(config, monkeypatch, market)
    assert out["cycles"] == 12
    assert "Setups enabled" in out["log"]
    assert "ERROR" not in out["log"]
    assert "CYCLE ERROR" not in out["log"]


def test_logs_session_open_and_close(config, monkeypatch, market):
    out = run_main(config, monkeypatch, market, cycles=30, minutes_per_cycle=20)
    assert "Session OPEN" in out["log"]
    assert "Session CLOSED" in out["log"]


def test_places_a_pending_entry_and_logs_a_signal(config, monkeypatch, market):
    out = run_main(config, monkeypatch, market, cycles=6, minutes_per_cycle=16)
    pending = [o for o in market.orders if o.get("action") == 5]
    assert pending, out["log"]
    assert pending[0]["magic"] == 20260917
    assert "SIGNAL {" in out["log"]
    assert "BUY EURUSD" in out["log"]


def test_survives_a_terminal_outage(config, monkeypatch, market):
    def outage(cycle, m):
        if cycle == 3:
            m.terminal_up = False
            m.can_initialize = False
        if cycle == 5:
            m.can_initialize = True
    out = run_main(config, monkeypatch, market, cycles=10, on_cycle=outage)
    assert out["cycles"] == 10
    assert "CYCLE ERROR" not in out["log"]


def test_daily_stop_halts_trading(config, monkeypatch, market):

    def crash(cycle, m):
        if cycle == 2:
            m.equity = 9_400.0          # -6%, past the 5% daily stop
    out = run_main(config, monkeypatch, market, cycles=8, on_cycle=crash)
    assert out["state"]["halted"] is True


def test_account_drawdown_kill_switch_trips(config, monkeypatch, market):

    def crash(cycle, m):
        if cycle == 2:
            m.equity = 8_800.0          # -12% from the 10,000 peak
    out = run_main(config, monkeypatch, market, cycles=8, on_cycle=crash)
    assert out["state"]["account_halted"] is True
    assert "ACCOUNT KILL SWITCH" in out["log"] or out["state"]["account_halted"]


def test_state_carries_the_session_and_bar_cursors(config, monkeypatch, market):
    out = run_main(config, monkeypatch, market, cycles=6, minutes_per_cycle=16)
    assert "last_bar_time" in out["state"]
    assert out["state"]["equity_peak"] == 10_000.0


def test_main_validates_config_before_connecting(config, monkeypatch, market):
    config["risk"]["risk_per_trade_pct"] = 0.037        # not a selectable level
    monkeypatch.setattr(trader, "load_config", lambda path="config.yaml": config)
    monkeypatch.setattr(trader.time, "sleep",
                        lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        trader.main()
    assert market.init_calls == 0, "connected to the terminal before validating config"
    assert market.orders == []


def test_mode_is_recorded_in_the_log(config, monkeypatch, market):
    """
    Whether a session could have sent orders is the first thing you want when
    reading back a day's decisions, so it belongs in the file, not just on the
    console.
    """
    out = run_main(config, monkeypatch, market, cycles=4)
    assert "MODE:" in out["log"]
    assert "LIVE" in out["log"] or "PAPER" in out["log"] or "BACKTEST" in out["log"]


def test_session_state_is_logged_on_the_first_pass(config, monkeypatch, market):
    """
    Starting OUTSIDE the session must still say so. Seeding the previous state
    with None would make `None != None` false and log nothing, leaving no
    evidence the session gate was even consulted.
    """
    from datetime import datetime
    from conftest import OUT_OF_SESSION_UTC
    out = run_main(config, monkeypatch, market, cycles=4,
                   clock_start=datetime.fromisoformat(OUT_OF_SESSION_UTC),
                   minutes_per_cycle=1)
    assert "Session CLOSED" in out["log"]


def test_vault_timestamps_carry_a_utc_offset(config, monkeypatch, market, tmp_path):
    """
    trades.md is read by a person, so it is stamped in local time - but the
    companion JSONL is stamped in UTC and the session windows are New York.
    Without an offset on this column there is no way to line the two files up,
    and a DST change would walk the column backwards unexplained.
    """
    from datetime import datetime

    vault = tmp_path / "stamp.md"
    trader.log_to_vault(str(vault), "a message")
    line = vault.read_text(encoding="utf-8").strip()

    stamp = line.split("**")[1]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() is not None, (
        f"timestamp {stamp!r} has no UTC offset, so which clock it is on is a "
        "guess"
    )


def test_log_lines_are_ascii(config, monkeypatch, market):
    """
    The files are written UTF-8, but PowerShell's Get-Content reads ANSI by
    default and renders an em-dash as mojibake. An operational log has to be
    readable in the shell the operator actually has.
    """
    out = run_main(config, monkeypatch, market, cycles=6, minutes_per_cycle=16)
    assert out["log"], "nothing was logged"
    try:
        out["log"].encode("ascii")
    except UnicodeEncodeError as e:
        offending = out["log"][max(0, e.start - 60):e.end + 60]
        raise AssertionError(f"non-ASCII in the trade log near: {offending!r}") from None
