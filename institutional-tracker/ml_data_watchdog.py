# =============================================================================
# ML DATA WATCHDOG
# Apex Capital ML Research Team
#
# Catches silent data corruption before it poisons the model:
#   - Label balance drift
#   - Feature null-rate spikes
#   - Macro cache staleness / VIX sanity
#   - Date continuity gaps in pipeline_snapshots
#   - rd_ml_history freshness
#   - Label count trend stall
#
# Writes data/data_quality_report.json.
# Fires an alert email on any CRITICAL check.
# Sends WARNING-level issues to the daily digest via ml_digest (which routes
# through email_buffer/mail_guard). data/digest_queue.json is now a DEAD-LETTER
# file: a row in it means delivery failed, and coherence_laws.law_work_queues_drain
# measures how long it has been there.
#
# Run:  python ml_data_watchdog.py
# GCP:  Python 3.11 — pandas, numpy, scikit-learn, lightgbm, xgboost, sqlite3,
#       pickle, yfinance
# =============================================================================

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TypedDict

import pandas as pd

from email_sender import send_alert_email
import ml_digest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOGS_DIR  = BASE_DIR / "logs"

DB_PATH             = DATA_DIR / "historical_13f.db"
MACRO_CACHE_PATH    = DATA_DIR / "macro_cache.parquet"
RD_ML_HISTORY_PATH  = DATA_DIR / "rd_ml_history.json"
REPORT_PATH         = DATA_DIR / "data_quality_report.json"
# digest_queue.json is ml_digest's file now — the dead-letter store behind
# ml_digest.deliver(). This module held its own path constant while it wrote
# the file directly; leaving the constant behind would keep this module
# looking like a reader of a queue it no longer touches.

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
LABEL_BALANCE_WARNING_LOW   = 0.35
LABEL_BALANCE_WARNING_HIGH  = 0.65
LABEL_BALANCE_CRITICAL_LOW  = 0.25
LABEL_BALANCE_CRITICAL_HIGH = 0.75

FEATURE_NULL_WARNING_RATE   = 0.40   # >40% nulls in last 30 days
MONITORED_FEATURES          = [
    "price_momentum_20d",
    "price_momentum_5d",
    "volatility_20d",
    "price_vs_52w_high",
    "conviction_score",
]

MACRO_CACHE_WARNING_AGE_DAYS = 7
VIX_MIN = 8.0
VIX_MAX = 90.0

DATE_GAP_WARNING_DAYS = 45   # largest consecutive gap in snapshot_dates

RD_ML_HISTORY_WARNING_HOURS = 48

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "ml_data_watchdog.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ml_data_watchdog")


# ---------------------------------------------------------------------------
# Type for a single check result
# ---------------------------------------------------------------------------
class CheckResult(TypedDict):
    check_name: str
    status: str          # "OK" | "WARNING" | "CRITICAL"
    detail: str


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_label_balance(conn: sqlite3.Connection) -> CheckResult:
    """
    Check 1 — Label balance.
    outcome_20d > 0.02 is treated as a positive label.
    """
    name = "label_balance"
    try:
        df = pd.read_sql_query(
            "SELECT outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL",
            conn,
        )
        total = len(df)
        if total == 0:
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail="No labeled rows found in pipeline_snapshots.",
            )
        positive_count = int((df["outcome_20d"] > 0.02).sum())
        positive_rate  = positive_count / total
        log.info(
            "[%s] total=%d  positives=%d  positive_rate=%.3f",
            name, total, positive_count, positive_rate,
        )

        if (
            positive_rate < LABEL_BALANCE_CRITICAL_LOW
            or positive_rate > LABEL_BALANCE_CRITICAL_HIGH
        ):
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail=(
                    f"Positive rate {positive_rate:.1%} is outside critical bounds "
                    f"[{LABEL_BALANCE_CRITICAL_LOW:.0%}, {LABEL_BALANCE_CRITICAL_HIGH:.0%}] "
                    f"(total={total:,}, positives={positive_count:,})."
                ),
            )
        if (
            positive_rate < LABEL_BALANCE_WARNING_LOW
            or positive_rate > LABEL_BALANCE_WARNING_HIGH
        ):
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"Positive rate {positive_rate:.1%} is outside warning bounds "
                    f"[{LABEL_BALANCE_WARNING_LOW:.0%}, {LABEL_BALANCE_WARNING_HIGH:.0%}] "
                    f"(total={total:,}, positives={positive_count:,})."
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=(
                f"Positive rate {positive_rate:.1%} is within bounds "
                f"(total={total:,}, positives={positive_count:,})."
            ),
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


def check_feature_null_rates(conn: sqlite3.Connection) -> CheckResult:
    """
    Check 2 — Feature null rates in last 30 days.
    """
    name = "feature_null_rates"
    try:
        # Build a SELECT that handles absent columns gracefully by checking
        # PRAGMA table_info first.
        cursor = conn.execute("PRAGMA table_info(pipeline_snapshots)")
        existing_cols = {row[1] for row in cursor.fetchall()}

        present_features   = [c for c in MONITORED_FEATURES if c in existing_cols]
        absent_features    = [c for c in MONITORED_FEATURES if c not in existing_cols]

        if absent_features:
            log.warning("[%s] Columns absent from table (treated as 100%% null): %s", name, absent_features)

        if not present_features:
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail=(
                    "None of the monitored feature columns exist in pipeline_snapshots: "
                    f"{MONITORED_FEATURES}"
                ),
            )

        col_list = ", ".join(f'"{c}"' for c in present_features)
        df = pd.read_sql_query(
            f"SELECT {col_list} FROM pipeline_snapshots "
            "WHERE snapshot_date >= date('now', '-30 days')",
            conn,
        )

        if len(df) == 0:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail="No rows found in pipeline_snapshots for the last 30 days.",
            )

        high_null: list[str] = []
        null_summary: dict[str, float] = {}

        for col in present_features:
            rate = float(df[col].isna().mean())
            null_summary[col] = round(rate, 4)
            if rate > FEATURE_NULL_WARNING_RATE:
                high_null.append(f"{col}={rate:.1%}")
                log.warning("[%s] High null rate: %s = %.1%% (last 30d, n=%d)", name, col, rate * 100, len(df))

        for col in absent_features:
            null_summary[col] = 1.0
            high_null.append(f"{col}=100.0% (column missing)")

        detail_summary = "  |  ".join(f"{k}: {v:.1%}" for k, v in null_summary.items())

        if high_null:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"High null rates detected (>{FEATURE_NULL_WARNING_RATE:.0%}): "
                    f"{', '.join(high_null)}.  "
                    f"All rates (last 30d, n={len(df):,}): {detail_summary}"
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=f"All null rates within threshold (last 30d, n={len(df):,}): {detail_summary}",
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


def check_macro_cache_freshness() -> CheckResult:
    """
    Check 3 — macro_cache.parquet mtime.
    """
    name = "macro_cache_freshness"
    try:
        if not MACRO_CACHE_PATH.exists():
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail=f"macro_cache.parquet not found at {MACRO_CACHE_PATH}.",
            )
        mtime     = datetime.fromtimestamp(MACRO_CACHE_PATH.stat().st_mtime, tz=timezone.utc)
        age_days  = (datetime.now(timezone.utc) - mtime).total_seconds() / 86400
        log.info("[%s] mtime=%s  age=%.1f days", name, mtime.isoformat(), age_days)

        if age_days > MACRO_CACHE_WARNING_AGE_DAYS:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"macro_cache.parquet is {age_days:.1f} days old "
                    f"(threshold: {MACRO_CACHE_WARNING_AGE_DAYS}d). "
                    f"Last modified: {mtime.isoformat()}."
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=f"Macro cache is current ({age_days:.1f} days old, last modified {mtime.isoformat()}).",
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


def check_vix_sanity() -> CheckResult:
    """
    Check 4 — VIX column values in macro_cache.parquet.
    """
    name = "vix_sanity"
    try:
        if not MACRO_CACHE_PATH.exists():
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail="macro_cache.parquet missing — cannot check VIX values.",
            )
        df = pd.read_parquet(MACRO_CACHE_PATH)

        if "vix_level" not in df.columns:
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail="vix_level column not found in macro_cache.parquet.",
            )

        vix = df["vix_level"].dropna()
        if len(vix) == 0:
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail="vix_level column is entirely null in macro_cache.parquet.",
            )

        out_of_range = vix[(vix < VIX_MIN) | (vix > VIX_MAX)]
        log.info(
            "[%s] n=%d  min=%.2f  max=%.2f  out_of_range=%d",
            name, len(vix), float(vix.min()), float(vix.max()), len(out_of_range),
        )

        if len(out_of_range) > 0:
            sample = out_of_range.head(5).tolist()
            return CheckResult(
                check_name=name,
                status="CRITICAL",
                detail=(
                    f"{len(out_of_range)} VIX value(s) outside sanity range "
                    f"[{VIX_MIN}, {VIX_MAX}]. "
                    f"Sample bad values: {sample}. Likely download corruption."
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=(
                f"All {len(vix):,} VIX values within [{VIX_MIN}, {VIX_MAX}] "
                f"(min={vix.min():.2f}, max={vix.max():.2f})."
            ),
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


def check_date_continuity(conn: sqlite3.Connection) -> CheckResult:
    """
    Check 5 — Largest gap between consecutive snapshot_dates in last 180 days.
    """
    name = "date_continuity"
    try:
        df = pd.read_sql_query(
            "SELECT DISTINCT snapshot_date FROM pipeline_snapshots "
            "WHERE snapshot_date >= date('now', '-180 days') "
            "ORDER BY snapshot_date ASC",
            conn,
        )
        if len(df) < 2:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"Only {len(df)} distinct snapshot_date(s) in last 180 days — "
                    "cannot meaningfully compute date gaps."
                ),
            )

        dates     = pd.to_datetime(df["snapshot_date"])
        gaps_days = dates.diff().dt.days.dropna()
        max_gap   = int(gaps_days.max())
        gap_idx   = int(gaps_days.idxmax())
        gap_start = dates.iloc[gap_idx - 1].strftime("%Y-%m-%d")
        gap_end   = dates.iloc[gap_idx].strftime("%Y-%m-%d")

        log.info("[%s] distinct dates=%d  max_gap=%d days  (%s → %s)", name, len(dates), max_gap, gap_start, gap_end)

        if max_gap > DATE_GAP_WARNING_DAYS:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"Largest date gap is {max_gap} days ({gap_start} → {gap_end}), "
                    f"exceeding threshold of {DATE_GAP_WARNING_DAYS}d. "
                    "Possible missing quarter of SEC filings."
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=(
                f"Largest date gap is {max_gap} days ({gap_start} → {gap_end}), "
                f"within threshold of {DATE_GAP_WARNING_DAYS}d "
                f"({len(dates)} distinct dates in last 180 days)."
            ),
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


def check_rd_ml_history_freshness() -> CheckResult:
    """
    Check 6 — rd_ml_history.json mtime (R&D ML snapshot freshness).
    """
    name = "rd_ml_history_freshness"
    try:
        if not RD_ML_HISTORY_PATH.exists():
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=f"rd_ml_history.json not found at {RD_ML_HISTORY_PATH}.",
            )
        mtime      = datetime.fromtimestamp(RD_ML_HISTORY_PATH.stat().st_mtime, tz=timezone.utc)
        age_hours  = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        log.info("[%s] mtime=%s  age=%.1fh", name, mtime.isoformat(), age_hours)

        if age_hours > RD_ML_HISTORY_WARNING_HOURS:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"rd_ml_history.json is {age_hours:.1f} hours old "
                    f"(threshold: {RD_ML_HISTORY_WARNING_HOURS}h). "
                    "R&D ML snapshot writer may have stopped. "
                    f"Last modified: {mtime.isoformat()}."
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=f"rd_ml_history.json is current ({age_hours:.1f}h old, last modified {mtime.isoformat()}).",
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


def check_label_count_trend(conn: sqlite3.Connection) -> CheckResult:
    """
    Check 7 — Compare labeled count last 7 days vs. prior 7 days (8–14 days ago).
    If last 7 days added 0 new labels → WARNING.
    """
    name = "label_count_trend"
    try:
        df_recent = pd.read_sql_query(
            "SELECT COUNT(*) AS cnt FROM pipeline_snapshots "
            "WHERE outcome_20d IS NOT NULL "
            "  AND snapshot_date >= date('now', '-7 days')",
            conn,
        )
        df_prior = pd.read_sql_query(
            "SELECT COUNT(*) AS cnt FROM pipeline_snapshots "
            "WHERE outcome_20d IS NOT NULL "
            "  AND snapshot_date >= date('now', '-14 days') "
            "  AND snapshot_date <  date('now', '-7 days')",
            conn,
        )
        recent_count = int(df_recent["cnt"].iloc[0])
        prior_count  = int(df_prior["cnt"].iloc[0])
        log.info("[%s] last_7d=%d  prior_7d=%d", name, recent_count, prior_count)

        if recent_count == 0:
            return CheckResult(
                check_name=name,
                status="WARNING",
                detail=(
                    f"0 new labeled rows in the last 7 days "
                    f"(prior 7d: {prior_count:,}). "
                    "Label builder pipeline may have stopped."
                ),
            )
        return CheckResult(
            check_name=name,
            status="OK",
            detail=(
                f"Label count trend OK: last 7d={recent_count:,}, prior 7d={prior_count:,}."
            ),
        )
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return CheckResult(check_name=name, status="CRITICAL", detail=f"Check failed with exception: {exc}")


# ---------------------------------------------------------------------------
# Digest queue helper
# ---------------------------------------------------------------------------

def _send_to_digest(entries: list[dict]) -> bool:
    """Deliver WARNING entries to the daily digest. True if they went out.

    This used to append them to `data/digest_queue.json` and stop. Nothing has
    ever read that file — its only other mentions are the two sibling monitors,
    each appending to it the same way — so three data-quality warnings raised on
    2026-06-12 were still sitting there 75 days later, unseen. `ml_digest` sends
    through `email_buffer`, which is the digest this function's old docstring
    was already claiming to feed, and dead-letters anything it cannot deliver so
    the failure has an age instead of a silence.
    """
    return ml_digest.deliver(entries, source="ml_data_watchdog")


# ---------------------------------------------------------------------------
# Core run function
# ---------------------------------------------------------------------------

def _run() -> int:
    log.info("=" * 70)
    log.info("ML Data Watchdog starting — %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 70)

    # ------------------------------------------------------------------
    # Open DB connection (used by most checks)
    # ------------------------------------------------------------------
    if not DB_PATH.exists():
        log.error("ABORT: database not found at %s", DB_PATH)
        return 0

    try:
        conn = sqlite3.connect(str(DB_PATH))
        log.info("Connected to %s", DB_PATH)
    except Exception as exc:
        log.exception("Cannot open database: %s", exc)
        return 1

    # ------------------------------------------------------------------
    # Run all checks
    # ------------------------------------------------------------------
    checks: list[CheckResult] = []

    try:
        checks.append(check_label_balance(conn))
        checks.append(check_feature_null_rates(conn))
        checks.append(check_macro_cache_freshness())
        checks.append(check_vix_sanity())
        checks.append(check_date_continuity(conn))
        checks.append(check_rd_ml_history_freshness())
        checks.append(check_label_count_trend(conn))
    finally:
        conn.close()

    # ------------------------------------------------------------------
    # Determine overall status
    # ------------------------------------------------------------------
    statuses = [c["status"] for c in checks]

    if "CRITICAL" in statuses:
        overall_status = "CRITICAL"
    elif "WARNING" in statuses:
        overall_status = "WARNING"
    else:
        overall_status = "OK"

    log.info(
        "Overall status: %s  (%d checks — %d CRITICAL, %d WARNING, %d OK)",
        overall_status,
        len(checks),
        statuses.count("CRITICAL"),
        statuses.count("WARNING"),
        statuses.count("OK"),
    )

    # ------------------------------------------------------------------
    # Write full report
    # ------------------------------------------------------------------
    # ── Fable 5 triage — gated to CRITICAL/WARNING runs ───────────────────
    llm_triage = None
    if overall_status != "OK":
        try:
            from apex_llm import reason as _r, is_available
            if is_available("ml_improvement"):
                bad = [c for c in checks if c["status"] != "OK"]
                txt = "; ".join(f"[{c['status']}] {c['check_name']}: {c['detail'][:70]}"
                                for c in bad[:4])
                llm_triage = _r(
                    "You triage ML data-pipeline failures for a 13F prediction model.",
                    f"{txt}\n"
                    "2 sentences: do these failures share a root cause, and can the "
                    "model safely keep training/serving until fixed?",
                    agent_name="ml_improvement", max_tokens=110,
                )
        except Exception:
            pass

    report = {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "overall_status": overall_status,
        **({"llm_triage": llm_triage.strip()} if llm_triage else {}),
        "checks":         list(checks),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("Data quality report written to %s", REPORT_PATH)

    # ------------------------------------------------------------------
    # Alert email for any CRITICAL checks
    # ------------------------------------------------------------------
    critical_checks = [c for c in checks if c["status"] == "CRITICAL"]
    if critical_checks:
        n         = len(critical_checks)
        subject   = f"\U0001f6a8 Data Quality CRITICAL — {n} check{'s' if n > 1 else ''} failed"
        rows_html = "\n".join(
            f"""
            <tr>
              <td style="padding:6px 12px;color:#f85149;font-weight:bold;">CRITICAL</td>
              <td style="padding:6px 12px;font-weight:bold;">{c['check_name']}</td>
              <td style="padding:6px 12px;color:#8b949e;">{c['detail']}</td>
            </tr>"""
            for c in critical_checks
        )
        body = f"""
        <h3 style="color:#f85149;">Data Quality CRITICAL Alert</h3>
        <p>{n} critical data quality check(s) failed. Immediate investigation required.</p>
        <table style="border-collapse:collapse;font-size:13px;width:100%;">
          <thead>
            <tr style="border-bottom:1px solid #30363d;">
              <th style="padding:6px 12px;text-align:left;color:#8b949e;">Status</th>
              <th style="padding:6px 12px;text-align:left;color:#8b949e;">Check</th>
              <th style="padding:6px 12px;text-align:left;color:#8b949e;">Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
        <p style="color:#8b949e;margin-top:16px;font-size:12px;">
          Full report: <code>{REPORT_PATH}</code>
        </p>
        """
        sent = send_alert_email(subject, body)
        if sent:
            log.info("CRITICAL alert email sent successfully.")
        else:
            log.error("CRITICAL alert email FAILED to send — check email_sender logs.")

    # ------------------------------------------------------------------
    # Append WARNING checks to daily digest queue
    # ------------------------------------------------------------------
    warning_checks = [c for c in checks if c["status"] == "WARNING"]
    if warning_checks:
        ts = datetime.now(timezone.utc).isoformat()
        digest_entries = [
            {
                "source":     "ml_data_watchdog",
                "timestamp":  ts,
                "subject":    f"Data Quality WARNING: {c['check_name']}",
                "detail":     c,
            }
            for c in warning_checks
        ]
        _send_to_digest(digest_entries)

    log.info("ML Data Watchdog complete.")
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
