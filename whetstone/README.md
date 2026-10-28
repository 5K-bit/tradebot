# Whetstone v0.2: numerical strategy research

Lathe executes. Whetstone reviews records, replays historical quotes, and tests
small rule changes. **No LLM, tokens, model server, network analysis call, or
streamed language response is required.** Every report uses calculated values
and fixed text. No Whetstone command changes Lathe or sends a broker order.

## Components

| Command | Purpose |
|---|---|
| `review` | Statistics and Markdown reports from completed trade records |
| `replay` | Historical bid/ask tick execution using Lathe's pure strategy modules |
| `research` | Bounded one-rule training search followed by a later screening period |
| `plan` | Register a fixed proposal, unseen holdout/forward windows, and criteria |
| `replay-plan` | Run the registered holdout baseline/candidate, without searching it |
| `validate` | Check holdout and actual demo-forward evidence; retain human approval |
| `import-proposals` | Validate a human-authored hypothesis against report statistics |
| `demo` | Generate an explicitly synthetic statistics example |

Whetstone is a separate package in this repo. Reporting and evidence checks use
Python 3.10+ standard-library code. Historical replay reuses Lathe's NumPy-based
indicators and requires the replay dependencies. Neither needs MetaTrader5.

```powershell
python -m pip install -r whetstone/requirements-replay.txt
python -m whetstone --help
```

## Start with a smoke test

```powershell
python -m whetstone replay --ticks whetstone/examples/ticks.synthetic.csv --config config.yaml --assumptions whetstone/examples/assumptions.synthetic.json --start 2026-01-15T03:30:00Z --end 2026-01-15T03:31:00Z --out whetstone-reports/replay-smoke
python -m pytest -q
```

The four synthetic ticks intentionally yield **zero trades**: there is no
indicator warmup. This verifies wiring without fabricating performance. For a
report-layout demonstration with synthetic closed trades:

```powershell
python -m whetstone demo --out whetstone-reports/demo
```

[The report example](../docs/whetstone-demo.md) contains synthetic results only.

## 1. Supply historical market data and costs

Replay consumes a chronological, interleaved CSV of quotes for every configured
symbol. It does not download prices, contact MT5, or infer bid/ask from candles.

```csv
timestamp,symbol,bid,ask,quote_to_account_rate
2026-01-15T03:30:00+00:00,EURUSD,1.10000,1.10010,1.0
```

- `timestamp`: timezone-aware quote time; rows must be nondecreasing.
- `bid`/`ask`: positive observed prices with ask >= bid.
- `quote_to_account_rate`: units of account currency per unit of quote currency,
  at that timestamp. This is required for P&L conversion and historical sizing.
  EURUSD on a USD account uses 1; other pairs need a sourced conversion policy.
- Include pre-window history for indicators. At least 60 complete H1 bars are
  required by the default configuration; provide enough for the full 400-bar
  buffers when matching live indicator history. The first partial bar is discarded.
- Missing ticks remain gaps; no synthetic intermediate quotes are invented.
  Missing symbols are flagged and disqualify a run from candidate selection.
  Presence of a symbol alone does not prove the dataset is complete.

Use the broker/feed you intend to validate against, and preserve its provenance
and timezone conversion. Verify completeness outside the program. Avoid mixing
feeds without documenting the change. Data quality remains an acceptance task.

Create an assumptions JSON based on the example, replacing every illustrative
value with your own evidence. Set `data_origin: historical_ticks` only for real
historical input. Declare account currency, starting balance, fixed historical
broker UTC offset, contract sizes, volume limits/steps, commission and fees per
lot **per side**, and adverse slippage in price units. Commission/fee inputs are
nonnegative charges; replay writes them as negative P&L components.

The historical broker offset must agree with any explicit Lathe configuration.
Split studies at broker offset changes; this version does not infer historical
DST from the current terminal. Account conversion and slippage are execution
assumptions, not evidence of actual historical fills.

Swaps are explicit signed per-lot account-currency events:

```json
{
  "timestamp": "2026-01-15T22:00:00+00:00",
  "symbol": "EURUSD",
  "long_per_lot": -2.0,
  "short_per_lot": 0.5
}
```

Place these inside `swaps` and declare `swap_policy: explicit_events`. An empty
list explicitly assumes no swap charges; Whetstone does not guess rollover
calendars, triple swaps, fees, or news events. Enter historical news blackout
windows in the research copy of Lathe's config. Never overwrite the running
configuration when preparing an experiment.

## 2. Establish a baseline replay

```powershell
python -m whetstone replay --ticks data/ticks.csv --config config.yaml --assumptions data/assumptions.json --start 2026-07-01T00:00:00Z --end 2026-09-01T00:00:00Z --out whetstone-reports/baseline
```

Outputs `replay.json` and simulated `trade_closed.jsonl`. Closed outcomes are
compatible with `review`. Historical records carry `execution_mode: paper`,
`data_origin: historical_replay`, `simulated: true`, and no fabricated broker
IDs. This replay simulator is separate from Lathe's existing PAPER/BACKTEST
modes, which still do not simulate completed positions.

Replay imports the actual configuration mapping, indicators, regime classifier,
setup detectors, scorer, session logic, protection rules, risk sizing, and stop
management functions. It **does not import the live trader or broker connector**.
Pure-module sharing is not proof of full live-engine parity; compare decisions
and fills against broker demo logs before trusting results.

Execution conventions:

- Bid bars aggregate on UTC boundaries. Only completed bars enter decisions.
- Management and signal polling use available ticks at the configured poll
  interval; broker-side pending fills/stops/targets inspect every recorded tick.
- A new pending order can fill only on a subsequent quote. Buys enter at ask,
  sells at bid, with declared adverse slippage. Gaps use the observed quote.
- Long stops/targets use bid; short stops/targets use ask. Stops receive adverse
  slippage; targets fill at their stated limit with no favorable improvement.
- Only one concurrent position/order is supported. Fills are full-volume;
  partial fills, liquidity, broker rejects, and network latency are not modeled.
- Currency conversion uses the supplied tick rate. Account risk uses simulated
  equity and a historical clock, never the computer's current date.
- An open position at the interval end is retained with marked P&L. It is not
  force-closed at a future price or silently dropped. Such runs cannot win a search.
- Pending orders at the boundary are reported; they have incurred no fill costs.

Results record data, config, cost-assumption, strategy-source, and replay-source
hashes. Tick-sampled equity drawdown and closed-trade drawdown are both reported;
neither can capture price moves absent from the input feed.

## 3. Search one rule on training data

```powershell
python -m whetstone research --ticks data/ticks.csv --config config.yaml --assumptions data/assumptions.json --rule spread.max_spread_pips --values "[1.0,1.5]" --train-start 2026-07-01T00:00:00Z --train-end 2026-09-01T00:00:00Z --screen-start 2026-09-01T00:00:00Z --screen-end 2026-10-01T00:00:00Z --out whetstone-reports/spread-research
```

Dates and values illustrate the command shape, not a recommendation for your
broker. Supported rules are deliberately limited:

- `spread.max_spread_pips`: tighten the existing ceiling only.
- `scoring.execution_threshold`: raise the existing threshold only, up to 100.

There are at most eight declared candidates and one changed scalar rule. Risk
limits are not search variables. Every candidate and baseline replays identical
data/cost assumptions. The original config stays untouched.

Training selects the highest positive net expectancy that improves baseline
without worsening either closed-trade or tick-sampled equity drawdown. Each arm
must have at least 30 completed trades (or a larger configured minimum), no open
boundary exposure, and no flagged missing symbols. This threshold is a screening
rule, not statistical proof. All trial results are retained in `research.json`.

Only the selected candidate and baseline then run on the later screening window.
Failure rejects that candidate; Whetstone does not try alternatives against the
same screen. One chronological split is **not** a complete walk-forward study.
Repeatedly trying new searches against the same period will still overfit it.

Outputs: `research.json`, `research.md`, and `proposals.json`. A proposal is
emitted only after training and screening criteria pass on nonsynthetic input.
Synthetic experiments never produce an actionable proposal. Every inspected
period, including screening, becomes discovery data; it cannot be reused as
unseen holdout in a subsequent validation plan.

## 4. Register and run untouched holdout

Use the `proposal_id` from `proposals.json`:

```powershell
python -m whetstone plan --proposals whetstone-reports/spread-research/proposals.json --proposal-id PROPOSAL_ID --holdout-start HOLDOUT_START_ISO --holdout-end HOLDOUT_END_ISO --forward-start FORWARD_START_ISO --forward-end FORWARD_END_ISO --max-drawdown DRAWDOWN_LIMIT --minimum-trades 30 --out whetstone-reports/validation-plan
```

Replace placeholders with explicit timezone-aware dates and a monetary drawdown
limit. All windows must be ordered and disjoint; forward testing starts after
plan registration. Preserve the plan before inspecting results. Hashes are not
signed evidence of preregistration, so human review still verifies that claim.

For research-generated plans, run the registered holdout without another search:

```powershell
python -m whetstone replay-plan --ticks data/reserved-ticks.csv --config config.yaml --assumptions data/assumptions.json --plan whetstone-reports/validation-plan/validation_plan.json --candidate-version CANDIDATE_RELEASE_SHA_OR_TAG --out whetstone-reports/holdout
```

This refuses a baseline source/config different from the one registered by
research, applies only the fixed rule change in memory, and emits
`holdout_export.json`. It refuses incomplete/open-exposure runs. Candidate release
name is supplied by the operator: verify the actual release's code and config
against recorded hashes before using that same release for demo-forward testing.
A version label by itself is not proof of matching code.

## 5. Demo forward test and human approval

Run the reviewed candidate release on a dedicated demo account. The journal must
record actual fills (`live` execution + `demo` account). Simulation/PAPER cannot
satisfy the forward gate. Build a forward export with the plan ID, exact plan
window/change, candidate version, dataset identifier, data SHA-256, engine/version
provenance, and a `candidate` array of complete Phase 1 journal records.

```powershell
python -m whetstone validate --plan whetstone-reports/validation-plan/validation_plan.json --holdout whetstone-reports/holdout/holdout_export.json --forward demo-export.json --out whetstone-reports/validation-result
```

The gate requires sufficient trades, matching windows/versions/currency, intact
monetary accounting, improved holdout expectancy without worsened closed-trade
drawdown, the registered drawdown limit, and positive demo-forward expectancy.
It checks supplied evidence; independent verification of broker records,
unseen-data selection, execution assumptions, and the exact one-rule code/config
diff remains human work. Additional registered time splits and sensitivity to
cost assumptions are necessary before relying on a pattern.

Exit 2 means evidence blocked, exit 1 means an input/error condition, and exit 0
means evidence checks passed **pending human review**. No command approves or
deploys a change. The plan retains the baseline as its rollback reference.

## Review-only reports

```powershell
python -m whetstone review --input ../vault/output/trade_closed.jsonl --week 2026-10-19 --out whetstone-reports/2026-10-19
```

`--week` defines a UTC Monday-to-Monday interval, end excluded. Alternatively use
`--start` and `--end` with explicit offsets. The source snapshot is never changed.
Reports are generated on demand; no automatic scheduled task is installed.

Win rate includes break-even trades in its denominator. Expectancy is mean net
P&L per trade in account currency. Cohorts separate execution mode, account type,
currency, strategy version, and data origin. Keep separate inputs per account.
Pair/session/setup groups each show their own sample count. Session means the
recorded session key (currently a date), not an inferred regional market session.

Report drawdown uses cumulative **closed-trade** P&L from zero, aggregating
simultaneous closes. It differs from replay's tick-sampled equity drawdown.
Wilson win-rate intervals assume independent trials; clustered trades and
multiple comparisons limit interpretation. A 30-trade sample is not proof.

Spread groups use per-symbol quantiles in price units. Fill and signal spreads
remain distinct; unknown fill spread cannot support a threshold proposal.
Late journal reconciliation can revise previous reports; source hashes expose
such changes. Re-run the interval to refresh its snapshot.

## Current acceptance status

Automated tests and synthetic cases verify software behavior, not profitability.
No historical broker dataset or cost schedule has been evaluated in this session.
The next operational dependency is your historical bid/ask export, conversions,
and broker costs, followed by decision/fill comparison against Windows/MT5 demo.
There is no model connection left to configure.
