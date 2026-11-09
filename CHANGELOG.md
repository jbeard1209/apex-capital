# Changelog

All noteworthy changes to Apex Capital. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely — this is
a working repo, not a versioned library, so entries are dated rather than
tagged.

The load-bearing incidents behind these entries have their own postmortems
in [docs/POSTMORTEMS.md](docs/POSTMORTEMS.md) and in the memory index.

---

## 2026-11-09

### Fixed
- Board CIT vote crashed every meeting since ~2026-08-18 on label confidence
  (`"HIGH" >= 75`); nothing was saved. Shared parser with deputy_board, one bad
  row can no longer abort the vote, tracebacks are logged, and the vote writes
  `data/board_vote_status.json`. Backlog drains 60 rows per meeting, most severe first.
- Approved SLA escalations no longer loop back to the Chairman's queue; the
  board's verdict is stamped on the Chairman queue row (`board_verdict`).
- `etf_pod_board.py` crashed printing fractional order quantities.

### Changed
- Board-approved changes no longer need the Chairman's sign-off unless crucial
  (carve-out, CRITICAL, direction change, money/risk/irreversible, or flagged
  `discuss_with_chairman`): `chairman_decision_categorizer.needs_signoff`.
  Routine approvals are filed `approved_by_board` (owner CIT), off his inbox.
  `reroute_board_approvals.py` applies the rule to rows already filed.

### Added
- `job_failure_monitor.py` (VM, hourly :40): repeated crashes and swallowed
  errors in any job's log are emailed to the Chairman and filed to CIT.
  See docs/POSTMORTEMS.md, "The board voted on nothing for 12 weeks".

---

## 2026-10-01

### Added
- `_WIRING_SNAPSHOT` module-level sentinel + `wiring_audit`
  `check_snapshot_declarations_match_writes` typo guard. Writers that
  rewrite a complete latest-state picture every run declare the
  snapshot filename; the destructive-reader-cadence sweep then
  downgrades their pairs to the ordinary lateness bucket. A declaration
  naming a file the module does not write fails the audit — this is
  the opt-out layer, and silencing a real finding by typo is the trap
  that forced the guard to exist.
- Opt-out applied to the seven writers whose semantics it fits:
  `etf_research_agent`, `etf_pod_board`, `ml_rd_bridge_agent`,
  `freshness_sentinel`, `reconciliation_check`, `rd_system_performance`,
  `operations_director`. Dead constant `fi_executor.CIT_HR_MEMO`
  deleted (it read a file written by `cit_director`).
- `TestSnapshotOptOut` (three cases) pins the mechanism.

### Fixed
- README pod file names (`pod1_rd_*.py` etc. did not exist) and the
  test count (`6000+` → `~1,500`); minor drift that misdescribed the
  repo layout.
- `apex_system_cit_agent._CRON_LOG_CHECKS` had drifted ~2 months from
  the actual VM crontab: 8 of 11 entries named scripts no longer
  scheduled anywhere (`ml_drift_monitor`, `ml_data_watchdog`,
  `ml_alpha_decay_monitor`, `ml_research_team_leader`, `fe_cit_agent`,
  `fe_watchlist_monitor`, `portfolio_risk_dashboard`, `edgar_scrape`)
  and 2 scheduled entries pointed at the wrong log path (`fe_pipeline`
  wrote `logs/fe_<date>.log`, `ml_improvement_loop` wrote
  `logs/ml_improve_<date>.log`). The result was a daily flood of
  false-positive MEDIUM Chairman items — 40+ of the
  `apex-sys-cit-*-cron_job_stale_on_we` series in the resolved
  archive. Rewrote the list to only track currently-scheduled jobs,
  added per-entry `max_age_hours` so weekday-only jobs (`* * * * 1-5`)
  no longer false-fire every Monday when Friday's log is 70h old,
  and autoresolved the open ML-drift finding
  (`apex-sys-cit-20260930-1300`) that opened this session. On the VM
  the detector now reports `All cron job logs are current` with real
  numbers (CIT pipeline integrity 0.2h, CIT director 0.9h, FE pipeline
  7.8h, ML feature drift monitor 11.0h, ML improvement loop 16.4h,
  EDGAR transcript agent 23.7h).

---

## 2026-09-30

### Added
- `kalshi_daily_status.py` — a single Chairman-facing brief that reads
  the five Kalshi output files (scanner log, calibration report, paper
  mirror report, execute queue, board vote) and writes one summary to
  `data/kalshi_daily_status.json`. Flips from BLOCKED to GREEN append a
  row to `data/chairman_action_required.json`. Scheduled at 12:20 UTC
  daily on the VM crontab, after every upstream job.
- Repository docs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [docs/QUANTITATIVE_METHODS.md](docs/QUANTITATIVE_METHODS.md),
  [docs/POSTMORTEMS.md](docs/POSTMORTEMS.md),
  [docs/kalshi_build_plan.md](docs/kalshi_build_plan.md).
- Governance surface: [CONTRIBUTING.md](CONTRIBUTING.md),
  [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
  PR / issue templates, Dependabot, CodeQL workflow, `.editorconfig`,
  `.github/CODEOWNERS`.

### Changed
- README now has a **Deep dives** section pointing at the new docs, and
  a CodeQL badge next to the tests badge.

---

## 2026-09-29

### Added
- Four-part Chairman-queue governance change (categorizer + 15-day
  fallback with carve-outs, session-start inbox, auto-diagnosis after
  rerun failure, poison-pill tests for each new coherence law).

### Changed
- `docs/INFRASTRUCTURE.md` moved from the repo root to `docs/`.
  `coherence_laws.py::infrastructure_doc()` and `deploy.sh` look in
  `docs/` first and fall back to the old root location.

### Security
- Git history scrubbed with `git-filter-repo` for two committed
  passwords (`apexcapital2026`, `viewonly123`); verified clean with
  `git log -S`.

---

## 2026-09-08

### Added
- Slow-gate report freshness watcher + VM cron entry.

---

For older changes, `git log --oneline` on `main` is the authority; the
commit messages carry the full incident context.
