"""
Loading and normalising the config.

The config file is grouped by concern; the engine wants flat values. This layer
maps one to the other and refuses settings that would leave the bot running but
never trading — which is the failure mode that looks like success.
"""
import copy

import pytest
import yaml

import config_schema as cs
from conftest import BASE_CONFIG

# The grouped layout, as written by hand.
GROUPED = yaml.safe_load("""
symbols: [EURUSD, USDJPY]
timeframes: {regime: H1, setup: M15, trigger: M5}
poll_seconds: 15
session: {timezone: "America/New_York", start: "22:00", end: "06:00",
          max_trades_per_session: 3, max_open_positions: 1}
pip:
  default: {size: 0.0001, value_per_lot: 10.0}
  overrides: {USDJPY: {size: 0.01, value_per_lot: 6.7}}
indicators:
  ema: {fast: 20, slow: 50}
  atr: {period: 14}
  adx: {period: 14}
  rsi: {period: 14}
  swing: {lookback: 2}
regime:
  allowed: [TREND, RANGE, BREAKOUT, NO_TRADE]
  trend: {minimum_adx: 20}
  range: {}
  breakout: {require_retest: true}
strategies:
  trend_pullback: {enabled: true, allowed_regimes: [TREND], minimum_rr: 2.0}
  range_reversion: {enabled: true, allowed_regimes: [RANGE], minimum_rr: 1.5}
  breakout_retest: {enabled: true, allowed_regimes: [BREAKOUT], minimum_rr: 2.0}
entry: {atr_buffer: {multiplier: 0.05}}
scoring: {execution_threshold: 80}
spread: {max_median_multiplier: 1.5, median_window: 50, max_atr_ratio: 0.10}
news: {blackout_minutes_before: 30, blackout_minutes_after: 30}
risk:
  modes:
    conservative: {risk_per_trade_pct: 0.005}
    normal: {risk_per_trade_pct: 0.01}
    aggressive: {risk_per_trade_pct: 0.02}
  default_mode: normal
  maximum_live_risk_pct: 0.02
  max_open_positions: 1
  max_trades_per_session: 3
  max_lot_size: 1.0
loss_protection:
  consecutive_loss_limit: 2
  max_session_drawdown_pct: 0.05
  max_rolling_account_drawdown_pct: 0.10
  cooldown_after_loss: {enabled: true, candles: 1, timeframe: M15}
stops: {minimum_stop_distance_pips: 3}
targets: {trend: {minimum_rr: 2.0}}
position_management:
  break_even: {minimum_r: 1.0}
  trailing_stop: {activation_r: 1.5}
data: {maximum_candle_age_seconds: 1800}
state: {path: ".lathe_state.json"}
broker: {utc_offset_hours: 2}
logging:
  trade_log: {path: "out/trades.md"}
  decision_log: {path: "out/decisions.jsonl"}
  rejection_log: {path: "out/rejections.jsonl"}
safety: {default_mode: PAPER, live_trading_requires_explicit_enable: true}
""")


def grouped(**over):
    cfg = copy.deepcopy(GROUPED)
    for path, value in over.items():
        parts = path.split("__")
        cur = cfg
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    return cfg


# --- both layouts must work -------------------------------------------------
def test_grouped_layout_normalises():
    n = cs.normalise(grouped())
    assert n.symbols == ["EURUSD", "USDJPY"]
    assert (n.tf_regime, n.tf_setup, n.tf_entry) == ("H1", "M15", "M5")
    assert n.risk_pct == 0.01
    assert n.trade_log == "out/trades.md"
    assert n.decision_log == "out/decisions.jsonl"


def test_flat_layout_still_normalises():
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["state"] = {"path": "s.json"}
    cfg["vault"] = {"log_path": "t.md"}
    n = cs.normalise(cfg)
    assert n.tf_entry == "M5"          # from timeframes.entry
    assert n.trade_log == "t.md"       # from vault.log_path


def test_entry_and_trigger_name_the_same_timeframe():
    cfg = grouped()
    cfg["timeframes"] = {"regime": "H1", "setup": "M15", "entry": "M5"}
    assert cs.normalise(cfg).tf_entry == "M5"


# --- risk modes -------------------------------------------------------------
@pytest.mark.parametrize("mode,expected", [
    ("conservative", 0.005), ("normal", 0.01), ("aggressive", 0.02)])
def test_risk_modes_resolve(mode, expected):
    assert cs.resolve_risk_pct(grouped(risk__default_mode=mode)) == expected


def test_unknown_risk_mode_is_rejected():
    with pytest.raises(cs.ConfigError, match="default_mode"):
        cs.resolve_risk_pct(grouped(risk__default_mode="reckless"))


def test_off_scale_risk_is_rejected():
    cfg = grouped()
    cfg["risk"]["modes"]["normal"]["risk_per_trade_pct"] = 0.05
    with pytest.raises(cs.ConfigError, match="risk_per_trade_pct"):
        cs.resolve_risk_pct(cfg)


def test_risk_above_the_live_ceiling_is_rejected():
    cfg = grouped(risk__default_mode="aggressive")
    cfg["risk"]["maximum_live_risk_pct"] = 0.01
    with pytest.raises(cs.ConfigError, match="maximum_live_risk_pct"):
        cs.resolve_risk_pct(cfg)


# --- settings that would stop the bot trading -------------------------------
def test_stale_limit_below_the_setup_timeframe_is_fatal():
    cfg = grouped(data__maximum_candle_age_seconds=120)
    problems = cs.check_safety(cfg, cs.normalise(cfg))
    assert any("maximum_candle_age_seconds" in p for p in problems)
    assert any("900s" in p for p in problems)


def test_stale_limit_above_the_setup_timeframe_is_fine():
    cfg = grouped(data__maximum_candle_age_seconds=1800)
    assert cs.check_safety(cfg, cs.normalise(cfg)) == []


def test_wide_swing_lookback_is_fatal():
    cfg = grouped(indicators__swing__lookback=20)
    problems = cs.check_safety(cfg, cs.normalise(cfg))
    assert any("swing.lookback" in p for p in problems)


def test_normal_swing_lookback_is_fine():
    assert cs.check_safety(grouped(), cs.normalise(grouped())) == []


# --- mode -------------------------------------------------------------------
def test_paper_is_the_default_posture():
    n = cs.normalise(grouped())
    assert cs.effective_mode(n) == cs.MODE_PAPER


def test_live_needs_an_explicit_second_switch():
    n = cs.normalise(grouped(safety__default_mode="LIVE"))
    assert n.mode == cs.MODE_LIVE
    assert cs.effective_mode(n) == cs.MODE_PAPER, "LIVE without the enable flag must not trade"
    assert "not true" in cs.describe_mode(n)


def test_live_with_the_switch_is_live():
    cfg = grouped(safety__default_mode="LIVE")
    cfg["safety"]["live_trading_enabled"] = True
    n = cs.normalise(cfg)
    assert cs.effective_mode(n) == cs.MODE_LIVE
    assert "real money" in cs.describe_mode(n)


def test_unknown_mode_is_rejected():
    with pytest.raises(cs.ConfigError, match="default_mode"):
        cs.normalise(grouped(safety__default_mode="YOLO"))


# --- per-module builders ----------------------------------------------------
def test_regime_config_reads_the_grouped_layout():
    r = cs.regime_config(grouped())
    assert r.ema_fast == 20 and r.ema_slow == 50
    assert r.trend_adx_min == 20
    assert r.breakout_enabled is True      # BREAKOUT listed in regime.allowed


def test_breakout_regime_off_when_not_listed():
    cfg = grouped()
    cfg["regime"]["allowed"] = ["TREND", "RANGE", "NO_TRADE"]
    assert cs.regime_config(cfg).breakout_enabled is False


def test_protection_config_reads_the_grouped_layout():
    p = cs.protection_config(grouped())
    assert p.spread_median_multiple == 1.5
    assert p.spread_atr_max == 0.10
    assert p.spread_history == 50
    assert p.max_trades_per_session == 3
    assert p.max_concurrent_trades == 1
    assert p.daily_loss_pct == 0.05
    assert p.account_drawdown_pct == 0.10
    assert p.min_stop_pips == 3


def test_spec_stop_rules_survive_the_pip_floor():
    """minimum_stop_distance_pips is additive, not a replacement."""
    p = cs.protection_config(grouped())
    assert p.stop_min_spread_multiple == 2.0
    assert p.stop_max_atr_multiple == 1.5


def test_candle_cooldown_becomes_minutes():
    cfg = grouped()
    cfg["loss_protection"]["stop_session_on_consecutive_loss_limit"] = False
    p2 = cs.protection_config(cfg)
    assert p2.cooldown_scope == "minutes"
    assert p2.cooldown_minutes == 15, "1 M15 candle is 15 minutes"


def test_session_stop_takes_precedence_over_the_candle_cooldown():
    cfg = grouped()
    cfg["loss_protection"]["stop_session_on_consecutive_loss_limit"] = True
    assert cs.protection_config(cfg).cooldown_scope == "session"


def test_management_config_reads_the_grouped_layout():
    m = cs.management_config(grouped())
    assert m.breakeven_at_r == 1.0
    assert m.trail_start_r == 1.5
    assert m.target_r == 2.0


def test_risk_config_takes_the_broker_offset():
    n = cs.normalise(grouped())
    assert cs.risk_config(grouped(), n).broker_utc_offset_hours == 2


def test_pip_lookup_both_layouts():
    assert cs.pip_for(grouped(), "EURUSD") == (0.0001, 10.0)
    assert cs.pip_for(grouped(), "USDJPY") == (0.01, 6.7)


def test_jpy_without_override_still_rejected():
    cfg = grouped()
    cfg["pip"]["overrides"] = {}
    with pytest.raises(cs.ConfigError, match="USDJPY"):
        cs.pip_for(cfg, "USDJPY")


def test_enabled_setups_from_strategies():
    import setups
    assert set(cs.enabled_setups(grouped())) == set(setups.ALL_SETUPS)
    cfg = grouped()
    cfg["strategies"]["range_reversion"]["enabled"] = False
    assert "range_reversion" not in cs.enabled_setups(cfg)


def test_regime_map_from_allowed_regimes():
    import regime as regime_mod
    import setups
    m = cs.regime_map(grouped())
    assert setups.TREND_PULLBACK in m[regime_mod.TREND_UP]
    assert setups.TREND_PULLBACK in m[regime_mod.TREND_DOWN]
    assert setups.RANGE_REVERSION in m[regime_mod.RANGE]
    assert setups.BREAKOUT_RETEST in m[regime_mod.BREAKOUT]


def test_min_score_from_execution_threshold():
    assert cs.min_score(grouped()) == 80
    assert cs.min_score(grouped(scoring__execution_threshold=90)) == 90


def test_missing_trade_log_is_rejected():
    cfg = grouped()
    del cfg["logging"]
    with pytest.raises(cs.ConfigError, match="trade log"):
        cs.normalise(cfg)


def test_no_symbols_is_rejected():
    with pytest.raises(cs.ConfigError, match="no symbols"):
        cs.normalise(grouped(symbols=[]))


def test_bad_timeframe_is_rejected():
    with pytest.raises(cs.ConfigError, match="timeframes"):
        cs.normalise(grouped(timeframes__setup="M7"))


def test_validate_config_refuses_the_dangerous_values():
    """
    check_safety is wired into startup, not just available. Without this the
    checks could be silently bypassed and the bot would run and never trade.
    """
    import trader
    cfg = grouped(data__maximum_candle_age_seconds=120)
    cfg["logging"]["trade_log"]["path"] = "t.md"
    with pytest.raises(cs.ConfigError, match="stop the bot trading"):
        trader.validate_config(cfg)

    cfg2 = grouped(indicators__swing__lookback=20)
    with pytest.raises(cs.ConfigError, match="swing.lookback"):
        trader.validate_config(cfg2)


def test_validate_config_accepts_the_shipped_config():
    import yaml
    from pathlib import Path
    import trader
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
    norm = trader.validate_config(cfg)
    assert cs.effective_mode(norm) == cs.MODE_PAPER, "shipped config must default to PAPER"


def test_breakout_precedence_is_wired():
    """config.yaml documents this as flipping the check order — it must."""
    assert cs.regime_config(grouped()).breakout_precedence == "after"
    cfg = grouped()
    cfg["regime"]["breakout"]["precedence"] = "before"
    assert cs.regime_config(cfg).breakout_precedence == "before"


def test_filling_mode_defaults_to_auto():
    assert cs.normalise(grouped()).filling_mode == "auto"


def test_filling_mode_can_be_pinned():
    cfg = grouped()
    cfg["execution"] = {"filling_mode": "FOK"}
    assert cs.normalise(cfg).filling_mode == "FOK"


def test_slippage_points_reach_the_config():
    cfg = grouped()
    cfg["execution"] = {"max_slippage_points": 35}
    assert cs.normalise(cfg).deviation_points == 35


# --- broker offset: configured, or derived from the server ------------------
def test_explicit_broker_offset_is_used():
    assert cs.broker_offset(grouped(broker__utc_offset_hours=3)) == 3.0


def test_auto_offset_defers_to_runtime():
    assert cs.broker_offset(grouped(broker__utc_offset_hours="auto")) is None


def test_absent_offset_defers_to_runtime():
    cfg = grouped()
    del cfg["broker"]["utc_offset_hours"]
    assert cs.broker_offset(cfg) is None


def test_explicit_offset_beats_the_derived_one():
    """A hand-set value is a deliberate override and must win."""
    n = cs.normalise(grouped(broker__utc_offset_hours=5))
    r = cs.risk_config(grouped(broker__utc_offset_hours=5), n, offset_override=9)
    assert r.broker_utc_offset_hours == 5.0


def test_derived_offset_is_used_when_auto():
    cfg = grouped(broker__utc_offset_hours="auto")
    r = cs.risk_config(cfg, cs.normalise(cfg), offset_override=3)
    assert r.broker_utc_offset_hours == 3.0


def test_auto_with_nothing_derived_falls_back_to_utc():
    cfg = grouped(broker__utc_offset_hours="auto")
    r = cs.risk_config(cfg, cs.normalise(cfg), offset_override=None)
    assert r.broker_utc_offset_hours == 0.0
