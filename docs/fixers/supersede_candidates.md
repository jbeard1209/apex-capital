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

## specialist-fixer-run 2026-11-19, afternoon (risk → ops → wiring)

All checks read-only on the VM, 2026-11-19 ~15:45–16:00Z, VM at deploy stamp `56391cf`
(11:17Z). Supersede each with
`board_dispatch.py --supersede <id> --check "<check>" --reason "<reason>" --by specialist-fixer-run`
**on the VM** (it is the VM's queue). Not run by this session.

| Row(s) | Premise | Check | Verdict |
|---|---|---|---|
| `loop-sig-50cc501208bb` | `positions_conserve`: 1 ledger entry with no broker position | `data/coherence_report.json` 13:56Z: HOLDS, "20 broker position(s), same 20 in the ledger", n_examined=20; lifecycle status RESOLVED | false, close |
| `loop-sig-018f54cc39f5` | `recorded_exits_are_executed`: XLK exit never executed | same report: HOLDS, 135 exits all match a broker fill, n_examined=135. **Caveat:** "predate the order window" rose 12 → 17; the September XLK `max_hold` rows (several `PENDING_NEW` order ids) have likely aged *out* of the window, not been fixed. The phantom P&L may still sit in `state.json` sell_log and in cit_director's win rate | **do not supersede yet**: needs a one-off broker lookup of the XLK order ids from 2026-09-22..29 (risk fixer, next run) |
| 27× `LT-*-wiring_contract_violation` ("[orphan-agent] pregenerate_lessons.py …") | the file runs on the VM unscheduled | `ls pregenerate_lessons.py` → absent; only `agents/archive/pregenerate_lessons.py` exists. `logs/logic_team_20261119.log`: fired at 00:00Z (pre-deploy code), **did not fire at 12:00Z** (post-deploy). Newest row `LT-000034-…` (queued 13:30Z) is the board approving the 00:00Z submission | false. Supersede after the resolution tracker's 3rd clean run (VM 2026-11-20 12:00Z) so no straggler re-files |
| `loop-sig-4506b99ca0ee` | CIT pipeline integrity log 668h old (threshold 36h) | `logs/pipeline_integrity_20261119.log` written 14:50Z; current `apex_system_cit_agent._CRON_LOG_CHECKS` threshold is 6h, not 36h — the row is from pre-fix code | false, close |
| `loop-sig-39e135893752` | FE pipeline log 673h old (threshold 36h) | `logs/fe_20261119.log` written 15:02Z; current threshold 73h | false, close |
| `loop-sig-52f253043d5e` | GCP disk 76% (threshold 70%) | `df -h /` → 69% | false today; recurring (20 occurrences) — close, it will re-file if it creeps back |
| `loop-sig-f144f924c8e4` | email buffer > 50 ⇒ sender broken | `logs/digest_20261118.log`: "Daily digest sent (85 reports)"; buffer 64 at 15:55Z, all since the last 22:00Z flush | false; the check was miscalibrated — fixed in the `claude/fixer-ops-20261119-email` PR. Supersede after that PR is deployed |

Skipped, not false: the 16-row "attribution stale" risk group and its 2 wiring twins
(`LT-*-loss_no_attribution`) — the premise is true (no loss attribution since 2026-06-09)
and the missing piece is a written judgment by the board/Chairman (see PR #63), not code.
