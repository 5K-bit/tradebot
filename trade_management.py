"""
trade_management.py — stop placement and in-trade stop movement.

From the strategy spec:
    Structural ATR stop
    Break-even around +1R
    Trailing begins around +1.5R
    Base target 2R

"Structural ATR stop" combines the two: the stop sits beyond the relevant
market structure (the swing the setup is built on), padded by a multiple of
ATR so normal noise around that level does not take the trade out. The pad
multiple is not given in the spec; it is configurable and defaults to 0.5.

R is the initial risk distance — the gap between entry and the original stop.
All the trigger levels are expressed in multiples of it, so they work
identically on either pair regardless of pip size.
"""
from dataclasses import dataclass


@dataclass
class ManagementConfig:
    atr_stop_multiple: float = 0.5      # ATR pad beyond the structural level
    breakeven_at_r: float = 1.0
    breakeven_offset_r: float = 0.0     # push past entry to cover costs
    trail_start_r: float = 1.5
    trail_distance_r: float = 1.0       # how far behind price the trail sits
    target_r: float = 2.0


def structural_stop(direction: str, structure_level: float, atr_value: float,
                    cfg: ManagementConfig = None) -> float:
    """Stop beyond the structural level, padded by atr_stop_multiple * ATR."""
    cfg = cfg or ManagementConfig()
    pad = cfg.atr_stop_multiple * atr_value
    if direction == "buy":
        return structure_level - pad
    return structure_level + pad


def target_from_r(direction: str, entry: float, stop: float,
                  cfg: ManagementConfig = None) -> float:
    """Base target at target_r multiples of the initial risk."""
    cfg = cfg or ManagementConfig()
    r = abs(entry - stop)
    if direction == "buy":
        return entry + cfg.target_r * r
    return entry - cfg.target_r * r


def r_multiple(direction: str, entry: float, stop: float, price: float) -> float:
    """How many R the trade is currently up (negative if offside)."""
    r = abs(entry - stop)
    if r <= 0:
        return 0.0
    move = (price - entry) if direction == "buy" else (entry - price)
    return move / r


def next_stop(direction: str, entry: float, original_stop: float,
              current_stop: float, price: float,
              cfg: ManagementConfig = None) -> tuple[float, str | None]:
    """
    Where the stop should be now, given how far the trade has run.

    Returns (stop, reason_if_moved). The stop only ever moves in the trade's
    favour — a trailing calculation that would loosen it is discarded, so a
    retrace can never widen risk beyond what was accepted at entry.
    """
    cfg = cfg or ManagementConfig()
    r = abs(entry - original_stop)
    if r <= 0:
        return current_stop, None

    progress = r_multiple(direction, entry, original_stop, price)
    proposed = current_stop
    reason = None

    if progress >= cfg.trail_start_r:
        trail = (price - cfg.trail_distance_r * r) if direction == "buy" \
            else (price + cfg.trail_distance_r * r)
        if _is_tighter(direction, trail, proposed):
            proposed, reason = trail, f"trailing at +{progress:.2f}R"

    elif progress >= cfg.breakeven_at_r:
        offset = cfg.breakeven_offset_r * r
        be = (entry + offset) if direction == "buy" else (entry - offset)
        if _is_tighter(direction, be, proposed):
            proposed, reason = be, f"break-even at +{progress:.2f}R"

    return proposed, reason


def _is_tighter(direction: str, candidate: float, current: float) -> bool:
    """Is `candidate` a stop closer to price (less risk) than `current`?"""
    return candidate > current if direction == "buy" else candidate < current
