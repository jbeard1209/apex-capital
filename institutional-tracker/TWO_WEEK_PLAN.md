# Apex Capital — Two-Week Autonomous Plan (2026-06-16 → 2026-06-30)

Cold-start brief for each scheduled agent run. Working dir:
`/Users/jackbeard/Claude investment strategy/institutional-tracker`.
Every run: read this + `CLAUDE.md` + memory index, do the **Daily Standing Protocol**,
then **today's dated block**, then **Close-out**. All times ET. `cd` into the dir first.

---
## FORTNIGHT OBJECTIVES (graded in the 6/30 summary — pass/fail, measurable)
- **O1 — Ops uptime:** 14/14 days with `test_pipeline_smoke.py` = 51 pass, `wiring_audit.py`
  no VIOLATION, all pod reports fresh <30h, canary not RED-unhandled.
- **O2 — FE integrity:** FE holds ONLY picks that re-pass verification (debt_known +
  earnings_stable + DCF-corroborated + 25–50% MoS). Re-verify the held slate every Tue/Thu;
  any pick that fails → flag + remove next FE run. Broad elite scan run ≥4× over the fortnight.
- **O3 — Model progress (honest):** ST + LT walk-forward AUC measured ≥1×/week via
  `model_test_harness.py`; ≥3 documented feature experiments; goal = ST `cv_auc_mean` ≥ 0.57
  honestly. If not reached, the deliverable is a documented reason, never a faked number.
- **O4 — Data growth:** labeled `outcome_20d` rows grow toward +5,000 via `--backfill-outcomes`
  reruns (record the count each Wed).
- **O5 — Governance:** council goals set weekly + graded; `council_digest.md` produced; any
  ratified goals reflected as status="ratified".
- **O6 — Zero silent failures:** every breakage gets a `data/chairman_action_required.json`
  entry the same day. No quiet skips.

## HARD SAFETY RULES (never violate)
1. Cash is a valid position — never deploy below the elite standard to hit a goal.
2. Never report a metric you didn't just measure; AUC from `model_metrics`/`model_test_harness` only.
3. Never `git push` if smoke or wiring audit fail — fix first.
4. Don't lower the FE 25% MoS bar or the 0.57 ML gate. Don't deploy ML capital below the gate.
5. Scratch scripts go in `/tmp` (or `_*.py`, gitignored). Never commit them.

---
## DAILY STANDING PROTOCOL (run EVERY day, in order, before the focus block)
1. `date` — confirm today; find today's dated block below.
2. `git pull --rebase 2>/dev/null` — sync if remote ahead.
3. `python3 pod_health_monitor.py` — for any STALE / ZERO_PICKS / ERROR_FLOOD: open the
   pod's latest `logs/<pod>_*.log`, find the root cause, fix it (not the symptom). Re-run the pod.
4. `python3 test_pipeline_smoke.py` → must end "OK" (51 tests). `python3 wiring_audit.py` →
   must show 3 "OK" lines, no VIOLATION. If red: STOP, fix, do not proceed to push.
5. Freshness check — these must be < 30h old (else run the named script):
   `data/canary_state.json` (canary_agent.py) · `data/fi_pipeline_report.json` (fi_pipeline.py) ·
   `data/fe_pipeline_log.json` (fe_pipeline.py) · `data/model_meta.json` (predictive_model/ml_model.py).
6. Do today's focus block.
7. **Close-out:** `git add -A` (excl. scratch) → `git commit` (clear message) → `git push`;
   pre-push hook must pass. Append ONE line to `STATUS_LOG.md`:
   `YYYY-MM-DD: <what changed> | ST_auc=<x> | FE_picks=<n> | tests=51 OK | commits=<shas>`.

---
## DAY-BY-DAY

### MON 6/16 & MON 6/23 — Models: inference-safe quantile-rank feature
- **Objective:** capture the validated +0.01 ST lift in a *deployable* way; try to move ST AUC up.
- **Steps:**
  1. In `predictive_model/feature_engineer.py`, compute reference quantiles (deciles) once from
     the training matrix for `price_momentum_20d, conviction_score, n_institutions, avg_conviction_3q`;
     persist to `data/feature_quantiles.json`.
  2. Add a `_pctile_transform(col, ref)` and produce `<col>_pctile` columns in BOTH
     `build_feature_matrix` AND `get_inference_features` (identical code path — verify by diffing logic).
  3. Add the 4 `*_pctile` columns to `FEATURE_COLS`.
  4. Rebuild + measure: `python3 model_test_harness.py --quiet` then read `data/model_test_report.json`.
- **Success criteria:** shortterm `walk_forward_auc_mean` > 0.5506 AND > current deployed `cv_auc_mean`.
- **Decision:** if it beats both → `python3 predictive_model/ml_model.py --force`; re-check gate; commit
  "feat(model): inference-safe quantile-rank features (ST AUC x→y)". If NOT → revert the FEATURE_COLS
  add, document the result in the FEATURE_COLS notes, commit the doc only.
- **Deliverable:** a kept-or-rejected decision with the exact before/after AUC in STATUS_LOG.md.

### TUE 6/17 & TUE 6/24 — FE breadth + slate re-verification (O2)
- **Steps:**
  1. `echo "[]" > data/iv_scan_cache.json` then run the broad scan over the ~700 funnel (FE_VALUE_UNIVERSE
     ∪ 13F top-held). Record BUY count + names.
  2. Re-verify the CURRENTLY HELD FE slate (read `data/fe_pipeline_log.json` sized_positions): re-run
     `calculate_iv` on each; any that now fails (verdict≠BUY, or debt/earnings flags) → note it.
  3. `python3 fe_pipeline.py` to refresh deployment; confirm `approved_count`>0 and only verified names hold.
  4. For the top 5 REVIEW names, write a one-line reason each (why not a buy) into STATUS_LOG.md.
- **Success:** held slate = 100% re-verified BUYs; broad scan logged; REVIEW queue triaged.
- **Decision:** any held pick failing re-verification → it must NOT be in the new slate. Never widen by lowering the bar.

### WED 6/18 & WED 6/25 — Data growth (O4)
- **Steps:**
  1. Record current: `sqlite3 data/historical_13f.db "SELECT count(*) FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"`.
  2. `python3 predictive_model/historical_label_builder_fast.py --backfill-outcomes` (downloads more
     tickers now cached; UPDATEs outcomes). Then `--backfill-lt` for the 252d label.
  3. Record new count + delta.
- **Success:** labeled count increases (log the delta).
- **Decision:** if delta ≥ 2,000 → `python3 predictive_model/ml_model.py --force` + `model_test_harness.py`;
  log new honest AUC. Else just log the delta.

### THU 6/19 & THU 6/26 — Standard hardening + FI hygiene
- **Steps:**
  1. Pick 8 names spanning sectors (incl. a bank, a REIT, a heavy-debt name, a negative-FCF name);
     run `calculate_iv` on each; look for any IV that's implausible (>5× price should already be rejected;
     check for negative-equity, intangible-heavy book, missing-fundamental edge cases).
  2. If a NEW false-positive class is found → add a guard in `fe_intrinsic_value.py` + a smoke test; commit.
  3. FI hygiene: `python3 fi_pipeline.py`; confirm 0 ERROR lines and qualified picks > 0.
- **Success:** no new FE false-positive class OR a new guard+test added; FI clean.

### FRI 6/20 & FRI 6/27 — Risk + governance
- **Steps:**
  1. `python3 strategy_council.py` → confirm digest written (`data/council_digest.md`), goals set,
     any matured goals graded, seat-weights logged.
  2. Read latest IRM output (`data/irm_daily_check.json`) + `data/risk_bulletin.json`; confirm the
     bear seat's risk level matches. If IRM HIGH/CRITICAL, confirm a risk escalation was filed.
  3. Verify CRO/risk gates didn't silently block ALL buys (check a pod actually traded this week).
- **Success:** council ran, digest emailed, goals graded; no silent full-block.

### SAT/SUN — Light maintenance only
- Daily standing protocol only. At most ONE clean model experiment if an obvious idea exists.
- NO risky changes, NO new deployments on weekends. Commit only if green.

### MON 6/30 — FULL FORTNIGHT SUMMARY (final deliverable)
Append a long entry to `STATUS_LOG.md` covering: O1–O6 pass/fail; every FE pick deployed +
its outcome; ST/LT AUC trajectory (start vs end); labeled-data growth; council goals-vs-actuals +
calibration; all commit SHAs; and the top 3 open issues for Jack to decide on.

---
## STRATEGY COUNCIL — feature-complete (2026-06-15)
4 seats (bull/bear/macro/chief); board-ratified goals w/ ratification loop; calibration auto-widen +
quarterly board report; per-pod output OKRs → HR; seat accuracy-weighting (`council_seat_record.json`);
cash-is-valid guardrail; extreme-dissent → Chairman; human digest (`council_digest.md` + Chairman email).
Weekly cron (Mon). Reads IRM risk + canary regime. Memory: `strategy_council`.

## STANDING BACKLOG (pull from on a light day)
- Test whether a liquid ML sub-universe legitimately clears the 0.57 gate.
- Widen FE funnel if the elite scan yields < 3 BUYs.
- Weight-by-accuracy + quarterly calibration confirmation once a goal track record exists.
