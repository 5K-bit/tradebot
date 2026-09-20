"""
Shared fixtures. This module installs the fake MT5 terminal into sys.modules
BEFORE any bot module is imported — pytest loads conftest first, so by the
time a test imports `trader`, its `import MetaTrader5` resolves to the fake.
"""
import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_mt5

# Unconditional: even on a Windows box with the real package installed, tests
# must never reach an actual terminal or broker.
sys.modules["MetaTrader5"] = fake_mt5

os.environ.setdefault("MT5_LOGIN", "12345678")
os.environ.setdefault("MT5_PASSWORD", "not-a-real-password")
os.environ.setdefault("MT5_SERVER", "Fake-Demo01")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import mt5_connector  # noqa: E402
import setups  # noqa: E402
from mt5_connector import MT5Connector  # noqa: E402
from state import JsonState  # noqa: E402

H1 = fake_mt5.TIMEFRAME_H1
M15 = fake_mt5.TIMEFRAME_M15
M5 = fake_mt5.TIMEFRAME_M5

# A Wednesday inside the 22:00-06:00 New York window (EST, UTC-5).
IN_SESSION_UTC = "2026-01-15T03:30:00+00:00"
OUT_OF_SESSION_UTC = "2026-01-15T18:00:00+00:00"

BASE_CONFIG = {
    "symbols": ["EURUSD", "USDJPY"],
    "timeframes": {"regime": "H1", "setup": "M15", "entry": "M5"},
    "poll_seconds": 30,
    "session": {"timezone": "America/New_York", "start": "22:00", "end": "06:00"},
    "regime": {"ema_fast": 20, "ema_slow": 50, "adx_period": 14, "atr_period": 14,
               "trend_adx_min": 20.0, "trend_separation_atr": 0.25,
               "range_adx_max": 18.0, "range_separation_atr": 0.20},
    "setups": {"enabled": list(setups.ALL_SETUPS)},
    "setup_params": {},
    "scoring": {"min_score": 80},
    "risk": {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.05,
             "account_drawdown_pct": 0.10, "max_concurrent_trades": 1,
             "max_trades_per_session": 3, "max_lot_size": 1.0,
             "broker_utc_offset_hours": 0},
    "management": {"atr_stop_multiple": 0.5, "breakeven_at_r": 1.0,
                   "breakeven_offset_r": 0.0, "trail_start_r": 1.5,
                   "trail_distance_r": 1.0, "target_r": 2.0},
    "protection": {"max_spread_pips": 20.0, "consecutive_loss_limit": 2,
                   "spread_median_multiple": 1.5, "spread_atr_max": 0.10,
                   "stop_min_spread_multiple": 2.0, "stop_max_atr_multiple": 1.5,
                   "cooldown_scope": "session", "cooldown_minutes": 0,
                   "max_candle_age_seconds": 1800,
                   "news_blackout_windows": [],
                   "news_blackout_minutes_before": 30,
                   "news_blackout_minutes_after": 30},
    "pip": {"size": 0.0001, "value_per_lot": 10,
            "overrides": {"USDJPY": {"size": 0.01, "value_per_lot": 6.7}}},
}


@pytest.fixture(autouse=True)
def market():
    """A fresh market per test, with reconnect backoff removed so tests don't sleep."""
    m = fake_mt5.reset()
    mt5_connector.RECONNECT_BACKOFF_SECONDS = (0, 0, 0)
    yield m


@pytest.fixture
def conn(market):
    c = MT5Connector()
    c.connect(quiet=True)
    return c


@pytest.fixture
def config(tmp_path):
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["state"] = {"path": str(tmp_path / "state.json")}
    cfg["vault"] = {"log_path": str(tmp_path / "trades.md")}
    return cfg


@pytest.fixture
def state(config):
    return JsonState(config["state"]["path"])


@pytest.fixture
def vault(config):
    return config["vault"]["log_path"]


@pytest.fixture
def lathe(config, conn, state):
    import trader
    return trader.Lathe(config, conn, state)


def read_log(path):
    p = Path(path)
    return p.read_text() if p.exists() else ""


# --- price series helpers ---------------------------------------------------
def trending_prices(n=160, start=100.0, step=0.20):
    """A clean uptrend: EMA20 > EMA50 and a high ADX."""
    return [start + i * step for i in range(n)]


def ranging_prices(n=160, mid=100.0, amp=0.35):
    """An oscillation with no direction: low ADX."""
    rng = np.random.default_rng(11)
    return [mid + np.sin(i / 4.0) * amp + rng.normal(0, 0.02) for i in range(n)]


def feed_all_timeframes(market, prices, forming=None):
    """Give every timeframe the same series, which is enough for most tests."""
    forming = forming if forming is not None else prices[-1] + 0.01
    for tf in (H1, M15, M5):
        market.set_series(tf, prices, forming)
    # symbol_info_tick() prices off these, and the spread gate reads the tick.
    market.closed_prices = list(prices)
    market.forming_price = forming


def feed_candles(market, h1=None, m15=None, m5=None, spread=None):
    """
    Drive each timeframe with its own OHLC scenario.

    The fake stores closing prices per timeframe, so the OHLC arrays from
    scenarios.py are installed directly and the tick price is taken from the
    M5 series, which is what the spread gate and entry prices read.
    """
    import numpy as np
    for tf, candles in ((H1, h1), (M15, m15), (M5, m5)):
        if candles is None:
            continue
        # The pipeline drops the newest bar as still-forming, so append a
        # neutral one. Without it the scenario's meaningful final bar — the
        # trigger candle, the EMA touch — is the bar that gets thrown away.
        market.ohlc[tf] = _with_forming_bar(candles)
    if m5 is not None:
        closes = [float(x) for x in np.asarray(m5["close"], dtype=float)]
        market.closed_prices = closes
        market.forming_price = closes[-1]
        if spread is None:
            # The spec caps spread at 10% of ATR_M5, so a fixed pip value would
            # fail on any scenario with a small ATR. Derive a realistic one.
            from indicators import atr as _atr
            a = float(_atr(m5, 14)[-1])
            spread = a * 0.05 if a == a and a > 0 else 0.00008
    market.spread = spread if spread is not None else 0.00008


def _with_forming_bar(candles):
    """Append a doji at the last close, standing in for the in-progress bar."""
    import numpy as np
    out = np.zeros(len(candles) + 1, dtype=candles.dtype)
    out[:-1] = candles
    last = candles[-1]
    step = int(candles[1]["time"] - candles[0]["time"]) if len(candles) > 1 else 900
    close = float(last["close"])
    out[-1] = (int(last["time"]) + step, close, close, close, close, 100, 10, 0)
    return out


@pytest.fixture
def trending_setup(market):
    """A market where the trend-pullback setup fires with a full trigger."""
    import scenarios as S
    feed_candles(market, h1=S.h1_uptrend(), m15=S.m15_pullback_to_ema(),
                 m5=S.m5_full_long_trigger())
    return market


@pytest.fixture
def trending_market(market):
    feed_all_timeframes(market, trending_prices())
    return market


@pytest.fixture
def ranging_market(market):
    feed_all_timeframes(market, ranging_prices())
    return market
