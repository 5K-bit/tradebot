"""
risk_manager.py — the non-negotiable safety layer.

This module decides HOW MUCH to trade and WHEN TO STOP TRADING ALTOGETHER.
Every order in trader.py must pass through here first. Tune the numbers
in config.yaml, but do not remove the checks.
"""
from dataclasses import dataclass
from datetime import date


@dataclass
class RiskConfig:
    risk_per_trade_pct: float      # e.g. 0.01 = risk 1% of equity per trade
    max_daily_loss_pct: float      # e.g. 0.03 = halt trading after -3% equity in a day
    max_open_positions: int        # cap on concurrent positions
    max_lot_size: float            # absolute ceiling regardless of calculation


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._day = date.today()
        self._equity_at_day_start = None
        self._halted = False

    def _roll_day_if_needed(self, current_equity: float):
        today = date.today()
        if today != self._day or self._equity_at_day_start is None:
            self._day = today
            self._equity_at_day_start = current_equity
            self._halted = False

    def check_kill_switch(self, current_equity: float) -> bool:
        """Returns True if trading should be HALTED for the rest of the day."""
        self._roll_day_if_needed(current_equity)
        if self._halted:
            return True

        drawdown_pct = (self._equity_at_day_start - current_equity) / self._equity_at_day_start
        if drawdown_pct >= self.config.max_daily_loss_pct:
            self._halted = True
            print(
                f"[risk] KILL SWITCH TRIPPED: daily drawdown {drawdown_pct:.2%} "
                f">= limit {self.config.max_daily_loss_pct:.2%}. Halting new trades until tomorrow."
            )
            return True
        return False

    def position_size(self, equity: float, entry_price: float, stop_price: float,
                       pip_value_per_lot: float, pip_size: float) -> float:
        """
        Calculates lot size so that a stop-out risks exactly risk_per_trade_pct
        of current equity. Clamped to max_lot_size.
        """
        risk_amount = equity * self.config.risk_per_trade_pct
        stop_distance_pips = abs(entry_price - stop_price) / pip_size
        if stop_distance_pips <= 0:
            raise ValueError("Stop distance must be > 0 — a stop-loss is required for every trade.")

        lots = risk_amount / (stop_distance_pips * pip_value_per_lot)
        lots = round(min(lots, self.config.max_lot_size), 2)
        if lots <= 0:
            raise ValueError("Calculated position size rounded to 0 — risk_per_trade_pct or stop too tight.")
        return lots

    def can_open_new_position(self, current_open_count: int) -> bool:
        return current_open_count < self.config.max_open_positions
