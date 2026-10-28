# Whetstone v0.1

**Lathe trades. Whetstone reads the outcomes and prepares evidence for better
rules.** Whetstone never places an order, modifies Lathe, or approves a change.

Whetstone is a separate Python package housed in this repository for a shared
record contract. It imports no Lathe modules and needs no MT5 installation.
Python 3.10+ and the standard library are enough. Tests use pytest.

## What is implemented

| Component | Behavior |
|---|---|
| Reviewer | Reads the Phase 1 JSONL snapshot; rejects unresolved, malformed, inconsistent, and conflicting records |
| Statistics | Win rate, 95% Wilson interval, average net P&L per trade (expectancy), total net P&L, closed-trade drawdown |
| Breakdowns | Pair, recorded session, setup, fill spread, and signal spread |
| Report | Markdown report, structured statistics, and a Claude-ready prompt for any interval or UTC week |
| Claude layer | Optional direct API call, or offline prompt/response import; aggregate statistics only |
| Proposals | One scalar rule change, hypothesis, rationale, falsification condition, supported subgroup, and source report ID |
| Validation gate | Registered holdout/forward windows, version/criteria checks, exported backtest/demo evidence checks, rollback reference |
| Authority | Every proposal requires human review and approval; no command applies it |

## Try it without credentials or trade data

Run from this repository's root:

```powershell
python -m whetstone demo --out whetstone-reports/demo
python -m pytest -q
```

Open `whetstone-reports/demo/report.md`. The 140 example trades are **synthetic**,
not Lathe results. Synthetic summaries cannot register a real validation plan.
A rendered Markdown example is also in [the example report](../docs/whetstone-demo.md).

## Review Lathe records

```powershell
python -m whetstone review --input ../vault/output/trade_closed.jsonl --week 2026-10-19 --out whetstone-reports/2026-10-19
```

`--week` must be a Monday and defines a UTC week with an exclusive end. For
another interval, use explicit timezone-aware boundaries instead:

```powershell
python -m whetstone review --input ../vault/output/trade_closed.jsonl --start 2026-10-01T00:00:00-04:00 --end 2026-10-28T00:00:00-04:00 --out whetstone-reports/october
```

Outputs:

- `report.md`: readable review and interpretation limits.
- `summary.json`: aggregate metrics, group counts, exclusions, and source/report hashes.
- `claude_prompt.txt`: instructions plus aggregate statistics; no raw trades or broker IDs.

The source JSONL is read once and never modified. Reports use the latest
snapshot; late reconciliation can change an earlier week's results. The source
fingerprint exposes that change. Re-run the desired interval to refresh it.
These commands generate weekly reports on demand; no scheduled task is installed.

Execution mode, demo/real account type, currency, strategy version, and synthetic
origin define separate cohorts. Results are not pooled across these boundaries.
Keep separate input files for separate accounts, as Phase 1 requires. A journal
row does not itself prove an authentic broker execution.

Break-even trades remain in the win-rate denominator. Expectancy is in account
currency per trade, not R or a percentage. Drawdown is the peak-to-trough drop in
cumulative **closed-trade** net P&L, starting at zero; it does not measure floating
losses, account equity, deposits, or risk-adjusted returns. Simultaneous closes
are summed before drawdown calculation.

The default minimum sample is 30 and cannot be reduced. It is an initial review
threshold, never proof of a stable edge. Every subgroup has its own count.
Wilson intervals assume independence; clustered trades and repeated subgroup
searches limit their interpretation. Session grouping uses Lathe's recorded
session key (currently a date), not inferred London/Asian/New York labels.

Spread buckets use per-symbol quantiles within the selected window, in price
units. Ties may leave an empty bucket. Fill spread never falls back to signal
spread; missing values are reported as unknown and cannot support a proposal.
Signal spread is measured when the pending order was submitted, not filled.
Quantile comparisons are descriptive, not evidence that a threshold will work.

## Claude, with or without an API connection

Manual path: give Claude `claude_prompt.txt`, save its JSON reply as
`claude-response.json`, then validate/import it:

```powershell
python -m whetstone import-proposals --summary whetstone-reports/october/summary.json --response claude-response.json --out whetstone-reports/october-proposals
```

API path: set `ANTHROPIC_API_KEY` in your own environment or secrets manager, and
provide a model ID available to your account. Some keys also require
`ANTHROPIC_WORKSPACE_ID`. Do not put keys into a committed file or report.

```powershell
python -m whetstone analyze --summary whetstone-reports/october/summary.json --model YOUR_CLAUDE_MODEL_ID --out whetstone-reports/october-proposals
```

Only `analyze` makes a network request. It sends aggregates to Anthropic's
Messages API, not raw trade/deal IDs, broker account numbers, or local paths.
Financial aggregate statistics still leave your machine when you run it.
Malformed/truncated replies and proposals referencing undersized or unknown
subgroups are rejected. Empty proposal arrays are valid. The API adapter is
covered with a mocked transport; a live Claude request has not been run here.

## Register a validation plan before testing

Choose one `proposal_id` from `proposals.json`. Register immutable windows and a
monetary drawdown limit appropriate to that cohort. Example placeholders below
must be replaced with your selected proposal and testing dates:

```powershell
python -m whetstone plan --proposals whetstone-reports/october-proposals/proposals.json --proposal-id PROPOSAL_ID --holdout-start HOLDOUT_START_ISO --holdout-end HOLDOUT_END_ISO --forward-start FORWARD_START_ISO --forward-end FORWARD_END_ISO --max-drawdown DRAWDOWN_LIMIT --minimum-trades 30 --out whetstone-reports/validation-plan
```

Discovery, holdout, and forward windows must be disjoint and ordered. Forward
testing must begin after registration. Preserve the plan file and its hash in
version control before looking at results. Hashes detect edits relative to their
stored values; they are not signed proof that someone never rewrote a plan.
Human review must verify genuine preregistration and unseen holdout data.

The gate requires:

- At least the registered number of trades in each holdout arm and forward arm.
- One declared rule change and the same candidate version in both stages.
- A matching baseline version and account currency.
- Improved holdout expectancy without worsened closed-trade drawdown.
- All arms within the registered monetary drawdown limit.
- Positive forward expectancy from actual demo executions (`live` + `demo`).
- Finished testing windows, distinct stages, no cross-stage reused trade IDs,
  no unresolved or synthetic rows, and intact monetary accounting.

A changed strategy can trade fewer times, so baseline/candidate counts need not
match. Both arms must be replayed over the same holdout dataset. This gate checks
export structure and declared provenance; it cannot independently prove that a
replay engine honored its source data or made only one code change.

## Backtest and forward export contract

**Whetstone does not contain a market replay/backtest engine.** Lathe's existing
BACKTEST mode is not one either. Run a genuine replay engine on reserved market
data, then supply its baseline and candidate trade outcomes. Do not pass a
filtered subset of old trades off as a new strategy backtest. An unimplemented
replay engine is a remaining system dependency, not a completed validation.

Each export is a JSON object:

```json
{
  "plan_id": "EXACT_PLAN_ID",
  "window": {"start": "EXACT_REGISTERED_START", "end": "EXACT_REGISTERED_END"},
  "candidate_version": "candidate-git-sha-or-tag",
  "change": {"rule": "spread.max_spread_pips", "from": 2.0, "to": 1.5},
  "dataset_id": "reserved-market-data-identifier",
  "data_sha256": "64-lowercase-hex-characters",
  "engine_version": "replay-engine-version",
  "baseline": [],
  "candidate": []
}
```

Replace arrays with complete schema-v1 trade records, including currency,
strategy version, timestamps, context, costs, and net P&L. The forward export
needs `candidate` only, with actual demo fills; PAPER cannot satisfy this gate
because Lathe does not simulate completed paper trades. Every trade must open
and close inside its registered window. The export's baseline version is checked
on every baseline row; candidate version is checked on every candidate row.

```powershell
python -m whetstone validate --plan whetstone-reports/validation-plan/validation_plan.json --holdout holdout-export.json --forward demo-export.json --out whetstone-reports/validation-result
```

Exit 2 means blocked evidence, exit 1 means an input/error condition, and exit 0
means the evidence checks passed **pending human review**. The output always
requires human approval. Review the full baseline/candidate code diff, data
provenance, broker costs, window selection, and rollback version yourself before
approving any change to Lathe. Whetstone has no deployment or rule-editing path.

## Acceptance still required

The automated tests and synthetic demonstration verify software behavior. They
do not establish trading performance. Remaining acceptance work is a broker
reconciliation check on Windows/MT5 demo, an actual Claude request if that path
is desired, and genuine unseen-data replay plus demo-forward evidence for each
proposal. None of those results are fabricated by the demo command.

API documentation used for the optional adapter:
https://platform.claude.com/docs/en/manage-claude/authentication
https://platform.claude.com/docs/en/api/versioning
