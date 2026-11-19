# Thursday op (run 2026-11-18 evening CT): 5-day scan + data-change record

Companion to docs/fixer_build_report_2026-11-19.md. This file covers the 5-day scan
(11-13 to 11-18) and the one live data change.

## Live data change (Mac only)

- **What:** `board_dispatch.backfill_open_rows()` was run against the Mac's live
  `data/cit_implementation_queue.json`.
- **Effect:** 22 open rows were stamped with `owner_area` and `routed_rule`. The change is
  additive; no status changed and no `escalated_to_board` row was touched.
- **Follow-up:** `LO-20261112015815`, which routed to the wiring fallback, was then
  re-stamped `risk` / `risk:broker` after the risk rule was widened.
- **Backups:**
  - `data/cit_implementation_queue.before-fixer-backfill.20261118212419.json` (Mac)
  - a scratchpad copy taken before the run
- **To undo:** copy the backup back over the queue. The new fields are ignored by old code.

The VM was not changed. Its queue will backfill itself on the first dispatch run after
deploy, and that run takes its own backup.

## Scan results

| Item | Status | Evidence |
|---|---|---|
| Mac = origin/main = VM | OK | Mac HEAD, origin/main and VM deploy_stamp are all `153958b`; the md5s of 5 core files match |
| "Uncommitted Mac work" | OK | fixer_build_plan.md, apply_permissions.py and the sandbox fix are already on main. The Mac tree diff is runtime data/ plus auto-written NEEDS_JACK.md |
| Every-3-days audit routine | **BROKEN (half)** | 11-13 M00 ran and merged (#42). 11-16 M01 ran and pushed `audit/M01-order-path-2026-11-16` (d40b3fb), but **opened no PR**, and PROGRESS.md still says M01 TODO. 11-19 is not due yet |
| VM → apex-reports at :55 | OK | hourly commits |
| Manager every 15 min | OK | VM logs |
| Copier (1 min) / scout (13:30) | OK, paper | Kalshi still HALTED; LOW_BALANCE alerts; arm files untouched |
| Board dispatch no_handler | Worse: 81 → 115 | This is what the fixers are for; inert until deploy |
| Deputy board 11-13 | Unverifiable | No history file is kept; only the latest report exists. 11-18: reviewed 2, approved 0, 2 sent to full board. The main board approves ~9-11/day |
| 58 of 85 queued rows lack descriptions | Fixed in code | 60 loop rows on the VM had empty evidence. The backfill fills them (dry run 60 → 0) |
| Re-filing loops | Open | 27 pregenerate_lessons `wiring_contract_violation` rows: the file was archived, but deploy.sh has no `--delete`, so the VM still carries it. 16 `unexplained_loss` rows |
| Stale-cron rows | Finding | No current code emits these labels; the queue keeps them alive. Dry run: 6 would rerun, 9 go to ops |
| Manager Step 5/5 crash | Fixed (PR) | Root cause: `hold_days=None`. PR on `claude/fix-manager-report-hold-days` |
| `LO-20261116135700-zero_picks_anomaly` | Likely false alarm | It counted "3 days zero picks" across a weekend. Left for the Chairman/research fixer |
| 13 June supersede candidates | Premises still false | Rechecked on the VM 11-18. Close them only after the deploy, because old code doesn't know `superseded` |
| 40 Chairman decisions | Deferred | Not dispositioned in this op |
