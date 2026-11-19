# Rows to close as `superseded` on build day (premises rechecked 2026-11-13)

Not closed yet, on purpose. Today `superseded` is a status no reader knows:
`board_dispatch` would count the row as standing forever (it is not in `DONE_STATUSES`) and
`problem_lifecycle_sentinel` would read it as still open (it is in neither
`RESOLVED_STATUSES` nor `WONT_FIX_STATUSES`). Writing it by hand now would create the very
producer/consumer mismatch the fixers exist to remove. Build-day step 1 adds the status to
both sets with a test, then these closes run, with a queue backup and this file as the record.

Never touched: any `escalated_to_board` row (coordinator constraint).

| Row(s) | Premise | Check run 2026-11-13 on the VM | Verdict |
|---|---|---|---|
| `LT-015346-fi_never_run` (x2) | FI pipeline never ran | `data/fi_pipeline_report.json` generated 2026-11-13T14:30Z | false, close |
| `LT-*-zero_picks` (x8), `LO-*-zero_picks_anomaly` (x8), June | research produced zero picks | `data/manager_decisions.json` 16:06Z today: 22 new-buy candidates among 42 decisions | false, close |
| `CIT-2026-037` | `fe_board.py:24` swallows exceptions silently | line 24 now logs `[FEBoard] load failed` (on main) | fixed, close |
| `CIT-2026-216` | no integration tests for the pipeline | `test_pipeline_smoke.py` has 223 test classes, push gate runs it in two zones | false, close |
| `CIT-2026-1448` (x2) | Manager step fails `'str' object has no attribute 'get'` | 0 occurrences in canary logs 2026-11-12/13 | gone, close |

Not a close, a new problem found during the check: the Manager step failed with
`unsupported format string passed to NoneType.__format__` 4 times (3x on 2026-11-11,
1x on 2026-11-12 20:45Z), at "Step 5/5: Building management report", after the decision log
was saved. Trading was not blocked; the report was. Last seen 2026-11-12, none today. A
None reaching an f-string format spec: Risk/Ops fixer, with a test that feeds a None metric.

## specialist-fixer-run 2026-11-19 (ops area)

**Heartbeat "52.6h old on a weekday" rows** (`loop-sig-3e4b3877067e`, `loop-sig-6e402d7a3919`,
`loop-sig-b80ccaa0ece2`): premise false today. Check, run read-only on the VM 2026-11-19 09:46Z:
`ls -la data/pipeline_heartbeat.json` and its `last_started` = 2026-11-18T20:15:02 (13.5h old).
The 52.6h reading is Fri 20:17Z → Mon ~01:00Z: a weekend gap that the two heartbeat checks
compared raw to the 26h threshold. Fixed on branch `claude/fixer-ops-20261119` (weekend hours
subtracted, both ages reported). Supersede these three after that PR is merged **and deployed**
(`board_dispatch.py --supersede <id> --check "<above>" --reason "weekend gap read as outage; fixed in PR"`).
Note the message text changes ("… (Nh excl. weekends) …"), so a genuine future outage will
file under a new signature rather than re-open these.

**Stale-cron rows whose job runs nowhere** — NOT given cron lines (that is a VM deploy, Jack's
call). Check: `crontab -l | grep -v '^#' | grep -c <job>` on the VM, 2026-11-19:

| Row | Job | crontab entries | script on VM | Decision needed |
|---|---|---|---|---|
| `loop-sig-b0725b67137e`, `loop-sig-b62d906d23a8` | `ml_data_watchdog.py` | 0 | present | schedule it, or retire job + its freshness check |
| `loop-sig-41b3f4a76a2c`, `loop-sig-38b8a2c066e0` | `ml_alpha_decay_monitor.py` | 0 | present | same |
| `loop-sig-6910a9e170de`, `loop-sig-0f72893682f3` | `ml_research_team_leader.py` | 0 | present | same |
| `loop-sig-21837bc97ded` | `fe_cit_agent.py` | 0 | present | same |
| `loop-sig-3616daec9161` | `portfolio_risk_dashboard.py` | 0 | present | same |
| `loop-sig-e890ba6a27c3` | `fe_watchlist_monitor` | 0 | **missing** | premise moot: no such script; retire the freshness check, then supersede |
| `loop-sig-4e1461c8cc17` | `ml_improvement_agent.py` | 0 | present | same (added 2026-11-19 afternoon run) |
| `loop-sig-a289dc0e1944` | `ml_drift_monitor.py` | 0 | present (log is 0 bytes, mtime 11-16 20:15) | same |

## specialist-fixer-run 2026-11-19 afternoon (risk area)

Checks run read-only on the VM 2026-11-19 ~19:30Z, against `data/coherence_report.json`
(checked 18:02Z) and the broker (GET only).

| Row | Premise | Check | Verdict |
|---|---|---|---|
| `loop-sig-50cc501208bb` | `positions_conserve`: 1 ledger entry with no broker position | law today: HOLDS, "17 broker position(s), same 17 in the ledger", n=17 | false today, supersede |
| `loop-sig-018f54cc39f5` | `recorded_exits_are_executed`: DXCM 2026-07-23 exit never executed | `GET /v2/orders?symbols=DXCM&after=2026-07-15` → sell created 2026-07-23T13:45:04Z, **filled 51/51**; the 500-order window opened 15:24:25Z the same day | false alarm from the law's window edge (compared dates, not timestamps). Fixed on `claude/fixer-risk-20261119-exitwindow`; close after merge + deploy |
| loss-attribution group (16 rows, `LO-*-unexplained_loss` + `loop-sig-b92740cbf61d`) | attribution stale | handled by PR #63 (merged, not yet deployed) | not re-worked; mark after deploy |

Ops premises rechecked in passing (not superseded, recorded only):
- `loop-sig-52f253043d5e` disk 76%: `df -h /` = 69% today, under the 70% threshold. Borderline
  and recurring (6.3G of 9.7G), so a supersede would just re-file; leave for the ops fixer.
- `loop-sig-fa6b55655db2` `runs_match_schedule` "main.py started N times": HOLDS today
  ("3 start(s) today, 4 scheduled"). Supersede candidate.
