# Fixer build day — report (run early, 2026-11-18 evening CT)

Jack asked for the Thursday operation to run on Wednesday night instead of Thursday
2 PM. The scheduled `fixer-build-day` task is **disabled** (not deleted) so it does not run a
second time.

## What was built, and why

The board approves fixes faster than anything executes them. On the VM tonight the
implementation queue has 212 distinct rows, 115 of them `queued`, and the dispatcher reports
`no_handler=115`: approved, and owned by nobody. The plan (docs/fixer_build_plan.md) is a set of
eight specialist fixers (risk, fi, fe, kalshi, ml, ops, wiring, research), each a Claude session
with a brief, that take a group of rows, fix them test-first and open a PR. That needs four
things the queue did not have, and they are built in this PR:

1. **Words for a fixer's work** (`board_dispatch.py`). The new statuses are `claimed`,
   `pr_open` and `superseded`, with verbs that enforce the order:
   - `claim`
   - `mark_pr_open`
   - `mark_implemented`, which refuses unless the note records the VM md5 check
   - `release`
   - `supersede`, which refuses without both the check that was run and the reason, and
     writes a queue backup first

   A claim older than 3 days goes back to `queued`, so a dead session cannot strand a row
   forever. No verb will move an `escalated_to_board` row.
   - *Why it matters:* the firm's main bug class is a producer writing a word no consumer
     knows. `superseded` was added to **both** readers in the same change (board_dispatch
     `DONE_STATUSES` and problem_lifecycle_sentinel `RESOLVED_STATUSES`). The sentinel was
     also missing `executed`, a pre-existing instance of exactly this bug: a row the
     dispatcher had finished still read as open to the sentinel. A test pins that the two
     sets agree.
2. **An owner on every row** (`fixer_routing.py`, promoted from the 11-13 draft). Each new row
   is stamped with `owner_area` and the rule that picked it. Old rows are backfilled on the
   dispatcher's next run, after a backup.
   - **Mac gate:** 22 open rows, 0 unrouted. Backfilled live tonight, backup
     `data/cit_implementation_queue.before-fixer-backfill.20261118212419.json`.
   - **VM gate (dry run on a copy):** 131 open rows (latest row per proposal), 0 unrouted.
     By area: wiring 42, ops 31, risk 20, research 17, fe 14, kalshi 5, fi 1, chairman 1.
     The queue file holds 263 rows; 51 are older copies of a re-filed proposal, which the
     dispatcher ignores, and 9 of those older copies stay unstamped on purpose.
3. **Problem text on loop rows.** All 60 `loop-sig-*` rows on the VM had empty evidence. The
   sentinel put the problem in `description` and the queue copied only `evidence`, so a fixer
   opening one saw "[LOOP] Recurring problem needs a STRUCTURAL fix" and nothing else.
   - Two fixes: `_new_row` now falls back to the description, and the backfill copies the
     problem's title and recurrence count from `problem_lifecycle.json`.
   - **Gate:** 0 loop rows with empty evidence (Mac live; VM dry run 60 → 0).
4. **A rerun-job handler.** A "Cron job stale on weekday -- X log" row now reruns X's own
   crontab line once, but only when all four of these hold:
   - exactly one script matches
   - it is scheduled on this host
   - it is not on the deny list (anything touching the broker, Kalshi, orders, deploy or
     main.py)
   - fewer than 2 reruns have happened this dispatch run

   Otherwise the row goes to the ops fixer with the reason.
   - *Dry run on the VM:* 6 rows would rerun. 9 go to ops, because the job they name has no
     crontab entry or no script at all.
   - *Finding:* no current code emits these stale-cron labels. The rows are kept alive only by
     the queue itself, and the ops fixer should retire them.

There is also a routing wiring contract: every rule's area is a known area, every fixer has a
brief file, and the Chairman carve-outs (capital, risk limits, the arm gate, terminations) beat
every other rule.

## The supervised fixer PR (step 5)

The candidate from the 11-13 premise checks was the Manager's Step 5/5 crash: `unsupported
format string passed to NoneType.__format__`. Cause: `review_agent` sets `hold_days=None`
when a position's entry date is unknown, which is always true on the Mac because it keeps no
entry-date ledger. The report did `{d.get('hold_days', 0):>3}`, and `.get(k, 0)` only
defaults a *missing* key. The same None printed as "Noned" in the HTML email.

Trading was never blocked, but that cycle's management report was lost. Separate branch
`claude/fix-manager-report-hold-days`. Its two tests fail on main with the exact production
error and pass with the fix.

The scheduled routine `specialist-fixer-run` exists, **manual-only**. Its first step checks
that the VM has this code and stops if it doesn't, so it cannot run before the deploy.

## Rows to supersede: rechecked tonight, not closed yet

The premises were re-checked on the VM tonight, read-only:
- `fi_pipeline_report.json` was written 11-18 14:30Z.
- The Manager saw 21 to 77 BUY_NEW candidates per run on 11-18, so research is producing
  picks. All were rejected because the book is about 107% deployed under CRO HIGH.
- `fe_board.py:24` logs its exception.
- The `'str'.get` error has 0 hits in logs from 11-17 to 11-19.

So all 13 June rows in docs/fixers/supersede_candidates.md are still moot. They are **not
closed yet**, on purpose. `superseded` is only understood by the code in this PR, so writing it
on the VM before deploy would recreate the exact mismatch this PR fixes. After the deploy:

```
python3 board_dispatch.py --supersede <id> --check "<what you ran>" --reason "<why moot>" --by fixer-build
```

Run it once per row on each host.

`LO-20261116135700-zero_picks_anomaly` (new, awaiting the Chairman) counted "zero picks for 3
days" across a weekend. It looks like a false alarm from a detector that does not skip market
holidays. Left alone; it is the research fixer's first job.

## Gates

See the final rerun in the PR description. The gates are:
- the suite in the local zone and with TZ=UTC
- `wiring_audit.py` exits 0
- `logic_team.py`
- 0 unrouted and 0 empty-evidence rows (Mac live, VM dry run)
- the mutation tests (stubbing the rerun call, or dropping `superseded` from the sentinel,
  each turns the suite red)

## Not done, and why

- **No deploy.** All of this is inert on the VM until Jack approves `deploy.sh`.
- Once deployed, the first dispatch run will:
  - backfill the VM queue, after taking its own backup
  - rerun up to 2 stale non-trading jobs per run
- **Nothing merged.** Both PRs need review.
