# Specialist fixers: briefs and routing (draft, 2026-11-13)

Prep for build day, Thursday 2026-11-19 2:00 PM CST. See `docs/fixer_build_plan.md`.

- `fixer_routing_draft.py`: the routing table. On build day it becomes
  `institutional-tracker/fixer_routing.py` and `board_dispatch._new_row()` stamps `owner_area`.
- `<area>.md`: the brief the scheduled fixer routine loads for a row in that area.

## Routing measured against the live VM queue (2026-11-13 16:47Z)

229 rows; 106 are open (not skipped/protected). Every open row was routed and each
assignment checked by eye. Result: **0 fell through to the fallback.**

| Area | Rows | Distinct problems (approx.) |
|---|---|---|
| ops | 32 | 14 (stale crons are 10 jobs, several filed twice) |
| wiring | 24 | 14 (7 rows are one stale file, see below) |
| research | 21 | 5 (16 are the June zero-picks duplicates) |
| fe | 12 | 9 (5 quarantine rows, 4 already standing) |
| risk | 9 | 5 (4 rows are one stale loss attribution) |
| kalshi | 5 | 2 |
| fi | 2 | 1 (June, premise false) |
| chairman | 1 | 1 (idle $30k ML capital: never a fixer) |

Two changes from the 2026-11-12 plan, both forced by the real rows:
1. **An eighth area, Research.** Zero-pick and pod-output rows (21) belong to neither Risk
   nor Wiring. The plan listed seven fixers.
2. **Fixers work on problems, not rows.** 106 rows are about 50 problems. The routine must
   group rows by (area, normalised title) and close every row of a group with one PR, or
   it will open duplicate PRs and spend its 5-PR budget on one bug.

Routing had to be two-pass (headline first, evidence second). A single pass over the whole
row sent 10 zero-pick rows to Risk because their evidence quotes the CRO level.

## Found while routing: a re-filing loop (not yet fixed)

7 of the 8 new "no handler" rows since 2026-11-12 are `LT-*-wiring_contract_violation`
for `[orphan-agent] pregenerate_lessons.py`. That file was archived from the repo on
2026-11-12, but `deploy.sh` rsyncs without `--delete`, so the VM still has its June copy
and the Logic Team re-files the orphan finding every run. This is the same gap the
`deployed_set_matches_repo` law reports ("1 file(s) run" that the repo does not have).
Fix belongs to the Ops fixer (deploy removes or archives files dropped from the repo),
and the 7 rows close together once it lands.

## Cost estimate for one run (N = 5 problems)

Not yet measured; build day's supervised run is the measurement. Bound from the work:
each problem needs reading the area, a fix, a failing-then-passing test and the suite in
two time zones (~8 min wall each). Expect one long Claude Code session of 1.5 to 3 hours
per run. At 50 open problems and about half being code fixes, a daily weekday run clears
the backlog in roughly two weeks; 3x/week takes about a month. Recommendation: daily for
two weeks, then 3x/week, decided after the first measured run.
