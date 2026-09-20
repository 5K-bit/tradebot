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
               "adx_trend_min": 25.0, "adx_range_max": 20.0},
    "setups": {"enabled": list(setups.ALL_SETUPS)},
    "scoring": {"min_score": 80},
    "risk": {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.05,
             "account_drawdown_pct": 0.10, "max_concurrent_trades": 1,
             "max_trades_per_session": 3, "max_lot_size": 1.0,
             "broker_utc_offset_hours": 0},
    "management": {"atr_stop_multiple": 0.5, "breakeven_at_r": 1.0,
                   "breakeven_offset_r": 0.0, "trail_start_r": 1.5,
                   "trail_distance_r": 1.0, "target_r": 2.0},
    "protection": {"max_spread_pips": 2.0, "consecutive_loss_limit": 2,
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


@pytest.fixture
def trending_market(market):
    feed_all_timeframes(market, trending_prices())
    return market


@pytest.fixture
def ranging_market(market):
    feed_all_timeframes(market, ranging_prices())
    return market
