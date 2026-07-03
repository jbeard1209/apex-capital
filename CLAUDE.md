# Apex Capital — Agent Guide (CLAUDE.md)

AI-driven **paper-trading** firm in Python. This file is the cold-start brief for any
Claude session. Read it before making changes.

- **Local dev:** `/Users/jackbeard/Claude investment strategy/` (most code under `institutional-tracker/`)
- **INFRASTRUCTURE.md is the single source of truth for every host/account — read it
  before assuming where anything runs. Code is NOT live until `bash gcp_setup/ship.sh`.**
- **Cloud (CRITICAL — two hosts run this firm, do not forget the VM):**
  - **GCP VM** `apex-capital-495523` (ssh alias `apex-capital`): ALWAYS-ON second brain with its
    own crontab (~63 jobs incl. main.py 6×/day) trading the SAME Alpaca paper account as the Mac.
    Its repo does NOT auto-pull — deploy via `gcp_setup/deploy.sh` or it runs stale code (it ran
    June-3 code for a month → phantom alert emails + divergent trading vs the Mac). Every audit
    MUST check it: `ssh apex-capital 'cd ~/institutional-tracker && git log -1 && uptime'`.
  - **Railway** — display-only dashboard viewer, no agents: https://apex-capital-production-17b6.up.railway.app
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

**Install the pre-push gate** (blocks pushes that fail tests/audit; `.git/hooks` isn't cloned):
```bash
cp scripts/git-hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
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
  are **cloud-synced via `sync_to_cloud.py`, not committed**. Most `data/*.json` (registries,
  reports, briefings) ARE tracked. Don't `git add -f` an ignored runtime file — push state via
  the cloud sync instead. If a code change needs a data migration, run it locally; it syncs separately.
- **Commit messages** end with the Co-Authored-By trailer for Claude.
- Commit/push only when asked. Branch off `main` if not already on a feature branch.

## Style

Match surrounding code. Explain the *why* to Jack, not just the diff. Be terse in chat
(see memory `feedback_terse`). Verify against current code before asserting — memories and
this file are point-in-time.
