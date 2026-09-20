"""
protection.py — the gates a trade must pass before it is allowed to exist.

Everything here answers one question: is the bot permitted to open a position
right now? Each gate returns a Decision carrying a human-readable reason, which
is what gets written to the trade log — the strategy rules require a recorded
reason for every rejection, not just for every fill.

Gates implemented, per LATHE ADAPTIVE SESSION STRATEGY v1:
  - inside the trading session
  - one concurrent trade
  - three trades per session
  - two consecutive losses trigger a cooldown
  - 5% daily loss stop (the per-day kill switch lives in risk_manager)
  - 10% account drawdown kill switch, measured from the equity high-water mark
  - spread filter
  - high-impact news blackout
  - API / data-health filter

The account drawdown kill switch is deliberately sticky: once tripped it stays
tripped across restarts until a human clears it, because a 10% drawdown is a
"stop and think" event, not something to be resumed automatically by a cron.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _parse_iso(text) -> datetime:
    """
    Parse an ISO-8601 timestamp, tolerating a trailing 'Z'.

    datetime.fromisoformat only accepts 'Z' from Python 3.11, and the project
    supports 3.10 — without this, a window written as "...12:30:00Z" would
    fail to parse there and silently never blackout anything.
    """
    raw = str(text).strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


@dataclass
class ProtectionConfig:
    max_spread_pips: float = 2.0
    consecutive_loss_limit: int = 2
    cooldown_scope: str = "session"      # "session" = sit out the rest of it
    cooldown_minutes: int = 0            # used when cooldown_scope == "minutes"
    max_trades_per_session: int = 3
    max_concurrent_trades: int = 1
    daily_loss_pct: float = 0.05
    account_drawdown_pct: float = 0.10
    max_candle_age_seconds: int = 1800   # must exceed the setup timeframe;
                                         # an M15 bar is legitimately up to
                                         # 900s old before the next one closes
    news_blackout_windows: list = field(default_factory=list)
    news_blackout_minutes_before: int = 30
    news_blackout_minutes_after: int = 30


@dataclass
class Decision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True, "all protection gates passed")


class Protection:
    def __init__(self, cfg: ProtectionConfig, state=None):
        self.cfg = cfg
        self.state = state
        self._consecutive_losses = self._get("consecutive_losses", 0)
        self._cooldown_session = self._get("cooldown_session", None)
        self._cooldown_until = self._get("cooldown_until", None)
        self._session_trades = dict(self._get("session_trades", {}) or {})
        self._equity_peak = self._get("equity_peak", None)
        self._account_halted = bool(self._get("account_halted", False))

        if self._account_halted:
            print("[protect] ACCOUNT DRAWDOWN KILL SWITCH is set — refusing all new trades. "
                  "Clear 'account_halted' in the state file once you have reviewed why.")

    # --- state plumbing ----------------------------------------------------
    def _get(self, key, default=None):
        return self.state.get(key, default) if self.state is not None else default

    def _persist(self):
        if self.state is None:
            return
        self.state.set(
            consecutive_losses=self._consecutive_losses,
            cooldown_session=self._cooldown_session,
            cooldown_until=self._cooldown_until,
            session_trades=self._session_trades,
            equity_peak=self._equity_peak,
            account_halted=self._account_halted,
        )

    # --- events ------------------------------------------------------------
    def observe_equity(self, equity: float) -> None:
        """Track the high-water mark and trip the account kill switch below it."""
        if self._equity_peak is None or equity > self._equity_peak:
            self._equity_peak = equity
            self._persist()
            return
        if self._equity_peak > 0:
            drawdown = (self._equity_peak - equity) / self._equity_peak
            if drawdown >= self.cfg.account_drawdown_pct and not self._account_halted:
                self._account_halted = True
                self._persist()
                print(f"[protect] ACCOUNT KILL SWITCH: drawdown {drawdown:.2%} from peak "
                      f"{self._equity_peak:.2f} >= limit {self.cfg.account_drawdown_pct:.2%}.")

    def record_trade_opened(self, session_key: str) -> None:
        self._session_trades[session_key] = self._session_trades.get(session_key, 0) + 1
        # Keep the map small; only the current session matters.
        if len(self._session_trades) > 10:
            for old in sorted(self._session_trades)[:-10]:
                self._session_trades.pop(old, None)
        self._persist()

    def record_trade_closed(self, profit: float, session_key: str | None,
                            now: datetime | None = None) -> None:
        """A win resets the streak; a loss may start a cooldown."""
        if profit < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        if self._consecutive_losses >= self.cfg.consecutive_loss_limit:
            if self.cfg.cooldown_scope == "minutes" and self.cfg.cooldown_minutes > 0:
                now = now or datetime.now(timezone.utc)
                until = now.timestamp() + self.cfg.cooldown_minutes * 60
                self._cooldown_until = until
                print(f"[protect] {self._consecutive_losses} consecutive losses — "
                      f"cooling down for {self.cfg.cooldown_minutes} minutes.")
            else:
                self._cooldown_session = session_key
                print(f"[protect] {self._consecutive_losses} consecutive losses — "
                      f"sitting out the rest of session {session_key}.")
        self._persist()

    def trades_this_session(self, session_key: str) -> int:
        return self._session_trades.get(session_key, 0)

    # --- the gate ----------------------------------------------------------
    def check(self, *, session_key: str | None, open_positions: int,
              spread_pips: float | None, last_candle_time: int | None,
              now: datetime | None = None, connected: bool = True) -> Decision:
        now = now or datetime.now(timezone.utc)

        if self._account_halted:
            return Decision(False, "account drawdown kill switch is set")

        if not connected:
            return Decision(False, "terminal/API not connected")

        if session_key is None:
            return Decision(False, "outside the trading session")

        if open_positions >= self.cfg.max_concurrent_trades:
            return Decision(False, f"already at {self.cfg.max_concurrent_trades} concurrent trade(s)")

        taken = self.trades_this_session(session_key)
        if taken >= self.cfg.max_trades_per_session:
            return Decision(False, f"session trade limit reached ({taken}/{self.cfg.max_trades_per_session})")

        if self._cooldown_session is not None and self._cooldown_session == session_key:
            return Decision(False, f"cooldown after {self._consecutive_losses} consecutive losses")

        if self._cooldown_until is not None and now.timestamp() < float(self._cooldown_until):
            remaining = (float(self._cooldown_until) - now.timestamp()) / 60
            return Decision(False, f"cooldown active for another {remaining:.0f} min")

        if spread_pips is None:
            return Decision(False, "spread unavailable — invalid tick data")
        if spread_pips > self.cfg.max_spread_pips:
            return Decision(False, f"spread {spread_pips:.2f} pips above limit {self.cfg.max_spread_pips}")

        if last_candle_time is None:
            return Decision(False, "no candle data")
        age = now.timestamp() - float(last_candle_time)
        if age > self.cfg.max_candle_age_seconds:
            return Decision(False, f"candle data stale by {age:.0f}s "
                                   f"(limit {self.cfg.max_candle_age_seconds}s)")

        blackout = self._news_blackout(now)
        if blackout:
            return Decision(False, blackout)

        return ALLOW

    def _news_blackout(self, now: datetime) -> str | None:
        """
        Manual blackout windows from config, widened by the before/after margins.

        The MetaTrader5 Python package exposes no economic calendar, so an
        automatic high-impact-news feed needs an external provider. Until one is
        wired in, these windows are entered by hand and anything not listed is
        NOT blacked out — see the README.
        """
        for window in (self.cfg.news_blackout_windows or []):
            try:
                start = _parse_iso(window["start"])
                end = _parse_iso(window.get("end", window["start"]))
            except (KeyError, ValueError, TypeError):
                # A window we cannot parse must be loud, not silently ignored —
                # a missed blackout is a trade taken into a news spike.
                print(f"[protect] IGNORING unparseable news window {window!r} — "
                      f"it will NOT block trading. Fix it in config.yaml.")
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            start = start.timestamp() - self.cfg.news_blackout_minutes_before * 60
            end = end.timestamp() + self.cfg.news_blackout_minutes_after * 60
            if start <= now.timestamp() <= end:
                label = window.get("label", "high-impact news")
                return f"news blackout: {label}"
        return None


def from_config(cfg: dict) -> ProtectionConfig:
    p = (cfg.get("protection") or {})
    r = (cfg.get("risk") or {})
    return ProtectionConfig(
        max_spread_pips=p.get("max_spread_pips", 2.0),
        consecutive_loss_limit=p.get("consecutive_loss_limit", 2),
        cooldown_scope=p.get("cooldown_scope", "session"),
        cooldown_minutes=p.get("cooldown_minutes", 0),
        max_trades_per_session=r.get("max_trades_per_session", 3),
        max_concurrent_trades=r.get("max_concurrent_trades", 1),
        daily_loss_pct=r.get("max_daily_loss_pct", 0.05),
        account_drawdown_pct=r.get("account_drawdown_pct", 0.10),
        max_candle_age_seconds=p.get("max_candle_age_seconds", 1800),
        news_blackout_windows=p.get("news_blackout_windows") or [],
        news_blackout_minutes_before=p.get("news_blackout_minutes_before", 30),
        news_blackout_minutes_after=p.get("news_blackout_minutes_after", 30),
    )
