# Liveness Gaps — features that can stop without anyone being told

**Root cause of July–November.** Almost every long-running failure had the same shape.
The job *ran*, exited 0 and rewrote its output, but did not do its job. Or it never ran,
and nothing expected it to. The monitors ask "did a file change?", "did the process exit
0?", and "do two numbers agree?". None of them asks **"did the thing this feature exists
to do actually happen, as often as it should?"**

| Incident | How long unnoticed | Why no alarm |
|---|---|---|
| Board vote crashed inside every meeting | ~08-18 → 11-09 (12 weeks) | Report rewritten daily (fresh), exit 0, error swallowed as "non-fatal" |
| Board approvals never executed | June 10 → now (5 months) | Executor on no schedule; nothing expects implementations |
| VM ran June-3 code | ~1 month | No freshness check that could run |
| FI orders APPROVED, never executed | 66 days | Unknown verb, no `else` |
| Shorts could open but never cover | weeks | Rejection logged, nobody counts covers |
| 2 Claude scheduled tasks dead at `enabled=true` | 9 days | The scheduler reports itself healthy |
| Orphaned shell burned 261 CPU-hours | 20 days | Monitors watch for jobs that *stop*, not ones that never end |
| 12 agents wired but scheduled nowhere | unknown | wiring_audit looked at wiring, not schedules |

## The fix pattern: an outcome heartbeat with an expected cadence

The intended design has three parts:

1. **Each feature writes an outcome heartbeat** when it does its *job*, not when it
   starts. The heartbeat goes to `data/heartbeats/<feature>.json` as
   `{ts, n_done, n_examined, host}`. "Board voted on 4 of 4 proposals" is an outcome.
   "board_meeting.py started" is not.
2. **One registry declares the expected cadence.** `config/liveness.json` holds
   `{feature: {max_silence_hours, min_n_done, days}}`. For example, board =
   26 h on weekdays with n_done ≥ 1 when proposals are pending.
3. **One watcher checks every heartbeat against the registry.** Something must also watch
   the watcher. The watcher pings healthchecks.io, the external dead-man's switch already
   used by main.py. If the watcher dies, healthchecks.io emails Jack. This is the one
   control that does not depend on our own code running.

Status: **NOT BUILT.** This is proposal #1 from the audit. It is one small module plus
one line per feature, and it would have caught every incident in the table above. It
gets built in the first recurring runs (see PROGRESS.md, M00), by PR, and is deployed
only with Jack's OK.

## Gap list (as of 2026-11-11)

Severity is graded by what silence would cost. **A** means money moves wrongly or
decisions stop. **B** means a control goes blind. **C** means a report stops.

| # | Feature | What "stopped" looks like | Current alarm | Gap | Sev |
|---|---|---|---|---|---|
| G01 | Board → execution (CIT implementation) | No implementations | None (unscheduled) | **Owned by the board-execution thread.** Needs an outcome heartbeat: approvals vs implementations | A |
| G02 | Board vote | Votes cast = 0 while proposals pending | job_failure_monitor (log errors only) | A vote that silently casts 0 votes with no error is still invisible | A |
| G03 | Deputy board | Runs but decides nothing | None | It does run (inside cit_director evening, 01:00). But 11-06→11-12 it made 0 decisions, all NEEDS_INFO. Heartbeat must count *decisions*, not runs | A |
| G04 | Pod order flow (each pod) | Pod places 0 orders for N days | pod_health_monitor (partial) | No per-pod "last order" expectation; ML pod idle with no decision recorded | A |
| G05 | Short covers | Cover attempts rejected | None | Count of failed covers / short positions older than max-hold | A |
| G06 | Kalshi copy-trader while armed | Armed but 0 orders for 48 h; balance in the wrong shard | manager breaker only covers *losses* | An idle-while-armed alarm, plus a shard balance check | A |
| G07 | CRO buy-block | Buys blocked > 3 trading days | None | Alert on long blocks with reason | A |
| G08 | main.py itself | No run | healthchecks.io ping ✓ | Covered. The ping fires at the end of the pipeline; confirm it isn't sent on a partial failure | — |
| G09 | job_failure_monitor | Monitor dies | None | Nothing watches the watcher, so route it through the healthchecks.io watcher | B |
| G10 | canary | Canary dies | None external | Same as G09 | B |
| G11 | Coherence laws | Not run (weekend-only cron line) | internal_audit (when run) | Weekday runs come only via main.py? Verify; heartbeat with n_examined | B |
| G12 | internal_audit_agent | Never runs | catch_up.py re-runs it if main.py missed | Invoked by main.py + catch_up (verified 11-12); still needs a watcher heartbeat | B |
| G13 | Sentinels (metric, data integrity, freshness, problem lifecycle) | Dies | None | Each one needs a heartbeat into the watcher | B |
| G14 | Mail delivery | Emails stop (SMTP creds expire) | None | A daily "I'm alive" digest. If Jack stops getting it, that is the alarm, and healthchecks.io does it for free | B |
| G15 | Claude scheduled routines (CIT bug fixer, audits) | Dead at enabled=true | launchd for Mac-critical only | Each routine writes a heartbeat; the watcher expects it | B |
| G16 | Mac launchd (kalshi report, vmstate, catchup) | Mac asleep → stops | None | Accept for reports, or move the hourly Kalshi report to the VM | C |
| G17 | Chairman SLA / compactor | Overdue items pile | SLA itself | Watcher heartbeat | B |
| G18 | HR evaluation | No evaluations | roster ratchet (shrink only) | Heartbeat: agents evaluated / roster | C |
| G19 | IRM email | No review | None | Heartbeat | B |
| G20 | Backups | No new backup / restore broken | None | Heartbeat + monthly test restore | B |
| G21 | ML harness / live runner | Report stale | freshness_sentinel? verify | Heartbeat with AUC + n | C |
| G22 | EDGAR backfill | Label count flat | None | Heartbeat with n labels | C |
| G23 | Disk | Full | disk_janitor (acts, doesn't alarm) | Alarm at 85% | B |
| G24 | Crontab snapshot | Snapshot stale | None | Diff check in M19 | C |
| G25 | Strategy council / sign-off gate | Stuck in one state forever | None | "State unchanged for > N weeks" note to the board | C |
| G26 | Long-running/zombie processes | Never stops | None since the 261 CPU-hour fix? verify | Max-runtime check per job | B |

## What gets closed first

The first build (M00) is the watcher plus the registry. After that, gaps close in order:
G02, G04, G05, G06, G07, G09/G10/G13 (all via the watcher), G14. G01 belongs to the
board-execution thread. Each closed gap gets a poison-pill test: plant silence and prove
the watcher alarms.
