# =============================================================================
# ML DRIFT MONITOR
# Apex Capital ML Research Team
#
# Detects predictive-power degradation in the live ensemble model by comparing
# AUC on recent data (last 60 days) against a historical baseline window
# (61–180 days ago).  Writes data/drift_report.json and fires an alert email
# when drift crosses the ALERT threshold.
#
# Run:  python ml_drift_monitor.py
# GCP:  Python 3.11 — pandas, numpy, scikit-learn, lightgbm, xgboost, sqlite3,
#       pickle, yfinance
# =============================================================================

from __future__ import annotations

import json
import logging
import pickle
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from email_sender import send_alert_email
import ml_digest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOGS_DIR  = BASE_DIR / "logs"

MODEL_PATH        = (DATA_DIR / "model_v5.pkl" if (DATA_DIR / "model_v5.pkl").exists()
                     else DATA_DIR / "model_v4.pkl" if (DATA_DIR / "model_v4.pkl").exists()
                     else DATA_DIR / "model_v3.pkl")
DB_PATH           = DATA_DIR / "historical_13f.db"
REPORT_PATH       = DATA_DIR / "drift_report.json"
# digest_queue.json is ml_digest's file now — the dead-letter store behind
# ml_digest.deliver(). This module held its own path constant while it wrote
# the file directly; leaving the constant behind would keep this module
# looking like a reader of a queue it no longer touches.

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
ALERT_THRESHOLD   = -0.03   # drift < this  → ALERT  (email immediately)
WARNING_THRESHOLD = -0.015  # drift < this  → WARNING (add to daily digest)
MIN_RECENT_SAMPLES = 50     # fewer than this → skip, not enough data

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "ml_drift_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ml_drift_monitor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_model(path: Path) -> dict:
    """
    Load a model pickle. Normalises v2/v3/v4 bundles (which use fitted_bases +
    meta_model) into the {models, ensemble_weights, feature_cols, ensemble_auc_val}
    format expected by _ensemble_predict.
    """
    if not path.exists():
        raise FileNotFoundError(f"Model file not found: {path}")
    with open(path, "rb") as fh:
        bundle = pickle.load(fh)

    if not isinstance(bundle, dict):
        raise ValueError("Model bundle is not a dict")

    # ── Normalise v2/v3/v4 format (fitted_bases dict) to legacy format ─────
    if "fitted_bases" in bundle and "models" not in bundle:
        bases   = bundle["fitted_bases"]          # {name: sklearn_clf}
        n_bases = len(bases)
        bundle["models"]           = bases
        bundle["ensemble_weights"] = {k: 1.0 / n_bases for k in bases}
        if "ensemble_auc_val" not in bundle:
            # Try to read from meta JSON alongside the pkl
            meta_path = path.with_suffix("").with_name(
                path.stem.replace("model", "model") + "_meta.json"
            )
            auc = 0.59
            try:
                import json as _json
                with open(meta_path) as _mf:
                    _meta = _json.load(_mf)
                auc = float(_meta.get("ensemble_auc_val", 0.59))
            except Exception:
                pass
            bundle["ensemble_auc_val"] = auc

        # Scaler needed — add if not present; let _build_feature_matrix handle missing cols
        log.info("v2/v3/v4 bundle normalised (%d base models)", n_bases)

    required_keys = {"models", "ensemble_weights", "feature_cols", "ensemble_auc_val"}
    missing = required_keys - set(bundle.keys())
    if missing:
        raise ValueError(f"Model bundle missing keys: {missing}")

    log.info(
        "Model loaded — %d sub-models, feature_cols=%d, baseline_auc=%.4f",
        len(bundle["models"]),
        len(bundle["feature_cols"]),
        bundle["ensemble_auc_val"],
    )
    return bundle


def _query_window(conn: sqlite3.Connection, date_filter: str) -> pd.DataFrame:
    """
    Return rows from pipeline_snapshots where outcome_20d IS NOT NULL
    and snapshot_date matches the provided WHERE clause fragment.
    """
    sql = f"""
        SELECT *
        FROM   pipeline_snapshots
        WHERE  outcome_20d IS NOT NULL
          AND  {date_filter}
    """
    df = pd.read_sql_query(sql, conn)
    return df


def _build_feature_matrix(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """
    Select feature_cols from df, filling any absent column with 0.
    Returns a DataFrame with exactly the columns in feature_cols.
    """
    missing_cols = [c for c in feature_cols if c not in df.columns]
    if missing_cols:
        log.warning("Feature columns absent from data (filled with 0): %s", missing_cols)
    X = df.reindex(columns=feature_cols, fill_value=0)
    return X


def _ensemble_predict(
    X: pd.DataFrame,
    models: dict,
    weights: dict,
) -> np.ndarray:
    """
    Generate weighted ensemble probability predictions.

    For each model name in models dict, call predict_proba(X)[:,1] and
    multiply by the corresponding weight.  Weights are normalised so they
    sum to 1.0 even if the stored values don't.
    """
    total_weight = sum(weights.get(name, 0.0) for name in models)
    if total_weight == 0:
        raise ValueError("All ensemble weights are zero — cannot produce predictions.")

    preds = np.zeros(len(X), dtype=float)
    for name, model in models.items():
        w = weights.get(name, 0.0)
        if w == 0.0:
            log.debug("Skipping model '%s' — weight is 0.", name)
            continue
        try:
            prob = model.predict_proba(X)[:, 1]
            preds += (w / total_weight) * prob
            log.debug("Model '%s' contributed (w=%.3f).", name, w / total_weight)
        except Exception as exc:
            log.error("predict_proba failed for model '%s': %s", name, exc)
            raise

    return preds


def _compute_auc(y_true: pd.Series, y_pred: np.ndarray, label: str) -> float:
    """Compute ROC-AUC; raises ValueError if only one class present."""
    unique_classes = y_true.nunique()
    if unique_classes < 2:
        raise ValueError(
            f"Cannot compute AUC for '{label}' window — only {unique_classes} class(es) present."
        )
    auc = roc_auc_score(y_true, y_pred)
    log.info("AUC [%s]: %.4f  (n=%d)", label, auc, len(y_true))
    return float(auc)


def _send_to_digest(entry: dict) -> bool:
    """Deliver a WARNING-level drift finding to the daily digest.

    Was an append to `data/digest_queue.json`, a file with three writers and no
    reader — see `ml_digest` for the 75-day escape that came of it.
    """
    return ml_digest.deliver([entry], source="ml_drift_monitor")


# ---------------------------------------------------------------------------
# Core run function
# ---------------------------------------------------------------------------

def _run() -> int:
    log.info("=" * 70)
    log.info("ML Drift Monitor starting — %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 70)

    # ------------------------------------------------------------------
    # 1. Load model bundle
    # ------------------------------------------------------------------
    try:
        bundle = _load_model(MODEL_PATH)
    except FileNotFoundError as exc:
        log.error("ABORT: %s", exc)
        return 0  # graceful — not a crash, model just isn't deployed yet
    except Exception as exc:
        log.exception("Unexpected error loading model: %s", exc)
        return 1

    models           = bundle["models"]
    ensemble_weights = bundle["ensemble_weights"]
    feature_cols     = bundle["feature_cols"]
    baseline_auc     = float(bundle["ensemble_auc_val"])

    # ------------------------------------------------------------------
    # 2. Connect to DB and fetch both windows
    # ------------------------------------------------------------------
    if not DB_PATH.exists():
        log.error("ABORT: database not found at %s", DB_PATH)
        return 0

    try:
        conn = sqlite3.connect(str(DB_PATH))
        log.info("Connected to %s", DB_PATH)

        recent_df = _query_window(
            conn,
            "snapshot_date >= date('now', '-60 days')",
        )
        historical_df = _query_window(
            conn,
            "snapshot_date >= date('now', '-180 days') "
            "AND snapshot_date < date('now', '-60 days')",
        )
        conn.close()
    except Exception as exc:
        log.exception("Database query failed: %s", exc)
        return 1

    log.info(
        "Window sizes — recent: %d rows, historical: %d rows",
        len(recent_df),
        len(historical_df),
    )

    # ------------------------------------------------------------------
    # 3. Guard: recent window must have enough samples
    # ------------------------------------------------------------------
    if len(recent_df) < MIN_RECENT_SAMPLES:
        log.warning(
            "Recent window has only %d samples (minimum %d required). "
            "Skipping drift computation — not enough data for reliable AUC.",
            len(recent_df),
            MIN_RECENT_SAMPLES,
        )
        return 0

    if len(historical_df) == 0:
        log.warning("Historical window is empty — cannot compute historical AUC. Skipping.")
        return 0

    # ------------------------------------------------------------------
    # 4. Build feature matrices
    # ------------------------------------------------------------------
    X_recent     = _build_feature_matrix(recent_df,     feature_cols)
    X_historical = _build_feature_matrix(historical_df, feature_cols)

    y_recent     = (recent_df["outcome_20d"] > 0).astype(int)
    y_historical = (historical_df["outcome_20d"] > 0).astype(int)

    # ------------------------------------------------------------------
    # 5 & 6. Generate ensemble predictions and compute AUC
    # ------------------------------------------------------------------
    try:
        preds_recent     = _ensemble_predict(X_recent,     models, ensemble_weights)
        preds_historical = _ensemble_predict(X_historical, models, ensemble_weights)
    except Exception as exc:
        log.exception("Ensemble prediction failed: %s", exc)
        return 1

    try:
        recent_auc     = _compute_auc(y_recent,     preds_recent,     "recent")
        historical_auc = _compute_auc(y_historical, preds_historical, "historical")
    except ValueError as exc:
        log.error("AUC computation failed: %s", exc)
        return 0

    # ------------------------------------------------------------------
    # 7. Compute drift and determine status
    # ------------------------------------------------------------------
    drift = recent_auc - historical_auc
    log.info("Drift = %.4f  (recent %.4f − historical %.4f)", drift, recent_auc, historical_auc)

    if drift < ALERT_THRESHOLD:
        status = "ALERT"
    elif drift < WARNING_THRESHOLD:
        status = "WARNING"
    else:
        status = "OK"

    log.info("Status: %s", status)

    # ------------------------------------------------------------------
    # 8. Write drift report
    # ------------------------------------------------------------------
    # ── Fable 5 drift interpretation — gated to WARNING/ALERT ─────────────
    llm_read = None
    if status != "OK":
        try:
            from apex_llm import reason as _r, is_available
            if is_available("ml_improvement"):
                llm_read = _r(
                    "You diagnose model drift for a 13F-based stock selection model.",
                    f"Recent AUC {recent_auc:.3f} vs historical {historical_auc:.3f} "
                    f"(drift {drift:+.3f}, status {status}). "
                    f"Samples: {len(recent_df)} recent / {len(historical_df)} historical.\n"
                    "2 sentences: most likely cause (regime shift vs data issue vs decay), "
                    "and should the model keep serving picks?",
                    agent_name="ml_improvement", max_tokens=110,
                )
        except Exception:
            pass

    report = {
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "recent_auc":      round(recent_auc,     4),
        "historical_auc":  round(historical_auc, 4),
        "drift":           round(drift,           4),
        "recent_n":        int(len(recent_df)),
        "historical_n":    int(len(historical_df)),
        "status":          status,
        "baseline_auc":    round(baseline_auc, 4),
        **({"llm_drift_read": llm_read.strip()} if llm_read else {}),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("Drift report written to %s", REPORT_PATH)

    # ------------------------------------------------------------------
    # 9 & 10. Alerts / digest
    # ------------------------------------------------------------------
    if status == "ALERT":
        subject = (
            f"\U0001f6a8 Apex Capital — Model Drift ALERT: AUC dropped {drift:.3f}"
        )
        body = f"""
        <h3 style="color:#f85149;">Model Drift ALERT</h3>
        <p>The live ensemble model's predictive performance has degraded significantly
        on the most recent data window.</p>
        <table style="border-collapse:collapse;font-size:14px;">
          <tr><td style="padding:6px 16px 6px 0;color:#8b949e;">Recent AUC (last 60d)</td>
              <td style="padding:6px 0;font-weight:bold;">{recent_auc:.4f}</td></tr>
          <tr><td style="padding:6px 16px 6px 0;color:#8b949e;">Historical AUC (61–180d)</td>
              <td style="padding:6px 0;">{historical_auc:.4f}</td></tr>
          <tr><td style="padding:6px 16px 6px 0;color:#8b949e;">Drift</td>
              <td style="padding:6px 0;color:#f85149;font-weight:bold;">{drift:+.4f}</td></tr>
          <tr><td style="padding:6px 16px 6px 0;color:#8b949e;">Baseline AUC (training val)</td>
              <td style="padding:6px 0;">{baseline_auc:.4f}</td></tr>
          <tr><td style="padding:6px 16px 6px 0;color:#8b949e;">Recent window N</td>
              <td style="padding:6px 0;">{len(recent_df):,}</td></tr>
          <tr><td style="padding:6px 16px 6px 0;color:#8b949e;">Historical window N</td>
              <td style="padding:6px 0;">{len(historical_df):,}</td></tr>
        </table>
        <p style="color:#f85149;margin-top:16px;">
          <strong>Recommended action:</strong> Review recent feature distribution,
          check for data pipeline issues, and consider retraining or re-weighting the ensemble.
        </p>
        """
        sent = send_alert_email(subject, body)
        if sent:
            log.info("ALERT email sent successfully.")
        else:
            log.error("ALERT email FAILED to send — check email_sender logs.")

    elif status == "WARNING":
        log.info("WARNING level drift — appending to daily digest queue.")
        digest_entry = {
            "source":    "ml_drift_monitor",
            "timestamp": report["timestamp"],
            "subject":   f"Model Drift WARNING: drift={drift:+.4f}",
            "detail":    report,
        }
        _send_to_digest(digest_entry)

    log.info("ML Drift Monitor complete.")
    return 0


def run() -> int:
    """Run every check; return a process exit status without ever taking one.

    The body lives in `_run` because this module is BOTH a standalone script and
    an in-process member of `ml_research_team_leader.AGENT_DEFS`. It used to say
    `sys.exit(0)` on routine skip conditions, which raises SystemExit — a
    BaseException the leader's `except Exception` could not see. One skip there
    terminated the whole leader process with status 0, and every agent after
    this one silently never ran. A library function returns; only `__main__`
    decides the exit code.

    `finally`, not a trailing call: `_run` returns early on several routine
    conditions, and the dead-letter retry must not be gated behind the happy
    path — that is how the undelivered rows accumulated in the first place.
    """
    try:
        return _run()
    finally:
        # Retry anything an earlier run could not deliver — see ml_digest.drain_backlog.
        ml_digest.drain_backlog()




# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(run())
