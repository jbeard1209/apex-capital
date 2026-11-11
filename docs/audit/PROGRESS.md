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
| M00 | Liveness watcher + registry (build; see LIVENESS_GAPS) | 0 | TODO | — | — | Build by PR; deploy needs Jack |
| M01 | Order path & choke point | 1 | TODO | — | 0/8 | |
| M02 | Positions, entry dates, max-hold | 1 | TODO | — | 0/4 | Open HIGH chairman item (re-buy dating) |
| M03 | Manager, picks, CRO | 1 | TODO | — | 0/4 | |
| M04 | Kalshi copy-trader / manager / scout | 1 | TODO | — | 0/6 | Shard balance issue 11-09 |
| M05 | Kalshi scanner / calibration / mirror | 1 | TODO | — | 0/4 | |
| M06 | Pods: budgets & boards | 1 | TODO | — | 0/4 | |
| M07 | Board meeting & deputy board | 2 | TODO | — | 0/4 | Vote crash fixed PR #22 |
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
| M19 | Ops (deploy, backup, disk, cron) | 5 | TODO | — | 0/4 | Dead June-22 cron line |
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
| 2026-11-11 | baseline | `deputy_board.py` and `internal_audit_agent.py` have no VM cron line | A/B | Verify in M07 / M14 |
| 2026-11-11 | baseline | VM crontab: dead June-22 one-shot model switch (re-fires yearly, uses bare python3) | C | M19; needs Jack's OK to remove |
| 2026-11-11 | baseline | Coherence `deployed_set_matches_repo` VIOLATED (5 files differ) and `positions_conserve` VIOLATED | B | M19 / M02 |

## Run log

| Date | Run | Module | Result | Tokens (approx) |
|---|---|---|---|---|
| 2026-11-11 | baseline | — | Wrote README, INTENT_REGISTER, AUDIT_PLAN, LIVENESS_GAPS, PROGRESS | — |

## Budget
Target is about 30% of the weekly token allowance. With a run every third day that comes
to about 2–3 runs a week, so roughly 10–15% each. If a module would run over, split it
(e.g. M01a/M01b) rather than rushing it.
