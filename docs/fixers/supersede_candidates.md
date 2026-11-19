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

## specialist-fixer-run 2026-11-19 evening (ops, then wiring)

**Stale-cron rows that are alive only because the Chairman item was never retracted**
(`loop-sig-4506b99ca0ee` CIT pipeline integrity "668h old", `loop-sig-39e135893752` FE pipeline
"673h old"). Check, read-only on the VM 2026-11-19 ~21:45Z: `ls -la logs/pipeline_integrity_*`
newest 20:50Z today, `logs/fe_*` newest 20:20Z today. The sentinel's "present" source is
`data/chairman_action_required.json`, which still holds 28 `apex_system_cit_agent`
"Cron job stale" items from 2026-07-30 to 09-09, all `awaiting_chairman`. Their text carries
the age, so their dedup keys never repeat and nothing could close them. Fixed on branch
`claude/fixer-ops-20261119-cronclose`: the agent closes its own items for a label it
re-observes fresh. A dry run on a copy of the VM file closes 5 (1 CIT integrity, 4 FE pipeline).
Once deployed and one weekday run has happened, the two rows are `superseded`/`implemented`.

**The other 23 old stale-cron Chairman items name labels the current check no longer
measures** (ML data watchdog, ML alpha decay monitor, ML research team leader, ML improvement
agent, ML drift monitor, FE CIT agent, FE watchlist monitor, Portfolio risk dashboard, EDGAR
scraper). The fix deliberately does not close them: a check that no longer runs is not
evidence the job recovered. They match the no-schedule table above. Jack decides per job:
schedule it, or retire it; then the items can be resolved by hand with a backup.

**`[orphan-agent] pregenerate_lessons.py` (28 `LT-*-wiring_contract_violation` rows, wiring).**
Premise false as of today's deploy. Check, read-only on the VM 2026-11-19 ~21:45Z:
`ls ~/institutional-tracker/pregenerate_lessons.py` → No such file; the copy is in
`~/archive_20261119/` and `agents/archive/`. Last row filed 2026-11-19T13:30Z, before the
20:28Z deploy (stamp `0d7764b`). Supersede all 28 after the next `logic_team.py` run (cron
12:00Z) files no new row: `board_dispatch.py --supersede <id> --check "file absent on VM,
archived 2026-11-19; no row after deploy" --reason "orphan file removed" --by fixer`.

**Also skipped this run:** risk `loss attribution stale` (2 risk + 5 wiring rows): premise true,
the missing piece is a written attribution, which is a board/Chairman judgment, not code
(see PR #63). `positions_conserve` and `runs_match_schedule` "N starts": HOLDS (PR #68 notes).
