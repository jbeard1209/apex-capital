# Ops fixer

Owns: `gcp_setup/` (crontab via `restore_crontab.sh`, `deploy.sh`), `catch_up.py`,
`mail_guard.py`, email buffer, disk, pipeline heartbeat, `canary_agent.py`, coherence laws
`runs_match_schedule`, `deployed_set_matches_repo`, `shipped_config_matches_repo`,
data-integrity rows (None scores / None canary indicators).

Live rows: 32. Problems: 10 jobs with stale or missing cron logs (5 ML, CIT pipeline
integrity, FE CIT, FE pipeline, FE watchlist monitor, portfolio risk dashboard), disk 76%,
email buffer overflow (87) and one repeated subject, heartbeat 52h stale (check it is not
historic), `main.py` 5 runs vs 4 scheduled, deploy leaves files deleted from the repo on
the VM (cause of the 7 `pregenerate_lessons` rows), None scores in sector_assignments,
`CIT-2026-1448` `get` errors.

Know first: a stale-cron row is first a `rerun-job` handler (deterministic). It reaches
this fixer only when the job has no crontab entry or crashes. Crontab changes ship in
`gcp_setup/` and only reach the VM on deploy, which is Jack's call: the PR says so.
The VM is not a git checkout; use `~/venv/bin/python3`.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
