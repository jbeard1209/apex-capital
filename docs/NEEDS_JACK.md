# Needs Jack

The ONE place for things only Jack can do. Every session appends here instead
of leaving paste-this-command instructions scattered across threads. Published
hourly to the private `apex-reports` repo as `NEEDS_JACK.md`, with a live
Mac / GitHub / VM sync section underneath.

Rules for sessions (see CLAUDE.md, "Shipping and the needs-Jack list"):
- Try it yourself first: `tools/ship.sh` commits, pushes, opens the PR, waits
  for CI and merges. Only a real failure goes here, with the exact error.
- Add with `tools/needs_jack.sh "title" "exact error + the one thing to do"`.
- Deploying to the VM, going live, deleting data and moving money still need
  Jack's OK. Ask in the thread; put it here only if the thread is gone.
- When an item is done, tick it (`- [x]`) and move it to "Done" with the date.

## Open

(nothing yet)

## Done

- [x] **ship.sh failed at 'deploy-precheck' on branch ?** (2026-11-19 14:28 CST, MacBook-Air-173)
      Command: tools/ship.sh --deploy-only 
      Error: refusing to deploy: deploy.sh ships the Mac checkout (/Users/jackbeard/Claude investment strategy), and its code differs from origin/main in: institutional-tracker/board_meeting.py institutional-tracker/reconciliation_check.py institutional-tracker/risk_gate.py institutional-tracker/test_pipeline_smoke.py 
      Resolved 2026-11-24: stale; those files reached main long ago and the VM was deployed 2026-11-24 (md5 verified).

- [x] **Mac disk nearly full: ~126 GB of old fixer worktrees in /private/tmp/claude-501** (2026-11-20 11:36 CST, MacBook-Air-173)
      specialist-fixer-run 2026-11-20 hit 'rsync: write failed ... No space left on device (28)' copying data/ into its worktree; /System/Volumes/Data had ~4.5 GB free. Cause: other sessions' fixer worktrees under /private/tmp/claude-501/-Users-jackbeard-Claude-investment-strategy/*/scratchpad/wt (fd261c1b 36G, 3b54e6fb 29G, e9fcc56d 15G, d78a0dad 15G, ...), each with a full data/ copy incl. the 6 GB chairman_queue_archive. Not deleted by the fixer (may hold unpushed work). One thing to do: confirm their branches are pushed (git -C <wt> status / log origin/<branch>..), then rm -rf those session dirs — or let fixer runs exclude chairman_queue_archive when copying data/.
      Resolved 2026-11-24: Jack approved deletion; the session folders were already gone (/private/tmp/claude-501 = 77 MB), 20 dead worktree entries pruned, the one live session's worktree kept.

- [x] **ship.sh failed at 'deploy-precheck' on branch claude/ship-20261124-142613** (2026-11-24 14:31 EST, MacBook-Air-173)
      Command: tools/ship.sh --deploy -t Sync\ check:\ 3h\ heartbeat\ publish\ and\ a\ VM\ watchdog\ that\ alerts\ when\ it\ stops -m $'Sync check: heartbeat publish + VM watchdog\n\nThe sync drift check kept running but skipped the publish whenever nothing\nchanged, so its public stamp froze on 11-22 and read as a dead job. It now\nre-publishes at least every 3h. sync_check_watchdog.py runs hourly on the VM\ninside kalshi_report_publish.py (cron entry cli(), never main()) and raises a\nChairman queue item + digest email when the Mac\'s check is 18h stale, or when\nit reports VM code drift. Silence never resolves an alarm.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>' .gitignore tools/sync_drift_check.py institutional-tracker/sync_check_watchdog.py institutional-tracker/kalshi_report_publish.py institutional-tracker/test_pipeline_smoke.py 
      Error: refusing to deploy: deploy.sh ships the Mac checkout (/Users/jackbeard/Claude investment strategy), and its code differs from origin/main in: docs/fixers/supersede_candidates.md 
      Resolved 2026-11-24: the stray file was shipped in the same session's next PR and the deploy re-run.

- [x] **Merge PR #39 (HR attendance roster fix)** (2026-11-12 15:42 CST, MacBook-Air-173)
      Both the audit thread and the sync thread were blocked from merging: Claude Code auto mode classifier denied 'gh pr merge 39 --merge' [Destructive or Unwanted Actions]. CI is 4/4 green and the diff (hr_agent.py, 10 lines) is reviewed. Do: reply 'merge 39' in the sync thread, or click Merge at https://github.com/jbeard1209/apex-capital/pull/39. After the merge, a session runs tools/ship.sh --deploy-only to take it to the VM.
      Resolved 2026-11-13: Jack OK'd in thread; merged (6c6daea) and deployed, VM md5 verified.
