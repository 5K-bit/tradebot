"""
mt5_connector.py — thin wrapper around the MetaTrader5 terminal API.

Requires the MT5 desktop terminal to be installed and running/logged in
on the same machine (Windows, or Windows-in-Wine). Credentials are read
from environment variables — never hardcode them.

Every order this bot sends is tagged with MAGIC, and every position it reads
back is filtered by MAGIC. That tag is the only thing separating "positions
this bot opened" from your manual trades and any other EA on the account —
without it the bot will happily close trades it never opened.

Env vars expected:
    MT5_LOGIN     — account number (int)
    MT5_PASSWORD  — account password
    MT5_SERVER    — broker server name, e.g. "ICMarkets-Demo01" or "-Live01"
    MT5_PATH      — (optional) path to terminal64.exe if not auto-detected
"""
import os
import time
from dataclasses import dataclass

import MetaTrader5 as mt5

# Identifies orders belonging to this bot. Change it only if you run two
# copies against one account — and then change it in BOTH copies.
MAGIC = 20260917

RECONNECT_BACKOFF_SECONDS = (2, 4, 8, 16, 32)


@dataclass
class AccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str


@dataclass
class SymbolLimits:
    digits: int
    volume_min: float
    volume_step: float
    volume_max: float


class MT5Connector:
    def __init__(self):
        self.login = int(os.environ["MT5_LOGIN"])
        self.password = os.environ["MT5_PASSWORD"]
        self.server = os.environ["MT5_SERVER"]
        self.path = os.environ.get("MT5_PATH")  # optional
        self._selected: set[str] = set()

    def connect(self, quiet: bool = False) -> None:
        init_kwargs = {}
        if self.path:
            init_kwargs["path"] = self.path

        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

        authorized = mt5.login(self.login, password=self.password, server=self.server)
        if not authorized:
            mt5.shutdown()
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

        acct = mt5.account_info()
        if acct is None:
            # Don't let the LIVE/DEMO banner be the thing that crashes — this
            # is exactly the moment you want a clear message.
            mt5.shutdown()
            raise RuntimeError(f"account_info() returned None after login: {mt5.last_error()}")

        # A fresh terminal session starts with nothing selected.
        self._selected.clear()

        if not quiet:
            # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL (ACCOUNT_TRADE_MODE_* enum).
            # Only REAL is "(LIVE)" — everything else is treated as non-live.
            is_live = acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL
            print(
                f"[mt5] connected: login={acct.login} server={self.server} "
                f"balance={acct.balance} {acct.currency} "
                f"{'(LIVE)' if is_live else '(DEMO)'}"
            )

    def is_connected(self) -> bool:
        return mt5.terminal_info() is not None and mt5.account_info() is not None

    def ensure_connected(self) -> bool:
        """
        Re-establish the terminal link if it dropped. Returns False if every
        retry failed, so the caller can keep looping instead of dying with
        positions open and unmanaged.
        """
        if self.is_connected():
            return True

        print("[mt5] connection to terminal lost — attempting to reconnect…")
        for attempt, delay in enumerate(RECONNECT_BACKOFF_SECONDS, start=1):
            try:
                mt5.shutdown()
            except Exception:
                pass
            try:
                self.connect(quiet=True)
                print(f"[mt5] reconnected after {attempt} attempt(s).")
                return True
            except Exception as e:
                print(f"[mt5] reconnect attempt {attempt} failed: {e} — retrying in {delay}s")
                time.sleep(delay)
        print("[mt5] reconnect failed; will try again next cycle.")
        return False

    def shutdown(self) -> None:
        mt5.shutdown()

    def account_info(self) -> AccountInfo:
        a = mt5.account_info()
        if a is None:
            raise RuntimeError(f"account_info() failed: {mt5.last_error()}")
        return AccountInfo(a.login, a.balance, a.equity, a.margin, a.margin_free, a.currency)

    def ensure_symbol(self, symbol: str) -> None:
        """
        A symbol that isn't in Market Watch returns no candles and rejects
        orders, so select it before doing anything else with it.
        """
        if symbol in self._selected:
            return
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {mt5.last_error()}")
        self._selected.add(symbol)

    def symbol_limits(self, symbol: str) -> SymbolLimits:
        self.ensure_symbol(symbol)
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
        return SymbolLimits(
            digits=info.digits,
            volume_min=info.volume_min,
            volume_step=info.volume_step,
            volume_max=info.volume_max,
        )

    def get_candles(self, symbol: str, timeframe: int, count: int = 200):
        """timeframe: one of mt5.TIMEFRAME_M1, M5, M15, H1, H4, D1, ...

        Index 0 is the oldest bar; the LAST bar is the one still forming, so
        callers that need completed candles must drop it.
        """
        self.ensure_symbol(symbol)
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None:
            raise RuntimeError(f"copy_rates_from_pos({symbol}) failed: {mt5.last_error()}")
        return rates

    def open_positions(self, symbol: str | None = None, magic: int | None = MAGIC):
        """
        Positions opened by THIS bot. Pass magic=None to see every position on
        the account (manual trades and other EAs included) — but never close
        anything from that list.
        """
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        positions = list(positions) if positions is not None else []
        if magic is not None:
            positions = [p for p in positions if p.magic == magic]
        return positions

    def symbol_tick(self, symbol: str):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
        return tick

    def market_order(self, symbol: str, volume: float, direction: str,
                      sl_price: float | None = None, tp_price: float | None = None,
                      deviation: int = 20, comment: str = "lathe"):
        """direction: 'buy' or 'sell'. volume in lots. sl/tp as absolute prices."""
        limits = self.symbol_limits(symbol)

        tick = self.symbol_tick(symbol)
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if direction == "buy" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "deviation": deviation,
            "magic": MAGIC,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        # `is not None`, not truthiness: a legitimate price is never 0, but a
        # silently dropped stop-loss is the one failure this bot must not have.
        if sl_price is not None:
            request["sl"] = round(sl_price, limits.digits)
        if tp_price is not None:
            request["tp"] = round(tp_price, limits.digits)

        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send({symbol}) returned None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"order_send failed: retcode={result.retcode} comment={result.comment}")
        return result

    def close_position(self, position, deviation: int = 20):
        if position.magic != MAGIC:
            # Belt and braces: open_positions() already filters, but this is
            # the call that actually spends money on someone else's trade.
            raise RuntimeError(
                f"refusing to close position {position.ticket} on {position.symbol}: "
                f"magic={position.magic} is not this bot's ({MAGIC})."
            )

        symbol = position.symbol
        volume = position.volume
        self.ensure_symbol(symbol)
        tick = self.symbol_tick(symbol)
        if position.type == mt5.ORDER_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": deviation,
            "magic": MAGIC,
            "comment": "lathe-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"close order_send({symbol}) returned None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"close order_send failed: retcode={result.retcode} comment={result.comment}")
        return result
