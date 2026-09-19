"""
Shared fixtures. This module installs the fake MT5 terminal into sys.modules
BEFORE any bot module is imported — pytest loads conftest first, so by the
time a test imports `trader`, its `import MetaTrader5` resolves to the fake.
"""
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

import pytest  # noqa: E402

import mt5_connector  # noqa: E402
import strategy  # noqa: E402
from mt5_connector import MT5Connector  # noqa: E402
from risk_manager import RiskConfig, RiskManager  # noqa: E402
from state import JsonState  # noqa: E402

M15 = fake_mt5.TIMEFRAME_M15


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
def config():
    return {
        "symbols": ["EURUSD"],
        "timeframe": "M15",
        "poll_seconds": 30,
        "pip": {
            "size": 0.0001,
            "value_per_lot": 10,
            "overrides": {"USDJPY": {"size": 0.01, "value_per_lot": 6.7}},
        },
        "stops": {"stop_loss_pips": 20, "take_profit_pips": 40},
        "risk": {
            "risk_per_trade_pct": 0.01,
            "max_daily_loss_pct": 0.03,
            "max_open_positions": 3,
            "max_lot_size": 1.0,
            "broker_utc_offset_hours": 0,
        },
    }


@pytest.fixture
def risk(tmp_path):
    return RiskManager(RiskConfig(0.01, 0.03, 3, 1.0, 0.0),
                       state=JsonState(str(tmp_path / "state.json")))


@pytest.fixture
def vault(tmp_path):
    return str(tmp_path / "trades.md")


# --- price series helpers ---------------------------------------------------
def crossing_prices():
    """Declines for 60 bars (fast SMA below slow), then rises hard so fast crosses up."""
    down = [1.1000 - i * 0.00020 for i in range(60)]
    up = [down[-1] + (i + 1) * 0.00120 for i in range(12)]
    return down + up


def find_cross_bar(prices):
    """Number of closed bars at which the strategy first returns 'buy'."""
    for i in range(51, len(prices)):
        if strategy.generate_signal(fake_mt5.make_rates(prices[:i]), False) == "buy":
            return i
    raise AssertionError("no buy cross in this price series — fixture is broken")


def find_close_bar(prices):
    """Number of closed bars at which the strategy first returns 'close' while long."""
    for i in range(51, len(prices)):
        if strategy.generate_signal(fake_mt5.make_rates(prices[:i]), True) == "close":
            return i
    raise AssertionError("no close signal in this price series — fixture is broken")


@pytest.fixture
def prices():
    return crossing_prices()


@pytest.fixture
def cross_bar(prices):
    return find_cross_bar(prices)
