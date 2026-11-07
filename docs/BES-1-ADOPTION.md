# BES-1 adoption: tradebot

**State:** Proposed, documentation-only adoption. **Not a certification** of release or live runtime.  
**Ownership boundary:** standalone consequential automation, not OBEOS-only.  
**Project role:** Lathe FX bot and Whetstone replay/reviewer.  
**Canonical policy:** [Blackfong Engineering Standard BES-1](https://github.com/5K-bit/O.B.E.O.S/blob/main/docs/standards/BES-1.md).

## Existing baseline

The inspected default branch contains workflow file(s): `tests.yml, release.yml`. Presence does **not** establish successful runs. Existing workflows and runtime remain authoritative. If automated checks are absent, define a dedicated minimum CI in a separate reviewed change; do not use document presence as a test.

## Evidence gates

- **Built:** change/commit, interfaces, threat/safety assessment and recovery path recorded.
- **Verified:** exact automated commands/CI run at SHA and unrun cases identified; separate simulated from real external dependencies.
- **Released:** artifact or deployment identified and reproducibly installed; release notes, rollback and integrity where applicable.
- **Operational:** dated validation of actual expected environment, service/dependency failures and recovery; no status promotion by green CI alone.

### Project-specific acceptance

Deterministic backtests; bounded replay and cost/slippage assumptions; signal-to-fill-to-exit IDs; partial-fill reconciliation; paper/demo versus live isolation; broker disconnect/duplicate order prevention; independently authorized live execution.

**Critical boundary:** No automated validation can authorize real-money trading or infer profitability from backtests.

## Next adoption actions

1. Inspect current CI configuration and prove the relevant tests run on an exact commit; add missing checks in a separate PR when justified.
2. Document one real or authorized-lab acceptance scenario plus one failure/recovery scenario.
3. Only add `just` aliases for verified underlying commands and test them on target OS; preserve the existing command surface.
4. Evaluate scans, dependency updates and telemetry with explicit scope; do not add network privileges, credentials or financial execution rights under BES-1.

**AI assistance:** scope must be declared for substantive work.  
**Human verification:** list actually observed actions, dates and evidence; unrun checks remain **not verified**.
