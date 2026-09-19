"""
risk_manager.py — the non-negotiable safety layer.

This module decides HOW MUCH to trade and WHEN TO STOP TRADING ALTOGETHER.
Every order in trader.py must pass through here first. Tune the numbers
in config.yaml, but do not remove the checks.

The kill switch is persisted (see state.py): restarting the bot after a bad
day must NOT clear the halt or re-baseline the day's starting equity, or the
daily loss limit can be blown through repeatedly in a single day.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal


@dataclass
class RiskConfig:
    risk_per_trade_pct: float      # e.g. 0.01 = risk 1% of equity per trade
    max_daily_loss_pct: float      # e.g. 0.03 = halt trading after -3% equity in a day
    max_open_positions: int        # cap on concurrent positions
    max_lot_size: float            # absolute ceiling regardless of calculation
    broker_utc_offset_hours: float = 0.0  # broker server timezone, for the daily reset


def _step_decimals(step: float) -> int:
    """Decimal places in a broker volume step, so 7 * 0.01 is 0.07 not 0.07000000000000001."""
    exponent = Decimal(str(step)).as_tuple().exponent
    return max(0, -int(exponent))


class RiskManager:
    def __init__(self, config: RiskConfig, state=None):
        self.config = config
        self.state = state

        # The day boundary follows the BROKER's server day, not the machine's
        # local date — otherwise "daily" loss resets at a different moment
        # than the broker's own trading day rolls over.
        self._day = self.state.get("trading_day") if self.state else None
        self._equity_at_day_start = self.state.get("equity_at_day_start") if self.state else None
        self._halted = bool(self.state.get("halted", False)) if self.state else False

        if self._halted:
            print(
                f"[risk] restored HALTED state for {self._day} from disk — "
                f"no new trades until the broker day rolls over."
            )

    def _trading_day(self) -> str:
        now = datetime.now(timezone.utc) + timedelta(hours=self.config.broker_utc_offset_hours)
        return now.date().isoformat()

    def _persist(self) -> None:
        if self.state is not None:
            self.state.set(
                trading_day=self._day,
                equity_at_day_start=self._equity_at_day_start,
                halted=self._halted,
            )

    def _roll_day_if_needed(self, current_equity: float):
        today = self._trading_day()
        if today != self._day or self._equity_at_day_start is None:
            self._day = today
            self._equity_at_day_start = current_equity
            self._halted = False
            self._persist()

    def check_kill_switch(self, current_equity: float) -> bool:
        """Returns True if trading should be HALTED for the rest of the day."""
        self._roll_day_if_needed(current_equity)
        if self._halted:
            return True

        drawdown_pct = (self._equity_at_day_start - current_equity) / self._equity_at_day_start
        if drawdown_pct >= self.config.max_daily_loss_pct:
            self._halted = True
            self._persist()
            print(
                f"[risk] KILL SWITCH TRIPPED: daily drawdown {drawdown_pct:.2%} "
                f">= limit {self.config.max_daily_loss_pct:.2%}. Halting new trades until tomorrow."
            )
            return True
        return False

    def position_size(self, equity: float, entry_price: float, stop_price: float,
                       pip_value_per_lot: float, pip_size: float,
                       volume_min: float = 0.01, volume_step: float = 0.01,
                       volume_max: float | None = None) -> float:
        """
        Calculates lot size so that a stop-out risks at most risk_per_trade_pct
        of current equity. Clamped to max_lot_size and to the broker's own
        volume limits, then FLOORED to the broker's volume step — rounding up
        would quietly risk more than the configured percentage.
        """
        risk_amount = equity * self.config.risk_per_trade_pct
        stop_distance_pips = abs(entry_price - stop_price) / pip_size
        if stop_distance_pips <= 0:
            raise ValueError("Stop distance must be > 0 — a stop-loss is required for every trade.")

        lots = risk_amount / (stop_distance_pips * pip_value_per_lot)

        ceiling = self.config.max_lot_size
        if volume_max is not None:
            ceiling = min(ceiling, volume_max)
        lots = min(lots, ceiling)

        if volume_step <= 0:
            raise ValueError(f"Broker volume_step must be > 0, got {volume_step}.")
        # round() before floor() so float dust (0.03/0.01 == 2.9999...) doesn't
        # cost a whole step.
        steps = int(round(lots / volume_step, 9) // 1)
        lots = round(steps * volume_step, _step_decimals(volume_step))

        if lots < volume_min or lots <= 0:
            raise ValueError(
                f"Calculated position size {lots} is below the broker minimum {volume_min} — "
                f"risking {self.config.risk_per_trade_pct:.2%} of {equity:.2f} over a "
                f"{stop_distance_pips:.1f} pip stop is too small to trade. "
                f"Widen the stop, raise risk_per_trade_pct, or drop this symbol."
            )
        return lots

    def can_open_new_position(self, current_open_count: int) -> bool:
        return current_open_count < self.config.max_open_positions
