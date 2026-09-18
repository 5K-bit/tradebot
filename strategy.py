"""
strategy.py — YOUR entry/exit logic goes here.

generate_signal() is called once per new candle close, per symbol — not
once per poll — and only ever sees COMPLETED candles.
Return one of: "buy", "sell", "close", or None (do nothing).

The placeholder below is a simple moving-average crossover so the pipeline
runs end-to-end. Replace the body of generate_signal() with your actual
rules — indicators, price action conditions, session filters, whatever
you want. Everything else in the project (risk sizing, order execution,
logging) stays the same regardless of what's in here.
"""
import numpy as np


def _sma(closes: np.ndarray, period: int) -> float:
    return closes[-period:].mean()


def generate_signal(candles, in_position: bool) -> str | None:
    """
    candles: numpy structured array from MT5 (fields: time, open, high, low,
             close, tick_volume, spread, real_volume), oldest first. The
             still-forming bar has already been stripped, so candles[-1] is
             the most recently CLOSED candle and its values will not change.
    in_position: whether a position opened by THIS bot is currently open for
             this symbol (your own manual trades are not counted).

    Returns: "buy" | "sell" | "close" | None
    """
    closes = candles["close"]
    if len(closes) < 50:
        return None  # not enough history yet

    fast = _sma(closes, 10)
    slow = _sma(closes, 50)
    prev_fast = _sma(closes[:-1], 10)
    prev_slow = _sma(closes[:-1], 50)

    crossed_up = prev_fast <= prev_slow and fast > slow
    crossed_down = prev_fast >= prev_slow and fast < slow

    if not in_position and crossed_up:
        return "buy"
    if not in_position and crossed_down:
        return "sell"
    if in_position and (crossed_down or crossed_up):
        # placeholder exit rule: opposite cross closes the position
        return "close"
    return None


def stop_loss_price(entry_price: float, direction: str, pip_size: float,
                     stop_pips: float) -> float:
    """Simple fixed-pip stop. Replace with ATR-based or structural stops if you prefer."""
    if direction == "buy":
        return entry_price - stop_pips * pip_size
    return entry_price + stop_pips * pip_size


def take_profit_price(entry_price: float, direction: str, pip_size: float,
                       tp_pips: float) -> float:
    if direction == "buy":
        return entry_price + tp_pips * pip_size
    return entry_price - tp_pips * pip_size
