# Lathe — Forex Trader Module

Adds a live MT5 trading capability to the assistant. Trades real money in
your MT5 account against rules you define in `strategy.py`.

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

## Put your strategy in

Open `strategy.py` and replace `generate_signal()` with your actual
entry/exit rules. It receives the recent candles and whether a position
is already open, and returns `"buy"`, `"sell"`, `"close"`, or `None`.
Everything else (sizing, execution, logging) stays as-is.

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
python3 -m pytest tests/
```

The suite runs anywhere — Linux, CI, a machine with no MT5 at all. It stubs
the `MetaTrader5` package with a fake terminal (`tests/fake_mt5.py`) that the
real bot code drives unmodified, so you can change `strategy.py` and check
the pipeline without pointing anything at a broker. The fake is installed
unconditionally, so tests never reach a live terminal even on Windows.

Coverage is aimed at the things that cost money rather than at a line-count
target: that the strategy runs once per closed candle, that the bot only ever
closes positions it opened, that the kill switch survives a restart, that lot
sizing cannot exceed the configured risk, and an end-to-end run of the main
loop through a simulated terminal outage.

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
