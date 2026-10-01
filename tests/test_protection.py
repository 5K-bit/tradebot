"""Every gate a trade must pass before it is allowed to exist."""
from datetime import datetime, timedelta, timezone

import pytest


from protection import Protection, ProtectionConfig, from_config
from state import JsonState

NOW = datetime(2026, 1, 15, 3, 30, tzinfo=timezone.utc)
SESSION = "2026-01-14"


def make(tmp_path=None, **overrides):
    cfg = ProtectionConfig(**overrides)
    state = JsonState(str(tmp_path / "s.json")) if tmp_path else None
    return Protection(cfg, state=state), state


def ok_args(**over):
    """Arguments that pass every gate. The candle timestamp follows `now`, so
    advancing the clock in a test does not accidentally trip the staleness
    gate and mask what the test is actually checking."""
    now = over.get("now", NOW)
    args = dict(session_key=SESSION, open_positions=0, spread_pips=1.0,
                last_candle_time=now.timestamp() - 60, now=now, connected=True)
    args.update(over)
    return args


def test_clean_state_allows():
    p, _ = make()
    assert p.check(**ok_args()).allowed is True


def test_outside_session_rejected():
    p, _ = make()
    d = p.check(**ok_args(session_key=None))
    assert not d and "outside the trading session" in d.reason


def test_one_concurrent_trade():
    p, _ = make()
    d = p.check(**ok_args(open_positions=1))
    assert not d and "concurrent" in d.reason


def test_three_trades_per_session():
    p, _ = make()
    for _ in range(3):
        p.record_trade_opened(SESSION)
    d = p.check(**ok_args())
    assert not d and "session trade limit" in d.reason


def test_session_counter_is_per_session():
    p, _ = make()
    for _ in range(3):
        p.record_trade_opened(SESSION)
    assert p.check(**ok_args(session_key="2026-01-15")).allowed is True


def test_spread_filter():
    p, _ = make(max_spread_pips=2.0)
    assert p.check(**ok_args(spread_pips=1.9)).allowed is True
    d = p.check(**ok_args(spread_pips=2.1))
    assert not d and "spread" in d.reason


def test_missing_spread_is_a_rejection_not_a_pass():
    p, _ = make()
    d = p.check(**ok_args(spread_pips=None))
    assert not d and "invalid tick data" in d.reason


def test_stale_candles_rejected():
    p, _ = make(max_candle_age_seconds=900)
    d = p.check(**ok_args(last_candle_time=NOW.timestamp() - 5000))
    assert not d and "stale" in d.reason


def test_missing_candles_rejected():
    p, _ = make()
    d = p.check(**ok_args(last_candle_time=None))
    assert not d and "no candle data" in d.reason


def test_disconnected_rejected():
    p, _ = make()
    d = p.check(**ok_args(connected=False))
    assert not d and "not connected" in d.reason


def test_two_consecutive_losses_trigger_cooldown():
    p, _ = make()
    p.record_trade_closed(-10.0, SESSION)
    assert p.check(**ok_args()).allowed is True, "one loss should not stop trading"
    p.record_trade_closed(-10.0, SESSION)
    d = p.check(**ok_args())
    assert not d and "consecutive losses" in d.reason


def test_a_win_resets_the_loss_streak():
    p, _ = make()
    p.record_trade_closed(-10.0, SESSION)
    p.record_trade_closed(+5.0, SESSION)
    p.record_trade_closed(-10.0, SESSION)
    assert p.check(**ok_args()).allowed is True


def test_cooldown_is_scoped_to_the_session():
    p, _ = make()
    p.record_trade_closed(-1.0, SESSION)
    p.record_trade_closed(-1.0, SESSION)
    assert p.check(**ok_args()).allowed is False
    assert p.check(**ok_args(session_key="2026-01-15")).allowed is True


def test_minutes_cooldown_expires():
    p, _ = make(cooldown_scope="minutes", cooldown_minutes=60)
    p.record_trade_closed(-1.0, SESSION, now=NOW)
    p.record_trade_closed(-1.0, SESSION, now=NOW)
    assert p.check(**ok_args(now=NOW + timedelta(minutes=30))).allowed is False
    assert p.check(**ok_args(now=NOW + timedelta(minutes=61))).allowed is True


def test_account_drawdown_kill_switch(tmp_path):
    p, _ = make(tmp_path, account_drawdown_pct=0.10)
    p.observe_equity(10_000.0)
    p.observe_equity(9_500.0)
    assert p.check(**ok_args()).allowed is True       # -5%, still trading
    p.observe_equity(9_000.0)                          # -10% from peak
    d = p.check(**ok_args())
    assert not d and "account drawdown" in d.reason


def test_account_kill_switch_is_sticky_across_restart(tmp_path):
    p, _ = make(tmp_path, account_drawdown_pct=0.10)
    p.observe_equity(10_000.0)
    p.observe_equity(8_900.0)
    assert p.check(**ok_args()).allowed is False

    revived = Protection(ProtectionConfig(account_drawdown_pct=0.10),
                         state=JsonState(str(tmp_path / "s.json")))
    assert revived.check(**ok_args()).allowed is False, "kill switch cleared on restart"
    # ...and recovering equity does NOT silently re-arm it.
    revived.observe_equity(9_900.0)
    assert revived.check(**ok_args()).allowed is False


def test_peak_tracks_upward(tmp_path):
    p, _ = make(tmp_path, account_drawdown_pct=0.10)
    p.observe_equity(10_000.0)
    p.observe_equity(12_000.0)
    p.observe_equity(11_000.0)         # -8.3% from the NEW peak
    assert p.check(**ok_args()).allowed is True
    p.observe_equity(10_700.0)         # -10.8% from 12,000
    assert p.check(**ok_args()).allowed is False


def test_session_counts_persist_across_restart(tmp_path):
    p, _ = make(tmp_path)
    p.record_trade_opened(SESSION)
    p.record_trade_opened(SESSION)
    revived = Protection(ProtectionConfig(), state=JsonState(str(tmp_path / "s.json")))
    assert revived.trades_this_session(SESSION) == 2


def test_loss_streak_persists_across_restart(tmp_path):
    p, _ = make(tmp_path)
    p.record_trade_closed(-1.0, SESSION)
    p.record_trade_closed(-1.0, SESSION)
    revived = Protection(ProtectionConfig(), state=JsonState(str(tmp_path / "s.json")))
    assert revived.check(**ok_args()).allowed is False


def test_news_blackout_window():
    p, _ = make(news_blackout_windows=[
        {"label": "US CPI", "start": "2026-01-15T03:45:00+00:00"}],
        news_blackout_minutes_before=30, news_blackout_minutes_after=30)
    d = p.check(**ok_args(now=NOW))            # 03:30, 15 min before
    assert not d and "news blackout: US CPI" in d.reason
    clear = p.check(**ok_args(now=NOW - timedelta(hours=2)))
    assert clear.allowed is True


def test_news_blackout_covers_the_window_after():
    p, _ = make(news_blackout_windows=[{"label": "NFP", "start": "2026-01-15T03:00:00Z"}],
                news_blackout_minutes_after=45)
    assert p.check(**ok_args(now=NOW)).allowed is False           # 30 min after
    assert p.check(**ok_args(now=NOW + timedelta(minutes=20))).allowed is True


def test_malformed_news_window_is_ignored_not_fatal():
    p, _ = make(news_blackout_windows=[{"label": "bad"}, {"start": "not-a-date"}])
    assert p.check(**ok_args()).allowed is True


def test_from_config_maps_the_yaml():
    cfg = from_config({
        "risk": {"max_trades_per_session": 5, "max_concurrent_trades": 2,
                 "account_drawdown_pct": 0.2, "max_daily_loss_pct": 0.07},
        "protection": {"max_spread_pips": 3.5, "consecutive_loss_limit": 4},
    })
    assert cfg.max_trades_per_session == 5
    assert cfg.max_concurrent_trades == 2
    assert cfg.account_drawdown_pct == 0.2
    assert cfg.daily_loss_pct == 0.07
    assert cfg.max_spread_pips == 3.5
    assert cfg.consecutive_loss_limit == 4


def test_rejection_reasons_are_always_human_readable():
    p, _ = make()
    for args in [ok_args(session_key=None), ok_args(open_positions=9),
                 ok_args(spread_pips=99), ok_args(connected=False),
                 ok_args(last_candle_time=None)]:
        d = p.check(**args)
        assert not d.allowed and len(d.reason) > 10


def test_news_window_accepts_a_trailing_z():
    """'...Z' is the form people write. datetime.fromisoformat only accepts it
    from Python 3.11, so parsing must normalise it or 3.10 silently skips the
    blackout entirely."""
    p, _ = make(news_blackout_windows=[{"label": "CPI", "start": "2026-01-15T03:30:00Z"}])
    d = p.check(**ok_args(now=NOW))
    assert not d and "news blackout: CPI" in d.reason


def test_news_window_accepts_an_explicit_offset():
    p, _ = make(news_blackout_windows=[{"label": "CPI", "start": "2026-01-15T03:30:00+00:00"}])
    assert p.check(**ok_args(now=NOW)).allowed is False


def test_naive_news_window_is_treated_as_utc():
    p, _ = make(news_blackout_windows=[{"label": "CPI", "start": "2026-01-15T03:30:00"}])
    assert p.check(**ok_args(now=NOW)).allowed is False


# --- the spec's spread rule -------------------------------------------------
def spread_gate(**over):
    p, _ = make(**over)
    for _ in range(20):
        p.observe_spread(0.00010)          # median becomes 0.00010
    return p


def test_median_spread_is_tracked():
    p = spread_gate()
    assert p.median_spread() == pytest.approx(0.00010)


def test_spread_within_median_multiple_passes():
    p = spread_gate(spread_median_multiple=1.5, spread_atr_max=0.10)
    assert p.check_spread(0.00014, 0.0020).allowed is True


def test_spread_above_median_multiple_is_rejected():
    """SPEC: current_spread <= 1.5 x recent_median_spread."""
    p = spread_gate(spread_median_multiple=1.5, spread_atr_max=1.0)
    d = p.check_spread(0.00016, 0.0020)
    assert not d and "median" in d.reason


def test_spread_above_atr_fraction_is_rejected():
    """SPEC: current_spread / ATR_M5 <= 0.10."""
    p = spread_gate(spread_median_multiple=99.0, spread_atr_max=0.10)
    d = p.check_spread(0.00012, 0.0008)          # 0.15 of ATR
    assert not d and "ATR_M5" in d.reason


def test_both_spread_clauses_must_hold():
    p = spread_gate(spread_median_multiple=1.5, spread_atr_max=0.10)
    assert p.check_spread(0.00011, 0.0020).allowed is True      # both fine
    assert p.check_spread(0.00016, 0.0020).allowed is False     # median fails
    assert p.check_spread(0.00011, 0.0005).allowed is False     # ATR fails


def test_spread_history_survives_restart(tmp_path):
    p, _ = make(tmp_path)
    for _ in range(20):
        p.observe_spread(0.00010)
    revived = Protection(ProtectionConfig(), state=JsonState(str(tmp_path / "s.json")))
    assert revived.median_spread() == pytest.approx(0.00010)


def test_nan_spread_is_not_recorded():
    p = spread_gate()
    p.observe_spread(float("nan"))
    p.observe_spread(-1.0)
    assert p.median_spread() == pytest.approx(0.00010)


# --- the spec's stop validation --------------------------------------------
def test_stop_must_clear_twice_the_spread():
    """SPEC: stop_distance >= 2 x current_spread."""
    p, _ = make(stop_min_spread_multiple=2.0)
    assert p.check_stop(0.00025, 0.00010, 0.0020).allowed is True
    d = p.check_stop(0.00015, 0.00010, 0.0020)
    assert not d and "spread" in d.reason


def test_stop_must_stay_within_atr_ceiling():
    """SPEC: stop_distance <= 1.5 x ATR_M15."""
    p, _ = make(stop_max_atr_multiple=1.5)
    assert p.check_stop(0.0029, 0.00010, 0.0020).allowed is True
    d = p.check_stop(0.0031, 0.00010, 0.0020)
    assert not d and "ATR_M15" in d.reason


def test_nonpositive_stop_is_rejected():
    p, _ = make()
    assert p.check_stop(0.0, 0.00010, 0.0020).allowed is False
    assert p.check_stop(float("nan"), 0.00010, 0.0020).allowed is False


def test_minimum_stop_pips_is_enforced():
    """An absolute pip floor on top of the spec's spread and ATR rules."""
    p, _ = make(min_stop_pips=3.0)
    pip = 0.0001
    assert p.check_stop(0.0004, 0.00001, 0.0020, pip).allowed is True    # 4 pips
    d = p.check_stop(0.0002, 0.00001, 0.0020, pip)                       # 2 pips
    assert not d and "3.0 pip minimum" in d.reason


def test_minimum_stop_pips_is_additive_not_a_replacement():
    """A stop clearing the pip floor still fails if it is under 2x the spread."""
    p, _ = make(min_stop_pips=3.0, stop_min_spread_multiple=2.0)
    d = p.check_stop(0.0004, 0.00030, 0.0020, 0.0001)   # 4 pips but spread is 3
    assert not d and "spread" in d.reason


def test_minimum_stop_pips_disabled_by_default():
    p, _ = make()
    assert p.cfg.min_stop_pips == 0.0
    assert p.check_stop(0.0002, 0.00001, 0.0020, 0.0001).allowed is True
