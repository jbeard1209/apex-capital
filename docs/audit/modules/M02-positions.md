# M02 — Positions, entry dates, max-hold (pass 1)

Run 2026-11-19, host `MacBook-Air-173.local` (MAC), worktree `apex-audit-2026-11-19`, branch
`audit/M02-positions-2026-11-19`. VM reachable: deploy_stamp sha 56391cf (deployed 2026-11-19 11:17Z), up 96 days.

## What was read
`entry_date_ledger.py` (whole file), `main.py` ledger writers (reconcile at start of run :439, sell delete :586,
13F buy :855, manager SELL/TRIM/ADD/BUY_NEW :1345-1370), `alpaca_trader.enforce_risk_rules`, readers
`review_agent.analyze_position`, `position_aging_agent`, `coherence_laws.law_positions_conserve`.

## Gates
| Gate | Result | n |
|---|---|---|
| F1 closed + re-bought inside the 500-order window is dated from the re-buy | **PASS** — already fixed (backward walk anchored on broker qty); `test_a_reopen_inside_a_truncated_window_is_dated_from_the_reopen` (XLE case) and `test_a_closed_and_reopened_position_is_dated_from_the_reopen` pass. The "open HIGH chairman item" is stale: close it | 2 cases |
| F2 TRIM keeps the original entry date | **PASS** — `test_a_trim_cannot_reset_or_erase_the_reconstructed_entry`; main.py only pops on SELL | 1 case |
| B1 recompute entry date from broker fills for every position held, diff vs ledger (VM) | **PASS** | 20 positions, 500 orders |
| L1 `positions_conserve` HOLDS with n = position count (VM) | **PASS** (was VIOLATED 11-11) | 20 |
| NEW F3 undated position must not read as "1 day old" in `enforce_risk_rules` | **FAIL → fixed** (finding 1) | 2 cases |
| NEW F4 an ADD must not reset the hold clock | **FAIL → fixed** (finding 2) | 1 site |
| Full `test_pipeline_smoke.py` | see bottom | |

### B1 — command and output (VM, read-only)
`entry_from_fills(orders, sym, held_qty)` for each broker position vs `state.json["entry_dates"]`:
```
positions 20 orders 500 ledger 20
held_not_ledgered []
ledger_ne_fills []
window_start_bounds []
phantom []
ledger_ge_60d []
```
No position is older than the 60-day max-hold, so max-hold has nothing to fire on today; that is
consistent, not proof it works (covered by `test_a_dated_position_past_max_hold_is_sold`).

### L1
```
positions_conserve HOLDS 20   (20 broker position(s), same 20 in the ledger)
```

## Findings
1. **`alpaca_trader.enforce_risk_rules` still used `entry_dates.get(t, now - 1 day)`** (sev B). The same
   fallback-that-means-act bug that was fixed in `review_agent`: a position with no date reads as one day
   old, max-hold can never fire, no message. Reachable only if the ledger is missing a name (reconcile
   leaves undatable positions absent). Fixed: None → max-hold abstains with a WARNING; stop-loss and
   take-profit still apply. Test: `TestM02PositionClockAudit`.
2. **ADD reset the hold clock** (sev B). main.py wrote `entry_dates[t] = today` for `ADD` and `BUY_NEW`
   alike; a top-up of an old position restarted its 60-day max-hold. Contradicts INTENT 1.6 ("real entry
   date"). reconcile keeps existing dates, so nothing healed it. Fixed: ADD uses `setdefault`. The buy_log
   has no action field, so how many positions were affected historically cannot be measured (finding 3).
3. **`buy_log` rows carry no ADD/BUY_NEW action** (sev C): adds are unauditable after the fact. CIT proposal:
   add `action` to the buy_log row.
4. **Liveness**: `positions_conserve` is the alarm for the ledger and HOLDS. No heartbeat exists for the
   start-of-run reconcile (`entry_date_ledger`) — if the broker read keeps refusing, the ledger silently
   stops healing. CIT proposal: log refusal count to the liveness registry.

## Deploy
Both fixes change `main.py` and `alpaca_trader.py`; they are NOT live until Jack approves a deploy.

## Full `test_pipeline_smoke.py`
Command: `/usr/bin/python3 test_pipeline_smoke.py` → `Ran 1919 tests ... FAILED (failures=2, errors=2, skipped=13)`.
All 4 were worktree artefacts (gitignored `data/hr_history.json` and `data/historical_13f.db` absent in a fresh
worktree), not code failures. After copying those two files from the Mac checkout, the 4 classes
(`TestReentrySlateCannotLieByOmission`, `TestWrongfulRetirement`, `TestLTExperimentsAreRealExperiments`,
`TestM02PositionClockAudit`) → `Ran 36 tests ... OK (skipped=1)`. Last code change preceded both runs.
