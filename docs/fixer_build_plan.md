# Specialist fixer build plan

Build day: **Thursday 2026-11-19, 2:00 PM CST** (scheduled task `fixer-build-day`).
Written 2026-11-12. Owner: Chairman (Jack). Approved in chat on 2026-11-12 ("schedule a build for next week… build a lot on that day").

## The problem in one paragraph

The board now approves a fix exactly once (`board_dispatch.py`), and the fix gets written to
`data/cit_implementation_queue.json`. Only one kind of fix has anything that actually carries it
out: `fund-sanity-*`, which quarantines a ticker. The other ~80 approved rows sit at
`status: queued` until someone hand-writes the change. Jack's direction is that the board should
order the agent whose job it is, and that each area should have its own specialist fixers.

## What is actually in the queue (VM, 2026-11-12 16:45Z)

224 rows: 121 skipped, 85 queued, 6 needs_human, 6 awaiting_chairman, 4 standing_quarantine,
2 protected.

The 85 queued rows break down like this:

| Group | Rows | Real underlying problem | Area |
|---|---|---|---|
| `loop-sig-*` (Problem Lifecycle Sentinel) | 58 | Recurring problems. The rows carry **no evidence**, only a signature hash; the text lives in `data/problem_lifecycle.json` | mixed (see below) |
| `LT-*-zero_picks` / `LO-*-zero_picks_anomaly` | 16 (8 ids, each twice) | June "research pipeline dark" | Research |
| `LT-015346-fi_never_run` | 2 | June "FI never ran". **Premise false**: FI ran 2026-11-12 14:30Z | FI |
| `CIT-2026-1448` | 2 | 6 ERROR lines, `'get'` | Ops |
| CIT BUG_FIX / NEW_FEATURE, investigator, other | ~7 | various | mixed |

Breaking the loop signatures down through `problem_lifecycle.json` gives these clusters:
- **Ops / scheduling:** stale cron logs (ML improvement, ML data watchdog, ML drift, ML alpha decay,
  ML research leader, CIT pipeline integrity, FE pipeline, FE CIT, FE watchlist monitor,
  portfolio risk dashboard), a stale pipeline heartbeat, disk 76–78%, email buffer overflow
  and repeated subjects, and `runs_match_schedule`.
- **Wiring / governance:** the sweep findings (45 employed agents that run nowhere, 13 inputs
  nobody produces, 9 outputs nobody reads, 5 agents that mutate state at import, and others),
  SLA re-escalations, and "disappeared from every queue with NO recorded resolution".
- **FE:** missing `fe_watchlist.json` and a missing `data/company_research/`.
- **Risk / broker:** `positions_conserve`, the designated writer going silent, VM unreachable.
- **Kalshi:** the build-or-drop verdict row.
- **ML:** idle $30k pod capital (this is a capital decision, so it goes to the **Chairman**, not a fixer).
- **HR:** Macro/ETF pod below its output OKR.

## Design

### Two lanes

1. **Data and settings lane: deterministic, no AI cost.** If the fix is "set a value / quarantine /
   re-run a job", the owning sector agent applies it through a handler in
   `board_dispatch.HANDLERS`. That's how BBIO works today. New handlers to add:
   - `rerun-job`: for stale-cron rows it runs the job once. If the job has no crontab entry at
     all, it escalates to the Ops fixer as a code/scheduling fix instead.
   - `superseded`: closes a row whose premise is re-checked and found false. It records the
     check, the reason and a backup.
2. **Code lane: specialist fixers (Claude sessions, cost usage).** One fixer per area. Each one
   gets a short system brief covering its area's files, its known postmortems, and the tests that
   guard the area.

| Fixer | Owns (files / prefixes) | Never touches |
|---|---|---|
| Risk | `main.py` CRO, `alpaca_trader.py`, `single_writer.py`, `signoff_ledger`, `positions_conserve` | risk limits and capital: Chairman only |
| Fixed Income | `fi_*` | |
| First Eagle | `fe_*`, `fe_watchlist`, `company_research` | |
| Kalshi | `kalshi_*` | arm gate: Chairman only |
| ML | `predictive_model/`, `ml_*`, `feature_engineer.py` | must pass `model_test_harness.py` vs prior report |
| Ops | crontab (`gcp_setup/`), heartbeat, disk, email/`mail_guard`, `catch_up` | |
| Wiring | sweep findings, `wiring_audit.py`, `logic_team*`, `board_dispatch`, `problem_lifecycle` | |

To save usage, this runs as **one scheduled routine, not seven**. Each run takes the top N rows
(ordered by severity, then age), loads the matching fixer brief, and works them one at a time.

### Routing (the contract)

- `board_dispatch._new_row()` stamps `owner_area` using a mapping table
  (`fixer_routing.py`): proposal_id prefix → source → file paths in the evidence → keywords.
- If nothing matches, the row goes to **Wiring**. A row is never left unrouted; that's the firm's
  main bug class.
- `TestWiringContracts` gets a case that checks every producer prefix seen in the queue maps to
  a fixer that has a brief file.
- The loop-sig producer (`problem_lifecycle_sentinel`) must **copy the problem text into
  `evidence`**. Today those rows are empty, so no fixer could act on them. This is a defect in its own right.

### Row lifecycle (new statuses)

`queued → claimed(owner, branch) → pr_open(url) → implemented` (after merge **and** deploy, checked by md5 on the VM)
plus `superseded` (premise false, with the check recorded) and `needs_human` (fixer gave up, with the reason).

`DONE_STATUSES` gains `superseded`. `claimed` and `pr_open` count as standing, so the board doesn't
re-vote them. A `claimed` row older than 3 days returns to `queued`, so a dead session can't
strand it. That's the "a queue that never empties" lesson.

### Hard rules for every fixer
- Work on a branch, run the full suite in both time zones plus `wiring_audit` and `logic_team`, then open a PR. Never push to main, and never deploy.
- Never edit live `data/` without a backup and a record file (see the 2026-11-12 fixture-leak cleanup).
- Risk limits, capital, the Kalshi arm gate and terminations go to the Chairman queue, never to a fixer.
- Each fix ships with a test that fails without the fix (a mutation check).
- At most 5 PRs per run. If a fix isn't proven within the run, it's set back to `needs_human` with notes.

## Prep before build day (2026-11-13 → 18)
1. Close the June duplicates through `superseded`, each with a recorded check: fi_never_run,
   CIT-2026-216, CIT-2026-037. Still to verify: the zero_picks rows and CIT-2026-1448.
   Back up the queue first, never touch `escalated_to_board` rows, and give Jack the list.
2. Commit and deploy the test-suite sandbox (fixture-leak guard).
3. ✓ 2026-11-13: `docs/fixers/fixer_routing_draft.py`, measured on the live VM queue: 106 open
   rows, 0 unrouted. See `docs/fixers/README.md`.
4. ✓ 2026-11-13: briefs in `docs/fixers/<area>.md`. **Eight areas, not seven:** Research was
   added (21 zero-pick / pod-output rows fit neither Risk nor Wiring).
5. ✓ 2026-11-13 (estimate only): see `docs/fixers/README.md`. The build-day supervised run
   is the real measurement.
6. Routine must group rows into problems (106 rows ≈ 50 problems) and close each group with
   one PR. Found while routing; add to build-day step 5.

## Build day checklist (gates must all pass before reporting done)
1. `superseded` / `claimed` / `pr_open` statuses + tests. **Gate:** suite OK in both zones.
2. `owner_area` stamping + `fixer_routing.py` + backfill on both hosts (with backup).
   **Gate:** `0 unrouted` over the live queue on the Mac and the VM.
3. Loop-sig evidence fix + backfill. **Gate:** 0 loop-sig rows with empty evidence.
4. `rerun-job` handler. **Gate:** a mutation test plus a dry run that lists what it would rerun.
5. Fixer briefs and the scheduled fixer routine. **Gate:** one supervised run produces at least 1 PR,
   and that PR's tests fail without its fix.
6. Wiring contract test for routing. **Gate:** `wiring_audit.py` exits 0.
7. Rerun every gate after the last change, then report to Jack with the PR list.
