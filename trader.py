"""
trader.py — main loop.

Flow per symbol, each polling cycle:
  1. Check kill switch (halts ALL new trades for the day if tripped)
  2. Pull latest candles
  3. Ask strategy.generate_signal() what to do
  4. If a new position: size it via risk_manager, attach SL/TP, send order
  5. If close signal: close the open position
  6. Log every action to the vault as markdown

Run with:  python3 trader.py
Stop with: Ctrl+C (does NOT auto-close open positions — see README)
"""
import time
import traceback
from datetime import datetime
from pathlib import Path

import yaml
import MetaTrader5 as mt5

from mt5_connector import MT5Connector
from risk_manager import RiskManager, RiskConfig
import strategy

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def log_to_vault(vault_path: str, message: str):
    p = Path(vault_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- **{ts}** — {message}\n")


def main():
    cfg = load_config()
    conn = MT5Connector()
    conn.connect()

    risk_cfg = RiskConfig(
        risk_per_trade_pct=cfg["risk"]["risk_per_trade_pct"],
        max_daily_loss_pct=cfg["risk"]["max_daily_loss_pct"],
        max_open_positions=cfg["risk"]["max_open_positions"],
        max_lot_size=cfg["risk"]["max_lot_size"],
    )
    risk = RiskManager(risk_cfg)
    timeframe = TIMEFRAME_MAP[cfg["timeframe"]]
    vault_log = cfg["vault"]["log_path"]

    log_to_vault(vault_log, f"Lathe trader started. Symbols={cfg['symbols']} TF={cfg['timeframe']}")

    try:
        while True:
            account = conn.account_info()

            if risk.check_kill_switch(account.equity):
                time.sleep(cfg["poll_seconds"])
                continue

            all_positions = conn.open_positions()

            for symbol in cfg["symbols"]:
                try:
                    candles = conn.get_candles(symbol, timeframe, count=200)
                    symbol_positions = [p for p in all_positions if p.symbol == symbol]
                    in_position = len(symbol_positions) > 0

                    signal = strategy.generate_signal(candles, in_position)
                    if signal is None:
                        continue

                    if signal == "close" and in_position:
                        for pos in symbol_positions:
                            conn.close_position(pos)
                            log_to_vault(vault_log, f"CLOSED {symbol} ticket={pos.ticket} profit={pos.profit}")

                    elif signal in ("buy", "sell") and not in_position:
                        if not risk.can_open_new_position(len(all_positions)):
                            log_to_vault(vault_log, f"Skipped {signal} {symbol}: max open positions reached")
                            continue

                        tick = conn.symbol_tick(symbol)
                        entry_price = tick.ask if signal == "buy" else tick.bid
                        pip_size = cfg["pip"]["size"]

                        sl = strategy.stop_loss_price(entry_price, signal, pip_size, cfg["stops"]["stop_loss_pips"])
                        tp = strategy.take_profit_price(entry_price, signal, pip_size, cfg["stops"]["take_profit_pips"])

                        lots = risk.position_size(
                            equity=account.equity,
                            entry_price=entry_price,
                            stop_price=sl,
                            pip_value_per_lot=cfg["pip"]["value_per_lot"],
                            pip_size=pip_size,
                        )

                        result = conn.market_order(symbol, lots, signal, sl_price=sl, tp_price=tp)
                        log_to_vault(
                            vault_log,
                            f"OPENED {signal.upper()} {symbol} lots={lots} entry={entry_price} sl={sl} tp={tp}"
                        )

                except Exception as e:
                    log_to_vault(vault_log, f"ERROR on {symbol}: {e}")
                    traceback.print_exc()

            time.sleep(cfg["poll_seconds"])

    except KeyboardInterrupt:
        log_to_vault(vault_log, "Lathe trader stopped by user (Ctrl+C). Open positions NOT auto-closed.")
    finally:
        conn.shutdown()


if __name__ == "__main__":
    main()
