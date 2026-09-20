"""
indicators.py — the technical indicators the strategy is defined in terms of.

All functions take a numpy structured array of MT5 rates (fields: time, open,
high, low, close, ...) or a plain close-price array, and return a full-length
array aligned to the input, with np.nan for bars where the indicator is not
yet defined. Returning nan rather than a shorter array keeps index alignment
with the candles, so a caller can never accidentally read an indicator value
against the wrong bar.

ATR and ADX use Wilder's smoothing (RMA), not a simple mean — that is what
"ATR14" and "ADX14" conventionally mean, and using an SMA instead would give
visibly different values on the same data.
"""
import numpy as np


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the SMA of the first `period`."""
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period or period < 1:
        return out

    alpha = 2.0 / (period + 1.0)
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing: the running average used inside ATR, ADX and RSI."""
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period or period < 1:
        return out

    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def true_range(candles) -> np.ndarray:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)."""
    high = np.asarray(candles["high"], dtype=float)
    low = np.asarray(candles["low"], dtype=float)
    close = np.asarray(candles["close"], dtype=float)

    tr = np.full(len(high), np.nan)
    tr[0] = high[0] - low[0]
    if len(high) > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum.reduce([
            high[1:] - low[1:],
            np.abs(high[1:] - prev_close),
            np.abs(low[1:] - prev_close),
        ])
    return tr


def atr(candles, period: int = 14) -> np.ndarray:
    """Average True Range (Wilder)."""
    return rma(true_range(candles), period)


def adx(candles, period: int = 14):
    """
    Average Directional Index (Wilder).

    Returns (adx, plus_di, minus_di), all aligned to the input candles.
    ADX measures trend STRENGTH only — direction comes from +DI vs -DI, or
    from the EMA relationship in regime.py.
    """
    high = np.asarray(candles["high"], dtype=float)
    low = np.asarray(candles["low"], dtype=float)
    n = len(high)

    nan = np.full(n, np.nan)
    if n < period * 2:
        # ADX is an average of DX, which is itself smoothed — it needs roughly
        # two periods of history before it means anything.
        return nan, nan.copy(), nan.copy()

    up_move = np.zeros(n)
    down_move = np.zeros(n)
    up_move[1:] = high[1:] - high[:-1]
    down_move[1:] = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr_ = rma(true_range(candles), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, period) / atr_
        minus_di = 100.0 * rma(minus_dm, period) / atr_
        di_sum = plus_di + minus_di
        dx = 100.0 * np.abs(plus_di - minus_di) / np.where(di_sum == 0, np.nan, di_sum)

    # DX is undefined until the DI values are; rma() would otherwise seed its
    # average from leading nans and poison every later value.
    adx_ = np.full(n, np.nan)
    valid = ~np.isnan(dx)
    if valid.any():
        first = int(np.argmax(valid))
        smoothed = rma(dx[first:], period)
        adx_[first:] = smoothed
    return adx_, plus_di, minus_di


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index (Wilder). Used by the range-reversion setups."""
    values = np.asarray(values, dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out

    delta = np.diff(values)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / np.where(avg_loss == 0, np.nan, avg_loss)
        value = 100.0 - (100.0 / (1.0 + rs))
    # An all-gain window has no losses: RSI is 100, not undefined.
    value = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, value)
    value = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, value)

    out[1:] = value      # diff() shortened the array by one
    return out


def candle_ranges(candles) -> np.ndarray:
    """High-low range of each bar."""
    return np.asarray(candles["high"], dtype=float) - np.asarray(candles["low"], dtype=float)


def median_range(candles, period: int = 20) -> float:
    """Median bar range over the last `period` bars. NaN if there are too few."""
    ranges = candle_ranges(candles)
    if len(ranges) < period or period < 1:
        return float("nan")
    return float(np.median(ranges[-period:]))


def highest_high(candles, period: int) -> float:
    highs = np.asarray(candles["high"], dtype=float)
    if len(highs) < period or period < 1:
        return float("nan")
    return float(highs[-period:].max())


def lowest_low(candles, period: int) -> float:
    lows = np.asarray(candles["low"], dtype=float)
    if len(lows) < period or period < 1:
        return float("nan")
    return float(lows[-period:].min())


def swing_high(candles, lookback: int = 2, index: int | None = None):
    """
    Most recent confirmed swing high at or before `index`.

    A swing high is a bar whose high exceeds the `lookback` bars either side,
    so it is only confirmed once `lookback` bars have printed after it.
    Returns (bar_index, price) or None.
    """
    return _swing(candles, "high", lookback, index, higher=True)


def swing_low(candles, lookback: int = 2, index: int | None = None):
    """Most recent confirmed swing low at or before `index`. See swing_high."""
    return _swing(candles, "low", lookback, index, higher=False)


def _swing(candles, field, lookback, index, higher):
    values = np.asarray(candles[field], dtype=float)
    n = len(values)
    if index is None:
        index = n - 1
    # A swing needs `lookback` bars on its right to be confirmed.
    for i in range(min(index, n - 1) - lookback, lookback - 1, -1):
        left = values[i - lookback:i]
        right = values[i + 1:i + 1 + lookback]
        if len(left) < lookback or len(right) < lookback:
            continue
        if higher and values[i] > left.max() and values[i] > right.max():
            return i, float(values[i])
        if not higher and values[i] < left.min() and values[i] < right.min():
            return i, float(values[i])
    return None
