# M00 — Liveness watcher + registry (pass 1, 2026-11-13, run `recurring`)

Host: MacBook-Air-173.local, /Users/jackbeard/Claude investment strategy -> MAC (worktree
apex-audit-2026-11-13). VM not probed: M00 is a build; nothing is deployed.

## What was built (PR on branch audit/M00-liveness-2026-11-13)
- `institutional-tracker/liveness.py` — `beat(feature, n_done, n_examined)` + `check()` watcher.
  Alarms on MISSING / STALE / NO_OUTCOME (alive but did 0 of N). Registered-but-uninstrumented
  features are counted as UNINSTRUMENTED (burn-down list), never reported healthy, not paged.
  Escalates alarms to the Chairman queue (dedup per feature). Pings `APEX_LIVENESS_HC_URL`
  (healthchecks.io) only if it judged >0 features — the external watch-the-watcher.
- `institutional-tracker/config/liveness.json` — registry, 15 features mapped to gaps G01–G20.
  All 15 start `instrumented:false`; each later module flips its feature to true in the same PR
  that adds the `beat()` call.
- `institutional-tracker/test_liveness.py` (8 tests, imported into test_pipeline_smoke.py so the
  suite runs it). Poison pills: planted silence => STALE alarm; alive-but-0-done => NO_OUTCOME;
  instrumented-but-no-file => MISSING; corrupt heartbeat => STALE, not crash.

## Gates
| Gate | Command | Result |
|---|---|---|
| G1 watcher unit tests incl. poison pills | `python3 test_liveness.py` | PASS, 8/8 |
| G2 registry parses, n>0 | `liveness.py --no-alert` | PASS, `examined=15 alarms=0 uninstrumented=15` |
| G3 full smoke suite | `python3 test_pipeline_smoke.py` | 1849 run, 5 failed in the bare worktree (gitignored data/ missing: hr_history.json, LT db; HR roster mismatch = M13 finding). 8 TestLiveness tests ran and passed. None touch liveness |
| G4 live VM heartbeat | — | BLOCKED: not deployed (needs Jack) |

## Honest limits
- 0 features are instrumented yet, so the watcher currently proves nothing about the firm; it
  makes the 15 gaps countable. It would have caught the board-vote incident only once
  `board_vote` is instrumented (G02, next).
- Deploy needs Jack: add cron line on VM + set APEX_LIVENESS_HC_URL. Not done (read-only rule).
