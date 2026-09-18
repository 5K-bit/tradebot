"""
The strategy must run once per CLOSED candle, not once per poll.

The original bug: trader.py evaluated on every poll and passed MT5's
still-forming bar straight to the strategy. The same crossover therefore
stayed true across consecutive polls, so the bot bought and then — seeing
identical data with in_position now True — closed the position it had just
opened, paying the spread twice, roughly every 30 seconds.
"""
import fake_mt5
import strategy
import trader
from conftest import find_cross_bar


def test_original_churn_reproduces_on_raw_candles(prices, cross_bar):
    """Regression guard: prove the old input shape really did flip buy -> close."""
    # State at the moment of entry: cross_bar-1 closed bars, the next still forming.
    raw = fake_mt5.make_rates(prices[:cross_bar])        # what the OLD code passed in

    assert strategy.generate_signal(raw, False) == "buy"
    # Same bar, same data, only in_position changed — this is the churn.
    assert strategy.generate_signal(raw, True) == "close"


def test_closed_bars_only_does_not_flip(prices, cross_bar):
    """With the forming bar stripped, that same market state produces no signal flip."""
    raw = fake_mt5.make_rates(prices[:cross_bar])
    closed = raw[:-1]
    assert strategy.generate_signal(closed, False) is None
    assert strategy.generate_signal(closed, True) is None


def test_one_order_across_many_polls_of_one_candle(market, conn, risk, config, vault,
                                                   prices, cross_bar):
    """20 polls while a single candle forms must produce exactly one order."""
    market.closed_prices = prices[:cross_bar]
    cursor = {}
    account = conn.account_info()
    open_count = 0

    for poll in range(20):
        # The forming bar's price moves around, as it would in a real market.
        market.forming_price = prices[cross_bar - 1] + 0.00005 * (poll % 4)
        open_count = trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                                       vault, cursor, account, open_count)

    assert len(market.orders) == 1, f"expected 1 order, got {len(market.orders)}"
    assert market.orders[0]["type"] == fake_mt5.ORDER_TYPE_BUY
    assert len(market.positions) == 1, "position was churned shut"
    assert open_count == 1


def test_cursor_advances_and_new_candle_is_evaluated(market, conn, risk, config, vault,
                                                     prices, cross_bar):
    market.closed_prices = prices[:cross_bar]
    market.forming_price = prices[cross_bar - 1]
    cursor = {}
    account = conn.account_info()

    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, cursor, account, 0)
    first = cursor["EURUSD"]
    assert first == (cross_bar - 1) * fake_mt5.BAR_SECONDS

    # A genuinely new candle closes.
    market.closed_prices = prices[:cross_bar + 1]
    market.forming_price = prices[cross_bar] + 0.0001
    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, cursor, account, 1)

    assert cursor["EURUSD"] > first, "cursor did not advance on a new candle"
    assert len(market.positions) == 1, "spurious close on the following bar"


def test_cursor_survives_restart(market, conn, risk, config, vault, prices, cross_bar, tmp_path):
    """A restart must not re-evaluate a candle already acted on."""
    from state import JsonState

    market.closed_prices = prices[:cross_bar]
    market.forming_price = prices[cross_bar - 1]
    account = conn.account_info()

    state = JsonState(str(tmp_path / "s.json"))
    cursor = {}
    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, cursor, account, 0)
    state.set(last_bar_time=cursor)
    orders_before = len(market.orders)

    # Restart: reload the cursor from disk, same candle still forming.
    reloaded = JsonState(str(tmp_path / "s.json"))
    cursor2 = {str(k): int(v) for k, v in reloaded.get("last_bar_time").items()}
    assert cursor2 == cursor

    trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                      vault, cursor2, account, 1)
    assert len(market.orders) == orders_before, "re-traded a candle after restart"


def test_too_few_candles_is_a_noop(market, conn, risk, config, vault):
    market.closed_prices = [1.1000]
    market.forming_price = 1.1001
    assert trader.run_symbol("EURUSD", config, conn, risk, fake_mt5.TIMEFRAME_M15,
                             vault, {}, conn.account_info(), 0) == 0
    assert market.orders == []
