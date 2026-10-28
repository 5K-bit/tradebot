# Whetstone Phase 1: trade outcomes

Lathe remains a deterministic executor. This component supplies the data foundation. The separate `whetstone/` package
provides numerical review and historical research without an LLM.

## Verified PAPER behavior

`MT5Connector._simulated()` returns synthetic tickets without storing simulated
positions, fills, exits, or costs. `open_positions()` reads the broker, not a
paper portfolio. PAPER and BACKTEST therefore cannot supply completed simulated
trades. The journal stores these submissions as `paper_signal`, with
`execution_mode: paper` and no fabricated broker IDs or `trade_closed` row.
The decision log still includes their trade ID, signal ID, and mode.

Actual broker orders use `execution_mode: live`; their separately observed
`account_type` is `demo` or `real`. Contest and unknown account types are refused
by the journal. Defaults remain PAPER with live execution disabled and a demo
account required. No account settings are changed by this feature.

## Lifecycle and persistence

1. Before an executable signal is submitted, UUID trade/signal IDs and a unique
   broker comment are persisted under `journal_trades` in `.lathe_state.json`.
2. Submission stores the broker order ID (and deal ID, if returned). Acknowledged
   order submission is not a fill. On the first loop and every configured
   reconciliation interval (default 60 seconds), order history supplies the
   stable position identifier; position history supplies all entry/exit deals.
3. Partial entries and exits are retained while the position or entry order is
   open. Reconciliation deduplicates deal IDs and computes volume-weighted
   entry/exit prices. A cancelled, rejected, or expired unfilled order is not
   counted as a completed trade.
4. A closed position resolves only when entry/exit volumes balance, entry volume
   matches the historical order's executed volume, all required cost fields are
   present, and position attribution is unambiguous. History errors, incomplete
   fills, shared netting positions, reversals, and unsupported adjustments remain
   unresolved with null P&L. API `None` is never treated as an empty account.
5. The state snapshot commits before output projections. Protection counters
   store each resolved trade ID with their loss-streak update, preventing repeat
   reconciliation or a restart from counting the same outcome twice.

Order and position identifiers are distinct. A position's current ticket may
change; reconciliation and management use its stable identifier. All deals for
that position are included, including manual closing deals with another magic.

If a process dies after sending an order but before persisting its receipt,
reconciliation searches current/historical orders for the saved unique comment.
Missing or ambiguous matches stay unresolved and block new entries; orders are
never resent automatically. Brokers that rewrite comments require manual review.

## Record contract

The record follows the proposed schema version 1, with `account_currency` added.
Trade/order/deal IDs are strings. Each fill/exit also carries `deal_id` and
`order_id`. `lots` is filled entry volume, not requested volume. UTC timestamps
come from broker deal milliseconds. Strategy version is a clean Git SHA, or a
source/config SHA-256 fingerprint when the checkout is dirty or Git unavailable.

`commission`, `swap`, and `fees` retain the broker's signed monetary values:

```
net_pnl = gross_pnl + commission + swap + fees
```

Zero is permitted only when supported by complete history; missing values are
null. Financial `outcome_status: resolved` does not imply all optional context is
available. `missing_fields` lists missing context even for a resolved outcome.
Use only resolved rows for performance calculations. Unresolved rows may represent
an unconfirmed closure or submission, not necessarily a known closed position.

Context includes setup, regime, session, M15 ATR and its timeframe, plus
`spread_at_signal` in price units. Exact fill-time bid/ask spread is not present
in deal history, so `spread_at_entry` remains null and is listed as missing.
A future tick-capture component is needed for trustworthy fill-spread analysis.
Do not substitute order-submission spread for fill spread.

Exit reasons preserve broker evidence: `tp`, `sl`, `manual`, `stop_out`, or
`unknown`. MT5 reports an SL execution for both original and trailing stops;
`stop_changes` records acknowledged Lathe changes without guessing an exit's
cause. Manual stop edits and changes made during an outage are not reconstructed.

## Files and reader behavior

- `.lathe_state.json` is authoritative and contains intents, current lifecycle,
  fills, outcomes, account binding, and protection state. Back it up.
- `../vault/output/trade_closed.jsonl` is an **atomic current snapshot**, one line
  per trade ID, including unresolved outcomes. A resolved revision replaces the
  unresolved row. Readers must reread the snapshot, not tail it as an event stream.
- `trades.md` retains existing readable activity. A marked Whetstone section is
  generated from exactly the same records and replaced on reconciliation.

A crash between state/output writes is repaired by the next reconciliation.
Missing/corrupt state cannot silently erase an existing outcome file. Use one
Lathe process per state/output set; concurrent writers are not supported. This
small-file design retains records in state and rewrites the views; archive or
move to transactional storage before large-scale operation.

Legacy `trade_meta` entries lack reliable mode/account provenance. They are not
automatically imported as outcomes and block new entries pending operator review.
Before enabling the branch on an existing installation, back up state/logs,
reconcile outstanding orders/positions against MT5, and resolve legacy metadata.
Do not delete the whole state file: it also contains kill switches and risk state.
Use separate state/log files for different accounts. Account changes are refused.

Reconciliation failure blocks new entries but existing position-management and
risk checks continue. Unresolved outcomes never reset the loss streak as wins.
The existing session entry counter still counts submissions, not confirmed fills.

## Validation and demo acceptance

Run `python -m pytest -q`. Tests replace MetaTrader5 with a fake terminal and
cannot contact a broker. Coverage includes partial fills/exits, weighted prices,
all signed costs, restarts, unavailable history, duplicate deals, API outages,
unfilled cancellations, paper/live labeling, acknowledgement recovery, account
switches, unsupported reversals, output crash recovery, and loss-counter replay.

Before merging/deploying, validate against a dedicated MT5 demo account with
real broker submissions. Compare order/deal/position IDs, volumes, prices, and
cost totals to terminal history for full and partial exits; restart mid-position;
interrupt history access and confirm unresolved rows later resolve without
extra rows. Confirm that your broker's commission allocation appears on the
position's deals. Unlinked account-level charges cannot be attributed here;
post-closure broker corrections are not automatically re-opened in this phase.
This environment has not performed that Windows/MT5 demo acceptance run.

## API references

- https://www.mql5.com/en/docs/python_metatrader5/mt5historyordersget_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py
- https://www.mql5.com/en/docs/constants/tradingconstants/positionproperties
- https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties
