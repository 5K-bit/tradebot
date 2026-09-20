"""
Synthetic markets that exercise each setup.

These are built by measurement, not by eye: m15_pullback_to_ema walks price
back toward the EMA until it is inside the valid-pullback distance, and
h1_true_range uses strongly mean-reverting noise because a smooth sine reads
ADX ~70 (it moves in one direction for many bars, which is exactly what ADX
measures).

The M5 trigger scenarios are written bar by bar rather than from closes,
because a close-only generator opens each bar at the previous close, which
makes a V-bottom span two bars with equal lows — and a swing point needs a
strictly lower low than its neighbours.
"""
import numpy as np

DT = [("time","<i8"),("open","<f8"),("high","<f8"),("low","<f8"),
      ("close","<f8"),("tick_volume","<i8"),("spread","<i4"),("real_volume","<i8")]

def ohlc(rows, bar_seconds=900, t0=0):
    a = np.zeros(len(rows), dtype=DT)
    for i,(o,h,l,c) in enumerate(rows):
        a[i] = (t0 + i*bar_seconds, o, h, l, c, 100, 10, 0)
    return a

def from_closes(closes, wick=0.0004):
    rows=[]
    prev=closes[0]
    for c in closes:
        o=prev; h=max(o,c)+wick; l=min(o,c)-wick; rows.append((o,h,l,c)); prev=c
    return ohlc(rows)

def h1_uptrend(n=160, start=1.0800, step=0.0012):
    return from_closes([start+i*step for i in range(n)], wick=0.0010)

def h1_downtrend(n=160, start=1.1800, step=0.0012):
    return from_closes([start-i*step for i in range(n)], wick=0.0010)

def h1_range(n=160, mid=1.1000, amp=0.0004):
    # Tiny oscillation: ADX low, EMAs compressed.
    return from_closes([mid + amp*np.sin(i/9.0) for i in range(n)], wick=0.0002)

def m15_trend_then_pullback(n=120, start=1.0900, step=0.0008, pull=10):
    """Rise, then a shallow pullback that lands near the EMA20."""
    closes=[start+i*step for i in range(n-pull)]
    top=closes[-1]
    closes += [top - (j+1)*step*0.9 for j in range(pull)]
    return from_closes(closes, wick=0.0003)

def m5_higher_low_breakout(n=60, base=1.0950, step=0.00015):
    """Dip, higher low, then a close above the previous 3-bar high."""
    closes=[base - i*step for i in range(12)]
    low1=closes[-1]
    closes += [low1 + (i+1)*step for i in range(6)]      # bounce
    closes += [closes[-1] - i*step*0.6 for i in range(4)] # higher low
    closes += [closes[-1] + (i+1)*step*1.4 for i in range(8)]  # thrust up
    pad=[closes[0]+0.00005*i for i in range(n-len(closes))]
    return from_closes(pad+closes, wick=0.00008)

def m5_lower_high_breakdown(n=60, base=1.0950, step=0.00015):
    closes=[base + i*step for i in range(12)]
    hi=closes[-1]
    closes += [hi - (i+1)*step for i in range(6)]
    closes += [closes[-1] + i*step*0.6 for i in range(4)]
    closes += [closes[-1] - (i+1)*step*1.4 for i in range(8)]
    pad=[closes[0]-0.00005*i for i in range(n-len(closes))]
    return from_closes(pad+closes, wick=0.00008)


def m15_pullback_to_ema(n=120, start=1.0900, step=0.0006, ema_period=20,
                        atr_period=14, limit_atr=0.25, max_extra=40, down=False):
    """
    Rise (or fall), then walk price back toward the EMA until it is inside the
    valid-pullback distance. Built by measuring, not guessing.
    """
    import numpy as np
    from indicators import ema as _ema, atr as _atr
    closes = [start + ((-1 if down else 1) * i * step) for i in range(n)]
    for _ in range(max_extra):
        c = from_closes(closes, wick=0.0002)
        arr = np.asarray(c["close"], dtype=float)
        e = float(_ema(arr, ema_period)[-1])
        a = float(_atr(c, atr_period)[-1])
        if abs(arr[-1] - e) <= limit_atr * a:
            return c
        closes.append(closes[-1] + (step * 0.55 * (1 if down else -1)))
    return from_closes(closes, wick=0.0002)


def m5_full_long_trigger(base=1.0950, u=0.00020):
    """
    Explicit OHLC: swing low A, rally, HIGHER swing low B, then a close above
    the previous 3-bar high. Both conditions, so trigger_score == 20.

    Written bar by bar because a close-only generator opens each bar at the
    previous close, which makes a V-bottom span two bars with equal lows — and
    a swing low needs a strictly lower low than its neighbours.
    """
    bars = [
        (8.0, 8.2, 7.8, 7.5), (7.5, 7.7, 7.0, 7.2), (7.2, 7.4, 6.5, 6.7),
        (6.7, 6.9, 5.5, 5.8), (5.8, 6.0, 4.5, 4.8), (4.8, 5.0, 3.5, 3.8),
        (3.8, 4.0, 2.5, 2.8), (2.8, 3.0, 1.0, 1.2),
        (1.2, 1.4, 0.0, 0.5),                                  # A: swing low
        (0.5, 1.5, 0.4, 1.4), (1.4, 2.5, 1.3, 2.4),
        (2.4, 3.5, 2.3, 3.4), (3.4, 4.5, 3.3, 4.4), (4.4, 5.0, 4.3, 4.6),
        (4.6, 4.8, 3.8, 4.0), (4.0, 4.2, 3.0, 3.2),
        (3.2, 3.4, 2.4, 2.6),                                  # B: higher low
        (2.6, 3.4, 2.5, 3.3), (3.3, 4.2, 3.2, 4.1),
        (4.1, 5.0, 4.0, 4.9), (4.9, 5.2, 4.8, 5.0),
        (5.0, 7.0, 4.9, 6.8),                                  # 3-bar high break
    ]
    return ohlc([tuple(base + v * u for v in bar) for bar in bars], bar_seconds=300)


def m5_full_short_trigger(base=1.0950, u=0.00020):
    """Mirror of the long trigger: swing high A, LOWER swing high B, 3-bar low break."""
    bars = [
        (0.0, 0.2, -0.2, 0.5), (0.5, 1.0, 0.3, 0.8), (0.8, 1.5, 0.6, 1.3),
        (1.3, 2.5, 1.1, 2.2), (2.2, 3.5, 2.0, 3.2), (3.2, 4.5, 3.0, 4.2),
        (4.2, 5.5, 4.0, 5.2), (5.2, 7.0, 5.0, 6.8),
        (6.8, 8.0, 6.6, 7.5),                                  # A: swing high
        (7.5, 7.6, 6.5, 6.6), (6.6, 6.7, 5.5, 5.6),
        (5.6, 5.7, 4.5, 4.6), (4.6, 4.7, 3.5, 3.6), (3.6, 3.7, 3.4, 3.5),
        (3.5, 4.2, 3.4, 4.0), (4.0, 5.0, 3.9, 4.8),
        (4.8, 5.6, 4.7, 5.4),                                  # B: lower high
        (5.4, 5.5, 4.6, 4.7), (4.7, 4.8, 3.8, 3.9),
        (3.9, 4.0, 3.0, 3.1), (3.1, 3.2, 2.8, 2.9),
        (2.9, 3.0, 1.0, 1.2),                                  # 3-bar low break
    ]
    return ohlc([tuple(base + v * u for v in bar) for bar in bars], bar_seconds=300)


def h1_true_range(n=160, mid=1.1000, seed=1, revert=0.35, sigma=0.00012):
    """
    Strongly mean-reverting noise: ADX below 18 and EMAs compressed.

    A smooth sine will NOT do — it moves in one direction for many bars at a
    time, which is exactly what ADX measures, and reads ~70.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    closes = [mid]
    for _ in range(1, n):
        closes.append(mid + (closes[-1]-mid)*revert + rng.normal(0, sigma))
    return from_closes(closes, wick=0.00018)


def m15_range_structure(n=60, low=1.0980, high=1.1020, seed=5):
    """A clean horizontal range; price ends near the LOW of it."""
    import numpy as np
    rng = np.random.default_rng(seed)
    mid, half = (high+low)/2, (high-low)/2
    closes = [mid + half*0.85*np.sin(i/5.0) + rng.normal(0, half*0.05) for i in range(n-6)]
    # walk down into the bottom zone
    closes += [low + (high-low)*0.30, low + (high-low)*0.20,
               low + (high-low)*0.12, low + (high-low)*0.06,
               low + (high-low)*0.03, low + (high-low)*0.05]
    return from_closes(closes, wick=(high-low)*0.02)


def m5_range_rejection_long(low=1.0980, high=1.1020, n=40):
    """
    A persistent sell-off into the range low, then a candle that wicks the low
    and closes back inside. The decline must be near-monotonic: RSI(14) only
    drops under 35 when losses dominate almost every bar.
    """
    size = high - low
    bars = []
    # Start high in the range and step down on nearly every bar.
    prev = low + size*0.80
    for i in range(n-1):
        c = prev - size*0.022 if i % 7 else prev + size*0.004   # rare small bounce
        h = max(prev, c) + size*0.004
        l = min(prev, c) - size*0.004
        bars.append((prev, h, l, c)); prev = c
    # Rejection candle: wick down into the bottom zone, close back inside.
    bars.append((prev, prev + size*0.03, low + size*0.01, low + size*0.15))
    return ohlc(bars, bar_seconds=300)


def m15_breakout_then_retest(n=60, base=1.0950, step=0.00008, seed=7):
    """12-bar range, an expansion candle through the high, then a retest of it."""
    import numpy as np
    rng = np.random.default_rng(seed)
    bars = []
    prev = base
    for i in range(n):                       # quiet consolidation
        c = base + rng.normal(0, step*2)
        bars.append((prev, max(prev, c)+step, min(prev, c)-step, c)); prev = c
    top = max(b[1] for b in bars[-12:])
    brk_close = top + step*14                # decisive close beyond the high
    bars.append((prev, brk_close + step*3, prev - step*6, brk_close))   # wide-range bar
    prev = brk_close
    # Drift back to sit just above the broken level. The breakout bar inflates
    # ATR, so the retest has to land close to satisfy 0.15 * ATR_M15.
    for c in (top + step*6, top + step*0.5):
        bars.append((prev, prev+step, min(prev, c)-step, c)); prev = c
    return ohlc(bars)


def m5_higher_low_no_break(base=1.0950, u=0.00020):
    """Higher low forms, but the last close does NOT clear the 3-bar high."""
    bars = [
        (8.0, 8.2, 7.8, 7.5), (7.5, 7.7, 7.0, 7.2), (7.2, 7.4, 6.5, 6.7),
        (6.7, 6.9, 5.5, 5.8), (5.8, 6.0, 4.5, 4.8), (4.8, 5.0, 3.5, 3.8),
        (3.8, 4.0, 2.5, 2.8), (2.8, 3.0, 1.0, 1.2),
        (1.2, 1.4, 0.0, 0.5),                       # A: swing low
        (0.5, 1.5, 0.4, 1.4), (1.4, 2.5, 1.3, 2.4),
        (2.4, 3.5, 2.3, 3.4), (3.4, 4.5, 3.3, 4.4), (4.4, 5.0, 4.3, 4.6),
        (4.6, 4.8, 3.8, 4.0), (4.0, 4.2, 3.0, 3.2),
        (3.2, 3.4, 2.4, 2.6),                       # B: higher low
        (2.6, 3.4, 2.5, 3.3), (3.3, 4.2, 3.2, 4.1),
        (4.1, 6.0, 4.0, 4.9), (4.9, 6.2, 4.8, 5.0),
        (5.0, 5.4, 4.9, 5.2),                       # close 5.2 < 3-bar high 6.2
    ]
    return ohlc([tuple(base + v * u for v in b) for b in bars], bar_seconds=300)


def m5_break_no_higher_low(base=1.0950, u=0.00020):
    """A 3-bar high break, but lows keep falling — no higher low anywhere."""
    bars = []
    level = 20.0
    for _ in range(18):                              # relentless decline
        bars.append((level, level + 0.3, level - 1.2, level - 1.0))
        level -= 1.0
    last = bars[-1][3]
    prior_high = max(b[1] for b in bars[-3:])
    bars.append((last, prior_high + 2.0, last - 0.2, prior_high + 1.5))
    return ohlc([tuple(base + v * u for v in b) for b in bars], bar_seconds=300)


def m15_failed_breakout(n=60, base=1.0950, step=0.00008, seed=7):
    """Breaks out, then closes back INSIDE the old range — spec says invalid."""
    import numpy as np
    rng = np.random.default_rng(seed)
    bars = []
    prev = base
    for _ in range(n):
        c = base + rng.normal(0, step * 2)
        bars.append((prev, max(prev, c) + step, min(prev, c) - step, c)); prev = c
    top = max(b[1] for b in bars[-12:])
    brk = top + step * 14
    bars.append((prev, brk + step * 3, prev - step * 6, brk)); prev = brk
    # Come back INSIDE the retest window but close BELOW the broken level, so
    # the distance check passes and only the failed-breakout rule can reject it.
    for c in (top + step * 6, top - step * 0.3):
        bars.append((prev, prev + step, min(prev, c) - step, c)); prev = c
    return ohlc(bars)
