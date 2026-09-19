"""
The daily loss limit must survive a restart.

The original bug: the halt flag and the day's starting equity lived only in
memory, so restarting after a bad day cleared the halt AND re-baselined to the
drawn-down equity — the -3% limit could be blown through again and again in a
single day.
"""
import json

from risk_manager import RiskConfig, RiskManager
from state import JsonState

CFG = RiskConfig(0.01, 0.03, 3, 1.0, 0.0)


def make(path):
    return RiskManager(CFG, state=JsonState(str(path)))


def test_trips_at_the_limit(tmp_path):
    r = make(tmp_path / "s.json")
    assert r.check_kill_switch(10_000.0) is False
    assert r.check_kill_switch(9_800.0) is False     # -2%
    assert r.check_kill_switch(9_700.0) is True      # -3%


def test_halt_survives_restart(tmp_path):
    p = tmp_path / "s.json"
    make(p).check_kill_switch(10_000.0)
    assert make(p).check_kill_switch(9_700.0) is True

    restarted = make(p)
    assert restarted.check_kill_switch(9_700.0) is True


def test_baseline_is_not_rebased_on_restart(tmp_path):
    p = tmp_path / "s.json"
    r = make(p)
    r.check_kill_switch(10_000.0)
    r.check_kill_switch(9_700.0)                     # trips

    restarted = make(p)
    assert restarted._equity_at_day_start == 10_000.0
    # Recovering to -2.5% must NOT re-arm trading for the rest of the day.
    assert restarted.check_kill_switch(9_750.0) is True


def test_state_is_written_to_disk(tmp_path):
    p = tmp_path / "s.json"
    r = make(p)
    r.check_kill_switch(10_000.0)
    r.check_kill_switch(9_700.0)

    saved = json.loads(p.read_text())
    assert saved["halted"] is True
    assert saved["equity_at_day_start"] == 10_000.0
    assert saved["trading_day"]


def test_new_broker_day_clears_the_halt(tmp_path):
    p = tmp_path / "s.json"
    r = make(p)
    r.check_kill_switch(10_000.0)
    r.check_kill_switch(9_700.0)

    nextday = make(p)
    nextday._day = "1999-01-01"                      # pretend the broker day rolled
    assert nextday.check_kill_switch(9_700.0) is False
    assert nextday._equity_at_day_start == 9_700.0   # re-baselined to the new day


def test_broker_offset_shifts_the_day_boundary():
    """A broker on UTC+13 can be on tomorrow's date while UTC is still on today's."""
    utc = RiskManager(RiskConfig(0.01, 0.03, 3, 1.0, 0.0))
    ahead = RiskManager(RiskConfig(0.01, 0.03, 3, 1.0, 13.0))
    behind = RiskManager(RiskConfig(0.01, 0.03, 3, 1.0, -13.0))
    days = {utc._trading_day(), ahead._trading_day(), behind._trading_day()}
    assert len(days) > 1, "offsets produced identical trading days at every hour"


def test_works_without_a_state_file():
    """State is optional — the manager must still function in memory."""
    r = RiskManager(CFG)
    assert r.check_kill_switch(10_000.0) is False
    assert r.check_kill_switch(9_700.0) is True


def test_corrupt_state_file_does_not_crash(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ this is not json")
    r = make(p)
    assert r.check_kill_switch(10_000.0) is False
