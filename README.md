# Lathe — Forex Trader Module

Implements LATHE ADAPTIVE SESSION STRATEGY v1 — six setups, a six-category
signal score, and a protection layer — against an MT5 account.

> ### ⚠️ Ships in PAPER mode. Not yet validated against a live broker.
>
> `safety.default_mode` is `PAPER`, so no order reaches the broker. Going live
> needs **both** `default_mode: LIVE` and `live_trading_enabled: true` —
> setting one alone still runs PAPER, so a mistyped mode cannot start trading
> real money.
>
> Every test runs against a fake MT5 terminal; no order has ever reached a real
> broker. Demo first, and read the REVIEW markers in `config.yaml`.

## Requirements

- **Windows** (or Windows-in-Wine on Linux/Mac) — the `MetaTrader5` package
  only works alongside a running MT5 desktop terminal, logged into your
  broker account.
- Python 3.10+
- MT5 terminal installed and logged in at least once manually first (so it
  trusts the account/server).

## Setup

```bash
pip install -r requirements.txt
```

Set credentials as environment variables (never put these in config.yaml
or commit them):

```bash
setx MT5_LOGIN "12345678"
setx MT5_PASSWORD "your-password"
setx MT5_SERVER "YourBroker-Live01"
```

(Use `export` instead of `setx` if running under Wine/bash.)

Edit `config.yaml`:
- `symbols` — which pairs to trade
- `timeframe` — candle timeframe the strategy runs on
- `pip.size` / `pip.value_per_lot` — defaults used to size positions
- `pip.overrides.<SYMBOL>` — per-symbol pip settings. JPY-quoted pairs use
  `0.01`, not `0.0001`; the bot **refuses to start** if a JPY pair would
  silently inherit the default, because that mis-sizes it by 100x
- `risk.risk_per_trade_pct` — % of equity risked per trade
- `risk.max_daily_loss_pct` — **kill switch**: trading halts for the day
  once equity drops this much from the day's starting value
- `risk.broker_utc_offset_hours` — your broker's server timezone, so the
  daily loss limit resets at *their* midnight rather than your machine's
- `state.path` — where the kill-switch baseline and candle cursor persist
- `vault.log_path` — where trade activity gets logged as markdown

## The pipeline

Per symbol, once per closed M15 bar inside the session:

```
session open?  ->  H1 regime  ->  M15 setup  ->  M5 entry refinement
               ->  RR check   ->  signal score >= 80
               ->  protection gates  ->  risk sizing  ->  order
```

Any gate that fails ends the evaluation and records a reason. Every decision,
taken or rejected, is written to the trade log — the rules require a reason for
rejections, not only for fills.

| Module | Role |
|---|---|
| `sessions.py` | the 22:00–06:00 New York window, DST-aware, Fri/Sat excluded |
| `regime.py` | H1 EMA20/EMA50 + ADX14 + ATR14 → trend / range / neither |
| `setups.py` | the six entry patterns (A–F) |
| `scoring.py` | the six-category signal score and its hard overrides |
| `protection.py` | session, concurrency, trade count, cooldown, spread, news, data health, account drawdown |
| `risk_manager.py` | position sizing and the daily loss stop |
| `trade_management.py` | structural ATR stop, break-even at +1R, trailing from +1.5R, 2R target |
| `indicators.py` | EMA, Wilder ATR and ADX, swing structure |

### The setups

| | Setup | Regime | Trigger | Min RR |
|---|---|---|---|---|
| A | Trend Pullback long | bullish trend | higher low + 3-bar M5 high break | 2.0 |
| B | Trend Pullback short | bearish trend | lower high + 3-bar M5 low break | 2.0 |
| C | Range Reversion long | range | bottom 20%, RSI<35, rejection close | 1.5 |
| D | Range Reversion short | range | top 20%, RSI>65, rejection close | 1.5 |
| E | Breakout+Retest long | trend | 12-bar high broken, retested, M5 break | 2.0 |
| F | Breakout+Retest short | trend | 12-bar low broken, retested, M5 break | 2.0 |

Entries are **pending stop orders** placed beyond the trigger candle
(`trigger_high + 0.05 × ATR_M5`), cancelled if price does not reach them within
three M5 candles.

### The signal score

Six categories, **summed** to a maximum of 100:

| Category | Max |
|---|---|
| Regime quality | 25 |
| Setup location | 20 |
| Entry trigger | 20 |
| Reward / risk | 15 |
| Execution quality | 10 |
| Session / volatility | 10 |

A trade needs **80 or more**, a trigger scoring **exactly 20**, a passing
spread filter, RR at or above the setup minimum, and every hard gate green.
The score never overrides a hard rule: a 90 with a failed spread filter is a
HOLD, not a BUY.

> The strategy document renders the formula with asterisks between the terms.
> Those are mangled bullet points — the categories max at 100 only when added,
> and both worked examples in the document confirm it
> (25+20+20+15+0+10 = 90, and 25+20+20+12+10+8 = 95 matching its
> `"score_total": 95`). Both are encoded as tests in `test_scoring.py`.

## Run

```bash
python3 trader.py
```

It polls on the interval in `config.yaml`, and **evaluates your strategy
once per closed candle** — not once per poll. Polling faster than your
timeframe is fine; the extra polls are no-ops. It sizes and places orders
through the risk manager and appends every action to your vault log.

The bot writes a small state file (`state.path`) holding the kill-switch
baseline, the halt flag, and the last candle processed per symbol. It is
per-machine and per-account — don't commit it, and don't delete it mid-day
unless you intend to reset the daily loss limit.

**Ctrl+C stops the loop but does not close open positions** — check MT5
directly before walking away.

## Connecting to MT5

The `MetaTrader5` package talks to a *running* MT5 terminal on the same
machine. There is no API key and no remote endpoint — if the terminal is closed,
nothing works.

1. **Install the MT5 terminal** from your broker and log into a **demo**
   account. (File → Open an Account, or your broker's demo signup.)
2. **Enable algorithmic trading** — the `Algo Trading` button in the toolbar,
   or Tools → Options → Expert Advisors → *Allow algorithmic trading*.
   Without this every order is rejected.
3. **Open an H1, M15 and M5 chart** for each symbol and scroll back a few
   hundred bars. MT5 downloads history lazily; the indicators need ~400 bars
   and will otherwise see nothing.
4. `pip install -r requirements.txt` in the same Python the bot runs under.
5. Set the credentials as environment variables — never in the config file:

   ```
   setx MT5_LOGIN "12345678"
   setx MT5_PASSWORD "your-demo-password"
   setx MT5_SERVER "YourBroker-Demo01"
   ```

   The server name must match the terminal exactly; copy it from the login
   dialog. Open a new terminal window after `setx` so it picks the values up.

6. **Run the preflight check before the bot:**

   ```bash
   python3 preflight.py
   ```

   It sends no orders — order validity is probed with MT5's `order_check()`,
   which asks the broker whether a request *would* be accepted. It reports the
   things that differ between brokers and that no test against a fake terminal
   can tell you:

   - whether the account is demo or real
   - what your symbols are actually called (`EURUSD`, `EURUSD.raw`, `EURUSDm`…)
   - which order filling modes the broker accepts
   - whether pending stop orders with an expiry are accepted — the bot enters
     with these, so a broker that refuses them breaks every entry
   - the server's UTC offset, for `broker.utc_offset_hours`
   - current spreads, and a suggested `max_spread_pips`
   - whether enough history is loaded

   It exits non-zero if anything is blocking, and prints the config values to
   set. Run it again during 22:00–06:00 New York: spreads widen overnight, and
   that is the only window this bot trades in.

7. **Run the bot.** It ships in PAPER, so it decides and logs but sends
   nothing:

   ```bash
   python3 trader.py
   ```

   Watch `lathe_rejections.jsonl`. A quiet session is still informative — it
   tells you which gate is holding, and whether that is what you intended.

## Configuration

`config.yaml` is grouped by concern; the engine wants flat values.
`config_schema.py` is the single place that maps one to the other, so a rename
in the config never has to be chased through a dozen readers. It accepts both
the grouped layout and the older flat one.

It also **refuses configurations that would leave the bot running but never
trading** — the failure mode that looks like success. Two are checked at
startup:

- `data.maximum_candle_age_seconds` at or below the setup timeframe. An M15 bar
  is legitimately up to 900s old just before the next closes, so a 120s limit
  rejects healthy data for 87% of every bar.
- `indicators.swing.lookback` above 5. A swing needs that many bars on *each*
  side, so 20 means 41 bars per swing and two swings for a higher low —
  trend-pullback entries would effectively never fire.

Values marked `SPEC` come from the strategy document. Values marked `REVIEW`
do not, and are yours to set deliberately.

### Modes

| Mode | Behaviour |
|---|---|
| `BACKTEST` / `PAPER` | decisions are made and logged; no order is sent |
| `LIVE` | orders reach the broker — needs `live_trading_enabled: true` too |

The guard lives on the connector, so there is exactly one place an order can
escape and one place to test it.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

The suite runs anywhere — Linux, CI, a machine with no MT5 at all. It stubs
the `MetaTrader5` package with a fake terminal (`tests/fake_mt5.py`) that the
real bot code drives unmodified, so you can develop a setup in `setups.py`
and exercise the whole pipeline without pointing anything at a broker. The fake is installed
unconditionally, so tests never reach a live terminal even on Windows.

Warnings are configured as failures (`pytest.ini`). On a bot meant to run
unattended for days against real money, a leaked file handle or a deprecation
notice is worth hearing about while it is still cheap to fix.

Coverage is aimed at the things that cost money rather than at a line-count
target: indicator math against hand-computed values, the session window across
DST and the weekend closure, regime classification including the deliberate
"neither" band, every protection gate, stop management that can only ever move
in the trade's favour, and an end-to-end run of the main loop through a
terminal outage and both kill switches.

The suite is mutation-tested: every strategy and safety rule is broken in turn
in a scratch copy, and the suite must fail for each one. The current sweep is
31/31 on the strategy rules. That exercise earns its keep — the first run found
12 holes, including that nothing at all tested the spread and stop validators,
and it surfaced a real bug where the spread median was never persisted, so a
restart silently disabled the "≤ 1.5× median" rule.

## Protection

| Gate | Source |
|---|---|
| Trading window | 22:00–06:00 America/New_York, Fri/Sat opens skipped |
| Concurrency | 1 open trade |
| Session cap | 3 trades per overnight session |
| Cooldown | 2 consecutive losses sit out the rest of the session |
| Daily stop | 5% of equity, persists across restarts |
| Account kill switch | 10% drawdown from the equity high-water mark — **sticky**, cleared only by hand |
| Spread filter | ≤ 1.5× rolling median AND ≤ 10% of ATR_M5, plus a pip ceiling |
| Stop validation | ≥ 2× current spread AND ≤ 1.5× ATR_M15 |
| Data health | stale candles, missing ticks and a dropped terminal all mean no trade |
| News blackout | manual windows in config (see below) |

### News blackout is manual

The MetaTrader5 Python API exposes no economic calendar, so high-impact news
windows are entered by hand in `config.yaml` under
`protection.news_blackout_windows`. **Anything not listed is not blacked out.**
Automating this needs an external calendar provider, which is not wired in.

## What the bot will and won't touch

Every order is tagged with a magic number (`MAGIC` in `mt5_connector.py`),
and the bot only ever reads back and closes positions carrying that tag.
Your manual trades and any other EA on the account are invisible to it:
they don't block its entries, don't count toward `max_open_positions`, and
will never be closed by it. If you run two copies of this bot against one
account, give each a different `MAGIC`.

## Safety notes (read before going live)

- The **max_daily_loss_pct kill switch is on by default** and halts new
  trades for the rest of the day if tripped. It does not close existing
  positions automatically. The halt and the day's starting equity are
  written to disk, so restarting the bot does **not** clear a halt or
  re-baseline to the drawn-down equity.
- Every trade requires a stop-loss — `risk_manager.position_size()` raises
  an error rather than sizing a trade with no stop.
- Lot sizes are **floored** to your broker's volume step and checked against
  its minimum, so rounding can never push you above the risk you configured.
- If the MT5 terminal link drops, the bot reconnects with backoff and keeps
  running rather than exiting and leaving open positions unmanaged.
- `max_lot_size` and `max_open_positions` are hard ceilings independent of
  what the strategy asks for.
- This is infrastructure, not a profitable strategy — the included
  moving-average crossover is a placeholder to prove the pipeline works,
  not a recommendation. Test any real rules on a demo account before
  pointing this at a funded live account; leverage means losses can
  exceed what you'd expect from the % risked per trade if slippage or
  gaps occur.
