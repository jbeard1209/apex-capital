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

## specialist-fixer-run 2026-11-20 (risk → ops → wiring)

**27 `LT-*-wiring_contract_violation` rows, `[orphan-agent] pregenerate_lessons.py`** (queued
2026-11-12 → 2026-11-19): premise false today. Check, read-only on the VM 2026-11-20 ~17:20Z:
`ls pregenerate_lessons.py` → "No such file or directory"; `crontab -l | grep -c pregenerate_lessons` → 0;
`grep -c pregenerate_lessons logs/logic_team_20261120.log` → 0 (no re-file today); not in
`origin/main`. Supersede all 27 (`board_dispatch.py --supersede <id> --check "<above>"
--reason "orphan file removed from the VM; Logic Team no longer files it"`). Ids:
LT-000028/030/032/034/057, LT-120028/030, LT-134655/134738/134806, LT-135624/135700,
LT-160229/160247/160255/160301/160338, LT-180129/180140/180143/180148/180156,
LT-201624/201628/201629/201631/201646 (each `-wiring_contract_violation`).

**Heartbeat rows `loop-sig-3e4b3877067e`, `loop-sig-6e402d7a3919`**: the weekend-gap fix (PR #62)
is now deployed: VM `deploy_stamp.json` sha f818ede at 2026-11-20T16:42Z, and
`md5sum cit_pipeline_integrity_agent.py` on the VM = origin/main (cc2b68de…). Heartbeat
mtime 2026-11-20 16:30Z; neither row reseen since 2026-11-19T10:15Z. The condition from the
2026-11-19 entry ("after merged **and deployed**") is met. Supersede both.

**Risk group, stale loss attribution** (`loop-sig-b92740cbf61d`, `LO-20261118201625-unexplained_loss`):
premise TRUE, not a supersede and not a code fix. `loss_attribution.status()` on the VM today:
STALE, newest attribution 2026-06-09 (164 days), 78 daily snapshots being written (newest
2026-11-20). What is missing is a written `loss_attribution`, which the module leaves to the
board or Chairman on purpose. Note `loop-sig-b92740cbf61d`'s frozen evidence still says
"Nothing in the repo writes performance_history.json", which stopped being true (PR #63).
