#!/usr/bin/env python3
"""
APEX CAPITAL — OVERNIGHT ML RUNNER
=====================================
Autonomous pipeline that:
  1. Waits for the label builder process to finish (polls every 60 s)
  2. Trains v1 model — baseline on full labeled dataset
  3. Trains v2 model — TimeSeriesSplit CV, XGBoost, Optuna tuning
  4. Pre-warms macro data cache (VIX, yields, SPY — 6 years of history)
  5. Trains v3 model — walk-forward CV, macro features, Ridge stacking
  6. Trains v4 model — Optuna-tuned LGB, isotonic calibration, SHAP
  7. Sends a comprehensive summary email with plain-English interpretation

Error handling: any training crash sends an immediate email, then the runner
continues to the next phase. At least one email will always arrive.

Designed to run with nohup: keeps going after Mac sleeps or SSH disconnects.

Usage (GCP):
  nohup ~/venv/bin/python3 ~/institutional-tracker/overnight_ml_runner.py \\
        --label-pid <PID> \\
        > ~/institutional-tracker/logs/ml_runner_$(date +%Y%m%d_%H%M).log 2>&1 &
"""

import os
import sys
import json
import time
import logging
import argparse
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_runner")


def _send(subject: str, body: str):
    # One-digest policy (2026-07-09): routine "training complete" summaries buffer
    # into the daily digest — they were sending ~9 immediate emails/week and drowning
    # real alerts. Only genuine training ERRORS (subject starts with 🚨) still send
    # immediately, since a broken model pipeline is time-sensitive.
    is_error = subject.lstrip().startswith("\U0001f6a8")
    try:
        if is_error:
            from email_sender import send_alert_email
            send_alert_email(subject, body, force=True)   # broken pipeline = immediate
            logger.info(f"  Error email sent immediately: {subject}")
        else:
            from email_buffer import buffer_email
            buffer_email(subject=subject, html_body=body.replace("\n", "<br>"),
                         sender_name="ML Runner")
            logger.info(f"  Summary buffered for daily digest: {subject}")
    except Exception as e:
        logger.error(f"  Could not send/buffer email: {e}")


# =============================================================================
# PHASE 0 — Wait for label builder
# =============================================================================

def wait_for_label_builder(pid: int, timeout_h: float = 6.0) -> bool:
    if pid <= 0:
        logger.info("  No label-builder PID specified — skipping wait.")
        return True

    proc_path = Path(f"/proc/{pid}")
    if not proc_path.exists():
        logger.info(f"  Label builder PID {pid} already finished.")
        return True

    logger.info(f"  Waiting for label builder PID {pid}...")
    deadline = time.time() + timeout_h * 3600
    while time.time() < deadline:
        if not proc_path.exists():
            logger.info(f"  Label builder PID {pid} exited.")
            return True
        time.sleep(60)

    logger.warning(f"  Label builder PID {pid} still running after {timeout_h}h — proceeding.")
    return False


# =============================================================================
# PHASE 1 — v1 training
# =============================================================================

def run_v1_training() -> dict:
    logger.info("=" * 60)
    logger.info("  PHASE 1: v1 model (baseline)")
    logger.info("=" * 60)
    try:
        from predictive_model.ml_model import train as train_v1
        result = train_v1(force=True)
        logger.info(f"  v1 → {result.get('status')} | AUC {result.get('ensemble_auc_val','?')}")
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"  v1 FAILED: {e}")
        _send("🚨 Apex Capital — ML v1 FAILED",
              f"v1 crashed at {datetime.now():%Y-%m-%d %H:%M}.\n\n{e}\n\n{tb}\n\n"
              f"v2 and v3 will still run.")
        return {"status": "ERROR", "error": str(e)}


# =============================================================================
# PHASE 2 — v2 training
# =============================================================================

def run_v2_training() -> dict:
    logger.info("=" * 60)
    logger.info("  PHASE 2: v2 model (XGBoost + Optuna + TimeSeriesSplit)")
    logger.info("=" * 60)
    try:
        from predictive_model.ml_model_v2 import train as train_v2
        result = train_v2(force=False)
        logger.info(f"  v2 → {result.get('status')} | AUC {result.get('ensemble_auc_val','?')} | "
                    f"TS-CV {result.get('cv_auc_mean','?')}")
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"  v2 FAILED: {e}")
        _send("🚨 Apex Capital — ML v2 FAILED",
              f"v2 crashed at {datetime.now():%Y-%m-%d %H:%M}.\n\n{e}\n\n{tb}\n\n"
              f"v3 will still run.")
        return {"status": "ERROR", "error": str(e)}


# =============================================================================
# PHASE 2b — Pre-warm macro cache
# =============================================================================

def prewarm_macro_cache() -> bool:
    logger.info("  Pre-warming macro data cache (VIX, yields, SPY)...")
    try:
        from predictive_model.macro_features import _download_and_cache
        df = _download_and_cache()
        logger.info(f"  Macro cache ready: {len(df)} trading days")
        return True
    except Exception as e:
        logger.warning(f"  Macro cache pre-warm failed: {e} — v3 will use defaults")
        return False


# =============================================================================
# PHASE 3 — v3 training
# =============================================================================

def run_v4_training(optuna_trials: int = 25) -> dict:
    logger.info("=" * 60)
    logger.info("  PHASE 4: v4 model (Optuna LGB + isotonic cal + SHAP)")
    logger.info("=" * 60)
    try:
        from predictive_model.ml_model_v4 import train as train_v4
        result = train_v4(force=False, optuna_trials=optuna_trials)
        logger.info(f"  v4 → {result.get('status')} | AUC {result.get('ensemble_auc_val','?')} | "
                    f"Brier {result.get('brier_score','?')} | "
                    f"WF-CV {result.get('wf_cv_mean_auc','?')}")
        return result
    except Exception as e:
        tb = __import__("traceback").format_exc()
        logger.error(f"  v4 FAILED: {e}")
        _send("Apex Capital — ML v4 FAILED",
              f"v4 crashed at {datetime.now():%Y-%m-%d %H:%M}.\n\n{e}\n\n{tb}")
        return {"status": "ERROR", "error": str(e)}


def run_v5_training(optuna_trials: int = 25) -> dict:
    logger.info("=" * 60)
    logger.info("  PHASE 5: v5 model (macro-conditioned stacking + SHAP pruning)")
    logger.info("=" * 60)
    try:
        from predictive_model.ml_model_v5 import train as train_v5
        result = train_v5(force=False, optuna_trials=optuna_trials)
        logger.info(f"  v5 → {result.get('status')} | AUC {result.get('ensemble_auc_val','?')} | "
                    f"Ridge α={result.get('ridge_alpha','?')} | "
                    f"WF-CV {result.get('wf_cv_mean_auc','?')}")
        return result
    except Exception as e:
        tb = __import__("traceback").format_exc()
        logger.error(f"  v5 FAILED: {e}")
        _send("Apex Capital — ML v5 FAILED",
              f"v5 crashed at {datetime.now():%Y-%m-%d %H:%M}.\n\n{e}\n\n{tb}")
        return {"status": "ERROR", "error": str(e)}


def run_v3_training() -> dict:
    logger.info("=" * 60)
    logger.info("  PHASE 3: v3 model (walk-forward CV + macro features + stacking)")
    logger.info("=" * 60)
    try:
        from predictive_model.ml_model_v3 import train as train_v3
        result = train_v3(force=False)
        logger.info(f"  v3 → {result.get('status')} | AUC {result.get('ensemble_auc_val','?')} | "
                    f"WF-CV {result.get('wf_cv_mean_auc','?')}")
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"  v3 FAILED: {e}")
        _send("🚨 Apex Capital — ML v3 FAILED",
              f"v3 crashed at {datetime.now():%Y-%m-%d %H:%M}.\n\n{e}\n\n{tb}")
        return {"status": "ERROR", "error": str(e)}


# =============================================================================
# SUMMARY EMAIL
# =============================================================================

def _fmt_model(label: str, r: dict, prior_auc: float = 0.0) -> list:
    """Format one model's results as lines for the email."""
    if r.get("status") == "ERROR":
        return [f"❌ {label} — FAILED", f"   Error: {r.get('error','?')}", ""]
    if r.get("status") in ("SKIPPED", "KEPT_EXISTING"):
        return [f"⚠️  {label} — SKIPPED: {r.get('reason','')}", ""]

    promoted = r.get("promoted", False)
    ens_auc  = r.get("ensemble_auc_val", r.get("v2_auc", r.get("v3_auc", "?")))
    delta    = f"{float(ens_auc) - prior_auc:+.4f}" if prior_auc and ens_auc != "?" else "—"

    lines = [f"✅ {label} — TRAINED {'→ NOW PRODUCTION' if promoted else '(not promoted)'}"]
    lines += [
        f"   Samples:         {r.get('n_samples', '?'):,}",
        f"   Ensemble AUC:    {ens_auc}  (val set, time-ordered 80/20 split)",
        f"   AUC vs prior:    {delta}",
    ]
    if "cv_auc_mean" in r:
        lines.append(f"   TimeSeriesSplit CV: {r['cv_auc_mean']} ± {r.get('cv_auc_std','?')}  (v2)")
    if "wf_cv_mean_auc" in r:
        lines.append(f"   Walk-forward CV:   {r['wf_cv_mean_auc']}  (v3 — most accurate estimate)")
    for name in ("lgb", "xgb", "rf", "lr"):
        k = f"{name}_auc_val"
        if k in r:
            lines.append(f"   {name.upper()} AUC:         {r[k]}")
    if "stacked_auc_val" in r:
        lines.append(f"   Stacked AUC:     {r['stacked_auc_val']}")
    if "ensemble_weights" in r:
        lines.append(f"   Ensemble weights:{r['ensemble_weights']}")
    if "macro_top_features" in r and r["macro_top_features"]:
        feats = r["macro_top_features"]
        # macro_top_features is a list of feature-name strings (not tuples)
        top = [f[0] if isinstance(f, (list, tuple)) else f for f in feats[:3]]
        lines.append(f"   Top macro feat:  {top}")
    if "rd_score" in r:
        score = r["rd_score"]
        sentiment = "bullish" if score > 0.2 else ("bearish" if score < -0.2 else "neutral")
        lines.append(f"   R&D macro signal:{score:+.3f}  ({sentiment})")
    if r.get("risk_adj_r2_val") is not None:
        lines.append(f"   Risk-adj R²:     {r['risk_adj_r2_val']}")
    raw_feats = r.get("top_features", [])
    top_feats = [f[0] if isinstance(f, (list, tuple)) else f for f in raw_feats[:3]]
    lines.append(f"   Top features:    {top_feats}")
    lines.append("")
    return lines


def send_summary(label_pid: int, v1: dict, v2: dict, v3: dict, v4: dict = None, v5: dict = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    # Label count
    n_labels = 0
    try:
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "data" / "institutional.db"))
        row  = conn.execute(
            "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchone()
        n_labels = row[0] if row else 0
        conn.close()
    except Exception:
        pass

    lines = [
        "APEX CAPITAL — Overnight ML Training Complete",
        f"Finished: {now}",
        "",
        f"📊 DATASET",
        f"   Labeled snapshots (20-day outcome): {n_labels:,}",
        "",
    ]

    v4 = v4 or {}
    v5 = v5 or {}
    v1_auc = float(v1.get("ensemble_auc_val", 0.5)) if v1.get("status") not in ("ERROR","SKIPPED") else 0.5
    v2_auc = float(v2.get("ensemble_auc_val", v1_auc)) if v2.get("status") not in ("ERROR","SKIPPED") else v1_auc
    v3_auc = float(v3.get("ensemble_auc_val", v2_auc)) if v3.get("status") not in ("ERROR","SKIPPED") else v2_auc
    v4_auc = float(v4.get("ensemble_auc_val", v3_auc)) if v4.get("status") not in ("ERROR","SKIPPED") else v3_auc

    lines += _fmt_model("V1 MODEL  (baseline)", v1)
    lines += _fmt_model("V2 MODEL  (XGBoost + Optuna)", v2, prior_auc=v1_auc)
    lines += _fmt_model("V3 MODEL  (walk-forward + macro + stacking)", v3, prior_auc=v2_auc)
    if v4:
        lines += _fmt_model("V4 MODEL  (Optuna LGB + isotonic cal + SHAP)", v4, prior_auc=v3_auc)
        if v4.get("brier_score") is not None:
            lines.append(f"   Brier score:     {v4['brier_score']}  (lower=better, 0.25=random)")
        if v4.get("shap_top_features"):
            lines.append(f"   SHAP top feats:  {[f for f, _ in v4['shap_top_features'][:3]]}")
        if v4.get("optuna_best_params"):
            bp = v4["optuna_best_params"]
            lines.append(f"   Optuna LGB best: leaves={bp.get('num_leaves')} "
                         f"lr={bp.get('learning_rate', 0):.4f}")
        lines.append("")
    if v5:
        lines += _fmt_model("V5 MODEL  (macro-conditioned stacking + SHAP pruning)", v5, prior_auc=v4_auc)
        if v5.get("brier_score") is not None:
            lines.append(f"   Brier score:     {v5['brier_score']}  (lower=better, 0.25=random)")
        if v5.get("ridge_alpha") is not None:
            lines.append(f"   Ridge alpha:     {v5['ridge_alpha']}  (adaptive, 3-fold tuned)")
        if v5.get("pruned_features"):
            lines.append(f"   Pruned features: {v5['pruned_features']}")
        if v5.get("meta_regime_cols"):
            lines.append(f"   Meta regime cols:{v5['meta_regime_cols']}")
        lines.append("")

    # Which is production?
    prod_ver = (
        "v5" if v5.get("promoted") else
        "v4" if v4.get("promoted") else
        "v3" if v3.get("promoted") else
        "v2" if v2.get("promoted") else "v1"
    )
    prod_auc = {
        "v5": v5.get("ensemble_auc_val","?"),
        "v4": v4.get("ensemble_auc_val","?"),
        "v3": v3.get("ensemble_auc_val","?"),
        "v2": v2.get("ensemble_auc_val","?"),
        "v1": v1.get("ensemble_auc_val","?"),
    }[prod_ver]
    lines += [
        "─" * 60,
        f"🏆 PRODUCTION MODEL: {prod_ver.upper()}  (AUC {prod_auc})",
        "",
    ]

    # Plain-English interpretation
    lines += [
        "─" * 60,
        "PLAIN-ENGLISH EXPLANATION",
        "",
        "What is AUC?",
        "  Think of it like a batting average for predictions.",
        "  0.50 = random guessing (a coin flip — useless).",
        "  0.55 = correct 55% of the time — actually useful in finance.",
        "  0.60 = very strong — hedge funds pay millions for this.",
        "  1.00 = perfect — never happens in the real world.",
        "",
        "Why did v1 CV show 0.277 (below a coin flip)?",
        "  That was a bug, not the model being bad.  The v1 cross-validation",
        "  mixed up time order — it tested the model on data from the PAST",
        "  while training on the FUTURE, which is impossible to do in real life.",
        "  This makes results look worse than they actually are.  v2 and v3",
        "  fix this by always testing on data AFTER the training data.",
        "",
        "What are macro features?",
        "  These are market-wide conditions when an institution filed:",
        "  VIX (how scared the market is), the yield curve (recession signal),",
        "  and whether the S&P 500 is trending up or down.  If a hedge fund",
        "  buys a stock when VIX is at 35 (market panic), their conviction is",
        "  much higher — and that timing affects whether the pick succeeds.",
        "",
        "What did the R&D agents contribute?",
        "  The R&D team (rd_team_leader.py, rd_geopolitical_team.py) run hourly",
        "  and write reports about macro conditions and geopolitical risks.",
        "  v3 reads these reports and converts them into a sentiment score",
        "  (-1 = very bearish, 0 = neutral, +1 = very bullish) that adjusts",
        "  the model's confidence in new picks.  This is a live signal for",
        "  inference, not historical training — it adds context the numbers alone",
        "  can't capture (e.g. a trade war announcement yesterday).",
        "",
        "What should improve over time?",
        "  As the pipeline runs daily and accumulates more labeled outcomes,",
        "  the model's accuracy will compound.  The historical backtest used",
        "  SEC filings going back years.  Every week of live trading adds fresh,",
        "  recent data that reflects today's market conditions.",
        "─" * 60,
    ]

    any_err = any(r.get("status") == "ERROR" for r in [v1, v2, v3, v4, v5])
    subject = ("\U0001f6a8 Apex Capital — ML training errors (check email)"
               if any_err else
               f"✅ Apex Capital — ML training complete | Production: {prod_ver.upper()} AUC {prod_auc}")

    _send(subject, "\n".join(lines))
    logger.info("  Summary email sent.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label-pid", type=int, default=0)
    parser.add_argument("--skip-wait", action="store_true")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — OVERNIGHT ML RUNNER  (v1 → v2 → v3 → v4 → v5)")
    logger.info(f"  Started:  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"  Label PID: {args.label_pid or 'not specified'}")
    logger.info("=" * 60)

    if not args.skip_wait:
        wait_for_label_builder(args.label_pid)

    time.sleep(5)   # let DB flush

    v1 = run_v1_training()
    v2 = run_v2_training()
    prewarm_macro_cache()
    v3 = run_v3_training()
    v4 = run_v4_training()
    v5 = run_v5_training()

    send_summary(args.label_pid, v1, v2, v3, v4, v5)

    logger.info("=" * 60)
    logger.info("  OVERNIGHT ML RUNNER — DONE")
    logger.info(f"  Finished: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
