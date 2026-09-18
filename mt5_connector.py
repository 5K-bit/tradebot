"""
mt5_connector.py — thin wrapper around the MetaTrader5 terminal API.

Requires the MT5 desktop terminal to be installed and running/logged in
on the same machine (Windows, or Windows-in-Wine). Credentials are read
from environment variables — never hardcode them.

Env vars expected:
    MT5_LOGIN     — account number (int)
    MT5_PASSWORD  — account password
    MT5_SERVER    — broker server name, e.g. "ICMarkets-Demo01" or "-Live01"
    MT5_PATH      — (optional) path to terminal64.exe if not auto-detected
"""
import os
import sys
from dataclasses import dataclass

import MetaTrader5 as mt5


@dataclass
class AccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str


class MT5Connector:
    def __init__(self):
        self.login = int(os.environ["MT5_LOGIN"])
        self.password = os.environ["MT5_PASSWORD"]
        self.server = os.environ["MT5_SERVER"]
        self.path = os.environ.get("MT5_PATH")  # optional

    def connect(self) -> None:
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
        print(
            f"[mt5] connected: login={acct.login} server={self.server} "
            f"balance={acct.balance} {acct.currency} "
            f"{'(LIVE)' if not acct.trade_mode else '(DEMO)'}"
        )

    def shutdown(self) -> None:
        mt5.shutdown()

    def account_info(self) -> AccountInfo:
        a = mt5.account_info()
        if a is None:
            raise RuntimeError(f"account_info() failed: {mt5.last_error()}")
        return AccountInfo(a.login, a.balance, a.equity, a.margin, a.margin_free, a.currency)

    def get_candles(self, symbol: str, timeframe: int, count: int = 200):
        """timeframe: one of mt5.TIMEFRAME_M1, M5, M15, H1, H4, D1, ..."""
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None:
            raise RuntimeError(f"copy_rates_from_pos({symbol}) failed: {mt5.last_error()}")
        return rates

    def open_positions(self, symbol: str = None):
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return list(positions) if positions is not None else []

    def symbol_tick(self, symbol: str):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
        return tick

    def market_order(self, symbol: str, volume: float, direction: str,
                      sl_price: float = None, tp_price: float = None,
                      deviation: int = 20, comment: str = "lathe"):
        """direction: 'buy' or 'sell'. volume in lots. sl/tp as absolute prices."""
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {mt5.last_error()}")

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
            "magic": 20260917,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl_price:
            request["sl"] = sl_price
        if tp_price:
            request["tp"] = tp_price

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"order_send failed: retcode={result.retcode} comment={result.comment}")
        return result

    def close_position(self, position, deviation: int = 20):
        symbol = position.symbol
        volume = position.volume
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
            "magic": 20260917,
            "comment": "lathe-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"close order_send failed: retcode={result.retcode} comment={result.comment}")
        return result
