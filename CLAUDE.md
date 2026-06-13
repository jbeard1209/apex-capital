# Apex Capital — Agent Guide (CLAUDE.md)

AI-driven **paper-trading** firm in Python. This file is the cold-start brief for any
Claude session. Read it before making changes.

- **Local dev:** `/Users/jackbeard/Claude investment strategy/` (most code under `institutional-tracker/`)
- **Cloud:** Railway — https://apex-capital-production-17b6.up.railway.app
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
seeded). Plain KFold on this time-ordered data leaks the future and inflates AUC — don't use it.

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
