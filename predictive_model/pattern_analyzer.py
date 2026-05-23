#!/usr/bin/env python3
"""
APEX CAPITAL — PATTERN ANALYZER
=================================
Statistical analysis of 13F filing patterns across tracked institutions.
Answers: "Which patterns in institutional filing behavior have historically
predicted strong 20-day price appreciation?"

Analysis modules
----------------
  1. Filing Speed Analysis
     → Institutions that file faster (closer to 45-day deadline) tend to
       have fresher, more actionable information.

  2. Herding Analysis
     → When 3+ institutions add the same stock in the same quarter,
       does that predict outperformance?

  3. Quant Consensus Analysis
     → When Renaissance + Two Sigma (or Two Sigma + D.E. Shaw) both hold,
       does the quant overlap predict outperformance?

  4. Position Size Signal
     → Large new positions (> 1% of fund AUM in value_usd) vs. small ones.

  5. Hold Streak Decay
     → Does a stock that's been held for many quarters start to revert?

  6. Quarter Seasonality
     → Do Q1 picks (filed in May) outperform Q3 picks (filed in November)?

  7. Institution-specific Hit Rates
     → Which of our 8 funds has the highest 20d forward return per signal?

Output
------
  data/pattern_analysis.json   — summary statistics and rankings
  Printed to stdout if run directly.

Usage
-----
  python3 predictive_model/pattern_analyzer.py
  from predictive_model.pattern_analyzer import run_analysis
"""

import sys
import json
import logging
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import get_db
from predictive_model.data_collector import _ensure_collector_tables

logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "pattern_analysis.json"


# =============================================================================
# HELPERS
# =============================================================================

def _load_snapshots(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql(
        """SELECT ticker, institution, snapshot_date, conviction_score,
                  n_institutions, herding_flag, quant_consensus,
                  days_since_filing, portfolio_weight,
                  outcome_1d, outcome_5d, outcome_20d
           FROM pipeline_snapshots
           WHERE outcome_20d IS NOT NULL""",
        conn,
    )
    if not df.empty:
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
        df["quarter"] = df["snapshot_date"].dt.quarter
    return df


def _mean_return_ci(series: pd.Series) -> dict:
    """Return mean, std, N, and a rough 95% CI."""
    n = len(series)
    if n == 0:
        return {"mean": None, "std": None, "n": 0, "ci_low": None, "ci_high": None}
    m   = float(series.mean())
    std = float(series.std())
    ci  = 1.96 * std / (n ** 0.5) if n > 1 else 0.0
    return {"mean": round(m, 5), "std": round(std, 5), "n": n,
            "ci_low": round(m - ci, 5), "ci_high": round(m + ci, 5)}


def _win_rate(series: pd.Series) -> float:
    if len(series) == 0:
        return 0.0
    return round(float((series > 0).mean()), 4)


# =============================================================================
# ANALYSIS MODULES
# =============================================================================

def analyze_herding(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    herded    = df[df["herding_flag"] == 1]["outcome_20d"]
    not_herd  = df[df["herding_flag"] == 0]["outcome_20d"]
    return {
        "herded":     {**_mean_return_ci(herded),     "win_rate": _win_rate(herded)},
        "not_herded": {**_mean_return_ci(not_herd),   "win_rate": _win_rate(not_herd)},
        "edge": round(float(herded.mean() - not_herd.mean()), 5) if len(herded) and len(not_herd) else None,
        "verdict": "HERDING_PREDICTIVE" if (
            len(herded) >= 5 and len(not_herd) >= 5 and
            herded.mean() > not_herd.mean()
        ) else "INSUFFICIENT_DATA" if len(herded) < 5 else "NO_EDGE",
    }


def analyze_quant_consensus(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    quant = df[df["quant_consensus"] == 1]["outcome_20d"]
    other = df[df["quant_consensus"] == 0]["outcome_20d"]
    return {
        "quant_overlap":  {**_mean_return_ci(quant), "win_rate": _win_rate(quant)},
        "no_overlap":     {**_mean_return_ci(other),  "win_rate": _win_rate(other)},
        "edge": round(float(quant.mean() - other.mean()), 5) if len(quant) and len(other) else None,
        "verdict": "QUANT_CONSENSUS_PREDICTIVE" if (
            len(quant) >= 5 and quant.mean() > other.mean()
        ) else "INSUFFICIENT_DATA" if len(quant) < 5 else "NO_EDGE",
    }


def analyze_filing_speed(df: pd.DataFrame) -> dict:
    """Quartile split on days_since_filing."""
    if df.empty or "days_since_filing" not in df.columns:
        return {}
    df = df.dropna(subset=["days_since_filing", "outcome_20d"])
    if len(df) < 20:
        return {"verdict": "INSUFFICIENT_DATA", "n": len(df)}

    q25 = df["days_since_filing"].quantile(0.25)
    q75 = df["days_since_filing"].quantile(0.75)

    fresh = df[df["days_since_filing"] <= q25]["outcome_20d"]
    stale = df[df["days_since_filing"] >= q75]["outcome_20d"]

    return {
        "fresh_filings": {**_mean_return_ci(fresh), "win_rate": _win_rate(fresh),
                          "threshold_days": round(q25, 1)},
        "stale_filings": {**_mean_return_ci(stale), "win_rate": _win_rate(stale),
                          "threshold_days": round(q75, 1)},
        "edge": round(float(fresh.mean() - stale.mean()), 5) if len(fresh) and len(stale) else None,
        "verdict": "FRESHNESS_PREDICTIVE" if (
            len(fresh) >= 5 and fresh.mean() > stale.mean()
        ) else "INSUFFICIENT_DATA" if len(fresh) < 5 else "NO_EDGE",
    }


def analyze_conviction_buckets(df: pd.DataFrame) -> dict:
    """Split into low/med/high conviction score buckets."""
    if df.empty:
        return {}
    df = df.dropna(subset=["conviction_score", "outcome_20d"])
    if len(df) < 20:
        return {"verdict": "INSUFFICIENT_DATA", "n": len(df)}

    low  = df[df["conviction_score"] <  df["conviction_score"].quantile(0.33)]["outcome_20d"]
    med  = df[(df["conviction_score"] >= df["conviction_score"].quantile(0.33)) &
              (df["conviction_score"] <  df["conviction_score"].quantile(0.67))]["outcome_20d"]
    high = df[df["conviction_score"] >= df["conviction_score"].quantile(0.67)]["outcome_20d"]

    return {
        "low_conviction":  {**_mean_return_ci(low),  "win_rate": _win_rate(low)},
        "med_conviction":  {**_mean_return_ci(med),  "win_rate": _win_rate(med)},
        "high_conviction": {**_mean_return_ci(high), "win_rate": _win_rate(high)},
        "verdict": "CONVICTION_PREDICTIVE" if (
            len(high) >= 5 and high.mean() > low.mean()
        ) else "INSUFFICIENT_DATA" if len(high) < 5 else "NO_EDGE",
    }


def analyze_institution_hit_rates(df: pd.DataFrame) -> dict:
    """Per-institution mean 20d return and win rate."""
    if df.empty:
        return {}
    out = {}
    for inst, grp in df.groupby("institution"):
        out[inst] = {
            **_mean_return_ci(grp["outcome_20d"]),
            "win_rate": _win_rate(grp["outcome_20d"]),
        }

    ranked = sorted(out.items(), key=lambda x: x[1].get("mean") or -999, reverse=True)
    return {
        "by_institution": out,
        "ranking": [k for k, _ in ranked],
        "verdict": "OK" if out else "INSUFFICIENT_DATA",
    }


def analyze_seasonality(df: pd.DataFrame) -> dict:
    """By quarter of year."""
    if df.empty or "quarter" not in df.columns:
        return {}
    out = {}
    for q in [1, 2, 3, 4]:
        grp = df[df["quarter"] == q]["outcome_20d"]
        out[f"Q{q}"] = {**_mean_return_ci(grp), "win_rate": _win_rate(grp)}
    return out


def analyze_n_institutions(df: pd.DataFrame) -> dict:
    """Group by n_institutions: 1, 2, 3, 4+"""
    if df.empty:
        return {}
    out = {}
    for n in [1, 2, 3]:
        grp = df[df["n_institutions"] == n]["outcome_20d"]
        out[f"n={n}"] = {**_mean_return_ci(grp), "win_rate": _win_rate(grp)}
    grp4 = df[df["n_institutions"] >= 4]["outcome_20d"]
    out["n=4+"] = {**_mean_return_ci(grp4), "win_rate": _win_rate(grp4)}

    best_n = max(out.items(), key=lambda x: x[1].get("mean") or -999)[0]
    return {"by_n_institutions": out, "best_group": best_n}


# =============================================================================
# MAIN
# =============================================================================

def run_analysis() -> dict:
    conn = get_db()
    _ensure_collector_tables(conn)
    df = _load_snapshots(conn)
    conn.close()

    n_labeled = len(df)
    logger.info(f"  pattern_analyzer: {n_labeled} labeled snapshots")

    if n_labeled < 20:
        result = {
            "generated_at": datetime.now().isoformat(),
            "n_labeled": n_labeled,
            "status": "INSUFFICIENT_DATA",
            "message": f"Need ~50 labeled rows to draw conclusions. Have {n_labeled}.",
        }
    else:
        result = {
            "generated_at":       datetime.now().isoformat(),
            "n_labeled":          n_labeled,
            "status":             "OK",
            "herding":            analyze_herding(df),
            "quant_consensus":    analyze_quant_consensus(df),
            "filing_speed":       analyze_filing_speed(df),
            "conviction_buckets": analyze_conviction_buckets(df),
            "institution_rates":  analyze_institution_hit_rates(df),
            "seasonality":        analyze_seasonality(df),
            "n_institutions":     analyze_n_institutions(df),
        }

        # Summary verdict
        verdicts = [
            result["herding"].get("verdict", ""),
            result["quant_consensus"].get("verdict", ""),
            result["filing_speed"].get("verdict", ""),
            result["conviction_buckets"].get("verdict", ""),
        ]
        predictive_count = sum(1 for v in verdicts if "PREDICTIVE" in v)
        result["summary"] = {
            "predictive_patterns": predictive_count,
            "total_patterns_tested": len(verdicts),
            "confidence": "HIGH" if predictive_count >= 3 else "MEDIUM" if predictive_count >= 2 else "LOW",
        }

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2, default=str)

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    report = run_analysis()
    print(json.dumps(report, indent=2, default=str))
