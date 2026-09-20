"""
config_schema.py — load the Lathe config and normalise it for the engine.

The config file is the human-facing document: it is grouped by concern
(indicators, regime, strategies, risk, safety) and is the source of truth for
what the bot is supposed to do. The engine modules want flat, specific values.
This module is the single place that maps one to the other, so a rename in the
config never has to be chased through a dozen readers.

It also refuses configurations that would quietly stop the bot working rather
than letting it run and never trade. Those checks are the reason this file
exists at all: a stale-data limit shorter than the setup timeframe, or a swing
lookback so wide no swing can form, both produce a bot that looks healthy and
takes no trades.
"""
from dataclasses import dataclass

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                     "H1": 3600, "H4": 14400, "D1": 86400}

ALLOWED_RISK_PCT = (0.005, 0.01, 0.02)

MODE_BACKTEST, MODE_PAPER, MODE_LIVE = "BACKTEST", "PAPER", "LIVE"
VALID_MODES = (MODE_BACKTEST, MODE_PAPER, MODE_LIVE)


class ConfigError(ValueError):
    """Raised for a configuration that cannot safely be run."""


def _get(cfg, path, default=None):
    """Read a dotted path, tolerating missing intermediate sections."""
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] is None:
            return default
        cur = cur[part]
    return cur


def _first(cfg, paths, default=None):
    """First path that is present. Lets old and new key names both work."""
    for path in paths:
        value = _get(cfg, path)
        if value is not None:
            return value
    return default


@dataclass
class Normalised:
    """What the engine actually consumes."""
    raw: dict
    symbols: list
    tf_regime: str
    tf_setup: str
    tf_entry: str
    poll_seconds: int
    mode: str
    live_enabled: bool
    risk_pct: float
    trade_log: str
    decision_log: str | None
    rejection_log: str | None
    state_path: str

    @property
    def setup_seconds(self) -> int:
        return TIMEFRAME_SECONDS[self.tf_setup]

    @property
    def entry_seconds(self) -> int:
        return TIMEFRAME_SECONDS[self.tf_entry]


def normalise(cfg: dict) -> Normalised:
    if not isinstance(cfg, dict):
        raise ConfigError("config root must be a mapping")

    symbols = cfg.get("symbols") or []
    if not symbols:
        raise ConfigError("config lists no symbols.")

    # `entry` and `trigger` name the same timeframe.
    tf_regime = _first(cfg, ["timeframes.regime"])
    tf_setup = _first(cfg, ["timeframes.setup"])
    tf_entry = _first(cfg, ["timeframes.entry", "timeframes.trigger"])
    for role, name in (("regime", tf_regime), ("setup", tf_setup),
                       ("entry/trigger", tf_entry)):
        if name not in TIMEFRAME_SECONDS:
            raise ConfigError(
                f"timeframes.{role} is {name!r}; expected one of "
                f"{sorted(TIMEFRAME_SECONDS)}.")

    mode = str(_first(cfg, ["safety.default_mode", "mode"], MODE_PAPER)).upper()
    if mode not in VALID_MODES:
        raise ConfigError(f"safety.default_mode is {mode!r}; expected one of {VALID_MODES}.")
    live_enabled = bool(_get(cfg, "safety.live_trading_enabled", False))

    risk_pct = resolve_risk_pct(cfg)

    trade_log = _first(cfg, ["logging.trade_log.path", "vault.log_path"])
    if not trade_log:
        raise ConfigError("no trade log path (logging.trade_log.path).")

    return Normalised(
        raw=cfg,
        symbols=list(symbols),
        tf_regime=tf_regime, tf_setup=tf_setup, tf_entry=tf_entry,
        poll_seconds=int(_first(cfg, ["poll_seconds"], 30)),
        mode=mode, live_enabled=live_enabled,
        risk_pct=risk_pct,
        trade_log=trade_log,
        decision_log=(_get(cfg, "logging.decision_log.path")
                      if _get(cfg, "logging.decision_log.enabled", True) else None),
        rejection_log=(_get(cfg, "logging.rejection_log.path")
                       if _get(cfg, "logging.rejection_log.enabled", True) else None),
        state_path=_first(cfg, ["state.path"], ".lathe_state.json"),
    )


def resolve_risk_pct(cfg: dict) -> float:
    """
    Risk per trade, from either the flat key or the named modes.

    The strategy allows three levels only. A typo must not become a 10% risk,
    so anything else is rejected rather than clamped.
    """
    flat = _get(cfg, "risk.risk_per_trade_pct")
    if flat is not None:
        risk = float(flat)
    else:
        mode_name = _get(cfg, "risk.default_mode", "normal")
        modes = _get(cfg, "risk.modes", {}) or {}
        if mode_name not in modes:
            raise ConfigError(
                f"risk.default_mode is {mode_name!r} but risk.modes defines "
                f"{sorted(modes)}.")
        risk = float(_get(cfg, f"risk.modes.{mode_name}.risk_per_trade_pct"))

    if risk not in ALLOWED_RISK_PCT:
        raise ConfigError(
            f"risk_per_trade_pct is {risk}; the strategy allows "
            f"{ALLOWED_RISK_PCT} (0.5%, 1%, 2%).")

    ceiling = _get(cfg, "risk.maximum_live_risk_pct")
    if ceiling is not None and risk > float(ceiling):
        raise ConfigError(
            f"risk per trade {risk} exceeds risk.maximum_live_risk_pct {ceiling}.")
    return risk


def check_safety(cfg: dict, norm: Normalised) -> list:
    """
    Settings that would leave the bot running but never trading, or trading
    when the config says it should not. Returns a list of fatal messages.
    """
    problems = []

    # A stale-data limit shorter than the setup timeframe rejects healthy bars.
    max_age = _first(cfg, ["data.maximum_candle_age_seconds",
                           "protection.max_candle_age_seconds"])
    if max_age is not None and float(max_age) <= norm.setup_seconds:
        pct = (norm.setup_seconds - float(max_age)) / norm.setup_seconds * 100
        problems.append(
            f"data.maximum_candle_age_seconds is {max_age}s but the setup "
            f"timeframe {norm.tf_setup} is {norm.setup_seconds}s. A just-closed "
            f"bar is legitimately up to {norm.setup_seconds}s old, so data would "
            f"be rejected as stale for {pct:.0f}% of every bar and the bot would "
            f"almost never trade. Set it above {norm.setup_seconds}.")

    # A swing needs `lookback` bars either side, so wide values never confirm.
    lookback = _first(cfg, ["indicators.swing.lookback", "setup_params.swing_lookback"])
    if lookback is not None and int(lookback) > 5:
        problems.append(
            f"indicators.swing.lookback is {lookback}, meaning {lookback} bars on "
            f"EACH side of a swing point ({int(lookback) * 2 + 1} bars per swing, "
            f"and two swings are needed for a higher low). Trend-pullback entries "
            f"would effectively never trigger. Use 2, or say you want a search "
            f"window instead.")

    return problems


def describe_mode(norm: Normalised) -> str:
    if norm.mode == MODE_LIVE and norm.live_enabled:
        return "LIVE — orders go to the broker with real money"
    if norm.mode == MODE_LIVE:
        return ("LIVE requested but safety.live_trading_enabled is not true — "
                "running in PAPER")
    return f"{norm.mode} — no orders are sent to the broker"


def effective_mode(norm: Normalised) -> str:
    """LIVE requires an explicit second switch; anything else degrades to PAPER."""
    if norm.mode == MODE_LIVE and not norm.live_enabled:
        return MODE_PAPER
    return norm.mode


# --- per-module builders ----------------------------------------------------
# Each accepts both the grouped config layout and the older flat one, so a
# config written either way runs without the engine caring which it got.

def regime_config(cfg: dict):
    from regime import RegimeConfig
    return RegimeConfig(
        ema_fast=_first(cfg, ["indicators.ema.fast", "regime.ema_fast"], 20),
        ema_slow=_first(cfg, ["indicators.ema.slow", "regime.ema_slow"], 50),
        adx_period=_first(cfg, ["indicators.adx.period", "regime.adx_period"], 14),
        atr_period=_first(cfg, ["indicators.atr.period", "regime.atr_period"], 14),
        trend_adx_min=_first(cfg, ["regime.trend.minimum_adx", "regime.trend_adx_min"], 20.0),
        trend_separation_atr=_first(cfg, ["regime.trend.minimum_separation_atr",
                                          "regime.trend_separation_atr"], 0.25),
        range_adx_max=_first(cfg, ["regime.range.maximum_adx", "regime.range_adx_max"], 18.0),
        range_separation_atr=_first(cfg, ["regime.range.maximum_separation_atr",
                                          "regime.range_separation_atr"], 0.20),
        strong_adx=_first(cfg, ["regime.scoring.strong_adx", "regime.strong_adx"], 25.0),
        strong_separation_atr=_first(cfg, ["regime.scoring.strong_separation_atr",
                                           "regime.strong_separation_atr"], 0.50),
        strong_range_adx=_first(cfg, ["regime.scoring.strong_range_adx",
                                      "regime.strong_range_adx"], 15.0),
        strong_range_separation_atr=_first(cfg, ["regime.scoring.strong_range_separation_atr",
                                                 "regime.strong_range_separation_atr"], 0.10),
        breakout_enabled=_breakout_regime_enabled(cfg),
        breakout_lookback=_first(cfg, ["regime.breakout.lookback_bars"], 12),
        breakout_buffer_atr=_first(cfg, ["regime.breakout.break_buffer_atr"], 0.10),
        breakout_expansion=_first(cfg, ["regime.breakout.expansion_multiple"], 1.20),
        breakout_median_bars=_first(cfg, ["regime.breakout.median_bars"], 20),
        breakout_retest_bars=_first(cfg, ["regime.breakout.retest_max_bars"], 3),
        breakout_retest_atr=_first(cfg, ["regime.breakout.retest_distance_atr"], 0.15),
        breakout_requires_retest=bool(_first(cfg, ["regime.breakout.require_retest"], True)),
    )


def _breakout_regime_enabled(cfg: dict) -> bool:
    """BREAKOUT is a regime only when the config lists it as one."""
    allowed = _get(cfg, "regime.allowed") or []
    listed = any(str(x).upper() == "BREAKOUT" for x in allowed)
    return bool(listed and _get(cfg, "regime.breakout") is not None)


def setup_config(cfg: dict):
    from setups import SetupConfig
    defaults = SetupConfig().__dict__
    explicit = _get(cfg, "setup_params", {}) or {}
    values = dict(defaults)
    values.update({k: v for k, v in explicit.items() if k in defaults})

    # Grouped-layout equivalents take precedence where present.
    mapped = {
        "entry_buffer_atr": _get(cfg, "entry.atr_buffer.multiplier"),
        "rsi_period": _get(cfg, "indicators.rsi.period"),
        "atr_period": _get(cfg, "indicators.atr.period"),
        "ema_fast": _get(cfg, "indicators.ema.fast"),
        "ema_slow": _get(cfg, "indicators.ema.slow"),
        "swing_lookback": _get(cfg, "indicators.swing.lookback"),
    }
    values.update({k: v for k, v in mapped.items() if v is not None})
    return SetupConfig(**values)


def protection_config(cfg: dict):
    from protection import ProtectionConfig
    return ProtectionConfig(
        max_spread_pips=_first(cfg, ["spread.max_spread_pips",
                                     "protection.max_spread_pips"], 2.0),
        spread_median_multiple=_first(cfg, ["spread.max_median_multiplier",
                                            "protection.spread_median_multiple"], 1.5),
        spread_atr_max=_first(cfg, ["spread.max_atr_ratio",
                                    "protection.spread_atr_max"], 0.10),
        spread_history=_first(cfg, ["spread.median_window",
                                    "protection.spread_history"], 50),
        stop_min_spread_multiple=_first(cfg, ["stops.minimum_spread_multiple",
                                              "protection.stop_min_spread_multiple"], 2.0),
        stop_max_atr_multiple=_first(cfg, ["stops.maximum_atr_multiple",
                                           "protection.stop_max_atr_multiple"], 1.5),
        min_stop_pips=_first(cfg, ["stops.minimum_stop_distance_pips"], 0.0),
        consecutive_loss_limit=_first(cfg, ["loss_protection.consecutive_loss_limit",
                                            "protection.consecutive_loss_limit"], 2),
        cooldown_scope=_cooldown_scope(cfg),
        cooldown_minutes=_cooldown_minutes(cfg),
        max_trades_per_session=_first(cfg, ["risk.max_trades_per_session",
                                            "session.max_trades_per_session"], 3),
        max_concurrent_trades=_first(cfg, ["risk.max_open_positions",
                                           "risk.max_concurrent_trades",
                                           "session.max_open_positions"], 1),
        daily_loss_pct=_first(cfg, ["loss_protection.max_session_drawdown_pct",
                                    "risk.max_daily_loss_pct"], 0.05),
        account_drawdown_pct=_first(cfg, ["loss_protection.max_rolling_account_drawdown_pct",
                                          "risk.account_drawdown_pct"], 0.10),
        max_candle_age_seconds=_first(cfg, ["data.maximum_candle_age_seconds",
                                            "protection.max_candle_age_seconds"], 1800),
        news_blackout_windows=_first(cfg, ["news.blackout_windows",
                                           "protection.news_blackout_windows"], []) or [],
        news_blackout_minutes_before=_first(cfg, ["news.blackout_minutes_before",
                                                  "protection.news_blackout_minutes_before"], 30),
        news_blackout_minutes_after=_first(cfg, ["news.blackout_minutes_after",
                                                 "protection.news_blackout_minutes_after"], 30),
    )


def _cooldown_scope(cfg: dict) -> str:
    """`stop_session_on_consecutive_loss_limit` and a candle cooldown coexist."""
    if _get(cfg, "loss_protection.stop_session_on_consecutive_loss_limit", True):
        return "session"
    if _get(cfg, "loss_protection.cooldown_after_loss.enabled"):
        return "minutes"
    return _first(cfg, ["protection.cooldown_scope"], "session")


def _cooldown_minutes(cfg: dict) -> int:
    """A candle-based cooldown is expressed in minutes internally."""
    block = _get(cfg, "loss_protection.cooldown_after_loss") or {}
    if block.get("enabled"):
        tf = str(block.get("timeframe", "M15")).upper()
        candles = int(block.get("candles", 1))
        if tf in TIMEFRAME_SECONDS:
            return max(1, candles * TIMEFRAME_SECONDS[tf] // 60)
    return int(_first(cfg, ["protection.cooldown_minutes"], 0))


def management_config(cfg: dict):
    from trade_management import ManagementConfig
    return ManagementConfig(
        atr_stop_multiple=_first(cfg, ["stops.atr.multiple",
                                       "management.atr_stop_multiple"], 0.5),
        breakeven_at_r=_first(cfg, ["position_management.break_even.minimum_r",
                                    "management.breakeven_at_r"], 1.0),
        breakeven_offset_r=_first(cfg, ["position_management.break_even.offset_r",
                                        "management.breakeven_offset_r"], 0.0),
        trail_start_r=_first(cfg, ["position_management.trailing_stop.activation_r",
                                   "management.trail_start_r"], 1.5),
        trail_distance_r=_first(cfg, ["position_management.trailing_stop.distance_r",
                                      "management.trail_distance_r"], 1.0),
        target_r=_first(cfg, ["targets.trend.minimum_rr", "management.target_r"], 2.0),
    )


def risk_config(cfg: dict, norm: Normalised):
    from risk_manager import RiskConfig
    return RiskConfig(
        risk_per_trade_pct=norm.risk_pct,
        max_daily_loss_pct=_first(cfg, ["loss_protection.max_session_drawdown_pct",
                                        "risk.max_daily_loss_pct"], 0.05),
        max_open_positions=_first(cfg, ["risk.max_open_positions",
                                        "risk.max_concurrent_trades",
                                        "session.max_open_positions"], 1),
        max_lot_size=_first(cfg, ["risk.max_lot_size"], 1.0),
        broker_utc_offset_hours=_first(cfg, ["broker.utc_offset_hours",
                                             "risk.broker_utc_offset_hours"], 0),
    )


def pip_for(cfg: dict, symbol: str) -> tuple:
    """Pip size and per-lot value, from either pip.default.* or pip.*."""
    import math
    overrides = _first(cfg, ["pip.overrides"], {}) or {}
    entry = overrides.get(symbol) or {}
    size = entry.get("size", _first(cfg, ["pip.default.size", "pip.size"]))
    value = entry.get("value_per_lot",
                      _first(cfg, ["pip.default.value_per_lot", "pip.value_per_lot"]))
    if size is None or value is None:
        raise ConfigError(f"no pip size/value configured for {symbol}.")

    if "size" not in entry and len(symbol) == 6 and symbol.isalpha():
        expected = 0.01 if symbol[-3:].upper() == "JPY" else 0.0001
        if not math.isclose(float(size), expected, rel_tol=1e-9):
            raise ConfigError(
                f"pip size {size} is wrong for {symbol} (expected {expected}). "
                f"Add pip.overrides.{symbol}.size explicitly.")
    if float(size) <= 0 or float(value) <= 0:
        raise ConfigError(f"pip size/value for {symbol} must be > 0.")
    return float(size), float(value)


def enabled_setups(cfg: dict) -> list:
    """Setup names that are switched on, from either layout."""
    import setups
    flat = _get(cfg, "setups.enabled")
    if flat:
        return [s for s in flat if s in setups.ALL_SETUPS]
    strategies = _get(cfg, "strategies", {}) or {}
    return [name for name in setups.ALL_SETUPS
            if (strategies.get(name) or {}).get("enabled", False)]


def regime_map(cfg: dict) -> dict:
    """Which setups may run in which regime, from strategies.*.allowed_regimes."""
    import regime as regime_mod
    import setups
    names = {"TREND": (regime_mod.TREND_UP, regime_mod.TREND_DOWN),
             "BULLISH_TREND": (regime_mod.TREND_UP,),
             "BEARISH_TREND": (regime_mod.TREND_DOWN,),
             "RANGE": (regime_mod.RANGE,),
             "BREAKOUT": (regime_mod.BREAKOUT,)}
    strategies = _get(cfg, "strategies", {}) or {}
    mapping: dict = {}
    for setup_name in setups.ALL_SETUPS:
        allowed = (strategies.get(setup_name) or {}).get("allowed_regimes")
        if not allowed:
            continue
        for label in allowed:
            for state in names.get(str(label).upper(), ()):
                mapping.setdefault(state, []).append(setup_name)
    return {k: tuple(v) for k, v in mapping.items()}


def min_score(cfg: dict) -> float:
    return float(_first(cfg, ["scoring.execution_threshold", "scoring.min_score"], 80))
