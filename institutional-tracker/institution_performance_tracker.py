#!/usr/bin/env python3
"""
APEX CAPITAL — INSTITUTION PERFORMANCE TRACKER
================================================
Breaks down ML prediction accuracy and raw pick performance by institution.

Answers questions like:
  - Which of our 8 tracked institutions has the highest win rate on their picks?
  - Which is most predictive (i.e., the model does best when their signal fires)?
  - How does each institution's trailing-90d performance compare to all-time?

Output
------
  data/institution_performance.json — per-institution stats + ranking
  Logs summary to STDOUT and logs/institution_tracker.log

Run
---
  python3 institution_performance_tracker.py
  python3 institution_performance_tracker.py --days 180  # widen lookback window

Used by
-------
  Operations Director briefings, board meeting notes, ml_improvement_agent.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
LOGS_DIR    = BASE_DIR / "logs"
DB_PATH     = DATA_DIR / "historical_13f.db"
REPORT_PATH = DATA_DIR / "institution_performance.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "institution_tracker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("institution_performance_tracker")


# ---------------------------------------------------------------------------
# Core stats computation
# ---------------------------------------------------------------------------

def _compute_stats(df: pd.DataFrame, label: str) -> dict:
    """
    Compute performance stats for a group of pipeline_snapshots rows.
    Requires outcome_20d to be non-null.
    """
    n = len(df)
    if n == 0:
        return {"n": 0, "label": label}

    outcomes = df["outcome_20d"].values
    wins     = outcomes > 0
    win_rate = float(wins.mean())
    mean_ret = float(outcomes.mean())
    std_ret  = float(outcomes.std()) if n > 1 else 0.0
    sharpe   = mean_ret / std_ret if std_ret > 0.01 else 0.0

    # Top 3 best and worst tickers by mean outcome
    if "ticker" in df.columns:
        per_ticker = (
            df.groupby("ticker")["outcome_20d"]
            .agg(["mean", "count"])
            .rename(columns={"mean": "mean_ret", "count": "n"})
            .query("n >= 2")
            .sort_values("mean_ret", ascending=False)
        )
        best_tickers  = per_ticker.head(3).reset_index().to_dict("records")
        worst_tickers = per_ticker.tail(3).sort_values("mean_ret").reset_index().to_dict("records")
    else:
        best_tickers  = []
        worst_tickers = []

    return {
        "label":          label,
        "n":              n,
        "win_rate":       round(win_rate,  4),
        "mean_ret_20d":   round(mean_ret,  5),
        "std_ret_20d":    round(std_ret,   5),
        "sharpe_like":    round(sharpe,    4),
        "best_tickers":   [{**t, "mean_ret": round(t["mean_ret"], 5)} for t in best_tickers],
        "worst_tickers":  [{**t, "mean_ret": round(t["mean_ret"], 5)} for t in worst_tickers],
    }


def _add_model_accuracy(stats: dict, df: pd.DataFrame, all_df: pd.DataFrame) -> dict:
    """
    If we have ML probability data in the snapshots, compute:
      - model_auc_vs_random: how well the model ranked this institution's picks
      - model_correct_pct:  fraction of picks where model predicted >50% and outcome > 0
                            (or model <50% and outcome <= 0)

    We approximate this from conviction_score as a proxy for model probability
    (the actual ML proba is not stored in pipeline_snapshots, but higher
    conviction_score → higher predicted probability).
    """
    if len(df) < 10 or "conviction_score" not in df.columns or "outcome_20d" not in df.columns:
        return stats

    try:
        from sklearn.metrics import roc_auc_score
        scores   = df["conviction_score"].fillna(0)
        labels   = (df["outcome_20d"] > 0).astype(int)
        if labels.nunique() < 2:
            return stats
        auc = float(roc_auc_score(labels, scores))
        stats["conviction_auc"] = round(auc, 4)
    except Exception:
        pass

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(lookback_days: int = 365, recent_days: int = 90) -> dict:
    log.info("=" * 70)
    log.info(
        "Institution Performance Tracker starting — lookback=%dd, recent=%dd",
        lookback_days, recent_days,
    )
    log.info("=" * 70)

    if not DB_PATH.exists():
        log.warning("DB not found at %s — skipping.", DB_PATH)
        return {}

    conn = sqlite3.connect(str(DB_PATH))

    try:
        all_df = pd.read_sql_query(
            f"""
            SELECT *
            FROM   pipeline_snapshots
            WHERE  outcome_20d IS NOT NULL
              AND  snapshot_date >= date('now', '-{lookback_days} days')
            """,
            conn,
        )
    except Exception as exc:
        log.exception("DB query failed: %s", exc)
        conn.close()
        return {}
    finally:
        conn.close()

    log.info("Loaded %d labeled rows from last %d days.", len(all_df), lookback_days)

    if len(all_df) == 0:
        log.warning("No labeled rows found. Check data_collector and outcome fill-in.")
        return {}

    # ------------------------------------------------------------------
    # 1. Overall baseline
    # ------------------------------------------------------------------
    overall = _compute_stats(all_df, "Overall")
    log.info(
        "Overall — N=%d  win_rate=%.1f%%  mean_ret=%.3f%%  sharpe=%.3f",
        overall["n"], overall["win_rate"] * 100,
        overall["mean_ret_20d"] * 100, overall["sharpe_like"],
    )

    # ------------------------------------------------------------------
    # 2. Per-institution stats
    # ------------------------------------------------------------------
    institutions: dict[str, dict] = {}

    if "institution" not in all_df.columns:
        log.warning("No 'institution' column in pipeline_snapshots — cannot break down by institution.")
    else:
        for inst_name, grp in all_df.groupby("institution"):
            if not inst_name or pd.isna(inst_name):
                continue

            stats = _compute_stats(grp, str(inst_name))
            stats = _add_model_accuracy(stats, grp, all_df)

            # Recent window stats
            recent_cutoff = f"snapshot_date >= date('now', '-{recent_days} days')"
            grp_recent = grp[
                pd.to_datetime(grp["snapshot_date"]) >=
                pd.Timestamp.now() - pd.Timedelta(days=recent_days)
            ]
            stats["recent"] = _compute_stats(grp_recent, f"{inst_name} (last {recent_days}d)")

            institutions[str(inst_name)] = stats

            log.info(
                "  %-22s  N=%4d  win=%.0f%%  μ=%.3f%%  sharpe=%.3f  conviction_auc=%s",
                inst_name,
                stats["n"],
                stats["win_rate"] * 100,
                stats["mean_ret_20d"] * 100,
                stats["sharpe_like"],
                f"{stats.get('conviction_auc', float('nan')):.4f}",
            )

    # ------------------------------------------------------------------
    # 3. Rankings
    # ------------------------------------------------------------------
    ranked_by_win_rate   = sorted(institutions, key=lambda k: -institutions[k].get("win_rate",   0))
    ranked_by_sharpe     = sorted(institutions, key=lambda k: -institutions[k].get("sharpe_like", 0))
    ranked_by_conviction = sorted(
        [k for k in institutions if "conviction_auc" in institutions[k]],
        key=lambda k: -institutions[k].get("conviction_auc", 0),
    )

    log.info("")
    log.info("Rankings by win rate:    %s", " > ".join(ranked_by_win_rate[:5]))
    log.info("Rankings by Sharpe:      %s", " > ".join(ranked_by_sharpe[:5]))
    if ranked_by_conviction:
        log.info("Rankings by conv. AUC:  %s", " > ".join(ranked_by_conviction[:5]))

    # ── Fable 5 institution read (one call/run) ───────────────────────────
    # The LLM knows these institutions' actual strategies — it can explain
    # WHY a manager's 13F signals work or don't in the current market.
    llm_read = None
    if institutions:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("institution_tracker"):
                top = ranked_by_sharpe[:3]
                bottom = ranked_by_sharpe[-2:] if len(ranked_by_sharpe) > 3 else []
                stats_txt = "; ".join(
                    f"{k}: win {institutions[k]['win_rate']*100:.0f}%, "
                    f"sharpe {institutions[k]['sharpe_like']:.2f}"
                    for k in (top + bottom))
                llm_read = _r(
                    "You analyze which institutional 13F filers produce tradeable signals.",
                    f"Baseline win rate {overall['win_rate']*100:.0f}%.\n"
                    f"Best/worst by Sharpe: {stats_txt}\n"
                    "2 sentences: given these managers' known strategies, why might the "
                    "leaders' filings predict well now, and whom should we weight up or down?",
                    agent_name="institution_tracker", max_tokens=140,
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 4. Save report
    # ------------------------------------------------------------------
    report = {
        **({"llm_institution_read": llm_read.strip()} if llm_read else {}),
        "timestamp":             datetime.now(timezone.utc).isoformat(),
        "lookback_days":         lookback_days,
        "recent_days":           recent_days,
        "overall":               overall,
        "institutions":          institutions,
        "ranking_win_rate":      ranked_by_win_rate,
        "ranking_sharpe":        ranked_by_sharpe,
        "ranking_conviction_auc": ranked_by_conviction,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("\nInstitution performance report written → %s", REPORT_PATH)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apex Capital — Institution Performance Tracker")
    p.add_argument(
        "--days", type=int, default=365,
        help="All-time lookback window in days (default: 365)",
    )
    p.add_argument(
        "--recent", type=int, default=90,
        help="Recent window in days for trailing comparison (default: 90)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(lookback_days=args.days, recent_days=args.recent)
