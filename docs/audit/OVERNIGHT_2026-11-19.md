# Overnight full-system check, 2026-11-19 (07:17Z to about 15:15Z)

Jack asked for a full check while he slept: are the agents talking, is the board deciding,
is every monitor reporting, plus a proactive hunt for problems, with authority to fix.
Hard limits were kept: no Kalshi cap changes, no money moved, the Kalshi error counter was
not reset, arm/halt files were not touched, the 126 escalated_to_board rows were left alone,
the Kalshi near-certainty rows were kept, the Kalshi balance was not polled, and no risk
limit was changed.

## Decisions for Jack (in priority order)

1. **PR #54, the 13F signal engine fix (draft, held for you).** The 13F engine has never
   produced signals from real filings. Because of that, `pipeline_snapshots` has had **no
   live rows since 2026-05-15** (all 544,379 rows are backfill). main.py logs
   "data_collector: no signals to snapshot today" every run. Downstream, the ML feature
   drift monitor reads 0 rows and the ML experiment queues are about 3,340h stale. Merging
   #54 should restart live collection. It changes what feeds picks, so it needs your yes.
2. **The 20-position cap is the binding constraint on new buys.** It blocks every new
   name, including the ETF/Macro pod's board-approved XLP/XLU/XLV and HII, D, PGNY, APPN,
   UL, U. The $30k Macro/ETF pod sits idle as a result. This is a risk limit, so I did not
   touch it. Options: raise the cap, give pods their own slots, or force rotation (trim the
   weakest holding to admit a stronger pick).
3. **The emergency board's PAUSE_NEW_BUYS has no actuator** (board_meeting.py around
   lines 3357-3536). The board can vote to pause buys and nothing enforces it. Wiring it
   changes trading behaviour, so it is yours to approve.
4. **Kalshi copytrade is -$9.90 on $10.13 deployed, and $20.44 is stranded on shard 1.**
   The mirror gate is correctly unarmed (measured edge +0.145c, below the 0.5c bar).
   Nothing was changed. Decide whether to keep copytrade running and what to do with the
   shard-1 cash.
5. **15 duplicate LO unexplained_loss rows** need a loss_attribution decision (dedupe, or
   attribute each).
6. **Two Sigma CIK 0001649339 may be wrong.** Verify it before #54 starts reading it.
7. **FE deep research has never run on the VM.** `fe_research_scheduler.py` is in neither
   the VM crontab nor `restore_crontab.sh`, and there has never been a log for it.
   `fe_research_queue.json` (126 companies) was last written 2026-05-19. `data/company_research`
   holds 12 companies, and none of its files is newer than October. `fe_pipeline.py` reads
   those theses, so FE picks rest on research that is months old. The sub-agents are
   deterministic (no LLM cost). I did not schedule it, because it would start feeding new FE
   theses into trading. If you say yes, the cron from the file's own docstring is
   `0 2 * * * cd ~/institutional-tracker && ~/venv/bin/python3 fe_research_scheduler.py >> logs/fe_research_scheduler.log 2>&1`.
   `wiring_audit` misses this: the module is import-reachable through `wake_agent`, which it
   counts as wired, even though nothing schedules it.

8. **The ML research chain is scheduled on neither host.** ml_data_watchdog, ml_drift_monitor,
   ml_research_team_leader and ml_digest have no cron line or launchd job (the code already
   notes this in coherence_laws.py and apex_system_cit_agent.py). As a result
   data_quality_report.json was last written 2026-05-23, drift_report.json does not exist, and
   the board's ML panel had been showing May's status as current until #61. Scheduling it would
   start a new job feeding ML research, so it is your call. Most of its inputs are frozen until
   #54 lands anyway, so it makes sense to decide this together with item 1.

## Fixed, merged, deployed and verified on the VM

| PR | What | Proof |
|----|------|-------|
| #53 | Liveness: a beat wired by today's deploy waits one cadence (AWAITING_FIRST_BEAT) instead of paging MISSING | VM log: `examined=16 alarms=0 uninstrumented=0` |
| #55 | A designed risk_gate refusal is logged at WARNING, not ERROR. At ERROR it was CIT's "dominant error class" and fed an unclosable HIGH-issue loop | Test failed before, passed after; VM md5 debb8931 matches |
| #56 | Hedge-exemption grading loads .env before the broker snapshot. The 13:00 cron never loaded it, so `risk_gate.snapshot` raised KeyError 'ALPACA_API_KEY' daily from 11-15 and every hedge exemption stayed ungraded | Test failed before, passed after; VM md5 bd295761 matches; a live VM snapshot returns 20 positions |
| #57 | Data integrity: debt above assets is "impossible" only if debt also exceeds total liabilities. FICO, MSCI, TDG and BBIO have negative equity (their liabilities exceed their assets), so they are real and still gated LOW, not RED | VM rerun: impossible tickers went from 5 to 1 (SBAC, which has no liabilities figure to check); md5 36d7d2e3 matches |
| #58 | Research picks: index symbols such as ^VIX are dropped before the Manager sees them. ^VIX came from historicalcontextanalyst_cos's watchlist; the Manager approved it daily, the broker rejected it, and it was graded in the performance ledger as a 0-day HOLD | Test failed before, passed after; md5 4bfed40a matches |
| #59 | Kalshi board: the calibration verdict is read once per session, not once per candidate (it reloaded the whole ledger 380 times). A session ran over 5 minutes, so agent_sweep flagged kalshi_board as TIMEOUT every day | VM side-by-side on the 02:10 scan: all 380 decisions identical, seat by seat; it now takes under 1s; md5 cfab1772 matches |
| #60 | Freshness sentinel: a queue holding only decided rows (approved/completed) is not waiting work. Each queue now uses its consumer's own "open" predicate, pinned to the consumer's source by a test | On the VM, ml_pod_pending went 1→0 open and the ML short/long experiment queues 6→0 and 13→0; rd_pod_pending 8→5; md5 6fd1b946 matches |
| #61 | Board minutes: the ML panel's data-quality and drift statuses read "STALE since <date>" when the report is over 54h old, and the tile is grey. It had been showing the 2026-05-23 "WARNING" as current | VM: `STALE since 2026-05-23 (last: WARNING)`; no vote reads these strings; md5 ad91b506 matches |
| #62 | CIT pipeline integrity: heartbeat age subtracts weekend hours, so Monday's "52.6h old" heartbeat is not stale (a found-by-fixer loop) | Test failed before, passed after; md5 cc2b68de matches |
| #63 | Loss attribution: the STALE text says snapshots are being written and that what is missing is a written loss_attribution judgment, not data | VM text now reads "Daily snapshots are being written (newest 2026-11-18…"; md5 2fb17962 matches |

Every PR: failing test first, then 1915 to 1939 tests OK in the local zone and with TZ=UTC,
logic_team rc=0, CI green, merge, deploy, and the Mac/main/VM md5 compared.

## What was checked and works

- **Board chain (VM):** the board meets at 13:30 on weekdays. On 11-18 it approved 1,
  rejected 1, deferred 1, split 8 with no consensus, and left 60 approved-in-force rows
  without a re-vote (correct). Manager orders flow (order_ledger has buys and sells through
  11-18). The digest emailed 85 reports at 22:00 on 11-18.
- **Agents talking:** every key agent posted to the VM firm channel within 24h. Logic
  Team's "6 agents silent for more than 7 days" came from stale clone data, not the VM.
- **Monitors:** liveness examines 16 jobs with 0 alarms. Backup, canary, kalshi_copy_trader,
  job_failure_monitor and liveness_watcher are beating. job_failure_monitor has one old,
  harmless pick_gate_trial FileNotFoundError (that trial is REVIEWED).
- **Chairman inbox:** I closed 16 stale items whose premise I re-checked as false today
  (for example: MU is not held, disk is 68%, positions_conserve HOLDS 20/20, the ETF board
  did approve). Each was closed as `resolved_upstream` with the check written in
  `closed_reason`. The inbox went from 38 to 22 pending. Backup:
  `data/chairman_decision_queue.before-overnight-autoresolve.20261119.json` (Mac). The
  arm_gate items, escalated_to_board rows and Kalshi rows were left untouched. The close
  propagated to the VM through chairman_queue_sync.
- **Chairman decisions 1 and 2** are closed on the VM queue.
- **Specialist fixers** are now scheduled every 3 hours, risk area first. They open PRs only;
  they never merge or deploy. The first run produced 2 fixes (#62, #63). I reviewed them,
  rebased, re-ran the gates and shipped them. The loss_attribution rows stay open for you (item 5).
- **3 heartbeat loop rows superseded on the VM** (loop-sig-3e4b3877067e, -6e402d7a3919,
  -b80ccaa0ece2) via `board_dispatch.py --supersede`, after #62 was deployed and the live
  heartbeat was re-checked (15h old). Each has its own queue backup
  (`cit_implementation_queue.before-supersede.20261119111955*.json`). Their copies in
  `cit_pending_proposals.json` were left for the board. The 6 stale-cron rows were not touched;
  they are items 7 and 8.
- **Freshness sentinel 11:15Z run (VM):** 33/35 fresh, 2 stale, and both are the real items 7 and 8.

## Still broken, reported only

- **Data integrity: SBAC is still flagged impossible** (debt above assets with no same-year
  liabilities figure to confirm negative equity). The other four were fixed in #57.
- **The freshness sentinel showed 5 stale files.** Three of them were false alarms (queues that
  held only decided rows), fixed in #60. The two real ones are fe_research_queue (item 7) and
  data_quality_report (item 8).
- **agent_sweep timeout:** `data_integrity_sentinel --report` took 39s when run alone on the VM,
  but it exceeds the 75s probe during the 04:10 sweep, probably because of contention. kalshi_board
  was fixed in #59.
- **Temp-file orphans** from interrupted writes are preserved in `data/.interrupted-writes`.
- **The VM crontab is 434 lines.** It works, but it is hard to audit.

## End-of-night addendum: heartbeats verified on the VM (checked 15:07Z)

| Due (UTC) | Job | Result |
|---|---|---|
| 11:15 | freshness_sentinel | 33/35 fresh; the 2 stale are real (items 7 and 8) |
| 12:20 | data_integrity_sentinel | Report written 12:20:58. RED only for SBAC (as expected after #57); risk, freshness and JSON health GREEN |
| 12:30 | chairman_decision_sla | Ran; 0 new items, 0 closed |
| 13:00 | apex_system_cit_agent + hedge grading | 0 `KeyError ALPACA` and 0 tracebacks in today's cron log (#56 holds); `hedge_exemption_outcomes.json` written 13:00 |
| 13:30 | board_meeting | Completed 13:30:17: 9 actions, 1 majority, 1 vetoed, 7 split. The ML panel in the buffered minutes reads `STALE since 2026-05-23` (#61 holds) |
| 13:30 | hr_agent | Firm health 10.0/10, 0 agents requiring action |
| 13:45 | main.py | Ran; regime hedge held (SPY inside the deadband), no trade |

Two things noticed in the 13:45 run, reported only:
- **SystemPerformanceAnalyst says "SYSTEM DEGRADED 0/10, 6 critical."** All 6 are ALERT_FATIGUE:
  the same alert fired 25-59 times without resolution (position pressure on CLOV, NNN, VRTX
  and NEM; QQQ; "VIX up +21.4% today" 59 times). Nothing is broken. The alerts repeat because no
  one acts on position pressure, and the 20-position cap (item 2) is the likely root cause.
- **The Manager got two grades in one run.** The CoS health check gave F (42/100) and the
  secret grader gave A (100/100). They are two different graders, but reporting both
  unreconciled is confusing. Worth a fixer row.

Final gates after the last change (the report only; there were no code changes after #63):
1939 tests OK in both the local zone and TZ=UTC, logic_team rc=0, wiring_audit rc=0 (rerun 15:10Z on main 56391cf).
