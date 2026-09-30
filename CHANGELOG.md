# Changelog

All noteworthy changes to Apex Capital. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely — this is
a working repo, not a versioned library, so entries are dated rather than
tagged.

The load-bearing incidents behind these entries have their own postmortems
in [docs/POSTMORTEMS.md](docs/POSTMORTEMS.md) and in the memory index.

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
