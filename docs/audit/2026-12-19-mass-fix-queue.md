# Mass-fix queue for 2026-12-19

Read-only inventory compiled 2026-11-28 from docs/audit/, the 2026-11-19 triple-check,
the 2026-11-13 verification, NEEDS_JACK.md and project memory. Nothing here has been fixed,
deployed or changed on either host. Open GitHub issues: none.

Status key: **SRC** = taken from a dated source doc, not re-checked today. **UNVERIFIED** = from
memory or a note only, needs a fresh probe before fixing. Every fix touching the VM, live trading or
data needs Jack's OK at the time (CLAUDE.md). Do NOT touch Kalshi arm/halt files or the
126 `escalated_to_board` rows (undecided, not to be marked resolved).

Ranked by risk to money (highest first).

| # | Risk | Problem | Evidence | Suggested fix | Status |
|---|---|---|---|---|---|
| 1 | HIGH | Kalshi copier re-halts on its first order error | `consecutive_errors: 3` in `data/kalshi_copytrade_state.json` since 2026-11-11; resets only on success (`kalshi_copytrade.py:646`), halt at 3 (`:133`). Jack said NO reset on 11-19 | Reset the counter when the halt is cleared. Needs Jack's explicit OK (he declined before) | SRC 11-19 |
| 2 | HIGH | Board decisions reach no executor | board_dispatch 11-18: executed 0, `no_handler_count` 115 (81 on 11-13); `cit_implementation_agent.py` unscheduled | Add handlers for all approved kinds, schedule the executor, add an outcome heartbeat (approvals vs implementations) | SRC 11-19 |
| 3 | HIGH | LLM calls failing: "credit balance is too low" | PROGRESS.md 2026-11-16 (M01 test run) | Check the production API key and credits. May be only the audit env | SRC 11-16, UNVERIFIED now |
| 4 | HIGH | Price features 100% empty in VM daily snapshots (5d/20d momentum, 20d vol, price vs 52w high) | Memory: empty for 30 days. Jack DEFERRED to ~12-19 | Find why the snapshot writer drops them, backfill, re-run `model_test_harness.py` and compare AUC | UNVERIFIED (from the other thread's list) |
| 5 | MED | VM not running PR #99 (ML watchdog typo crash plus FRED false alarm) | Merged as 9cb01d9 on main. Memory 11-28: deploy approved but now deferred | Deploy via `tools/ship.sh --deploy-only` after Jack's yes, then md5 check on `main.py` and the changed files | UNVERIFIED (deploy state) |
| 6 | MED | Manager cron hourly instead of every 15 min | Memory 11-28: changed about 11-19..25 | Restore `*/15` in the Mac-side cron source, then deploy and check `crontab -l` on the VM | UNVERIFIED |
| 7 | MED | Deputy board approves nothing | 0 approved/rejected every day 11-06 to 11-12, all NEEDS_INFO; no decisions 11-13 | Loosen the "no file:line" gate or feed it concrete proposals. Heartbeat must count decisions, not runs | SRC 11-13 |
| 8 | MED | Liveness watcher is wired to 0 of 15 jobs | `liveness: examined=15 alarms=0 uninstrumented=15` | Add an outcome heartbeat per feature and wire the watcher into healthchecks.io (LIVENESS_GAPS.md G01-G13) | SRC 11-19 |
| 9 | MED | Entry orders from the Mac on the shared account | PROGRESS 11-16: Mac AMAT buy 11-12; `single_writer` shadow-only | Make single_writer enforcing. Chairman decision | SRC 11-16 |
| 10 | MED | Order gate enforces only count cap and cash floor | PROGRESS 11-16: 7% single-name cap lives only in `_size_position`; head_agent and ETF-pod BUY have no cap | Add BLOCK_POSITION_PCT at the gate, shadow first | SRC 11-16 |
| 11 | MED | Kalshi leader backtest gate weak | Applies only at 10 or more settled replays; Scottaholic passed on 1. Copied record 0 wins in 6 (-$9.90) | Require a minimum sample before a leader is followed | SRC 11-13/11-19 |
| 12 | LOW-MED | 16 "unexplained loss" items | Memory only | Trace each to a trade and classify | UNVERIFIED |
| 13 | LOW-MED | 27 recurring items for an archived file still on the VM | Memory. Likely `pregenerate_lessons.py` (archived 11-12) | Remove the stale VM copy on the next deploy and close the items | UNVERIFIED |
| 14 | LOW | Mac 10-minute bug-fixer wastes runs | 3,894 runs, each "nothing fixed: same 8 known kinds" | Move to hourly or pause until the fixer layer ships | SRC 11-19 |
| 15 | LOW | 107 temp files on the Mac | Memory only | Clean up, with Jack's OK to delete | UNVERIFIED |
| 16 | LOW | Freshness sentinel DEGRADED 6/35 | Stale data_quality_report, ml_pod_pending, fe_research_queue (126), ML experiment queues; slow_gate_report ok=False | Re-check each, then fix or retire | SRC 11-13 |
| 17 | LOW | CRO queue item shows 107% deployed vs board log 55% | 11-13 | Check whether stale, then fix | SRC 11-13 |
| 18 | LOW | Only the latest reconciliation report is kept | PROGRESS 11-16 | Add a reconciliation history file | SRC 11-16 |
| 19 | LOW | Tests write into live data | PROGRESS 11-12: fix on `claude/suite-cannot-write-live-queue`, not on main; HR tests rewrite tracked `data/agent_status.json` | Merge that branch, check the HR leak | SRC 11-12 |
| 20 | LOW | Fresh-clone test failures | 5 fail + 2 errors need gitignored host data or a stale `sector_assignments.json` | Skip or fixture those tests | SRC 11-13 |
| 21 | LOW | Dependabot PRs #9-#13 open since Oct 5 | Triple-check 11-19 | Review and merge or close | SRC 11-19, UNVERIFIED |
| 22 | LOW | Copier vs manager disagree on spend today | $1.00 vs $0.00 (paper counted differently) | Use one definition | SRC 11-13 |
| 23 | LOW | Drift check skips hours while the Mac sleeps | Last scheduled run 14:50Z on 11-13 | Run it on the VM or accept the gap | SRC 11-13 |
| 24 | LOW | 3 leftover git worktrees under /private/tmp on the Mac | 11-13 (NEEDS_JACK says 20 pruned 11-24) | Re-check, prune | UNVERIFIED |
| 25 | LOW | Project-files copy of fixer plan says 11:00, repo copy says 2 PM | 11-13 | Reconcile | SRC 11-13 |

## Already closed (do not re-fix)
Merge-conflict markers in PROGRESS.md (fixed 11-16), Mac disk full (11-24), deploy-precheck failure (11-24), pre-push hook Python (#36/#37), HR roster/attendance mismatch, TRIM-on-short bug.

## Before running on Dec 19
1. Re-probe every UNVERIFIED row first (host check on both hosts, since cloud threads can't ssh to the VM).
2. Anything touching live trading, the VM crontab, Kalshi state or deletes needs Jack's OK that day.
