"""
regime.py — classify the H1 backdrop as trending, ranging, or neither.

Per the strategy spec: H1 EMA20/EMA50 + ADX14 + ATR14. EMAs give direction,
ADX gives strength, ATR is carried through because the setups size their stops
from it.

The ADX cut-offs are NOT specified in the strategy document. The values in
config.yaml are the conventional Wilder readings (>=25 trending, <=20 ranging)
and are yours to tune. The band between them is deliberately neither: an ADX
of 22 is not evidence of a trend or of a range, and the house rule is that
ambiguity means no trade.
"""
from dataclasses import dataclass

import numpy as np

from indicators import adx as adx_series
from indicators import atr as atr_series
from indicators import ema

TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
RANGE = "range"
UNDEFINED = "undefined"      # not enough data, or between the thresholds


@dataclass
class RegimeConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    adx_period: int = 14
    atr_period: int = 14
    adx_trend_min: float = 25.0     # at or above -> trending
    adx_range_max: float = 20.0     # at or below -> ranging


@dataclass
class Regime:
    state: str
    adx: float
    atr: float
    ema_fast: float
    ema_slow: float
    reason: str

    @property
    def is_trend(self) -> bool:
        return self.state in (TREND_UP, TREND_DOWN)

    @property
    def is_range(self) -> bool:
        return self.state == RANGE

    @property
    def direction(self) -> str | None:
        if self.state == TREND_UP:
            return "buy"
        if self.state == TREND_DOWN:
            return "sell"
        return None


def classify(h1_candles, cfg: RegimeConfig = None) -> Regime:
    """Classify the most recent CLOSED H1 bar. Caller strips the forming bar."""
    cfg = cfg or RegimeConfig()
    closes = np.asarray(h1_candles["close"], dtype=float)

    needed = max(cfg.ema_slow, cfg.adx_period * 2, cfg.atr_period) + 1
    if len(closes) < needed:
        return Regime(UNDEFINED, float("nan"), float("nan"), float("nan"), float("nan"),
                      f"need {needed} H1 bars, have {len(closes)}")

    fast = ema(closes, cfg.ema_fast)[-1]
    slow = ema(closes, cfg.ema_slow)[-1]
    adx_val = adx_series(h1_candles, cfg.adx_period)[0][-1]
    atr_val = atr_series(h1_candles, cfg.atr_period)[-1]

    if np.isnan(fast) or np.isnan(slow) or np.isnan(adx_val) or np.isnan(atr_val):
        return Regime(UNDEFINED, adx_val, atr_val, fast, slow, "indicators not warmed up")

    if adx_val >= cfg.adx_trend_min:
        if fast > slow:
            return Regime(TREND_UP, adx_val, atr_val, fast, slow,
                          f"ADX {adx_val:.1f} >= {cfg.adx_trend_min} and EMA{cfg.ema_fast} > EMA{cfg.ema_slow}")
        if fast < slow:
            return Regime(TREND_DOWN, adx_val, atr_val, fast, slow,
                          f"ADX {adx_val:.1f} >= {cfg.adx_trend_min} and EMA{cfg.ema_fast} < EMA{cfg.ema_slow}")
        return Regime(UNDEFINED, adx_val, atr_val, fast, slow, "EMAs exactly equal")

    if adx_val <= cfg.adx_range_max:
        return Regime(RANGE, adx_val, atr_val, fast, slow,
                      f"ADX {adx_val:.1f} <= {cfg.adx_range_max}")

    return Regime(UNDEFINED, adx_val, atr_val, fast, slow,
                  f"ADX {adx_val:.1f} between {cfg.adx_range_max} and {cfg.adx_trend_min} "
                  f"— neither trending nor ranging")


def from_config(cfg: dict) -> RegimeConfig:
    r = (cfg.get("regime") or {})
    return RegimeConfig(
        ema_fast=r.get("ema_fast", 20),
        ema_slow=r.get("ema_slow", 50),
        adx_period=r.get("adx_period", 14),
        atr_period=r.get("atr_period", 14),
        adx_trend_min=r.get("adx_trend_min", 25.0),
        adx_range_max=r.get("adx_range_max", 20.0),
    )
