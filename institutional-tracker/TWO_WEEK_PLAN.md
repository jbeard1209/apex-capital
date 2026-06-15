# Apex Capital — Two-Week Autonomous Plan (2026-06-16 → 2026-06-30)

Jack is in Europe ~2 weeks. This file is the **cold-start brief for each scheduled
agent run**. Read it + `CLAUDE.md` + the memory index every invocation, then do
**today's focus** plus the **daily standing protocol**. Commit + push everything.
Append a dated line to `STATUS_LOG.md` each run so progress is auditable.

## Execution model (how this runs while Jack is away)
- **Cron runs daily operations unattended** (canary, main pipeline, FI/FE pipelines,
  pod_health_monitor, strategy_council weekly, model_test_harness weekly, nightly
  auto-commit+push, wakeup_recovery for missed cycles). This keeps the firm alive.
- **A scheduled daily agent** (cloud routine) does the judgment work below. If a day
  is missed (machine off / network), the next run reads STATUS_LOG.md and resumes —
  the plan is date-anchored but **idempotent**: re-running a day is safe.
- **Hard safety rules (never violate):**
  1. Never deploy capital below the elite standard to hit a goal (cash is valid).
  2. Never report a metric you didn't measure; AUC/returns must be honest + sourced.
  3. Never push if `test_pipeline_smoke.py` or `wiring_audit.py` fails — fix first.
  4. Anything broken + unfixable → write a `chairman_action_required.json` item, don't paper over it.

## DAILY STANDING PROTOCOL (every run, before the focus task)
1. `date`; identify today's section below.
2. `git pull` (if remote ahead); `python3 pod_health_monitor.py` — any STALE/ZERO/ERROR_FLOOD it
   escalated, investigate + fix the root cause (not the symptom).
3. `python3 test_pipeline_smoke.py` + `python3 wiring_audit.py` — must be green.
4. Confirm cron actually ran: canary_state.json + each pod's *_pipeline_log/report fresh (<30h).
   If a pipeline didn't run, run it manually and note why.
5. Do **today's focus** (below). Measure honestly. Keep only what improves.
6. `git commit` + `git push` each validated change. Append one line to STATUS_LOG.md.

## THE FOCUS SCHEDULE
- **Mon (6/16, 6/23):** Models. Implement/continue the **inference-safe quantile-rank feature**
  (per feature_engineer.py FEATURE_COLS notes): per-feature reference quantiles persisted to
  data/feature_quantiles.json, applied identically in build_feature_matrix + get_inference_features;
  add *_pctile cols to FEATURE_COLS; rebuild; KEEP only if walk-forward AUC beats current; retrain;
  re-check the 0.57 gate. Verify strategy_council ran (Mon cron).
- **Tue (6/17, 6/24):** FE breadth. Re-run the broad elite scan (700+ funnel); refresh FE deployment
  within the elite standard; work the REVIEW queue (document why each REVIEW name is/ isn't a buy).
- **Wed (6/18, 6/25):** Data. Re-run `python3 predictive_model/historical_label_builder_fast.py
  --backfill-outcomes` (more tickers now cached → more labels); if materially larger, retrain + re-measure.
- **Thu (6/19, 6/26):** Standard hardening + FI. Hunt IV edge cases (negative equity, intangible-heavy
  book, missing fundamentals); verify FI picks are clean (no error flood, sane scores).
- **Fri (6/20, 6/27):** Risk + governance. Review IRM bulletins; confirm council goal grading +
  calibration; ensure ratified goals flowed back; check CRO/risk gates didn't silently block everything.
- **Sat/Sun:** Light. Standing protocol only + one model experiment if a clean idea exists. No risky changes on weekends.
- **Mon 6/30 (Jack returns ~end of window):** Write a FULL FORTNIGHT SUMMARY to STATUS_LOG.md:
  every pick deployed + outcome, model AUC trajectory, goals-vs-actuals, all commits, open issues.

## STANDING BACKLOG (pull from when a focus day is light)
- Build the inference-consistent rank features (the validated +0.01 ST direction) — Mon focus.
- Investigate whether restricting ML live picks to a liquid sub-universe legitimately clears 0.57.
- Expand FE_VALUE_UNIVERSE / 13F funnel breadth if the elite scan yields too few names.
- Once a goal track record exists, confirm seat accuracy-weighting + quarterly calibration are firing.

## STRATEGY COUNCIL — feature-complete as of 2026-06-15
4 seats (bull/bear/macro/chief), board-ratified goals (ratification loop closes the
status), calibration auto-widen + quarterly board report, per-pod output OKRs → HR,
seat accuracy-weighting (council_seat_record.json), cash-is-valid guardrail, extreme-
dissent → Chairman, and a human-readable digest (data/council_digest.md + Chairman
email). Weekly cron (Mon). Reads IRM risk + canary regime. See memory: strategy_council.

## DO NOT
- Do not lower the FE 25% MoS bar or the 0.57 ML gate.
- Do not commit scratch files (data/_*.py is gitignored; keep scratch in /tmp).
- Do not deploy ML capital while models are below the 0.57 gate.
