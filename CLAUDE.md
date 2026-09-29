# Apex Capital — Agent Guide (CLAUDE.md)

AI-driven **paper-trading** firm in Python. This file is the cold-start brief for any
Claude session. Read it before making changes.

- **Local dev:** `/Users/jackbeard/Claude investment strategy/` (most code under `institutional-tracker/`)
- **[docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md) is the single source of truth for every
  host/account — read it before assuming where anything runs. Code is NOT live until you
  deploy — and both scripts live under `institutional-tracker/`, not the repo root, so run
  them from there: `cd institutional-tracker && bash gcp_setup/deploy.sh`. (Written as
  `bash gcp_setup/ship.sh` here since the scripts were committed; from the "Local dev"
  directory two lines above, that path does not exist and the command fails instantly —
  the same shape as the VM freshness check below, which was documented for a month and
  could never have run.) The doc moved from the repo root to docs/ on 2026-09-29;
  `coherence_laws.py::infrastructure_doc()` and `deploy.sh` both look in docs/ first and
  fall back to the old repo-root location, so nothing broke — but if you find a comment
  or a script still hardcoded to the root path, fix it.**
- **Cloud (CRITICAL — two hosts run this firm, do not forget the VM):**
  - **GCP VM** `apex-capital-495523` (ssh alias `apex-capital`): ALWAYS-ON second brain with its
    own crontab (~63 jobs incl. main.py 6×/day) trading the SAME Alpaca paper account as the Mac.
    Its repo does NOT auto-pull — deploy via `institutional-tracker/gcp_setup/deploy.sh` or it runs stale code (it ran
    June-3 code for a month → phantom alert emails + divergent trading vs the Mac).
    **The VM is NOT a git checkout** — `deploy.sh` rsyncs with `--exclude=.git/`, so any
    `git log` there fails with "not a git repository". (CLAUDE.md told every session to run
    exactly that until 2026-07-30; the freshness check had been silently erroring out.)
    Its Python is `~/venv/bin/python3`, NOT bare `python3` — bare python3 has no `dotenv`
    and every test will ImportError. Every audit MUST check it:
    ```
    ssh apex-capital 'cat ~/institutional-tracker/deploy_stamp.json; uptime'
    ```
    `deploy_stamp.json` is written by `deploy.sh` only, so a hand-`scp` leaves it stale
    (under-reporting drift) — it is a hint, **not** proof. The ground truth is a checksum
    diff of the changed files:
    ```
    ssh apex-capital 'cd ~/institutional-tracker && md5sum main.py alpaca_trader.py'
    ```
  - **Railway** — DECOMMISSIONED 2026-07-10 (paid tier declined). Local dashboard: `python3 dashboard_server.py` → port 5050.
- **GitHub:** https://github.com/jbeard1209/apex-capital
- **Owner:** Jack Beard, Co-Chairman (learning finance — explain reasoning, don't just emit code)

## How to run / test

```bash
cd institutional-tracker
python3 main.py                     # full daily pipeline
python3 test_pipeline_smoke.py      # smoke + wiring-contract tests (run before committing)
python3 wiring_audit.py             # sweep for dead integrations (see below)
python3 logic_team.py               # coherence checks (incl. WiringContractLogicAgent self-check)
python3 model_test_harness.py       # reproducible, no-leakage model AUC (weekly cron)
python3 <agent>.py --report         # most agents support --report
```

**Install the pre-push gate** (blocks pushes that fail tests/audit; `.git/hooks` isn't cloned).
The gate lives at **repo root**, not under `institutional-tracker/` — run this from the root,
which is what the `git rev-parse` prefix guarantees regardless of where you are:
```bash
cd "$(git rev-parse --show-toplevel)" && cp scripts/git-hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```
Until 2026-08-23 there were **two tracked copies** of this hook and the command above sat
under `cd institutional-tracker`, so following it resolved to the older, fail-open stub —
the same "install command downgrades the gate" failure the hook's own header postmortems.
The duplicate is deleted; `test_pipeline_smoke.py::TestThePrePushGateIsTheOneWeShip` pins
that there is exactly one, and that this file names its real path.

**The gate runs the suite twice** (local zone + `TZ=UTC`, ~8 min) — longer than an idle SSH
connection to GitHub survives, so a push that PASSES the gate then dies with
`Connection to github.com closed by remote host` and exit 141. `core.sshCommand` is set
repo-locally to `ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30`; that is not
cloned, so re-run it on a fresh checkout:
```bash
git config core.sshCommand "ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30"
```

## Model metrics — single source of truth
Never hardcode or guess an AUC. `model_metrics.get_model_metrics()` reads the real values
from `data/model_meta.json` (written by `predictive_model/ml_model.py`); it returns
`trained=False, auc=None` when no model exists — callers must show "N/A", never a number.
The honest, reproducible evaluation is `model_test_harness.py` (walk-forward / TimeSeriesSplit,
seeded, with a 20-day **embargo gap** so the label window can't leak across folds). Plain KFold on
this time-ordered data leaks the future and inflates AUC — don't use it. The harness reports the
model AUC **against a naive momentum baseline** (it must beat that to add value), a **Brier score**
(calibration), and a **sample-size confidence** flag.

### Adding model features — do it the disciplined way
`feature_engineer.py::FEATURE_COLS` carries empirical notes: commodity momentum and bridge
features were tried and **regressed AUC** (0.6057→0.5842) — they're intentionally excluded. So:
1. Never add a feature straight into `FEATURE_COLS` and assume it helps.
2. Add the column, then run `model_test_harness.py` and compare `walk_forward_auc_mean` +
   `edge_over_baseline` against the prior report (`data/model_test_report.json`).
3. Keep it only if it improves the embargoed walk-forward AUC. Record the result in a code comment
   next to the feature (matching the existing notes). Watch for `SUSPICIOUS_HIGH` (leakage).
4. More labeled data beats more features — small samples make AUC swing run-to-run. Backfill
   history (EDGAR scraper / label builder) before trusting a new number.

## Architecture (subsystems)

Daily pipeline lives in `main.py`. Key agents:
- **Canary** (`canary_agent.py`) — system health GREEN/ORANGE/RED
- **Chief of Staff** (`chief_of_staff_agent.py`) — agent factory + daily brief
- **Research Team** (`research_team.py`) — loads active+reassigned agents, produces picks
- **Manager** (`manager_agent.py`) — reviews picks, executes via Alpaca paper trading
- **CRO** (in `main.py`) — risk bulletins; HIGH/CRITICAL blocks all buys
- **IRM** (`irm_review_agent.py`) — independent risk, emails Chairman
- **HR** (`hr_agent.py`) — agent evaluation, PIP lifecycle
- **CIT** (`cit_director.py` + `cit_*`) — finds code bugs / improvements, queues board proposals
- **Logic Team** (`logic_team.py` + `logic_team_implementation_agent.py`) — finds *logical/systemic*
  incoherence (distinct from CIT's code bugs) and acts on it
- **Coherence laws** (`coherence_laws.py`) — each law compares TWO independently-sourced
  measurements of the same quantity and alarms when the arithmetic doesn't close. A HOLDS
  must carry `n_examined`; a pass on zero items is not a pass.
- **Internal Audit** (`internal_audit_agent.py`, doc `CONTROLS.md`) — the **third line**: it
  audits the *controls*, not the firm. CIT and the red team are `critique(artifact)`, so they
  are blind to anything never in their input — a relation between two correct artifacts, an
  absence, or a control's own blindness. This runs each law against a poison pill built to
  make it fail (HOLDS ⇒ BLIND ⇒ every HOLDS it reported was worthless), demands denominators,
  and keeps a census of failure classes → detectors. Detector refs are **AST-resolved, never
  grepped** — every control is named in some postmortem comment. Findings escalate: BLIND to
  the Chairman queue, the rest to CIT as proposals. **A new law without a poison pill fails
  the suite.**
- **Pods:** pod1_rd (R&D), pod2_ml (ML), pod3_macro (ETF/Macro); FI & FE pods exist as agent files
- **ML** (`predictive_model/`, `ml_live_test_runner.py`) — ST/LT models + live validation
- **Dashboard** (`dashboard_server.py`) — `apex_live_dashboard.html` on port 5050

## ⚠️ Producer → queue → consumer contracts (read this)

The system's main bug class is **silent identifier mismatches**: a producer emits a rule
name / pod name / category that no consumer handles, and `.get()`/try-except swallow it, so
output is produced but never acted on. Three such bugs were fixed June 2026. Before adding
any routing/dispatch, verify the consumer side exists.

Known live contracts:
- **Logic Team findings:** `logic_team.py` emits `rule=` names → `logic_team_implementation_agent.py`
  `RESOLUTION_HANDLERS` (via `_normalize_rule()`) must cover every one. Guarded by
  `test_pipeline_smoke.py::TestWiringContracts`.
- **Proposal routing:** `cit_director.submit_to_pod_queue()` → only `rd`/`ml` pod boards
  (`rd_pod_board.py`, `ml_pod_board.py`) read their `*_pod_pending.json`. `fi`/`fe`/`etf` have
  **no consumer** — `_effective_pod()` redirects those to the main board queue
  (`cit_pending_proposals.json`, read by `board_meeting.py` + `deputy_board.py`).
- **Proposal categories:** must be in one of `deputy_board.py`'s sets (`DEPUTY_CATEGORIES`,
  `FULL_BOARD_ONLY_CATEGORIES`, `AUTO_IMPLEMENT_CATEGORIES`) or they skip the deputy path.
- **Detection tell:** an empty `*_log.json` / `*_pending.json` / `*_queue.json` often means an
  integration never fires. `wiring_audit.py` sweeps for these.

When you add a producer of routed items, add a `TestWiringContracts` case for it.

## Data & git conventions

- **`data/` is selectively gitignored, on purpose.** Live trade state, PII, agent evaluations,
  and proposal *queues* (e.g. `cit_pending_proposals.json`, `*_pod_pending.json`, `state.json`)
  are **not committed**. Most `data/*.json` (registries, reports, briefings) ARE tracked.
  Don't `git add -f` an ignored runtime file.
- **There is NO state sync between hosts.** This section said runtime state was
  "cloud-synced via `sync_to_cloud.py`" until 2026-08-07; that script POSTed to Railway, which
  was decommissioned 2026-07-10, and it is on neither host's scheduler. **The Mac's and the VM's
  gitignored `data/` have been fully independent for a month.** Do not reason as if a value
  written on one host is visible on the other — that assumption is what let two hosts trade the
  same account while each read its own `state.json` (see `single_writer.py`), and what made
  `signoff_ledger`'s "firm-wide" deploy budget per-host. The only medium both hosts share is the
  broker. If a code change needs a data migration, it must be run **on each host separately**.
- **Commit messages** end with the Co-Authored-By trailer for Claude.
- Commit/push only when asked. Branch off `main` if not already on a feature branch.

## Style

Match surrounding code. Explain the *why* to Jack, not just the diff. Be terse in chat
(see memory `feedback_terse`). Verify against current code before asserting — memories and
this file are point-in-time.
