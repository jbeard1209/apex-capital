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
