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
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
import yaml

import config_schema as schema
import indicators
import protection as protection_mod
import regime as regime_mod
import scoring
import sessions
import setups
import trade_management as tm
from mt5_connector import MT5Connector
from risk_manager import RiskManager
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

# How many M5 bars of history the indicators need before anything is decided.
CANDLE_COUNT = 400


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_pip(cfg: dict, symbol: str) -> tuple[float, float]:
    """Pip size and per-lot value for one symbol. See config_schema.pip_for."""
    return schema.pip_for(cfg, symbol)


def validate_config(cfg: dict):
    """
    Fail at startup, not on the first live signal.

    Returns the normalised view the engine runs from. Refuses a config that
    would leave the bot running but never trading — those are reported all at
    once rather than one restart at a time.
    """
    norm = schema.normalise(cfg)
    problems = schema.check_safety(cfg, norm)
    if problems:
        raise schema.ConfigError(
            "this configuration would stop the bot trading:\n  - "
            + "\n  - ".join(problems))
    for symbol in norm.symbols:
        schema.pip_for(cfg, symbol)

    # A setup name that matches nothing is a typo, not a disabled strategy —
    # silently ignoring it would leave the operator thinking it was running.
    named = set((cfg.get("strategies") or {}).keys()) | set(
        (cfg.get("setups") or {}).get("enabled") or [])
    unknown = sorted(named - set(setups.ALL_SETUPS))
    if unknown:
        raise schema.ConfigError(
            f"unknown setups in config: {unknown}. "
            f"Known: {list(setups.ALL_SETUPS)}")

    if not schema.enabled_setups(cfg):
        raise schema.ConfigError("no setups are enabled.")

    sessions.from_config(cfg)            # raises on a bad timezone or HH:MM
    return norm


def log_to_vault(vault_path: str, message: str):
    p = Path(vault_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- **{ts}** - {message}\n")


def log_decision(vault_path: str, symbol: str, outcome: str, reason: str, **fields):
    """
    One structured record per decision. The strategy rules require the reason
    for a rejection to be recorded, not just the details of a fill.
    """
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
    detail = (" " + " ".join(parts)) if parts else ""
    log_to_vault(vault_path, f"{outcome} {symbol}{detail} - {reason}")


def append_jsonl(path: str | None, record: dict) -> None:
    """One JSON object per line. Machine-readable companion to the vault log."""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              **record}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


class Lathe:
    def __init__(self, cfg: dict, conn: MT5Connector, state: JsonState,
                 norm=None):
        self.cfg = cfg
        self.conn = conn
        self.state = state
        self.norm = norm or schema.normalise(cfg)

        self.vault = self.norm.trade_log
        self.decision_log = self.norm.decision_log
        self.rejection_log = self.norm.rejection_log

        self.mode = schema.effective_mode(self.norm)
        # One place an order can escape, and it is shut unless the mode is LIVE.
        self.conn.dry_run = self.mode != schema.MODE_LIVE
        self.conn.filling_mode = self.norm.filling_mode
        self.conn.deviation = self.norm.deviation_points

        self.session = sessions.from_config(cfg)
        self.regime_cfg = schema.regime_config(cfg)
        self.setup_cfg = schema.setup_config(cfg)
        self.protection = protection_mod.Protection(
            schema.protection_config(cfg), state=state)
        self.mgmt = schema.management_config(cfg)
        # When the offset is "auto", ask the server. A hand-set value is wrong
        # for half the year on any broker that observes daylight saving.
        derived = None
        if schema.broker_offset(cfg) is None:
            derived = self.conn.server_utc_offset_hours()
            if derived is None:
                derived = 0.0
                print("[lathe] could not derive the broker's UTC offset (no tick - "
                      "market closed?); assuming UTC. Set broker.utc_offset_hours "
                      "explicitly if the daily reset looks wrong.")
            else:
                print(f"[lathe] broker server is UTC{derived:+.0f}; the daily loss "
                      f"limit resets at its midnight")
        self.risk = RiskManager(schema.risk_config(cfg, self.norm, derived), state=state)

        self.tf_regime = TIMEFRAME_MAP[self.norm.tf_regime]
        self.tf_setup = TIMEFRAME_MAP[self.norm.tf_setup]
        self.tf_entry = TIMEFRAME_MAP[self.norm.tf_entry]
        self.entry_bar_seconds = self.norm.entry_seconds

        self.enabled_setups = schema.enabled_setups(cfg)
        self.regime_map = schema.regime_map(cfg) or DEFAULT_REGIME_MAP
        self.min_score = schema.min_score(cfg)

        self.bar_cursor = {str(k): int(v) for k, v in (state.get("last_bar_time") or {}).items()}
        self.trade_meta = dict(state.get("trade_meta") or {})
        self._atr_history: dict = {}

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
    def expire_stale_orders(self, now: datetime) -> None:
        """Cancel entry orders whose window has passed (SPEC: 3 M5 candles)."""
        for order in self.conn.pending_orders():
            expires = getattr(order, "expiration", 0)
            if expires and now.timestamp() > float(expires):
                try:
                    self.conn.cancel_order(order)
                    log_decision(self.vault, order.symbol, "ORDER-EXPIRED",
                                 "entry not triggered within the window",
                                 ticket=order.ticket)
                except RuntimeError as e:
                    log_decision(self.vault, order.symbol, "CANCEL-FAILED", str(e))

    def evaluate(self, symbol: str, session_key: str, account, now: datetime) -> None:
        m15_all = self.conn.get_candles(symbol, self.tf_setup, count=CANDLE_COUNT)
        if len(m15_all) < 2:
            return
        m15 = m15_all[:-1]
        bar_time = int(m15["time"][-1])
        if self.bar_cursor.get(symbol) == bar_time:
            return
        self.bar_cursor[symbol] = bar_time

        pip_size, pip_value_per_lot = resolve_pip(self.cfg, symbol)
        tick = self.conn.symbol_tick(symbol)
        spread = float(tick.ask - tick.bid)
        self.protection.observe_spread(spread)

        gate = self.protection.check(
            session_key=session_key,
            open_positions=len(self.conn.open_positions()) + len(self.conn.pending_orders()),
            spread_pips=spread / pip_size,
            last_candle_time=bar_time,
            now=now,
            connected=True,
        )
        if not gate:
            append_jsonl(self.rejection_log,
                         {"symbol": symbol, "signal": "HOLD", "reason": gate.reason})
            log_decision(self.vault, symbol, "HOLD", gate.reason)
            return

        h1_all = self.conn.get_candles(symbol, self.tf_regime, count=CANDLE_COUNT)
        current = regime_mod.classify(h1_all[:-1], self.regime_cfg)
        if not current.is_clear:
            log_decision(self.vault, symbol, "HOLD", current.reason)
            return

        m5_all = self.conn.get_candles(symbol, self.tf_entry, count=CANDLE_COUNT)
        m5 = m5_all[:-1]
        atr_m15 = float(indicators.atr(m15, self.regime_cfg.atr_period)[-1])
        atr_m5 = float(indicators.atr(m5, self.regime_cfg.atr_period)[-1])

        spread_ok = self.protection.check_spread(spread, atr_m5)

        eligible = [n for n in self.regime_map.get(current.state, ())
                    if n in self.enabled_setups]
        if not eligible:
            log_decision(self.vault, symbol, "HOLD",
                         f"no enabled setup for regime {current.state}")
            return

        misses = []
        for name in eligible:
            candidate, why = setups.detect(name, m15, m5, current, self.setup_cfg)
            if candidate is None:
                misses.append(f"{name}: {why}")
                continue
            self._consider(symbol, candidate, current, session_key, account,
                           pip_size, pip_value_per_lot, spread, spread_ok,
                           atr_m5, atr_m15, now)
            return
        log_decision(self.vault, symbol, "HOLD", "; ".join(misses),
                     regime=current.state, adx=round(current.adx, 1))

    def _consider(self, symbol, candidate, current, session_key, account,
                  pip_size, pip_value_per_lot, spread, spread_ok,
                  atr_m5, atr_m15, now) -> None:
        regime_points, regime_reason = regime_mod.regime_score(current, self.regime_cfg)

        breakdown = scoring.build(
            regime_points=regime_points, regime_reason=regime_reason,
            candidate=candidate, setup_class=candidate.setup_class,
            spread=spread,
            median_spread=self.protection.median_spread(),
            max_spread=self.protection.cfg.max_spread_pips * pip_size,
            atr_m5=atr_m5, median_atr_m5=self.median_atr(symbol, atr_m5),
            news_clear=True, data_healthy=True,
            min_score=self.min_score,
        )
        if not spread_ok:
            breakdown.execution = 0
            breakdown.blockers.append(spread_ok.reason)

        stop_ok = self.protection.check_stop(candidate.risk_distance, spread,
                                             atr_m15, pip_size)
        if not stop_ok:
            breakdown.blockers.append(stop_ok.reason)

        signal = scoring.signal_object(symbol, candidate, current.state,
                                       breakdown, self.risk.config.risk_per_trade_pct)

        if not breakdown.executable:
            append_jsonl(self.rejection_log, signal)
            append_jsonl(self.decision_log, signal)
            log_decision(self.vault, symbol, "HOLD",
                         "; ".join(breakdown.blockers) or "score below threshold",
                         setup=candidate.setup, score=breakdown.total,
                         classification=breakdown.classification)
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
        expires_at = int(now.timestamp() + candidate.expiry_bars * self.entry_bar_seconds)
        result = self.conn.pending_stop_order(
            symbol, lots, candidate.direction, candidate.entry_price,
            candidate.stop_price, candidate.target_price, expires_at=expires_at)

        self.trade_meta[str(result.order)] = {
            "symbol": symbol,
            "entry": candidate.entry_price,
            "original_stop": candidate.stop_price,
            "session_key": session_key,
            "setup": candidate.setup,
        }
        self.state.set(trade_meta=self.trade_meta)
        self.protection.record_trade_opened(session_key)

        signal["ticket"] = result.order
        signal["lots"] = lots
        signal["mode"] = self.mode
        append_jsonl(self.decision_log, signal)
        log_to_vault(self.vault, f"SIGNAL {json.dumps(signal, default=str)}")
        log_decision(
            self.vault, symbol, signal["signal"],
            f"{candidate.setup} in {current.state} - {breakdown.classification}",
            score=breakdown.total, lots=lots, entry=round(candidate.entry_price, 5),
            sl=round(candidate.stop_price, 5), tp=round(candidate.target_price, 5),
            rr=round(candidate.rr, 2), ticket=result.order,
        )

    def median_atr(self, symbol: str, atr_now: float) -> float:
        """Rolling median ATR per symbol, for the session/volatility score."""
        history = self._atr_history.setdefault(symbol, [])
        if atr_now == atr_now:
            history.append(atr_now)
            del history[:-50]
        if not history:
            return float("nan")
        ordered = sorted(history)
        mid = len(ordered) // 2
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def main():

    cfg = load_config()
    norm = validate_config(cfg)

    state = JsonState(norm.state_path)
    conn = MT5Connector()
    conn.connect()

    bot = Lathe(cfg, conn, state, norm=norm)
    poll_seconds = norm.poll_seconds

    # The mode belongs in the log, not just on the console. Whether a session
    # could have sent orders is the first thing you want to know when reading
    # back a day's decisions.
    banner = schema.describe_mode(norm)
    print(f"[lathe] mode: {banner}")
    log_to_vault(bot.vault, f"MODE: {banner}")

    log_to_vault(bot.vault, f"Lathe started. Symbols={norm.symbols} "
                            f"session={bot.session.describe()} "
                            f"TFs={cfg['timeframes']}")
    enabled = ", ".join(bot.enabled_setups) or "none"
    log_to_vault(bot.vault, f"Setups enabled: {enabled}. Score threshold "
                            f"{bot.min_score}, trigger must score "
                            f"{scoring.REQUIRED_TRIGGER_SCORE}.")

    # A sentinel rather than None: starting outside the session is itself worth
    # recording, and `None != None` would never fire on the first pass.
    was_open = object()
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
                bot.expire_stale_orders(now)
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
