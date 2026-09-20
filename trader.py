"""
trader.py — main loop for LATHE ADAPTIVE SESSION STRATEGY v1.

Pipeline, per symbol, once per closed M15 setup bar inside the session:

    session open?  ->  H1 regime  ->  M15 setup  ->  M5 entry refinement
                   ->  RR check   ->  signal score >= 80
                   ->  protection gates  ->  risk sizing  ->  order

Any gate that fails ends the evaluation with a recorded reason. Every decision,
taken or rejected, is written to the trade log — the strategy rules require a
reason for rejections, not only for fills.

Open positions are managed every poll rather than every setup bar, so the
break-even and trailing steps react on M5 timing rather than M15.

NOTE: the bot cannot currently open a trade. setups.py and scoring.py are
fail-closed stubs — the strategy document names the three setups and states the
score threshold, but defines neither the setup rules nor the score formula. Both
refuse rather than guess. Everything around them is live and tested.

Run with:  python3 trader.py
Stop with: Ctrl+C (does NOT auto-close open positions — see README)
"""
import math
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
import yaml

import protection as protection_mod
import regime as regime_mod
import scoring
import sessions
import setups
import trade_management as tm
from mt5_connector import MT5Connector
from risk_manager import RiskConfig, RiskManager
from state import JsonState

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

ALLOWED_RISK_PCT = (0.005, 0.01, 0.02)

# Which setups are eligible in which regime. Not stated in the strategy
# document; this is the reading that follows from the setup names, and it is
# config-overridable via setups.regime_map.
DEFAULT_REGIME_MAP = {
    regime_mod.TREND_UP: (setups.TREND_PULLBACK, setups.BREAKOUT_RETEST),
    regime_mod.TREND_DOWN: (setups.TREND_PULLBACK, setups.BREAKOUT_RETEST),
    regime_mod.RANGE: (setups.RANGE_REVERSION,),
}


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_pip(cfg: dict, symbol: str) -> tuple[float, float]:
    """Pip size and per-lot pip value for one symbol, with per-symbol overrides."""
    pip_cfg = cfg["pip"]
    overrides = pip_cfg.get("overrides") or {}
    entry = overrides.get(symbol) or {}

    size = entry.get("size", pip_cfg["size"])
    value_per_lot = entry.get("value_per_lot", pip_cfg["value_per_lot"])

    if "size" not in entry and len(symbol) == 6 and symbol.isalpha():
        expected = 0.01 if symbol[-3:].upper() == "JPY" else 0.0001
        if not math.isclose(size, expected, rel_tol=1e-9):
            raise ValueError(
                f"pip.size {size} is wrong for {symbol} (expected {expected}). "
                f"Add an explicit pip.overrides.{symbol}.size in config.yaml."
            )
    if size <= 0 or value_per_lot <= 0:
        raise ValueError(f"pip size/value_per_lot for {symbol} must be > 0.")
    return size, value_per_lot


def validate_config(cfg: dict) -> None:
    """Fail at startup, not on the first live signal."""
    symbols = cfg.get("symbols") or []
    if not symbols:
        raise ValueError("config.yaml lists no symbols.")

    tfs = cfg.get("timeframes") or {}
    for role in ("regime", "setup", "entry"):
        name = tfs.get(role)
        if name not in TIMEFRAME_MAP:
            raise ValueError(
                f"timeframes.{role} is {name!r}; expected one of {sorted(TIMEFRAME_MAP)}."
            )

    risk_pct = (cfg.get("risk") or {}).get("risk_per_trade_pct")
    if risk_pct not in ALLOWED_RISK_PCT:
        raise ValueError(
            f"risk.risk_per_trade_pct is {risk_pct}; the strategy allows "
            f"{ALLOWED_RISK_PCT} (0.5%, 1%, 2%)."
        )

    enabled = (cfg.get("setups") or {}).get("enabled") or []
    unknown = [s for s in enabled if s not in setups.ALL_SETUPS]
    if unknown:
        raise ValueError(f"unknown setups in config: {unknown}. Known: {list(setups.ALL_SETUPS)}")

    sessions.from_config(cfg)            # raises on a bad timezone or HH:MM
    for symbol in symbols:
        resolve_pip(cfg, symbol)


def log_to_vault(vault_path: str, message: str):
    p = Path(vault_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- **{ts}** — {message}\n")


def log_decision(vault_path: str, symbol: str, outcome: str, reason: str, **fields):
    """
    One structured record per decision. The strategy rules require the reason
    for a rejection to be recorded, not just the details of a fill.
    """
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
    detail = (" " + " ".join(parts)) if parts else ""
    log_to_vault(vault_path, f"{outcome} {symbol}{detail} — {reason}")


class Lathe:
    def __init__(self, cfg: dict, conn: MT5Connector, state: JsonState):
        self.cfg = cfg
        self.conn = conn
        self.state = state
        self.vault = cfg["vault"]["log_path"]

        self.session = sessions.from_config(cfg)
        self.regime_cfg = regime_mod.from_config(cfg)
        self.protection = protection_mod.Protection(
            protection_mod.from_config(cfg), state=state)

        m = cfg.get("management") or {}
        self.mgmt = tm.ManagementConfig(
            atr_stop_multiple=m.get("atr_stop_multiple", 0.5),
            breakeven_at_r=m.get("breakeven_at_r", 1.0),
            breakeven_offset_r=m.get("breakeven_offset_r", 0.0),
            trail_start_r=m.get("trail_start_r", 1.5),
            trail_distance_r=m.get("trail_distance_r", 1.0),
            target_r=m.get("target_r", 2.0),
        )

        r = cfg["risk"]
        self.risk = RiskManager(RiskConfig(
            risk_per_trade_pct=r["risk_per_trade_pct"],
            max_daily_loss_pct=r["max_daily_loss_pct"],
            max_open_positions=r.get("max_concurrent_trades", 1),
            max_lot_size=r.get("max_lot_size", 1.0),
            broker_utc_offset_hours=r.get("broker_utc_offset_hours", 0),
        ), state=state)

        tfs = cfg["timeframes"]
        self.tf_regime = TIMEFRAME_MAP[tfs["regime"]]
        self.tf_setup = TIMEFRAME_MAP[tfs["setup"]]
        self.tf_entry = TIMEFRAME_MAP[tfs["entry"]]

        self.enabled_setups = (cfg.get("setups") or {}).get("enabled") or []
        self.regime_map = (cfg.get("setups") or {}).get("regime_map") or DEFAULT_REGIME_MAP
        self.min_score = (cfg.get("scoring") or {}).get("min_score", scoring.MIN_SCORE)

        self.bar_cursor = {str(k): int(v) for k, v in (state.get("last_bar_time") or {}).items()}
        self.trade_meta = dict(state.get("trade_meta") or {})   # ticket -> entry/stop

    # --- open position management -----------------------------------------
    def manage_open_positions(self, now: datetime) -> None:
        """Step stops to break-even and then trail them. Runs every poll."""
        for pos in self.conn.open_positions():
            meta = self.trade_meta.get(str(pos.ticket))
            if not meta:
                continue        # opened before this process knew about it
            try:
                tick = self.conn.symbol_tick(pos.symbol)
            except RuntimeError:
                continue
            direction = "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell"
            price = tick.bid if direction == "buy" else tick.ask

            new_stop, reason = tm.next_stop(
                direction, float(meta["entry"]), float(meta["original_stop"]),
                float(pos.sl) if pos.sl else float(meta["original_stop"]),
                price, self.mgmt)
            if reason is None:
                continue
            try:
                self.conn.modify_stop(pos, new_stop)
                log_decision(self.vault, pos.symbol, "STOP-MOVED", reason,
                             ticket=pos.ticket, new_sl=round(new_stop, 5))
            except RuntimeError as e:
                log_decision(self.vault, pos.symbol, "STOP-MOVE-FAILED", str(e),
                             ticket=pos.ticket)

    def reconcile_closed_trades(self, session_key: str | None) -> None:
        """Positions we were tracking that no longer exist have been closed."""
        live = {str(p.ticket) for p in self.conn.open_positions()}
        for ticket in list(self.trade_meta):
            if ticket in live:
                continue
            meta = self.trade_meta.pop(ticket)
            profit = self._closed_profit(ticket)
            self.protection.record_trade_closed(profit, meta.get("session_key") or session_key)
            log_decision(self.vault, meta.get("symbol", "?"), "CLOSED",
                         "position no longer open", ticket=ticket, profit=profit)
        self.state.set(trade_meta=self.trade_meta)

    def _closed_profit(self, ticket: str) -> float:
        """Realised profit for a ticket, from the broker's deal history."""
        try:
            deals = mt5.history_deals_get(position=int(ticket))
        except Exception:
            return 0.0
        if not deals:
            return 0.0
        return float(sum(getattr(d, "profit", 0.0) for d in deals))

    # --- per-symbol evaluation --------------------------------------------
    def evaluate(self, symbol: str, session_key: str, account, now: datetime) -> None:
        setup_candles = self.conn.get_candles(symbol, self.tf_setup, count=300)
        if len(setup_candles) < 2:
            return
        closed_setup = setup_candles[:-1]
        bar_time = int(closed_setup["time"][-1])
        if self.bar_cursor.get(symbol) == bar_time:
            return                      # already decided on this setup bar
        self.bar_cursor[symbol] = bar_time

        pip_size, pip_value_per_lot = resolve_pip(self.cfg, symbol)
        tick = self.conn.symbol_tick(symbol)
        spread_pips = (tick.ask - tick.bid) / pip_size

        gate = self.protection.check(
            session_key=session_key,
            open_positions=len(self.conn.open_positions()),
            spread_pips=spread_pips,
            last_candle_time=bar_time,
            now=now,
            connected=True,
        )
        if not gate:
            log_decision(self.vault, symbol, "NO-TRADE", gate.reason, spread=round(spread_pips, 2))
            return

        h1 = self.conn.get_candles(symbol, self.tf_regime, count=300)
        current_regime = regime_mod.classify(h1[:-1], self.regime_cfg)
        if current_regime.state == regime_mod.UNDEFINED:
            log_decision(self.vault, symbol, "NO-TRADE", current_regime.reason)
            return

        eligible = [s for s in self.regime_map.get(current_regime.state, ())
                    if s in self.enabled_setups]
        if not eligible:
            log_decision(self.vault, symbol, "NO-TRADE",
                         f"no enabled setup for regime {current_regime.state}")
            return

        entry_candles = self.conn.get_candles(symbol, self.tf_entry, count=300)
        reasons = []
        for name in eligible:
            candidate, reason = setups.detect(name, closed_setup, entry_candles[:-1],
                                              current_regime, self.cfg)
            if candidate is None:
                reasons.append(f"{name}: {reason}")
                continue
            self._consider(symbol, candidate, current_regime, session_key,
                           account, pip_size, pip_value_per_lot)
            return
        log_decision(self.vault, symbol, "NO-TRADE", "; ".join(reasons),
                     regime=current_regime.state, adx=round(current_regime.adx, 1))

    def _consider(self, symbol, candidate, current_regime, session_key,
                  account, pip_size, pip_value_per_lot) -> None:
        if not candidate.meets_min_rr():
            log_decision(self.vault, symbol, "REJECTED",
                         f"RR {candidate.rr:.2f} below minimum "
                         f"{setups.MIN_RR[candidate.setup]} for {candidate.setup}")
            return

        value, why = scoring.score(candidate, current_regime, {"symbol": symbol})
        if not scoring.passes(value, self.min_score):
            shown = "n/a" if value is None else f"{value:.0f}"
            log_decision(self.vault, symbol, "REJECTED",
                         f"signal score {shown} < {self.min_score}: {why}")
            return

        limits = self.conn.symbol_limits(symbol)
        lots = self.risk.position_size(
            equity=account.equity,
            entry_price=candidate.entry_price,
            stop_price=candidate.stop_price,
            pip_value_per_lot=pip_value_per_lot,
            pip_size=pip_size,
            volume_min=limits.volume_min,
            volume_step=limits.volume_step,
            volume_max=limits.volume_max,
        )
        result = self.conn.market_order(symbol, lots, candidate.direction,
                                        sl_price=candidate.stop_price,
                                        tp_price=candidate.target_price)
        self.trade_meta[str(result.order)] = {
            "symbol": symbol,
            "entry": result.price,
            "original_stop": candidate.stop_price,
            "session_key": session_key,
        }
        self.state.set(trade_meta=self.trade_meta)
        self.protection.record_trade_opened(session_key)
        log_decision(
            self.vault, symbol, "OPENED",
            f"{candidate.setup} in {current_regime.state} (score {value:.0f})",
            direction=candidate.direction.upper(), lots=result.volume,
            fill=result.price, sl=candidate.stop_price, tp=candidate.target_price,
            rr=round(candidate.rr, 2), risk_pct=self.risk.config.risk_per_trade_pct,
            ticket=result.order,
        )


def main():
    cfg = load_config()
    validate_config(cfg)

    state = JsonState((cfg.get("state") or {}).get("path", ".lathe_state.json"))
    conn = MT5Connector()
    conn.connect()

    bot = Lathe(cfg, conn, state)
    poll_seconds = cfg["poll_seconds"]

    log_to_vault(bot.vault, f"Lathe started. Symbols={cfg['symbols']} "
                            f"session={bot.session.describe()} "
                            f"TFs={cfg['timeframes']}")
    if not setups.any_configured():
        msg = ("NO SETUPS ARE CONFIGURED — setups.py and scoring.py are stubs, so "
               "no trade can be opened. Running in observation mode.")
        print(f"[lathe] {msg}")
        log_to_vault(bot.vault, msg)

    was_open = None
    try:
        while True:
            try:
                if not conn.ensure_connected():
                    time.sleep(poll_seconds)
                    continue

                now = datetime.now(timezone.utc)
                account = conn.account_info()
                bot.protection.observe_equity(account.equity)

                session_key = bot.session.session_key(now)
                if session_key != was_open:
                    log_to_vault(bot.vault, f"Session {'OPEN ' + session_key if session_key else 'CLOSED'}")
                    was_open = session_key

                bot.manage_open_positions(now)
                bot.reconcile_closed_trades(session_key)

                if bot.risk.check_kill_switch(account.equity) or session_key is None:
                    time.sleep(poll_seconds)
                    continue

                cursor_before = dict(bot.bar_cursor)
                for symbol in cfg["symbols"]:
                    try:
                        bot.evaluate(symbol, session_key, account, now)
                    except Exception as e:
                        log_decision(bot.vault, symbol, "ERROR", str(e))
                        traceback.print_exc()
                if bot.bar_cursor != cursor_before:
                    state.set(last_bar_time=bot.bar_cursor)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log_to_vault(bot.vault, f"CYCLE ERROR: {e}")
                traceback.print_exc()

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        log_to_vault(bot.vault, "Lathe stopped by user (Ctrl+C). Open positions NOT auto-closed.")
    finally:
        conn.shutdown()


if __name__ == "__main__":
    main()
