# Whetstone trade review

Window: 2026-01-05T00:00:00+00:00 to 2026-01-12T00:00:00+00:00 (end excluded).

Included trades: 140. Minimum review sample: 30.

## Data quality

Excluded/duplicate counts: `{"outside_window": 0}`.

## live / demo / USD / synthetic-example-v1 / synthetic

| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |
|---|---:|---:|---:|---:|---:|---|
| Overall | 140 | 66.4% | 1.94 | 271.00 | 16.00 | exploratory |

Win-rate 95% Wilson interval: 58.3% to 73.7%.

### symbol

| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |
|---|---:|---:|---:|---:|---:|---|
| EURUSD | 70 | 67.1% | 2.13 | 149.00 | 16.00 | exploratory |
| USDJPY | 70 | 65.7% | 1.74 | 122.00 | 16.00 | exploratory |

### session

| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |
|---|---:|---:|---:|---:|---:|---|
| 2026-01-05 | 24 | 66.7% | 2.00 | 48.00 | 16.00 | insufficient |
| 2026-01-06 | 24 | 66.7% | 2.00 | 48.00 | 16.00 | insufficient |
| 2026-01-07 | 24 | 66.7% | 2.00 | 48.00 | 16.00 | insufficient |
| 2026-01-08 | 24 | 66.7% | 2.00 | 48.00 | 16.00 | insufficient |
| 2026-01-09 | 24 | 66.7% | 2.00 | 48.00 | 16.00 | insufficient |
| 2026-01-10 | 20 | 65.0% | 1.55 | 31.00 | 16.00 | insufficient |

### setup

| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |
|---|---:|---:|---:|---:|---:|---|
| trend_pullback | 140 | 66.4% | 1.94 | 271.00 | 16.00 | exploratory |

### spread_at_entry

Buckets are relative to this symbol/window; thresholds: `{}`.

| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |
|---|---:|---:|---:|---:|---:|---|
| EURUSD:unknown | 70 | 67.1% | 2.13 | 149.00 | 16.00 | exploratory |
| USDJPY:unknown | 70 | 65.7% | 1.74 | 122.00 | 16.00 | exploratory |

### spread_at_signal

Buckets are relative to this symbol/window; thresholds: `{"EURUSD": {"low_max": 0.0002, "medium_max": 0.0002, "unit": "price"}, "USDJPY": {"low_max": 0.01, "medium_max": 0.02, "unit": "price"}}`.

| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |
|---|---:|---:|---:|---:|---:|---|
| EURUSD:high | 23 | 100.0% | 11.00 | 253.00 | 0.00 | insufficient |
| EURUSD:low | 47 | 51.1% | -2.21 | -104.00 | 126.00 | exploratory |
| USDJPY:high | 23 | 100.0% | 11.00 | 253.00 | 0.00 | insufficient |
| USDJPY:low | 24 | 0.0% | -16.00 | -384.00 | 384.00 | insufficient |
| USDJPY:medium | 23 | 100.0% | 11.00 | 253.00 | 0.00 | insufficient |

## Interpretation limits

- Closed-trade P&L drawdown is not equity or intratrade drawdown.
- Thirty trades is a review threshold, not statistical proof.
- Subgroup comparisons are exploratory and may reflect multiple testing.
- Wilson intervals assume independent trials; clustered trades weaken that assumption.
- Observed performance does not establish a causal rule improvement.
