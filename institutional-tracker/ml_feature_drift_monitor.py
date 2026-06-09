#!/usr/bin/env python3
"""
APEX CAPITAL — ML FEATURE DRIFT MONITOR
=========================================
Detects distribution shifts in ML input features before they cause AUC
degradation.  Complements ml_drift_monitor.py (which watches AUC) by
watching the INPUTS rather than the output.

Method: Population Stability Index (PSI) — industry standard for
detecting whether a feature's distribution has shifted between a
reference window (61–180 days ago) and the current window (last 60 days).

PSI thresholds:
  PSI < 0.10  → stable (no action)
  0.10–0.20   → slight shift (add to digest)
  PSI > 0.20  → major drift (ALERT email)

Run:  python3 ml_feature_drift_monitor.py
GCP:  Runs daily via cron (see deploy_overnight_changes.sh for cron entry)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from email_sender import send_alert_email

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOGS_DIR  = BASE_DIR / "logs"

DB_PATH           = DATA_DIR / "historical_13f.db"
REPORT_PATH       = DATA_DIR / "feature_drift_report.json"
DIGEST_QUEUE_PATH = DATA_DIR / "digest_queue.json"

PSI_ALERT   = 0.20   # > this → ALERT email immediately
PSI_WARNING = 0.10   # > this → WARNING in digest

# How many PSI-alert features before firing the alert email
# (some noise is expected, so only alert if multiple features drift)
ALERT_FEATURE_COUNT = 2

MIN_RECENT_ROWS = 30   # fewer than this → skip, not enough data

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "ml_feature_drift.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ml_feature_drift_monitor")


# ---------------------------------------------------------------------------
# PSI helpers
# ---------------------------------------------------------------------------

def _psi_for_feature(
    ref: pd.Series,
    cur: pd.Series,
    n_bins: int = 10,
) -> float:
    """
    Compute PSI between reference distribution (ref) and current distribution (cur).

    For continuous features: uses equal-width bins from ref; clips cur to same range.
    For boolean / binary features (<=2 unique values): uses the two values as bins.

    PSI = Σ (pct_cur - pct_ref) * ln(pct_cur / pct_ref)
    """
    ref = ref.dropna()
    cur = cur.dropna()

    if len(ref) == 0 or len(cur) == 0:
        return 0.0

    unique_vals = ref.nunique()

    if unique_vals <= 2:
        # Binary / flag feature — use value counts directly
        ref_counts = ref.value_counts(normalize=True).sort_index()
        cur_counts = cur.value_counts(normalize=True).reindex(ref_counts.index, fill_value=1e-4)
        ref_pct = ref_counts.values.clip(1e-4)
        cur_pct = cur_counts.values.clip(1e-4)
    else:
        # Continuous — equal-width binning from reference distribution
        ref_min = ref.min()
        ref_max = ref.max()
        if ref_min == ref_max:
            return 0.0  # constant feature — no drift possible
        bins = np.linspace(ref_min, ref_max, n_bins + 1)
        bins[0]  -= 1e-6   # include minimum
        bins[-1] += 1e-6   # include maximum

        ref_counts, _ = np.histogram(ref, bins=bins)
        cur_counts, _ = np.histogram(cur.clip(ref_min, ref_max), bins=bins)

        ref_pct = (ref_counts / len(ref)).clip(1e-4)
        cur_pct = (cur_counts / len(cur)).clip(1e-4)

    psi = float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))
    return psi


# ---------------------------------------------------------------------------
# DB fetch
# ---------------------------------------------------------------------------

def _fetch_windows(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (recent_df, historical_df) from pipeline_snapshots."""
    recent = pd.read_sql_query(
        """
        SELECT * FROM pipeline_snapshots
        WHERE snapshot_date >= date('now', '-60 days')
        """,
        conn,
    )
    historical = pd.read_sql_query(
        """
        SELECT * FROM pipeline_snapshots
        WHERE snapshot_date >= date('now', '-180 days')
          AND snapshot_date <  date('now', '-60 days')
        """,
        conn,
    )
    return recent, historical


# ---------------------------------------------------------------------------
# Digest queue helper
# ---------------------------------------------------------------------------

def _append_digest(entry: dict) -> None:
    queue: list = []
    if DIGEST_QUEUE_PATH.exists():
        try:
            with open(DIGEST_QUEUE_PATH, "r", encoding="utf-8") as fh:
                queue = json.load(fh)
            if not isinstance(queue, list):
                queue = []
        except json.JSONDecodeError:
            queue = []
    queue.append(entry)
    with open(DIGEST_QUEUE_PATH, "w", encoding="utf-8") as fh:
        json.dump(queue, fh, indent=2)
    log.info("Appended WARNING to digest queue (%d items total).", len(queue))


# ---------------------------------------------------------------------------
# Candidate feature columns
# ---------------------------------------------------------------------------
# All numeric columns that should be stable if data collection is healthy.
# Excludes id/date/text columns.
_CANDIDATE_COLS = [
    "conviction_score", "n_institutions", "days_since_filing",
    "herding_flag", "quant_consensus", "portfolio_weight",
    # pipeline_snapshots also stores outcome columns but those aren't input features
]

# Extended list — columns added by historical label builder / feature engineer
# that may or may not exist in pipeline_snapshots depending on schema version
_EXTENDED_COLS = [
    "pct_change_shares", "new_position_flag", "large_increase_flag",
    "position_size_pct_aum", "hold_streak_quarters", "avg_conviction_3q",
    "institution_hit_rate", "price_momentum_20d", "price_vs_52w_high",
    "volatility_20d", "short_interest_pct", "sector_momentum_vs_spy",
    "rd_vix", "rd_spy_rsi", "rd_regime_bull", "earnings_surprise_flag",
    "insider_buy_dollars", "insider_is_ceo_cfo", "institution_regime_hit_rate",
]


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def run() -> None:
    log.info("=" * 70)
    log.info("ML Feature Drift Monitor starting — %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 70)

    # ------------------------------------------------------------------
    # 1. Load DB
    # ------------------------------------------------------------------
    if not DB_PATH.exists():
        log.warning("Database not found at %s — skipping.", DB_PATH)
        sys.exit(0)

    try:
        conn = sqlite3.connect(str(DB_PATH))
        recent_df, historical_df = _fetch_windows(conn)
        conn.close()
    except Exception as exc:
        log.exception("DB query failed: %s", exc)
        sys.exit(1)

    log.info(
        "Window sizes — recent: %d rows, historical: %d rows",
        len(recent_df), len(historical_df),
    )

    if len(recent_df) < MIN_RECENT_ROWS:
        log.warning(
            "Recent window has only %d rows (min %d). Skipping.",
            len(recent_df), MIN_RECENT_ROWS,
        )
        sys.exit(0)

    if len(historical_df) == 0:
        log.warning("Historical window is empty. Skipping.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 2. Determine which columns are actually present
    # ------------------------------------------------------------------
    available_cols = set(recent_df.columns) & set(historical_df.columns)
    cols_to_check  = [
        c for c in (_CANDIDATE_COLS + _EXTENDED_COLS)
        if c in available_cols
    ]

    if not cols_to_check:
        log.warning("No numeric feature columns found in both windows. Skipping.")
        sys.exit(0)

    log.info("Checking PSI for %d feature columns.", len(cols_to_check))

    # ------------------------------------------------------------------
    # 3. Compute PSI per feature
    # ------------------------------------------------------------------
    results: list[dict] = []
    alert_features:   list[str] = []
    warning_features: list[str] = []

    for col in cols_to_check:
        ref = pd.to_numeric(historical_df[col], errors="coerce")
        cur = pd.to_numeric(recent_df[col],     errors="coerce")

        psi = _psi_for_feature(ref, cur)

        if psi > PSI_ALERT:
            status = "ALERT"
            alert_features.append(col)
        elif psi > PSI_WARNING:
            status = "WARNING"
            warning_features.append(col)
        else:
            status = "OK"

        results.append({
            "feature": col,
            "psi":     round(psi, 4),
            "status":  status,
            "ref_mean": round(float(ref.mean()), 5) if ref.notna().any() else None,
            "cur_mean": round(float(cur.mean()), 5) if cur.notna().any() else None,
            "ref_n":    int(ref.notna().sum()),
            "cur_n":    int(cur.notna().sum()),
        })

        log.info(
            "  %-40s PSI=%.4f  [%s]  ref_μ=%.3f  cur_μ=%.3f",
            col, psi, status,
            ref.mean() if ref.notna().any() else float("nan"),
            cur.mean() if cur.notna().any() else float("nan"),
        )

    # ------------------------------------------------------------------
    # 4. Determine overall status
    # ------------------------------------------------------------------
    results.sort(key=lambda x: -x["psi"])

    if len(alert_features) >= ALERT_FEATURE_COUNT:
        overall_status = "ALERT"
    elif alert_features or warning_features:
        overall_status = "WARNING"
    else:
        overall_status = "OK"

    log.info(
        "Summary — ALERT features: %d, WARNING: %d, OK: %d → overall: %s",
        len(alert_features), len(warning_features),
        len(results) - len(alert_features) - len(warning_features),
        overall_status,
    )

    # ------------------------------------------------------------------
    # 5. Write report
    # ------------------------------------------------------------------
    report = {
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "overall_status":  overall_status,
        "recent_n":        len(recent_df),
        "historical_n":    len(historical_df),
        "alert_features":  alert_features,
        "warning_features": warning_features,
        "features":        results,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("Feature drift report written → %s", REPORT_PATH)

    # ------------------------------------------------------------------
    # 6. Fire alerts
    # ------------------------------------------------------------------
    if overall_status == "ALERT":
        top_drifted = ", ".join(f"{r['feature']} (PSI={r['psi']:.3f})"
                                for r in results[:5] if r["status"] == "ALERT")
        subject = (
            f"\U0001f6a8 Apex Capital — Feature Drift ALERT: "
            f"{len(alert_features)} features drifted"
        )
        body = f"""
        <h3 style="color:#f85149;">ML Feature Drift ALERT</h3>
        <p>
          {len(alert_features)} input feature(s) have PSI &gt; {PSI_ALERT:.2f},
          indicating a significant distribution shift in the ML pipeline inputs.
          This can degrade model performance <em>before</em> AUC regression becomes visible.
        </p>
        <p><strong>Most drifted features:</strong><br/>{top_drifted}</p>
        <table style="border-collapse:collapse;font-size:13px;margin-top:12px;">
          <tr style="background:#21262d;color:#e6edf3;">
            <th style="padding:6px 12px;text-align:left;">Feature</th>
            <th style="padding:6px 12px;">PSI</th>
            <th style="padding:6px 12px;">Status</th>
            <th style="padding:6px 12px;">Ref μ</th>
            <th style="padding:6px 12px;">Cur μ</th>
          </tr>
          {''.join(
              f'<tr><td style="padding:5px 12px;">{r["feature"]}</td>'
              f'<td style="padding:5px 12px;text-align:center;color:{"#f85149" if r["status"]=="ALERT" else "#d29922" if r["status"]=="WARNING" else "#3fb950"};">{r["psi"]:.4f}</td>'
              f'<td style="padding:5px 12px;text-align:center;">{r["status"]}</td>'
              f'<td style="padding:5px 12px;text-align:center;">{r["ref_mean"]}</td>'
              f'<td style="padding:5px 12px;text-align:center;">{r["cur_mean"]}</td></tr>'
              for r in results
          )}
        </table>
        <p style="color:#f85149;margin-top:16px;">
          <strong>Action:</strong> Check data pipeline for source changes,
          new schema columns, or upstream data quality issues.
          Also check ml_drift_monitor.py for correlated AUC degradation.
        </p>
        """
        sent = send_alert_email(subject, body)
        log.info("Alert email %s.", "sent" if sent else "FAILED")

    elif overall_status == "WARNING":
        top_warning = [r for r in results if r["status"] in ("ALERT", "WARNING")][:5]
        _append_digest({
            "source":    "ml_feature_drift_monitor",
            "timestamp": report["timestamp"],
            "subject":   f"Feature drift WARNING: {len(alert_features)} ALERT, {len(warning_features)} WARNING features",
            "detail":    {"alert_features": alert_features, "warning_features": warning_features,
                          "top_features": top_warning},
        })

    log.info("ML Feature Drift Monitor complete.")


if __name__ == "__main__":
    run()
