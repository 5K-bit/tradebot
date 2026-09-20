"""
regime.py — classify the H1 backdrop, per LATHE SETUP RULES v1 Rule 1.

    BULLISH TREND   EMA20 > EMA50
                    AND ADX >= 20
                    AND (EMA20 - EMA50) >= 0.25 * ATR

    BEARISH TREND   EMA20 < EMA50
                    AND ADX >= 20
                    AND (EMA50 - EMA20) >= 0.25 * ATR

    RANGE           ADX < 18
                    AND |EMA20 - EMA50| <= 0.20 * ATR

    Anything else   TRANSITION / UNCLEAR  ->  HOLD

The three conditions are deliberately not exhaustive: ADX 19 with wide EMAs is
neither a trend nor a range, and unclear means hold. That gap is the rule, not
an oversight.
"""
from dataclasses import dataclass

import numpy as np

from indicators import adx as adx_series
from indicators import atr as atr_series
from indicators import ema

TREND_UP = "bullish_trend"
TREND_DOWN = "bearish_trend"
RANGE = "range"
UNDEFINED = "unclear"


@dataclass
class RegimeConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    adx_period: int = 14
    atr_period: int = 14
    trend_adx_min: float = 20.0          # Rule 1
    trend_separation_atr: float = 0.25   # Rule 1
    range_adx_max: float = 18.0          # Rule 1
    range_separation_atr: float = 0.20   # Rule 1
    # Score band cut-offs (the spec gives the bands, not the thresholds).
    strong_adx: float = 25.0             # REVIEW
    strong_separation_atr: float = 0.50  # REVIEW
    strong_range_adx: float = 15.0       # REVIEW
    strong_range_separation_atr: float = 0.10   # REVIEW


@dataclass
class Regime:
    state: str
    adx: float
    atr: float
    ema_fast: float
    ema_slow: float
    reason: str
    close: float = float("nan")

    @property
    def is_trend(self) -> bool:
        return self.state in (TREND_UP, TREND_DOWN)

    @property
    def is_range(self) -> bool:
        return self.state == RANGE

    @property
    def is_clear(self) -> bool:
        return self.state != UNDEFINED

    @property
    def direction(self) -> str | None:
        if self.state == TREND_UP:
            return "buy"
        if self.state == TREND_DOWN:
            return "sell"
        return None

    @property
    def separation(self) -> float:
        return abs(self.ema_fast - self.ema_slow)

    @property
    def separation_in_atr(self) -> float:
        return self.separation / self.atr if self.atr > 0 else float("nan")


def classify(h1_candles, cfg: RegimeConfig = None) -> Regime:
    """Classify the most recent CLOSED H1 bar. The caller strips the forming bar."""
    cfg = cfg or RegimeConfig()
    closes = np.asarray(h1_candles["close"], dtype=float)

    needed = max(cfg.ema_slow, cfg.adx_period * 2, cfg.atr_period) + 1
    if len(closes) < needed:
        nan = float("nan")
        return Regime(UNDEFINED, nan, nan, nan, nan,
                      f"need {needed} H1 bars, have {len(closes)}")

    fast = float(ema(closes, cfg.ema_fast)[-1])
    slow = float(ema(closes, cfg.ema_slow)[-1])
    adx_val = float(adx_series(h1_candles, cfg.adx_period)[0][-1])
    atr_val = float(atr_series(h1_candles, cfg.atr_period)[-1])
    close = float(closes[-1])

    if any(np.isnan(v) for v in (fast, slow, adx_val, atr_val)):
        return Regime(UNDEFINED, adx_val, atr_val, fast, slow,
                      "indicators not warmed up", close)

    separation = abs(fast - slow)
    trend_gap = cfg.trend_separation_atr * atr_val
    range_gap = cfg.range_separation_atr * atr_val

    if fast > slow and adx_val >= cfg.trend_adx_min and separation >= trend_gap:
        return Regime(TREND_UP, adx_val, atr_val, fast, slow,
                      f"EMA20>EMA50, ADX {adx_val:.1f} >= {cfg.trend_adx_min}, "
                      f"separation {separation:.5f} >= {trend_gap:.5f}", close)

    if fast < slow and adx_val >= cfg.trend_adx_min and separation >= trend_gap:
        return Regime(TREND_DOWN, adx_val, atr_val, fast, slow,
                      f"EMA20<EMA50, ADX {adx_val:.1f} >= {cfg.trend_adx_min}, "
                      f"separation {separation:.5f} >= {trend_gap:.5f}", close)

    if adx_val < cfg.range_adx_max and separation <= range_gap:
        return Regime(RANGE, adx_val, atr_val, fast, slow,
                      f"ADX {adx_val:.1f} < {cfg.range_adx_max}, EMAs compressed "
                      f"({separation:.5f} <= {range_gap:.5f})", close)

    return Regime(UNDEFINED, adx_val, atr_val, fast, slow,
                  f"unclear: ADX {adx_val:.1f}, EMA separation {separation:.5f} "
                  f"({separation / atr_val:.2f} ATR) satisfies neither the trend "
                  f"nor the range rule", close)


def regime_score(r: Regime, cfg: RegimeConfig = None) -> tuple[int, str]:
    """
    Category 1 of the signal score: 0-25.

    The spec gives the bands (25 strong / 20 moderate / 10 marginal / 0 invalid)
    but not the cut-offs. A regime that is not clear never reaches scoring —
    unclear is HOLD — so in practice this returns 25 or 20.
    """
    cfg = cfg or RegimeConfig()
    if not r.is_clear:
        return 0, "regime invalid or contradictory"

    if r.is_trend:
        strong = (r.adx >= cfg.strong_adx
                  and r.separation >= cfg.strong_separation_atr * r.atr)
        if strong:
            return 25, (f"strong trend: ADX {r.adx:.1f} >= {cfg.strong_adx}, "
                        f"separation {r.separation_in_atr:.2f} ATR")
        return 20, f"valid trend, moderate strength (ADX {r.adx:.1f})"

    strong = (r.adx < cfg.strong_range_adx
              and r.separation <= cfg.strong_range_separation_atr * r.atr)
    if strong:
        return 25, (f"strong range: ADX {r.adx:.1f} < {cfg.strong_range_adx}, "
                    f"EMAs tightly compressed")
    return 20, f"valid range, moderate compression (ADX {r.adx:.1f})"


def from_config(cfg: dict) -> RegimeConfig:
    r = (cfg.get("regime") or {})
    return RegimeConfig(
        ema_fast=r.get("ema_fast", 20),
        ema_slow=r.get("ema_slow", 50),
        adx_period=r.get("adx_period", 14),
        atr_period=r.get("atr_period", 14),
        trend_adx_min=r.get("trend_adx_min", 20.0),
        trend_separation_atr=r.get("trend_separation_atr", 0.25),
        range_adx_max=r.get("range_adx_max", 18.0),
        range_separation_atr=r.get("range_separation_atr", 0.20),
        strong_adx=r.get("strong_adx", 25.0),
        strong_separation_atr=r.get("strong_separation_atr", 0.50),
        strong_range_adx=r.get("strong_range_adx", 15.0),
        strong_range_separation_atr=r.get("strong_range_separation_atr", 0.10),
    )
