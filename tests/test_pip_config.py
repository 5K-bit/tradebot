"""
Pip size must be resolved per symbol. Adding a JPY pair previously inherited
size 0.0001 instead of 0.01, mis-sizing every position on it by 100x.
"""
import copy

import pytest

import trader


def test_defaults_apply_to_a_normal_pair(config):
    assert trader.resolve_pip(config, "EURUSD") == (0.0001, 10)


def test_override_applies_to_a_jpy_pair(config):
    assert trader.resolve_pip(config, "USDJPY") == (0.01, 6.7)


def test_jpy_pair_without_override_is_rejected(config):
    cfg = copy.deepcopy(config)
    cfg["pip"]["overrides"] = {}
    cfg["symbols"] = ["EURUSD", "USDJPY"]
    with pytest.raises(ValueError, match="USDJPY"):
        trader.validate_config(cfg)


def test_eurusd_with_a_jpy_sized_default_is_rejected(config):
    cfg = copy.deepcopy(config)
    cfg["pip"]["size"] = 0.01
    with pytest.raises(ValueError, match="EURUSD"):
        trader.validate_config(cfg)


def test_non_fx_symbols_do_not_false_positive(config):
    """Metals and indices have their own conventions — don't second-guess them."""
    for symbol in ["XAUUSD", "US30", "GER40", "BTCUSD"]:
        cfg = copy.deepcopy(config)
        cfg["symbols"] = [symbol]
        trader.validate_config(cfg)


def test_explicit_override_is_trusted(config):
    """An override is a deliberate statement of intent, even if unusual."""
    cfg = copy.deepcopy(config)
    cfg["symbols"] = ["EURUSD"]
    cfg["pip"]["overrides"] = {"EURUSD": {"size": 0.001, "value_per_lot": 5}}
    trader.validate_config(cfg)
    assert trader.resolve_pip(cfg, "EURUSD") == (0.001, 5)


def test_missing_overrides_section_is_fine(config):
    cfg = copy.deepcopy(config)
    del cfg["pip"]["overrides"]
    assert trader.resolve_pip(cfg, "EURUSD") == (0.0001, 10)
    cfg["pip"]["overrides"] = None
    assert trader.resolve_pip(cfg, "EURUSD") == (0.0001, 10)


def test_nonpositive_pip_values_rejected(config):
    cfg = copy.deepcopy(config)
    cfg["pip"]["overrides"] = {"EURUSD": {"size": 0.0001, "value_per_lot": 0}}
    with pytest.raises(ValueError, match="must be > 0"):
        trader.resolve_pip(cfg, "EURUSD")


@pytest.mark.parametrize("role", ["regime", "setup", "entry"])
def test_unknown_timeframe_rejected_at_startup(config, role):
    cfg = copy.deepcopy(config)
    cfg["timeframes"][role] = "M7"
    with pytest.raises(ValueError, match=f"timeframes.{role}"):
        trader.validate_config(cfg)


def test_missing_timeframe_role_rejected(config):
    cfg = copy.deepcopy(config)
    del cfg["timeframes"]["entry"]
    with pytest.raises(ValueError, match="timeframes.entry"):
        trader.validate_config(cfg)


@pytest.mark.parametrize("pct", [0.005, 0.01, 0.02])
def test_selectable_risk_levels_accepted(config, pct):
    cfg = copy.deepcopy(config)
    cfg["risk"]["risk_per_trade_pct"] = pct
    trader.validate_config(cfg)


@pytest.mark.parametrize("pct", [0.03, 0.1, 0.0, 1.0])
def test_other_risk_levels_rejected(config, pct):
    """The strategy allows 0.5/1/2% only — a typo must not become a 10% risk."""
    cfg = copy.deepcopy(config)
    cfg["risk"]["risk_per_trade_pct"] = pct
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        trader.validate_config(cfg)


def test_unknown_setup_name_rejected(config):
    cfg = copy.deepcopy(config)
    cfg["setups"]["enabled"] = ["trend_pullback", "moon_phase"]
    with pytest.raises(ValueError, match="moon_phase"):
        trader.validate_config(cfg)


def test_bad_session_timezone_rejected(config):
    cfg = copy.deepcopy(config)
    cfg["session"]["timezone"] = "Not/AZone"
    with pytest.raises(Exception):
        trader.validate_config(cfg)


def test_empty_symbol_list_rejected(config):
    cfg = copy.deepcopy(config)
    cfg["symbols"] = []
    with pytest.raises(ValueError, match="no symbols"):
        trader.validate_config(cfg)


def test_shipped_config_is_valid():
    """The config.yaml in the repo must itself pass validation."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
    trader.validate_config(cfg)
