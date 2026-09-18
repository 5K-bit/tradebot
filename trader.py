"""
trader.py — main loop.

Flow per symbol, each polling cycle:
  1. Check kill switch (halts ALL new trades for the day if tripped)
  2. Pull latest candles; skip the symbol unless a NEW candle has closed
  3. Ask strategy.generate_signal() what to do (closed candles only)
  4. If a new position: size it via risk_manager, attach SL/TP, send order
  5. If close signal: close the open position
  6. Log every action to the vault as markdown

The strategy is evaluated once per closed candle, not once per poll. Polling
faster than the timeframe is fine — extra polls are no-ops — but evaluating a
half-formed candle repeatedly makes signals appear and disappear within the
same bar, which churns the account.

Run with:  python3 trader.py
Stop with: Ctrl+C (does NOT auto-close open positions — see README)
"""
import math
import time
import traceback
from datetime import datetime
from pathlib import Path

import yaml
import MetaTrader5 as mt5

from mt5_connector import MT5Connector
from risk_manager import RiskManager, RiskConfig
from state import JsonState
import strategy

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_pip(cfg: dict, symbol: str) -> tuple[float, float]:
    """
    Pip size and per-lot pip value for one symbol.

    These are global defaults with per-symbol overrides, because a JPY-quoted
    pair uses 0.01 where everything else uses 0.0001 — inheriting the wrong
    default sizes every position on that pair 100x off.
    """
    pip_cfg = cfg["pip"]
    overrides = pip_cfg.get("overrides") or {}
    entry = overrides.get(symbol) or {}

    size = entry.get("size", pip_cfg["size"])
    value_per_lot = entry.get("value_per_lot", pip_cfg["value_per_lot"])

    # Only sanity-check inherited defaults on plain 6-letter FX pairs. An
    # explicit override is a deliberate statement of intent, and metals or
    # indices have their own conventions.
    if "size" not in entry and len(symbol) == 6 and symbol.isalpha():
        expected = 0.01 if symbol[-3:].upper() == "JPY" else 0.0001
        if not math.isclose(size, expected, rel_tol=1e-9):
            raise ValueError(
                f"pip.size {size} is wrong for {symbol} (expected {expected}). "
                f"Add an explicit pip.overrides.{symbol}.size (and value_per_lot) "
                f"in config.yaml — inheriting the default here would mis-size every "
                f"position on this pair."
            )

    if size <= 0 or value_per_lot <= 0:
        raise ValueError(f"pip size/value_per_lot for {symbol} must be > 0 (got {size}, {value_per_lot}).")
    return size, value_per_lot


def validate_config(cfg: dict) -> None:
    """Fail at startup, not on the first live signal."""
    if cfg["timeframe"] not in TIMEFRAME_MAP:
        raise ValueError(f"Unknown timeframe {cfg['timeframe']!r}; expected one of {sorted(TIMEFRAME_MAP)}.")
    if not cfg.get("symbols"):
        raise ValueError("config.yaml lists no symbols.")
    for symbol in cfg["symbols"]:
        resolve_pip(cfg, symbol)


def log_to_vault(vault_path: str, message: str):
    p = Path(vault_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- **{ts}** — {message}\n")


def run_symbol(symbol, cfg, conn, risk, timeframe, vault_log, bar_cursor, account, open_count):
    """
    One symbol, one cycle. Returns the updated bot-wide open position count.
    """
    candles = conn.get_candles(symbol, timeframe, count=200)
    if len(candles) < 2:
        return open_count

    # The last bar from MT5 is still forming. Evaluating it re-runs the same
    # signal every poll against a moving close, which opens a position and
    # then immediately closes it on the next poll.
    closed = candles[:-1]
    last_closed_time = int(closed["time"][-1])
    if bar_cursor.get(symbol) == last_closed_time:
        return open_count  # already acted on this candle
    # Advance the cursor BEFORE acting, deliberately: one attempt per candle.
    # If the order then fails we lose that signal, which is much cheaper than
    # retrying every poll and risking a duplicate position when a rejection
    # was really a response we never saw. The in_position check below is the
    # backstop if we crash before this cursor reaches disk.
    bar_cursor[symbol] = last_closed_time

    # Only positions this bot opened — manual trades and other EAs are not
    # ours to count or close.
    symbol_positions = conn.open_positions(symbol=symbol)
    in_position = len(symbol_positions) > 0

    signal = strategy.generate_signal(closed, in_position)
    if signal is None:
        return open_count

    if signal == "close" and in_position:
        for pos in symbol_positions:
            result = conn.close_position(pos)
            open_count -= 1
            log_to_vault(
                vault_log,
                f"CLOSED {symbol} ticket={pos.ticket} exit={result.price} profit={pos.profit}"
            )

    elif signal in ("buy", "sell") and not in_position:
        if not risk.can_open_new_position(open_count):
            log_to_vault(vault_log, f"Skipped {signal} {symbol}: max open positions reached")
            return open_count

        pip_size, pip_value_per_lot = resolve_pip(cfg, symbol)
        limits = conn.symbol_limits(symbol)

        tick = conn.symbol_tick(symbol)
        entry_price = tick.ask if signal == "buy" else tick.bid

        sl = strategy.stop_loss_price(entry_price, signal, pip_size, cfg["stops"]["stop_loss_pips"])
        tp = strategy.take_profit_price(entry_price, signal, pip_size, cfg["stops"]["take_profit_pips"])

        lots = risk.position_size(
            equity=account.equity,
            entry_price=entry_price,
            stop_price=sl,
            pip_value_per_lot=pip_value_per_lot,
            pip_size=pip_size,
            volume_min=limits.volume_min,
            volume_step=limits.volume_step,
            volume_max=limits.volume_max,
        )

        result = conn.market_order(symbol, lots, signal, sl_price=sl, tp_price=tp)
        open_count += 1
        # Log the FILL, not the quote we sized against — they differ by
        # slippage, and the fill is what your money actually did.
        log_to_vault(
            vault_log,
            f"OPENED {signal.upper()} {symbol} lots={result.volume} fill={result.price} "
            f"sl={sl} tp={tp} ticket={result.order} (quoted {entry_price})"
        )

    return open_count


def main():
    cfg = load_config()
    validate_config(cfg)

    state = JsonState((cfg.get("state") or {}).get("path", ".lathe_state.json"))
    conn = MT5Connector()
    conn.connect()

    risk_cfg = RiskConfig(
        risk_per_trade_pct=cfg["risk"]["risk_per_trade_pct"],
        max_daily_loss_pct=cfg["risk"]["max_daily_loss_pct"],
        max_open_positions=cfg["risk"]["max_open_positions"],
        max_lot_size=cfg["risk"]["max_lot_size"],
        broker_utc_offset_hours=cfg["risk"].get("broker_utc_offset_hours", 0),
    )
    risk = RiskManager(risk_cfg, state=state)
    timeframe = TIMEFRAME_MAP[cfg["timeframe"]]
    vault_log = cfg["vault"]["log_path"]
    poll_seconds = cfg["poll_seconds"]

    # Which candle we last acted on, per symbol. Persisted so a restart
    # doesn't re-evaluate a bar we already traded.
    bar_cursor = {str(k): int(v) for k, v in (state.get("last_bar_time") or {}).items()}

    log_to_vault(vault_log, f"Lathe trader started. Symbols={cfg['symbols']} TF={cfg['timeframe']}")

    try:
        while True:
            try:
                # A dropped terminal link used to kill the process outright,
                # leaving open positions with nothing managing them.
                if not conn.ensure_connected():
                    time.sleep(poll_seconds)
                    continue

                account = conn.account_info()

                if risk.check_kill_switch(account.equity):
                    time.sleep(poll_seconds)
                    continue

                open_count = len(conn.open_positions())
                cursor_before = dict(bar_cursor)

                for symbol in cfg["symbols"]:
                    try:
                        open_count = run_symbol(
                            symbol, cfg, conn, risk, timeframe, vault_log,
                            bar_cursor, account, open_count,
                        )
                    except Exception as e:
                        log_to_vault(vault_log, f"ERROR on {symbol}: {e}")
                        traceback.print_exc()

                if bar_cursor != cursor_before:
                    state.set(last_bar_time=bar_cursor)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                # Anything that escapes the per-symbol handler (account info,
                # state writes, a terminal that died mid-cycle) must not end
                # the loop.
                log_to_vault(vault_log, f"CYCLE ERROR: {e}")
                traceback.print_exc()

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        log_to_vault(vault_log, "Lathe trader stopped by user (Ctrl+C). Open positions NOT auto-closed.")
    finally:
        conn.shutdown()


if __name__ == "__main__":
    main()
