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
- `risk.risk_per_trade_pct` — % of equity risked per trade
- `risk.max_daily_loss_pct` — **kill switch**: trading halts for the day
  once equity drops this much from the day's starting value
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

It polls on the interval in `config.yaml`, evaluates your strategy per
symbol, sizes and places orders through the risk manager, and appends
every action to your vault log.

**Ctrl+C stops the loop but does not close open positions** — check MT5
directly before walking away.

## Safety notes (read before going live)

- The **max_daily_loss_pct kill switch is on by default** and halts new
  trades for the rest of the day if tripped. It does not close existing
  positions automatically.
- Every trade requires a stop-loss — `risk_manager.position_size()` raises
  an error rather than sizing a trade with no stop.
- `max_lot_size` and `max_open_positions` are hard ceilings independent of
  what the strategy asks for.
- This is infrastructure, not a profitable strategy — the included
  moving-average crossover is a placeholder to prove the pipeline works,
  not a recommendation. Test any real rules on a demo account before
  pointing this at a funded live account; leverage means losses can
  exceed what you'd expect from the % risked per trade if slippage or
  gaps occur.
