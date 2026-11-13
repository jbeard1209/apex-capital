# Audit Progress Tracker

**Every recurring run reads this first and updates it last.**

## How a run picks its module
1. If any module is `IN PROGRESS`, finish it first.
2. Otherwise take the first `TODO` row in the table, top to bottom (it is risk-ordered).
3. Skip `BLOCKED` rows unless their blocker cleared, and note the reason in the run log.
4. **One module per run.** Stop when its gates are done, even if budget remains.

## How a run finishes
1. Write or append `modules/<id>-<slug>.md` with each gate's command, quoted output, and
   PASS/FAIL, plus n_examined.
2. Update the module's row here: status, date, gates passed/total, PRs.
3. Add each finding to the findings log below, with its disposition (PR / CIT proposal /
   chairman item / accepted risk).
4. Add a line to the run log.
5. Post a plain-English summary to the project chat.

Status values: `TODO`, `IN PROGRESS`, `DONE` (all gates pass), `DONE-WITH-FINDINGS`
(gates ran; failures have a PR or an owner), `BLOCKED` (reason required).

## Modules

| ID | Module | Tier | Status | Last run | Gates | PRs / notes |
|---|---|---|---|---|---|---|
| M00 | Liveness watcher + registry (build; see LIVENESS_GAPS) | 0 | DONE-WITH-FINDINGS | 2026-11-13 | 3/4 | Built by PR audit/M00-liveness-2026-11-13; VM gate BLOCKED until deployed (Jack). 0 of 15 features instrumented yet |
| M01 | Order path & choke point | 1 | TODO | — | 0/8 | |
| M02 | Positions, entry dates, max-hold | 1 | TODO | — | 0/4 | Open HIGH chairman item (re-buy dating) |
| M03 | Manager, picks, CRO | 1 | TODO | — | 0/4 | |
| M04 | Kalshi copy-trader / manager / scout | 1 | TODO | — | 0/6 | Shard balance issue 11-09 |
| M05 | Kalshi scanner / calibration / mirror | 1 | TODO | — | 0/4 | |
| M06 | Pods: budgets & boards | 1 | TODO | — | 0/4 | |
| M07 | Board meeting & deputy board | 2 | TODO | — | 0/4 | Vote crash fixed PR #22; deputy 0 decisions all week (finding 11-12) |
| M08 | Board → execution | 2 | BLOCKED | — | — | Owned by thread "Make board decisions actually get executed"; re-verify after it lands |
| M09 | CIT director & routing | 2 | TODO | — | 0/2 | |
| M10 | Logic team | 2 | TODO | — | 0/2 | |
| M11 | Chairman queue / inbox / SLA | 2 | TODO | — | 0/4 | |
| M12 | Sign-off ledger / governor / council | 2 | TODO | — | 0/2 | |
| M13 | HR | 3 | TODO | — | 0/4 | Retirement-reader smoke test failing |
| M14 | Monitors | 3 | TODO | — | 0/3 | |
| M15 | IRM | 3 | TODO | — | 0/1 | |
| M16 | Model metrics & harness | 4 | TODO | — | 0/3 | |
| M17 | ML long-term / EDGAR | 4 | TODO | — | 0/1 | |
| M18 | Email | 5 | TODO | — | 0/2 | |
| M19 | Ops (deploy, backup, disk, cron) | 5 | TODO | — | 0/4 | Dead June cron lines removed + verified 11-12; L3 can be re-run as a check |
| M20 | Dashboard & reports | 5 | TODO | — | 0/1 | |

The run order is M00 first, because every later module adds its heartbeat to the watcher.
Then M01 onward. After M20, the cycle restarts at M01 as a **re-audit**, checking only what
changed since the last DONE date (`git log --since`).

**Year plan (Jack, 2026-11-11: "spend A YEAR on this").** One module per run, every third
day, comes to about 120 runs a year. Pass 1 (M00–M20, about 21 runs, roughly 2 months) is
the line-by-line baseline. Each later pass goes deeper on the same modules in the same risk
order:
- **Pass 2**: backtests and forward tests. Replay more history and compare live outcomes
  with the intent register.
- **Pass 3**: adversarial. Poison-pill every alarm added in passes 1–2, and plant silence to
  prove the watcher fires.
- **Pass 4+**: re-audit only what changed (`git log --since`), plus anything that broke
  in between.
Record the pass number in the run log.

## Findings log

| Date | Module | Finding | Sev | Disposition |
|---|---|---|---|---|
| 2026-11-11 | baseline | `cit_implementation_agent.py` scheduled on neither host; last ran June 10 | A | Board-execution thread |
| 2026-11-11 | baseline | No outcome-level liveness alarm exists for most features (26 gaps) | A | M00 |
| 2026-11-11 | baseline | `deputy_board.py` and `internal_audit_agent.py` have no VM cron line | A/B | **Closed 2026-11-12, not a bug.** Deputy runs inside `cit_director.py --session evening` (VM cron 01:00); internal audit runs inside main.py (main.py:2025) and catch_up.py. No new cron needed |
| 2026-11-12 | M07 pre-check | Deputy board **decides nothing**: VM logs show 0 approved / 0 rejected on every day 11-06→11-12; today 8 of 8 proposals NEEDS_INFO ("no file:line / not actionable"). It runs and reports fresh, so no alarm fires | A | Sent to board-execution thread; gate in M07 (decisions > 0) |
| 2026-11-11 | baseline | VM crontab: dead June-22 one-shot model switch (re-fires yearly, uses bare python3) | C | **Fixed 2026-11-12** (with the dead June pregenerate line), by the health-check thread. Verified on VM: 107 jobs, 0 June lines (backup ~/crontab_backup_2026-11-12.txt had 2), diff vs backup = exactly those 2 removed + pod/kalshi pnl_history added, snapshot body matches `crontab -l` |
| 2026-11-12 | baseline | `pregenerate_lessons.py` was a June-only lesson-cache filler with a dead cron line | C | **Retired 2026-11-12** (#36): moved to `agents/archive/`, cron line removed. deploy.sh has no `--delete`, so a stale copy sits on the VM top level; harmless, nothing schedules it. Remove on a future deploy |
| 2026-11-12 | pre-push hook | Hook ran bare `python3` (Mac: 3.15, no packages), so every push from Jack's Terminal failed with 13 ImportErrors that looked like test failures | B | **Fixed** #36 (hook picks APEX_PYTHON → ~/venv → /usr/bin → PATH python3) + #37 (gate-shape tests accept `"$PY"`). Lesson: CI runs only TestWiringContracts, so a hook change needs the FULL suite locally |
| 2026-11-12 | test suite | Suite leaked fixture rows (a, b, LTIA-1, LT-1/LT-2) into the live `cit_implementation_queue.json` on both hosts: router tests chdir to a tempdir but `board_dispatch.DATA` is absolute | B | Fix on branch `claude/suite-cannot-write-live-queue` (be82ce2, not yet on main); live rows removed on both hosts with backups. Related: running the HR tests also rewrites tracked `data/agent_status.json` — next run should check whether that is the same leak class |
| 2026-11-12 | M13 (HR) | `TestHRSeesTheWholeFirm.test_attendance_and_the_roster_agree_on_who_is_scheduled` failing since ≥11-09. Root cause: `audit_attendance` rebuilt the roster only when `data/agent_roster.json` existed AND was stale; when missing (gitignored, so every fresh worktree / cloud run) it fell back to a substring test and called `issue_tracker`, `cit_implementation_agent`, `proxy_chairman_agent` (reached only via imports) never-scheduled, while `audit_roster_coverage` rebuilt and said they run | B | **Fixed** on `claude/project-thread-i9d8t1-hr`: rebuild on missing too (except under an explicit `base_dir` sandbox). Verified: test FAILS before, 14/14 pass after, with and without the roster file |
| 2026-11-11 | baseline | Coherence `deployed_set_matches_repo` VIOLATED (5 files differ) and `positions_conserve` VIOLATED | B | M19 / M02 |

## Run log

| Date | Run | Module | Result | Tokens (approx) |
|---|---|---|---|---|
| 2026-11-11 | baseline | — | Wrote README, INTENT_REGISTER, AUDIT_PLAN, LIVENESS_GAPS, PROGRESS | — |
| 2026-11-12 | follow-up | — | Routine moved to the Mac bridge env (VM reachable); verified deputy/internal-audit invokers; logged deputy zero-decision finding | — |
| 2026-11-12 | follow-up | M19-L3 (partial) | Verified VM crontab after health-thread edit: 107 jobs, both June lines gone, snapshot matches | — |
<<<<<<< HEAD
| 2026-11-13 | recurring | M00 (pass 1) | Built liveness.py + registry + 8 tests; host MAC (MacBook-Air-173.local) | — |
=======
| 2026-11-12 | follow-up | M13 (partial) | Routine prompt made host-aware (records hostname; MAC vs CLOUD branches; cloud marks VM/Mac gates BLOCKED). Logged pregenerate retirement, hook-python fix, test-queue leak; fixed HR attendance/roster disagreement | — |
>>>>>>> origin/main

## Budget
Target is about 30% of the weekly token allowance. With a run every third day that comes
to about 2–3 runs a week, so roughly 10–15% each. If a module would run over, split it
(e.g. M01a/M01b) rather than rushing it.
