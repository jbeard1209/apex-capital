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

## specialist-fixer-run 2026-11-19 ~12:45Z (second pass: risk → ops → wiring)

VM deploy stamp `56391cf` (2026-11-19T11:17Z). All checks read-only on the VM. None of these
rows were superseded in this run; each needs `board_dispatch.py --supersede <id> --check "<check>"
--reason "<reason>"` on the VM (it writes its own backup).

| Row(s) | Premise | Check run 2026-11-19 | Verdict |
|---|---|---|---|
| `loop-sig-50cc501208bb` (risk) | positions_conserve: 1 ledger entry with no position | `data/coherence_report.json` (11-18 20:17Z): HOLDS, 20 broker positions = 20 in ledger | false, close |
| `loop-sig-018f54cc39f5` (risk) | recorded_exits_are_executed: XLK exit never executed | same report: HOLDS, 135 of 135 recorded exits match a broker fill | false, close |
| `loop-sig-4506b99ca0ee` (ops) | CIT pipeline integrity log 668h old (threshold 36h) | `logs/pipeline_integrity_20261119.log` written 12:50Z today; `apex_system_cit_agent.py` md5 `73859dd3…` matches on Mac and VM, and its threshold is now 6h, not 36h. So the 36h text comes from the retired list; only the queue row is keeping it alive | false, close |
| `loop-sig-39e135893752` (ops) | FE pipeline log 673h old | `logs/fe_20261118.log` written 11-18 15:04Z (22h old, threshold 73h) | false, close |
| `loop-sig-52f253043d5e` (ops) | GCP disk 76% (threshold 70%) | `df -h /` reads 68% | false today; close, and it re-files if usage climbs past 70% again |
| `loop-sig-f144f924c8e4` (ops) | email buffer 87 unsent (threshold 50) | `data/email_buffer.json` holds 15 | false, close |
| `loop-sig-fa6b55655db2` (ops) | main.py started 5x vs 4 scheduled | coherence report: runs_match_schedule HOLDS, 4 of 4 | false, close |
| `LT-*-wiring_contract_violation` ×27 (wiring) | `[orphan-agent] pregenerate_lessons.py` | no `pregenerate_lessons.py` in the VM root (only `agents/archive/`); 0 findings mentioning it in `data/logic_team_findings.json`; newest queue row 11-18 20:16Z, before today's deploy | false after deploy, close (re-check that no new row has appeared since the next logic_team run) |

Claimed this run (now `pr_open`, PR jbeard1209/apex-capital#64): `loop-sig-f7af8b6e2e5b`,
`loop-sig-eb3d681f6a23` (designated writer "silent", caused by weekends plus a firm-wide buy wall).
VM queue backup: `data/cit_implementation_queue.before-fixer-claim.20261119125854.json`.

Skipped: risk `attribution stale` (16 rows). Its text was fixed in #63, and the premise is
still true: it needs a written `loss_attribution`, which is a board or Chairman judgment, not
code.
