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
ORDER_FILLING_FOK = 0
ORDER_FILLING_RETURN = 2
ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 2
TRADE_ACTION_PENDING = 5
TRADE_ACTION_REMOVE = 8
ORDER_TIME_GTC = 0
ORDER_TIME_SPECIFIED = 2
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


def make_rates(prices, first_index: int = 0, anchor: float = 0.0):
    """Build an MT5-shaped rates array from a list of closing prices."""
    arr = np.zeros(len(prices), dtype=RATES_DTYPE)
    for i, p in enumerate(prices):
        t = int(anchor + (first_index + i) * BAR_SECONDS)
        arr[i] = (t, p, p + 0.0002, p - 0.0002, p, 100, 10, 0)
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
        self.can_initialize = True
        self.init_error = (-6, 'Terminal: Authorization failed')
        self.init_needs_credentials = False    # will a fresh initialize() succeed?
        self.login_ok = True
        self.account_info_none = False
        self.next_ticket = 1000
        self.slippage = 0.00002       # fills differ from the quote
        self.spread = 0.00010
        self.selected = []            # symbols pushed into Market Watch
        self.trade_mode = ACCOUNT_TRADE_MODE_DEMO
        self.init_calls = 0

        # Bar times are anchored so the forming bar sits at now_ts, matching a
        # real terminal where the newest bar is "now" — otherwise every candle
        # looks decades stale to the data-health gate.
        self.now_ts = 1768447800.0
        self.series = {}          # timeframe -> list of closed prices
        self.ohlc = {}            # timeframe -> a full rates array
        self.deals = {}           # ticket -> realised profit
        self.pending = []         # working orders
        self.symbol_names = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
        self.filling_mask = 2     # 1=FOK, 2=IOC
        self.order_check_retcode = 0
        self.pending_check_retcode = 0
        self.trade_allowed = True

    def set_series(self, timeframe, prices, forming=None):
        """Give one timeframe its own candle series."""
        self.series[timeframe] = (list(prices), forming)

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
    return MARKET.init_error


def initialize(**kwargs):
    MARKET.init_calls += 1
    if not MARKET.can_initialize:
        return False
    # Some brokers only authorise when credentials are passed to initialize().
    if MARKET.init_needs_credentials and "login" not in kwargs:
        return False
    MARKET.terminal_up = True   # the terminal came back
    return True


def login(login, password=None, server=None):
    return MARKET.login_ok


def shutdown():
    return True


def terminal_info():
    return (Obj(name="fake", trade_allowed=MARKET.trade_allowed)
            if MARKET.terminal_up else None)


def account_info():
    if MARKET.account_info_none or not MARKET.terminal_up:
        return None
    return Obj(login=12345678, balance=MARKET.balance, equity=MARKET.equity,
               margin=0.0, margin_free=MARKET.equity, currency="USD",
               trade_mode=MARKET.trade_mode)


def symbol_select(symbol, enable=True):
    MARKET.selected.append(symbol)
    return True


def symbols_get(*args, **kwargs):
    return tuple(Obj(name=n) for n in MARKET.symbol_names)


def order_check(request):
    pending = request.get("action") == TRADE_ACTION_PENDING
    code = MARKET.pending_check_retcode if pending else MARKET.order_check_retcode
    return Obj(retcode=code, comment="ok" if code == 0 else "rejected",
               request=request)


def symbol_info(symbol):
    return Obj(digits=3 if symbol.endswith("JPY") else 5,
               volume_min=0.01, volume_step=0.01, volume_max=100.0, point=0.00001,
               filling_mode=MARKET.filling_mask, trade_mode=4,
               trade_stops_level=0)


def symbol_info_tick(symbol):
    mid = MARKET.price()
    return Obj(bid=mid - MARKET.spread / 2, ask=mid + MARKET.spread / 2, last=mid,
               time=int(MARKET.now_ts))


def copy_rates_from_pos(symbol, timeframe, start_pos, count):
    if timeframe in MARKET.ohlc:
        arr = MARKET.ohlc[timeframe][-count:].copy()
        # Re-anchor so the newest bar sits at now_ts, as a live terminal would.
        if len(arr):
            arr["time"] = (MARKET.now_ts
                           - (len(arr) - 1 - np.arange(len(arr))) * BAR_SECONDS).astype("int64")
        return arr
    if timeframe in MARKET.series:
        closed, forming = MARKET.series[timeframe]
    else:
        closed, forming = MARKET.closed_prices, MARKET.forming_price
    prices = list(closed)
    if forming is not None:
        prices.append(forming)
    prices = prices[-count:]
    if not prices:
        return None
    n_forming = 1 if forming is not None else 0
    total = len(closed) + n_forming
    first_index = total - len(prices)
    # Place the newest bar at now_ts and step backwards from there.
    anchor = MARKET.now_ts - (total - 1) * BAR_SECONDS
    return make_rates(prices, first_index, anchor)


def positions_get(symbol=None):
    return tuple(p for p in MARKET.positions if symbol is None or p.symbol == symbol)


def orders_get(symbol=None, **kwargs):
    return tuple(o for o in MARKET.pending if symbol is None or o.symbol == symbol)


def history_deals_get(position=None, **kwargs):
    profit = MARKET.deals.get(int(position)) if position is not None else None
    if profit is None:
        return ()
    return (Obj(profit=profit, position=int(position)),)


def order_send(request):
    MARKET.orders.append(dict(request))

    if request.get("action") == TRADE_ACTION_PENDING:
        MARKET.next_ticket += 1
        MARKET.pending.append(Obj(
            ticket=MARKET.next_ticket, symbol=request["symbol"], volume=request["volume"],
            type=request["type"], magic=request.get("magic", 0),
            price_open=request["price"], sl=request.get("sl", 0.0),
            tp=request.get("tp", 0.0), expiration=request.get("expiration", 0)))
        return Obj(retcode=TRADE_RETCODE_DONE, price=request["price"],
                   volume=request["volume"], order=MARKET.next_ticket,
                   deal=0, comment="pending placed")

    if request.get("action") == TRADE_ACTION_REMOVE:
        MARKET.pending = [o for o in MARKET.pending if o.ticket != request["order"]]
        return Obj(retcode=TRADE_RETCODE_DONE, price=0.0, volume=0.0,
                   order=request["order"], deal=0, comment="removed")

    if request.get("action") == TRADE_ACTION_SLTP:
        for p in MARKET.positions:
            if p.ticket == request["position"]:
                p.sl = request.get("sl", p.sl)
                if "tp" in request:
                    p.tp = request["tp"]
        return Obj(retcode=TRADE_RETCODE_DONE, price=0.0, volume=0.0,
                   order=request["position"], deal=0, comment="sltp done")

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
