# Lathe — Forex Trader Module

Implements LATHE ADAPTIVE SESSION STRATEGY v1 against a live MT5 account.

> ### ⚠️ This bot cannot currently open a trade — by design
>
> The strategy document names three setups (Trend Pullback, Range Reversion,
> Breakout + Retest) and requires a signal score of 80 or more, but defines
> neither the setup rules nor the score formula. `setups.py` and `scoring.py`
> therefore ship **fail-closed**: they refuse and record why, rather than
> guessing at entry logic for an account with money in it.
>
> Everything around them is built and tested: session gating, H1 regime
> classification, the protection layer, risk sizing, RR validation, trade
> management and decision logging. Fill in the two stubs — each lists exactly
> what it needs in its docstring — and the bot goes live.
>
> Until then it runs in observation mode, logging the decision it would have
> made at every setup bar.

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
| `setups.py` | the three entry patterns — **stubs, see above** |
| `scoring.py` | the ≥80 signal score — **stub, see above** |
| `protection.py` | session, concurrency, trade count, cooldown, spread, news, data health, account drawdown |
| `risk_manager.py` | position sizing and the daily loss stop |
| `trade_management.py` | structural ATR stop, break-even at +1R, trailing from +1.5R, 2R target |
| `indicators.py` | EMA, Wilder ATR and ADX, swing structure |

### Filling in a setup

Each detector takes the closed M15 candles, the closed M5 candles, the current
`Regime`, and the config, and returns `(SetupCandidate, reason)` or
`(None, reason)`. The candidate carries entry, stop, target and the structural
level the stop is anchored to; RR is computed from those and checked against
the strategy minimums (2.0 for trend and breakout, 1.5 for range) before
anything is sized. See the docstrings in `setups.py` and `scoring.py` for the
specific parameters each one needs.

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

The suite is mutation-tested: 31 safety rules were each broken in a scratch
copy and the suite confirmed to fail for every one. That exercise is what
caught the two tests that were passing for the wrong reason.

## Protection

| Gate | Source |
|---|---|
| Trading window | 22:00–06:00 America/New_York, Fri/Sat opens skipped |
| Concurrency | 1 open trade |
| Session cap | 3 trades per overnight session |
| Cooldown | 2 consecutive losses sit out the rest of the session |
| Daily stop | 5% of equity, persists across restarts |
| Account kill switch | 10% drawdown from the equity high-water mark — **sticky**, cleared only by hand |
| Spread filter | configurable ceiling, checked per decision |
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
