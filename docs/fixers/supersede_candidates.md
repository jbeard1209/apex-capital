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

## specialist-fixer-run 2026-11-20 (read-only checks on the VM, ~18:45Z, after the 16:42Z deploy of f818ede)

| Row(s) | Premise | Check | Verdict |
|---|---|---|---|
| 28x `LT-*-wiring_contract_violation` (pregenerate_lessons) | orphan file runs on VM | `ls pregenerate_lessons.py` on VM: absent; 0 crontab entries; newest row filed 2026-11-19 13:30 | false, supersede |
| `loop-sig-3e4b3877067e`, `loop-sig-6e402d7a3919` (heartbeat) | pipeline dark | PR #62 merged and deployed; heartbeat last_started 2026-11-20T18:00 | false, supersede now |
| `loop-sig-fa6b55655db2` (main.py 5 starts vs 4) | overrun | heartbeat starts per day 11-02..11-19 all exactly 4 | false, supersede |
| `loop-sig-8501052b501e` (main.py scheduled 0x) | not scheduled | VM crontab has 4 main.py lines | false, supersede |
| `loop-sig-26e9569bab21`, `loop-sig-57c882a5de45` (deployed/shipped set) | VM not running repo | md5 of the 10 named files + vm_crontab_snapshot.txt match Mac post-deploy; sync_drift_check IN_SYNC | false (merge-to-deploy lag), supersede |
| `loop-sig-966c67cd4118` (QQQ index gap) | market event | not a code defect | close |
| `loop-sig-e70fa24cd3d0` (deputy-board crash detector gap) | no alarm on crash | deputy_board beats liveness (max_silence 26h, since 11-19); VM heartbeat fresh 11-20 10:15Z | false, supersede |
| `loop-sig-0adf10ac94ad` (6 tests read gitignored agent_registry) | suite breaks without it | tests skipTest when absent; Mac has no file and the gate passes | false, supersede |
| `loop-sig-618c2c21fb90`, `-f0ff0d888af8`, `-39c582aff1bd`, `-2be31998caed` (BBIO/FICO/MSCI/TDG) | impossible fundamentals | fund-sanity rows `executed`; check_ticker shows negative equity, impossible=False | false, supersede |
| `loop-sig-cdf68e3a3a4b`, `loop-sig-8a4cf5e69830` | company_research / fe_watchlist missing | both exist on VM (14 files; watchlist 2026-11-02) | false, supersede |
| `loop-sig-45bd6464e89d` (out of sync) | drift | sync_drift_check IN_SYNC 18:48Z | false, supersede |
| risk `loop-sig-b92740cbf61d`, `LO-20261118201625-unexplained_loss` | attribution stale | TRUE: 78 snapshots, newest today, only attribution 2026-06-09. Not code: a written loss_attribution is a board/Chairman judgment | not a fixer row |

**Real defect, claimed, NOT fixed (run aborted, Mac disk full):** `loop-sig-154bd6829ee9` / `fund-sanity-SBAC`.
SBAC files no us-gaap `Liabilities` tag, so `fe_edgar_fundamentals` stores `total_liabilities: {}` and
`fundamentals_sanity` fails closed to "physically impossible". EDGAR (fetched on VM): FY2025
LiabilitiesAndStockholdersEquity $11.575B, StockholdersEquity -$4.854B => liabilities $16.43B >= debt $12.90B:
negative equity, consistent. Fix: in `_fetch_edgar_fundamentals`, fill missing `total_liabilities` years with
LiabilitiesAndStockholdersEquity minus StockholdersEquity(IncludingNCI). Row is `claimed` on the VM
(backup `data/cit_implementation_queue.before-fixer-claim.*.json`); release it or let the 3-day expiry return it.

## specialist-fixer-run 2026-11-24 (read-only checks on the VM, ~18:50-19:20Z; VM at f818ede, deployed 2026-11-20)

| Row(s) | Premise | Check | Verdict |
|---|---|---|---|
| `CIT-2026-268` | None canary indicators (`spy_price`, `vix`, ...) | `canary_state.json` has no such top-level keys; `last_readings` 18:50Z today: spy_pct -0.58, vix_level 26.22, market_open true. The detector was rewritten (cit_director.py check 4 now reads `last_readings`, title "None canary readings") | false (old detector read keys that never existed), supersede |
| `CIT-2026-267`, `CIT-2026-1005`, `CIT-2026-1149` | None score in sector_assignments | VM file 16:00Z: 8 assignments, 0 with None/NaN `momentum_score` or price; detector now reads `momentum_score` (old one read `score`, a field never written) | false, supersede |
| `CIT-2026-1448` | Manager step `'str' object has no attribute 'get'` | `grep -c "has no attribute" logs/canary_2026112*.log`: 0 on each of 11-20..11-24 | gone, supersede |
| `CIT-2026-210` | hardcoded ticker at cit_director.py:326 | the flagged line was the scanner's own comment (`"id": "CLOSE_BA"` example); that text no longer exists in cit_director.py on the VM | false (self-match), supersede |
| `investigator-20261114-vix-index-as-pick` | ^VIX emitted as a BUY_NEW pick | `research_team._drop_untradeable` on main and on the VM (2 refs); `TestAnIndexIsNotAPick` | fixed, supersede |
| `loop-sig-52f253043d5e` (disk 76%) | disk elevated | `df -h /` on VM: 71% of 9.7G (2.7G free); data/ 1.2G, venv 1.6G. Largest removable item: `data/historical_13f.before-two-sigma-cik.20261119201815.db` (155M backup) | still true but not a code fix: Jack decides whether to delete that backup or grow the disk |
| risk `loop-sig-b92740cbf61d`, `LO-20261118201625-unexplained_loss`, wiring 16x "no current loss attribution" | attribution stale | unchanged from 2026-11-20: a written loss attribution is a board/Chairman judgment | not a fixer row |

Claimed and PR'd this run (both rows now `pr_open` on the VM): `investigator-cit-20261024-pt-id-collision`
(wiring, PR #82, branch `claude/fixer-wiring-20261124`) and `loop-sig-154bd6829ee9` (FE SBAC, PR #83,
branch `claude/fixer-fe-20261124`).
Backup before claim: VM `data/cit_implementation_queue.before-fixer-claim.<UTC stamp>.json`.
The PT-collision row's second half ("PROCESS_IMPROVEMENT unrouted") is not fixed: PT proposals go to
`rd_pod_pending.json` → `rd_pod_board` → `rd_pod_approved.json`, and no executor reads
`rd_pod_approved.json`. Whether R&D pod approvals should reach `board_dispatch` is a design call.

## specialist-fixer-run 2026-11-26 ~20:45Z (read-only checks on the VM; stamp d8b4986, deployed 2026-11-25 21:43Z)

Nothing new to fix in risk, ops, wiring, fe, research or kalshi. Every open group has
either an open fixer PR (#64 #66 #67 #68 #74 #75 #76 #81 #82 #83 #86 #87 #92 #96) or a recorded check
from an earlier run (11-20, 11-24, 11-25, and 11-26 at ~05:45Z and ~08:48Z). The only rows filed
since the 08:48Z run are 5 more `LT-*-loss_no_attribution` copies (not a fixer row; PR #76 stops the
re-filing) and two `awaiting_chairman` rows (Kelly sizing, ETF zero picks), which go to the Chairman.

**The rerun-job loop that PR #86 fixes is still running on the VM, under new signature ids.**
| Row | Job | `executed` reruns filed in the VM queue | Job health |
|---|---|---|---|
| `loop-sig-a9c8bd129799` | ML improvement agent | 11-24: 1, 11-25: 6, 11-26: 5 | `logs/ml_improve_20261126.log` written 06:30Z today |
| `loop-sig-2733dd4cbc6c` | ML drift monitor | 11-24: 1, 11-25: 6, 11-26: 5 | `logs/ml_feature_drift.log` written 13:30Z today |

Check: `grep -c RERUN_REPEAT_WINDOW_DAYS board_dispatch.py` on the VM returns 0, so #86 is not
deployed. Both signatures read `SILENT_DROP` in `problem_lifecycle.json`. They differ from #86's
rows (`loop-sig-4e1461c8cc17`, `loop-sig-a289dc0e1944`), but #86 keys its repeat check on the
script rather than the row id, so it covers these too. No new PR. The loop stops once #86 is
merged and deployed. The rows close after the stale Chairman items listed under 11-24 are resolved.
