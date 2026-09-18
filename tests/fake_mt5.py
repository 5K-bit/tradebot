"""
A fake MetaTrader5 terminal.

The real `MetaTrader5` package only works on Windows alongside a running MT5
desktop terminal, so it can never be imported in CI — and we would not want
tests touching a live broker connection even where it can. This stands in for
it: enough of the API surface for the real bot code to run unmodified, plus
test hooks for the failure modes we care about (a dropped terminal, foreign
positions, slippage between quote and fill).
"""
import numpy as np

# --- constants mirrored from the real package -------------------------------
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 30, 16385, 16388, 16408
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
TRADE_ACTION_DEAL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_IOC = 1
TRADE_RETCODE_DONE = 10009
ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2

BAR_SECONDS = 900  # M15, the timeframe the tests use

RATES_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
               ("close", "<f8"), ("tick_volume", "<i8"), ("spread", "<i4"),
               ("real_volume", "<i8")]


class Obj:
    """Stand-in for the named tuples the real API returns."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return f"Obj({self.__dict__})"


def make_rates(prices, first_index: int = 0):
    """Build an MT5-shaped rates array from a list of closing prices."""
    arr = np.zeros(len(prices), dtype=RATES_DTYPE)
    for i, p in enumerate(prices):
        arr[i] = ((first_index + i) * BAR_SECONDS, p, p + 0.0002, p - 0.0002, p, 100, 10, 0)
    return arr


class Market:
    """World state the tests drive directly."""
    def __init__(self):
        self.closed_prices = []       # completed bars
        self.forming_price = None     # the bar still in progress
        self.positions = []
        self.orders = []              # every order_send request, in order
        self.equity = 10_000.0
        self.balance = 10_000.0
        self.terminal_up = True       # is the terminal answering?
        self.can_initialize = True    # will a fresh initialize() succeed?
        self.login_ok = True
        self.account_info_none = False
        self.next_ticket = 1000
        self.slippage = 0.00002       # fills differ from the quote
        self.spread = 0.00010
        self.selected = []            # symbols pushed into Market Watch
        self.trade_mode = ACCOUNT_TRADE_MODE_DEMO
        self.init_calls = 0

    def price(self):
        return self.forming_price if self.forming_price is not None else self.closed_prices[-1]

    def add_foreign_position(self, symbol="EURUSD", ticket=777, volume=0.5, magic=0):
        """A trade someone else opened — by hand, or by another EA."""
        pos = Obj(ticket=ticket, symbol=symbol, volume=volume, type=ORDER_TYPE_BUY,
                  magic=magic, profit=-12.0, price_open=1.1, sl=0.0, tp=0.0)
        self.positions.append(pos)
        return pos


MARKET = Market()


def reset():
    global MARKET
    MARKET = Market()
    return MARKET


# --- the API surface the bot uses -------------------------------------------
def last_error():
    return (-1, "fake error")


def initialize(**kwargs):
    MARKET.init_calls += 1
    if not MARKET.can_initialize:
        return False
    MARKET.terminal_up = True   # the terminal came back
    return True


def login(login, password=None, server=None):
    return MARKET.login_ok


def shutdown():
    return True


def terminal_info():
    return Obj(name="fake") if MARKET.terminal_up else None


def account_info():
    if MARKET.account_info_none or not MARKET.terminal_up:
        return None
    return Obj(login=12345678, balance=MARKET.balance, equity=MARKET.equity,
               margin=0.0, margin_free=MARKET.equity, currency="USD",
               trade_mode=MARKET.trade_mode)


def symbol_select(symbol, enable=True):
    MARKET.selected.append(symbol)
    return True


def symbol_info(symbol):
    return Obj(digits=3 if symbol.endswith("JPY") else 5,
               volume_min=0.01, volume_step=0.01, volume_max=100.0, point=0.00001)


def symbol_info_tick(symbol):
    mid = MARKET.price()
    return Obj(bid=mid - MARKET.spread / 2, ask=mid + MARKET.spread / 2, last=mid)


def copy_rates_from_pos(symbol, timeframe, start_pos, count):
    prices = list(MARKET.closed_prices)
    if MARKET.forming_price is not None:
        prices.append(MARKET.forming_price)
    prices = prices[-count:]
    if not prices:
        return None
    forming = 1 if MARKET.forming_price is not None else 0
    first_index = len(MARKET.closed_prices) + forming - len(prices)
    return make_rates(prices, first_index)


def positions_get(symbol=None):
    return tuple(p for p in MARKET.positions if symbol is None or p.symbol == symbol)


def order_send(request):
    MARKET.orders.append(dict(request))
    slip = MARKET.slippage if request["type"] == ORDER_TYPE_BUY else -MARKET.slippage
    fill = request["price"] + slip

    if "position" in request:                     # closing an existing position
        MARKET.positions = [p for p in MARKET.positions if p.ticket != request["position"]]
    else:                                         # opening a new one
        MARKET.next_ticket += 1
        MARKET.positions.append(Obj(
            ticket=MARKET.next_ticket, symbol=request["symbol"], volume=request["volume"],
            type=request["type"], magic=request.get("magic", 0), profit=0.0,
            price_open=fill, sl=request.get("sl", 0.0), tp=request.get("tp", 0.0),
        ))
    return Obj(retcode=TRADE_RETCODE_DONE, price=fill, volume=request["volume"],
               order=MARKET.next_ticket, deal=MARKET.next_ticket, comment="done")
