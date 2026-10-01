"""
setups.py — the six entry patterns of LATHE SETUP RULES v1.

    A  Trend Pullback   long     regime = bullish trend
    B  Trend Pullback   short    regime = bearish trend
    C  Range Reversion  long     regime = range, bottom 20% of the range
    D  Range Reversion  short    regime = range, top 20% of the range
    E  Breakout+Retest  long     12-bar M15 high broken, then retested
    F  Breakout+Retest  short    12-bar M15 low broken, then retested

Each detector returns (SetupCandidate, reason) or (None, reason). A None
candidate is a normal "nothing here" outcome, and its reason is logged so every
evaluated bar is accounted for.

Constants taken verbatim from the spec are marked SPEC. Values the spec does
not give are marked REVIEW and are configurable.
"""
from dataclasses import dataclass, field

import numpy as np

from indicators import (atr, ema, highest_high, lowest_low, median_range, rsi,
                        swing_high, swing_low)

TREND_PULLBACK = "trend_pullback"
RANGE_REVERSION = "range_reversion"
BREAKOUT_RETEST = "breakout_retest"

ALL_SETUPS = (TREND_PULLBACK, RANGE_REVERSION, BREAKOUT_RETEST)

# Which score class each setup uses for its reward:risk band.
SETUP_CLASS = {
    TREND_PULLBACK: "trend",
    BREAKOUT_RETEST: "breakout",
    RANGE_REVERSION: "range",
}

MIN_RR = {TREND_PULLBACK: 2.0, BREAKOUT_RETEST: 2.0, RANGE_REVERSION: 1.5}   # SPEC


@dataclass
class SetupConfig:
    # --- from the spec ---
    pullback_structure_atr: float = 0.25    # SPEC valid pullback distance
    entry_buffer_atr: float = 0.05          # SPEC entry beyond the trigger
    stop_buffer_atr: float = 0.20           # SPEC stop beyond structure
    trigger_bars: int = 3                   # SPEC 3-bar M5 break
    order_expiry_bars: int = 3              # SPEC cancel after 3 M5 candles
    breakout_lookback: int = 12             # SPEC 12-bar M15 structure
    breakout_buffer_atr: float = 0.10       # SPEC close beyond the level
    breakout_expansion: float = 1.20        # SPEC candle range vs median
    breakout_median_bars: int = 20          # SPEC median of previous 20
    retest_max_bars: int = 3                # SPEC retest window
    retest_distance_atr: float = 0.15       # SPEC retest proximity
    rsi_period: int = 14                    # SPEC
    rsi_long_max: float = 35.0              # SPEC
    rsi_short_min: float = 65.0             # SPEC
    range_zone_pct: float = 0.20            # SPEC outer 20% of the range
    range_target_pct: float = 0.80          # SPEC ~80% toward the far side
    target_r: float = 2.0                   # SPEC base target for trend/breakout
    # --- not given by the spec ---
    swing_lookback: int = 2                 # REVIEW bars either side of a swing
    range_lookback: int = 20                # REVIEW bars defining "recent range structure"
    ema_fast: int = 20                      # REVIEW M15 EMAs used as structure
    ema_slow: int = 50
    atr_period: int = 14
    # Location score bands, as a fraction of the setup's own validity distance.
    location_excellent: float = 0.40        # REVIEW <= 40% of the limit -> 20 pts
    location_good: float = 0.70             # REVIEW <= 70% of the limit -> 15 pts


@dataclass
class SetupCandidate:
    setup: str
    direction: str                 # "buy" or "sell"
    entry_price: float
    stop_price: float
    target_price: float
    structure_level: float
    location_score: int = 0
    location_reason: str = ""
    trigger_score: int = 0
    trigger_reason: str = ""
    expiry_bars: int = 3
    notes: list = field(default_factory=list)

    @property
    def setup_class(self) -> str:
        return SETUP_CLASS.get(self.setup, "trend")

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def reward_distance(self) -> float:
        return abs(self.target_price - self.entry_price)

    @property
    def rr(self) -> float:
        risk = self.risk_distance
        return self.reward_distance / risk if risk > 0 else 0.0

    def meets_min_rr(self) -> bool:
        return self.rr >= MIN_RR.get(self.setup, 2.0)


# --- shared helpers ---------------------------------------------------------
def _location_points(distance: float, limit: float, cfg: SetupConfig,
                     label: str) -> tuple[int, str]:
    """Category 2 of the score, banded against the setup's own validity limit."""
    if limit <= 0 or distance > limit:
        return 0, f"{label}: {distance:.5f} beyond the {limit:.5f} limit"
    ratio = distance / limit
    if ratio <= cfg.location_excellent:
        return 20, f"{label}: directly at structure ({ratio:.0%} of limit)"
    if ratio <= cfg.location_good:
        return 15, f"{label}: close to structure ({ratio:.0%} of limit)"
    return 10, f"{label}: valid but loose ({ratio:.0%} of limit)"


def _three_bar_break(m5, cfg: SetupConfig, up: bool) -> tuple[bool, float, str]:
    """
    SPEC: the last completed M5 candle closes beyond the extreme of the
    previous `trigger_bars` completed candles. Returns (fired, level, why).
    """
    n = cfg.trigger_bars
    if len(m5) < n + 1:
        return False, float("nan"), "not enough M5 history for the trigger"
    prior = m5[-(n + 1):-1]
    close = float(m5["close"][-1])
    if up:
        level = float(np.max(prior["high"]))
        return (close > level, level,
                f"M5 close {close:.5f} {'>' if close > level else '<='} "
                f"{n}-bar high {level:.5f}")
    level = float(np.min(prior["low"]))
    return (close < level, level,
            f"M5 close {close:.5f} {'<' if close < level else '>='} "
            f"{n}-bar low {level:.5f}")


def _higher_low(m5, cfg: SetupConfig) -> bool:
    """SPEC: price creates a higher low before a long trigger."""
    latest = swing_low(m5, cfg.swing_lookback)
    if latest is None:
        return False
    prior = swing_low(m5, cfg.swing_lookback, index=latest[0] - 1)
    return prior is not None and latest[1] > prior[1]


def _lower_high(m5, cfg: SetupConfig) -> bool:
    """SPEC: price creates a lower high before a short trigger."""
    latest = swing_high(m5, cfg.swing_lookback)
    if latest is None:
        return False
    prior = swing_high(m5, cfg.swing_lookback, index=latest[0] - 1)
    return prior is not None and latest[1] < prior[1]


def _trigger_points(structure_ok: bool, break_fired: bool,
                    why: str) -> tuple[int, str]:
    """
    Category 3. SPEC: a trade cannot execute unless this is exactly 20.
    20 = full trigger, 10 = forming, 0 = none.
    """
    if structure_ok and break_fired:
        return 20, f"trigger confirmed — {why}"
    if structure_ok or break_fired:
        return 10, f"trigger forming — {why}"
    return 0, f"no trigger — {why}"


def _atr_of(candles, cfg: SetupConfig) -> float:
    series = atr(candles, cfg.atr_period)
    return float(series[-1]) if len(series) and not np.isnan(series[-1]) else float("nan")


def _ready(candles, need: int) -> bool:
    return candles is not None and len(candles) >= need


# --- A / B: trend pullback --------------------------------------------------
def _trend_pullback(m15, m5, regime, cfg: SetupConfig, long: bool):
    need = max(cfg.ema_slow, cfg.atr_period) + 5
    if not _ready(m15, need) or not _ready(m5, cfg.trigger_bars + cfg.swing_lookback * 3):
        return None, "not enough candle history for a trend pullback"

    atr_m15 = _atr_of(m15, cfg)
    atr_m5 = _atr_of(m5, cfg)
    if np.isnan(atr_m15) or np.isnan(atr_m5):
        return None, "ATR not warmed up"

    closes15 = np.asarray(m15["close"], dtype=float)
    ema_fast15 = float(ema(closes15, cfg.ema_fast)[-1])
    ema_slow15 = float(ema(closes15, cfg.ema_slow)[-1])
    close15 = float(closes15[-1])

    # SPEC invalidation: M15 closing beyond the far EMA voids the pullback.
    if long and close15 < ema_slow15:
        return None, f"M15 close {close15:.5f} below EMA{cfg.ema_slow} — pullback invalidated"
    if not long and close15 > ema_slow15:
        return None, f"M15 close {close15:.5f} above EMA{cfg.ema_slow} — pullback invalidated"

    # SPEC: H1 close must stay the right side of its EMA20.
    if long and regime.close < regime.ema_fast:
        return None, "H1 close is below EMA20 — bullish pullback invalid"
    if not long and regime.close > regime.ema_fast:
        return None, "H1 close is above EMA20 — bearish pullback invalid"

    # Structure: the M15 EMA20, plus recent swing structure on the trade's side.
    structures = [(ema_fast15, f"EMA{cfg.ema_fast}")]
    swing = swing_low(m15, cfg.swing_lookback) if long else swing_high(m15, cfg.swing_lookback)
    if swing is not None:
        structures.append((swing[1], "swing structure"))

    limit = cfg.pullback_structure_atr * atr_m15
    level, label, distance = min(
        ((lv, lb, abs(close15 - lv)) for lv, lb in structures), key=lambda t: t[2])
    if distance > limit:
        return None, (f"price is {distance:.5f} from {label} — beyond the "
                      f"{limit:.5f} pullback limit")

    structure_ok = _higher_low(m5, cfg) if long else _lower_high(m5, cfg)
    fired, trigger_level, why = _three_bar_break(m5, cfg, up=long)
    structure_word = "higher low" if long else "lower high"
    trigger_points, trigger_reason = _trigger_points(
        structure_ok, fired, f"{structure_word} {'yes' if structure_ok else 'no'}; {why}")
    if trigger_points == 0:
        return None, trigger_reason

    trigger_extreme = float(m5["high"][-1]) if long else float(m5["low"][-1])
    if long:
        entry = trigger_extreme + cfg.entry_buffer_atr * atr_m5
        pullback = swing_low(m5, cfg.swing_lookback)
        anchor = pullback[1] if pullback else float(np.min(m5["low"][-10:]))
        stop = anchor - cfg.stop_buffer_atr * atr_m5
        target = entry + cfg.target_r * (entry - stop)
    else:
        entry = trigger_extreme - cfg.entry_buffer_atr * atr_m5
        pullback = swing_high(m5, cfg.swing_lookback)
        anchor = pullback[1] if pullback else float(np.max(m5["high"][-10:]))
        stop = anchor + cfg.stop_buffer_atr * atr_m5
        target = entry - cfg.target_r * (stop - entry)

    loc_points, loc_reason = _location_points(distance, limit, cfg,
                                              f"pullback to {label}")
    return SetupCandidate(
        TREND_PULLBACK, "buy" if long else "sell", entry, stop, target, anchor,
        loc_points, loc_reason, trigger_points, trigger_reason,
        cfg.order_expiry_bars,
        [f"H1 {regime.state}", f"M15 pullback to {label}"],
    ), "candidate"


# --- C / D: range reversion -------------------------------------------------
def _range_reversion(m15, m5, regime, cfg: SetupConfig, long: bool):
    need = max(cfg.range_lookback, cfg.atr_period) + 5
    if not _ready(m15, need) or not _ready(m5, cfg.rsi_period + cfg.trigger_bars + 2):
        return None, "not enough candle history for a range reversion"

    atr_m5 = _atr_of(m5, cfg)
    if np.isnan(atr_m5):
        return None, "ATR not warmed up"

    range_high = highest_high(m15, cfg.range_lookback)
    range_low = lowest_low(m15, cfg.range_lookback)
    size = range_high - range_low
    if not np.isfinite(size) or size <= 0:
        return None, "no measurable range structure"

    zone = cfg.range_zone_pct * size
    price = float(m5["close"][-1])
    if long:
        in_zone = price <= range_low + zone
        distance = abs(price - range_low)
        boundary, label = range_low, "range low"
    else:
        in_zone = price >= range_high - zone
        distance = abs(range_high - price)
        boundary, label = range_high, "range high"
    if not in_zone:
        return None, (f"price {price:.5f} is not in the outer "
                      f"{cfg.range_zone_pct:.0%} of the range "
                      f"[{range_low:.5f}, {range_high:.5f}]")

    rsi_m5 = float(rsi(np.asarray(m5["close"], dtype=float), cfg.rsi_period)[-1])
    if np.isnan(rsi_m5):
        return None, "RSI not warmed up"
    if long and rsi_m5 >= cfg.rsi_long_max:
        return None, f"RSI {rsi_m5:.1f} not below {cfg.rsi_long_max}"
    if not long and rsi_m5 <= cfg.rsi_short_min:
        return None, f"RSI {rsi_m5:.1f} not above {cfg.rsi_short_min}"

    # SPEC: the candle must reject the boundary and close back inside the range.
    low5, high5 = float(m5["low"][-1]), float(m5["high"][-1])
    if long:
        rejected = low5 <= range_low + zone and price > low5
        inside = price > range_low
    else:
        rejected = high5 >= range_high - zone and price < high5
        inside = price < range_high
    structure_ok = bool(rejected and inside)
    why = (f"RSI {rsi_m5:.1f}; rejection {'yes' if rejected else 'no'}; "
           f"closed back inside {'yes' if inside else 'no'}")
    trigger_points, trigger_reason = _trigger_points(structure_ok, structure_ok, why)
    if trigger_points == 0:
        return None, trigger_reason

    if long:
        entry = high5 + cfg.entry_buffer_atr * atr_m5
        stop = range_low - cfg.stop_buffer_atr * atr_m5
        target = range_low + cfg.range_target_pct * size
    else:
        entry = low5 - cfg.entry_buffer_atr * atr_m5
        stop = range_high + cfg.stop_buffer_atr * atr_m5
        target = range_high - cfg.range_target_pct * size

    loc_points, loc_reason = _location_points(distance, zone, cfg, f"distance to {label}")
    return SetupCandidate(
        RANGE_REVERSION, "buy" if long else "sell", entry, stop, target, boundary,
        loc_points, loc_reason, trigger_points, trigger_reason,
        cfg.order_expiry_bars,
        [f"range [{range_low:.5f}, {range_high:.5f}]", f"RSI {rsi_m5:.1f}"],
    ), "candidate"


# --- E / F: breakout + retest ----------------------------------------------
def _breakout_retest(m15, m5, regime, cfg: SetupConfig, long: bool):
    need = cfg.breakout_median_bars + cfg.breakout_lookback + cfg.retest_max_bars + 5
    if not _ready(m15, need) or not _ready(m5, cfg.trigger_bars + 2):
        return None, "not enough candle history for a breakout retest"

    atr_m15 = _atr_of(m15, cfg)
    atr_m5 = _atr_of(m5, cfg)
    if np.isnan(atr_m15) or np.isnan(atr_m5):
        return None, "ATR not warmed up"

    # Look back over the retest window for a qualifying breakout candle.
    for bars_ago in range(1, cfg.retest_max_bars + 1):
        idx = len(m15) - 1 - bars_ago
        if idx <= cfg.breakout_lookback:
            continue
        prior = m15[idx - cfg.breakout_lookback:idx]
        level = (float(np.max(prior["high"])) if long else float(np.min(prior["low"])))
        bar = m15[idx]
        close = float(bar["close"])
        buffer_ = cfg.breakout_buffer_atr * atr_m15
        broke = (close > level + buffer_) if long else (close < level - buffer_)
        if not broke:
            continue

        med = median_range(m15[:idx + 1], cfg.breakout_median_bars)
        bar_range = float(bar["high"]) - float(bar["low"])
        if not np.isfinite(med) or bar_range < cfg.breakout_expansion * med:
            return None, (f"breakout candle range {bar_range:.5f} below "
                          f"{cfg.breakout_expansion}x median {med:.5f}")

        # SPEC: the retest must come back to the level and close the right side.
        last_close = float(m15["close"][-1])
        distance = abs(last_close - level)
        limit = cfg.retest_distance_atr * atr_m15
        if distance > limit:
            return None, (f"retest {distance:.5f} from the broken level, "
                          f"beyond the {limit:.5f} limit")
        held = (last_close > level) if long else (last_close < level)
        if not held:
            return None, "failed breakout — M15 closed back inside the old range"

        fired, _, why = _three_bar_break(m5, cfg, up=long)
        trigger_points, trigger_reason = _trigger_points(True, fired, why)
        if trigger_points == 0:
            return None, trigger_reason

        if long:
            entry = float(m5["high"][-1]) + cfg.entry_buffer_atr * atr_m5
            sw = swing_low(m5, cfg.swing_lookback)
            anchor = sw[1] if sw else float(np.min(m5["low"][-10:]))
            stop = anchor - cfg.stop_buffer_atr * atr_m5
            target = entry + cfg.target_r * (entry - stop)
        else:
            entry = float(m5["low"][-1]) - cfg.entry_buffer_atr * atr_m5
            sw = swing_high(m5, cfg.swing_lookback)
            anchor = sw[1] if sw else float(np.max(m5["high"][-10:]))
            stop = anchor + cfg.stop_buffer_atr * atr_m5
            target = entry - cfg.target_r * (stop - entry)

        loc_points, loc_reason = _location_points(distance, limit, cfg,
                                                  "retest of the broken level")
        return SetupCandidate(
            BREAKOUT_RETEST, "buy" if long else "sell", entry, stop, target, level,
            loc_points, loc_reason, trigger_points, trigger_reason,
            cfg.order_expiry_bars,
            [f"broke {level:.5f} {bars_ago} bar(s) ago", f"retest within {limit:.5f}"],
        ), "candidate"

    return None, f"no qualifying breakout in the last {cfg.retest_max_bars} M15 candles"


# --- dispatch ---------------------------------------------------------------
def detect(setup: str, m15, m5, regime, cfg: SetupConfig = None):
    """Returns (SetupCandidate | None, reason). Direction follows the regime."""
    cfg = cfg or SetupConfig()
    if setup not in ALL_SETUPS:
        return None, f"unknown setup {setup!r}"

    from regime import RANGE, TREND_DOWN, TREND_UP

    if setup == TREND_PULLBACK:
        if regime.state == TREND_UP:
            return _trend_pullback(m15, m5, regime, cfg, long=True)
        if regime.state == TREND_DOWN:
            return _trend_pullback(m15, m5, regime, cfg, long=False)
        return None, "trend pullback needs a trending regime"

    if setup == RANGE_REVERSION:
        if regime.state != RANGE:
            return None, "range reversion needs a ranging regime"
        long_side = _range_reversion(m15, m5, regime, cfg, long=True)
        if long_side[0] is not None:
            return long_side
        short_side = _range_reversion(m15, m5, regime, cfg, long=False)
        if short_side[0] is not None:
            return short_side
        return None, f"long: {long_side[1]}; short: {short_side[1]}"

    if regime.state == TREND_UP:
        return _breakout_retest(m15, m5, regime, cfg, long=True)
    if regime.state == TREND_DOWN:
        return _breakout_retest(m15, m5, regime, cfg, long=False)
    return None, "breakout retest needs a trending regime"


def from_config(cfg: dict) -> SetupConfig:
    s = (cfg.get("setup_params") or {})
    known = SetupConfig().__dict__
    return SetupConfig(**{k: s.get(k, v) for k, v in known.items()})


def configured_setups() -> tuple:
    return ALL_SETUPS


def any_configured() -> bool:
    return True
