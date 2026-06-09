#!/usr/bin/env python3
"""
APEX CAPITAL — ML IMPROVEMENT AGENT (RULE-BASED)
==================================================
Runs 4× nightly (GCP cron: 0 1,3,5,7 * * *).
Analyzes current model performance, data quality, feature coverage,
and generates concrete improvement suggestions using deterministic rules.
No external API calls — fully self-contained on GCP.

Outputs to data/ml_suggestions.json:
  [{"id", "title", "rationale", "category": "IMPLEMENT|REVIEW|DISCARD",
    "estimated_auc_delta", "effort", "status": "PENDING|DONE|EMAILED|DISCARDED",
    "created_at", "implemented_at"}]

Daily email digest (6 AM UTC) — batches all PENDING REVIEW items into one email.
IMPLEMENT items are queued for Claude's next session review.
DISCARD items are logged but not emailed.
"""

import json
import logging
import os
import pickle
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from email_sender import send_alert_email

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
SUGGESTIONS_FILE = DATA_DIR / "ml_suggestions.json"
LAST_EMAIL_FILE = DATA_DIR / "ml_last_email.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ml_improvement_agent")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CURRENT_MODEL_VERSION = "v5"
CURRENT_AUC = 0.59

FEATURE_COLUMNS = {
    "base": [
        "conviction_score", "n_institutions", "days_since_filing", "herding_flag",
        "quant_consensus", "portfolio_weight", "price_momentum_20d", "amendment_flag",
        "position_size_pct_aum", "sector_rotation_score", "days_since_ipo",
        "short_interest_pct", "price_vs_52w_high", "filing_quarter_consensus",
        "insider_buy_flag", "insider_net_shares_30d", "risk_adjusted_20d",
        "volatility_20d", "signal_type_*", "sector_*",
    ],
    "macro": [
        "vix_level", "vix_regime", "yield_spread_10y3m", "yield_inverted",
        "spy_momentum_60d", "spy_momentum_20d", "market_regime",
        "qqq_spy_ratio_20d", "xlu_spy_ratio_20d",
    ],
    "rd": [
        "rd_macro_regime", "rd_sector_breadth", "rd_cro_risk_level",
        "rd_intl_sentiment", "rd_intl_score_raw", "rd_n_high_conviction",
        "rd_avg_conviction",
    ],
}

# All known feature column names (flat list for membership checks)
ALL_KNOWN_FEATURES: list[str] = (
    FEATURE_COLUMNS["base"] + FEATURE_COLUMNS["macro"] + FEATURE_COLUMNS["rd"]
)

# Threshold below which a feature importance is considered negligible
LOW_IMPORTANCE_THRESHOLD = 0.005
FEATURE_COUNT_THRESHOLD_FOR_DROP = 35
LABELED_COUNT_THRESHOLD = 30_000
OVERFITTING_GAP_THRESHOLD = 0.05
IMBALANCE_HIGH_THRESHOLD = 0.60
IMBALANCE_LOW_THRESHOLD = 0.40
DATA_STALENESS_DAYS = 90


# ---------------------------------------------------------------------------
# Suggestion Loading / Saving
# ---------------------------------------------------------------------------

def load_suggestions() -> list[dict]:
    """Load existing suggestions from disk, returning empty list on any error."""
    if not SUGGESTIONS_FILE.exists():
        return []
    try:
        with open(SUGGESTIONS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("ml_suggestions.json is not a list — resetting to []")
            return []
        return data
    except Exception as e:
        logger.error(f"Failed to load suggestions file: {e}")
        return []


def save_suggestions(suggestions: list[dict]) -> None:
    """Persist suggestions list to disk atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = SUGGESTIONS_FILE.with_suffix(".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(suggestions, f, indent=2, default=str)
        tmp_path.replace(SUGGESTIONS_FILE)
        logger.info(f"Saved {len(suggestions)} suggestions to {SUGGESTIONS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save suggestions: {e}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# ID Generation
# ---------------------------------------------------------------------------

def next_suggestion_id(suggestions: list[dict]) -> str:
    """Generate the next sequential suggestion ID in format mlsugg-YYYY-NNN."""
    year = datetime.now(timezone.utc).strftime("%Y")
    existing_nums: list[int] = []
    pattern = re.compile(rf"^mlsugg-{year}-(\d+)$")
    for s in suggestions:
        m = pattern.match(s.get("id", ""))
        if m:
            existing_nums.append(int(m.group(1)))
    next_num = max(existing_nums, default=0) + 1
    return f"mlsugg-{year}-{next_num:03d}"


# ---------------------------------------------------------------------------
# Context Gathering
# ---------------------------------------------------------------------------

def extract_model_features(model_path: Path) -> dict[str, Any]:
    """
    Attempt to load model_v3.pkl and extract feature importances, AUC,
    and per-feature metadata (names if available, importance scores).
    Returns a dict with whatever we can recover; never raises.
    """
    result: dict[str, Any] = {
        "auc": CURRENT_AUC,
        "val_auc": None,
        "wf_cv_auc": None,
        "version": CURRENT_MODEL_VERSION,
        "feature_names": [],
        "feature_importances": {},   # name -> float
        "top_features": [],
        "n_features": None,
        "load_error": None,
    }
    if not model_path.exists():
        result["load_error"] = f"Model file not found: {model_path}"
        logger.warning(result["load_error"])
        return result
    try:
        with open(model_path, "rb") as f:
            model_obj = pickle.load(f)

        # Support dict-wrapped models: {"model": clf, "auc": 0.59, ...}
        # Also supports v2/v3/v4 bundles with fitted_bases dict + meta_model.
        if isinstance(model_obj, dict):
            # v2/v3/v4 bundle: extract the best base model for importance analysis
            if "fitted_bases" in model_obj:
                bases = model_obj["fitted_bases"]
                clf = bases.get("lgb") or bases.get("rf") or bases.get("xgb") or bases.get("lr")
                result["version"] = model_obj.get("version", CURRENT_MODEL_VERSION)
            else:
                clf = model_obj.get("model", model_obj.get("clf"))
            result["auc"] = float(model_obj.get("auc", CURRENT_AUC))
            result["val_auc"] = model_obj.get("val_auc") or model_obj.get("ensemble_auc_val") or model_obj.get("auc")
            result["wf_cv_auc"] = model_obj.get("wf_cv_auc") or model_obj.get("wf_cv_mean_auc") or model_obj.get("walk_forward_auc")
            # Feature names may be stored alongside the model
            stored_names = model_obj.get("feature_names") or model_obj.get("feature_cols") or model_obj.get("features")
            if isinstance(stored_names, list):
                result["feature_names"] = [str(n) for n in stored_names]
        else:
            clf = model_obj

        if clf is None:
            result["load_error"] = "Could not locate classifier inside pickle"
            return result

        # Extract feature importances (sklearn / LightGBM / XGBoost style)
        if hasattr(clf, "feature_importances_"):
            importances = list(clf.feature_importances_)
            result["n_features"] = len(importances)

            # Attempt to pair with feature names
            names = result["feature_names"]
            if not names and hasattr(clf, "feature_name_"):
                names = list(clf.feature_name_())
            if not names and hasattr(clf, "get_booster"):
                try:
                    names = list(clf.get_booster().feature_names or [])
                except Exception:
                    pass
            if not names:
                names = [f"feature_{i}" for i in range(len(importances))]

            result["feature_names"] = names
            result["feature_importances"] = {
                names[i]: float(importances[i]) for i in range(len(importances))
            }

            top_idx = sorted(range(len(importances)), key=lambda i: importances[i], reverse=True)[:10]
            result["top_features"] = [
                {
                    "rank": rank + 1,
                    "name": names[idx],
                    "importance": round(float(importances[idx]), 4),
                }
                for rank, idx in enumerate(top_idx)
            ]
        elif hasattr(clf, "coef_"):
            coef = clf.coef_
            if hasattr(coef, "flatten"):
                coef = list(coef.flatten())
            result["n_features"] = len(coef)
            names = result["feature_names"] or [f"feature_{i}" for i in range(len(coef))]
            result["feature_names"] = names
            result["feature_importances"] = {
                names[i]: abs(float(coef[i])) for i in range(len(coef))
            }
            top_idx = sorted(range(len(coef)), key=lambda i: abs(float(coef[i])), reverse=True)[:10]
            result["top_features"] = [
                {
                    "rank": rank + 1,
                    "name": names[idx],
                    "coef": round(float(coef[idx]), 4),
                }
                for rank, idx in enumerate(top_idx)
            ]

        logger.info(
            f"Model loaded: AUC={result['auc']}, val_auc={result['val_auc']}, "
            f"wf_cv_auc={result['wf_cv_auc']}, n_features={result['n_features']}"
        )
    except Exception as e:
        result["load_error"] = str(e)
        logger.error(f"Error loading model from {model_path}: {e}")

    return result


def query_database_context(db_path: Path) -> dict[str, Any]:
    """
    Query historical_13f.db for sample count, date range, and label distribution.
    Returns a summary dict; never raises.
    """
    result: dict[str, Any] = {
        "total_samples": None,
        "labeled_samples": None,
        "date_range": None,
        "latest_snapshot_date": None,
        "label_distribution": None,
        "positive_rate": None,
        "db_error": None,
    }
    if not db_path.exists():
        result["db_error"] = f"Database not found: {db_path}"
        logger.warning(result["db_error"])
        return result

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Discover tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        logger.info(f"DB tables found: {tables}")

        # Prefer tables with ML-relevant keywords
        candidate_tables = [t for t in tables if any(
            kw in t.lower() for kw in ("signal", "label", "filing", "sample", "training", "ml")
        )]
        if not candidate_tables:
            candidate_tables = tables

        for table in candidate_tables[:3]:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
                count = cursor.fetchone()[0]
                result["total_samples"] = count

                cursor.execute(f"PRAGMA table_info([{table}])")
                cols = [row[1] for row in cursor.fetchall()]

                label_col = next(
                    (c for c in cols if c.lower() in ("label", "target", "outcome", "hit", "y")),
                    None,
                )
                date_col = next(
                    (c for c in cols if "date" in c.lower() or "filing" in c.lower()),
                    None,
                )

                if label_col:
                    cursor.execute(
                        f"SELECT [{label_col}], COUNT(*) FROM [{table}] "
                        f"WHERE [{label_col}] IS NOT NULL GROUP BY [{label_col}]"
                    )
                    dist = {str(row[0]): row[1] for row in cursor.fetchall()}
                    result["labeled_samples"] = sum(dist.values())
                    result["label_distribution"] = dist

                    # Compute positive rate (label == 1 or "1")
                    pos_count = dist.get("1", dist.get("1.0", dist.get("True", 0)))
                    total_labeled = result["labeled_samples"]
                    if total_labeled and total_labeled > 0:
                        result["positive_rate"] = float(pos_count) / float(total_labeled)

                if date_col:
                    cursor.execute(
                        f"SELECT MIN([{date_col}]), MAX([{date_col}]) FROM [{table}]"
                    )
                    row = cursor.fetchone()
                    if row:
                        result["date_range"] = {"min": str(row[0]), "max": str(row[1])}
                        result["latest_snapshot_date"] = str(row[1])

                if result["labeled_samples"] is not None:
                    break
            except Exception as e:
                logger.debug(f"Skipping table {table}: {e}")
                continue

        conn.close()
    except Exception as e:
        result["db_error"] = str(e)
        logger.error(f"DB query failed: {e}")

    return result


def load_rd_regime_context(rd_path: Path) -> dict[str, Any]:
    """Load a summary from rd_ml_history.json, including all macro regimes seen."""
    result: dict[str, Any] = {
        "recent_regimes": [],
        "all_macro_regimes": [],
        "n_entries": 0,
        "load_error": None,
    }
    if not rd_path.exists():
        result["load_error"] = f"File not found: {rd_path}"
        logger.warning(result["load_error"])
        return result
    try:
        with open(rd_path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            result["n_entries"] = len(data)
            recent = data[-5:] if len(data) >= 5 else data
            result["recent_regimes"] = [
                {k: v for k, v in entry.items() if k in (
                    "date", "macro_regime", "sector_breadth", "cro_risk_level",
                    "avg_conviction", "n_high_conviction",
                )}
                for entry in recent
            ]
            # Collect all unique macro_regime values from the full history
            all_regimes = set()
            for entry in data:
                regime = entry.get("macro_regime") or entry.get("rd_macro_regime")
                if regime:
                    all_regimes.add(str(regime).upper())
            result["all_macro_regimes"] = sorted(all_regimes)
        elif isinstance(data, dict):
            result["n_entries"] = 1
            result["recent_regimes"] = [data]
            regime = data.get("macro_regime") or data.get("rd_macro_regime")
            if regime:
                result["all_macro_regimes"] = [str(regime).upper()]
    except Exception as e:
        result["load_error"] = str(e)
        logger.error(f"Failed to load rd_ml_history: {e}")
    return result


def scan_log_warnings(logs_dir: Path) -> list[str]:
    """
    Scan today's and yesterday's run_*.log files for ML-related warnings/errors.
    Returns a deduplicated list of relevant log lines (capped at 30).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    ml_keywords = re.compile(
        r"(AUC|accuracy|precision|recall|f1|feature|model|train|predict|overfit|"
        r"data leak|label|class imbalance|nan|inf|missing|impute|scaler|"
        r"ValueError|KeyError|MemoryError|convergence|sklearn)",
        re.IGNORECASE,
    )

    relevant_lines: list[str] = []
    if not logs_dir.exists():
        return relevant_lines

    try:
        log_files = sorted(logs_dir.glob("run_*.log"), reverse=True)[:10]
        for log_file in log_files:
            try:
                mtime = datetime.fromtimestamp(log_file.stat().st_mtime).strftime("%Y-%m-%d")
                if mtime not in (today, yesterday):
                    continue
            except Exception:
                continue
            try:
                with open(log_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.rstrip()
                        if ml_keywords.search(line) and (
                            "error" in line.lower()
                            or "warning" in line.lower()
                            or "warn" in line.lower()
                        ):
                            relevant_lines.append(f"[{log_file.name}] {line}")
                            if len(relevant_lines) >= 30:
                                break
            except Exception as e:
                logger.debug(f"Could not read {log_file}: {e}")
    except Exception as e:
        logger.warning(f"Log scan error: {e}")

    return list(dict.fromkeys(relevant_lines))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# Deduplication Helpers
# ---------------------------------------------------------------------------

def build_existing_titles(suggestions: list[dict]) -> set[str]:
    """Return lowercased titles from existing suggestions for duplicate detection."""
    return {s.get("title", "").lower().strip() for s in suggestions}


def _is_duplicate(title: str, existing_titles: set[str]) -> bool:
    """
    Return True if `title` substantially overlaps with any existing title.
    Uses case-insensitive substring matching in both directions.
    """
    t = title.lower().strip()
    for existing in existing_titles:
        if t in existing or existing in t:
            return True
    return False


# ---------------------------------------------------------------------------
# Rule-Based Suggestion Generator
# ---------------------------------------------------------------------------

def _make_suggestion(
    *,
    title: str,
    rationale: str,
    category: str,
    estimated_auc_delta: str,
    effort: str,
) -> dict[str, Any]:
    """Construct a raw suggestion dict (no id/status/timestamps yet)."""
    assert category in ("IMPLEMENT", "REVIEW", "DISCARD"), f"Bad category: {category}"
    return {
        "title": title,
        "rationale": rationale,
        "category": category,
        "estimated_auc_delta": estimated_auc_delta,
        "effort": effort,
    }


def generate_rule_based_suggestions(context: dict) -> list[dict]:
    """
    Apply all analytical rules against the gathered context dict and return
    a list of raw suggestion dicts (no ids/timestamps — those are assigned
    later in assemble_suggestions).

    Context keys expected:
      model_info   — from extract_model_features()
      db_info      — from query_database_context()
      rd_info      — from load_rd_regime_context()
      log_warnings — list[str] from scan_log_warnings()
    """
    model_info: dict = context.get("model_info", {})
    db_info: dict = context.get("db_info", {})
    rd_info: dict = context.get("rd_info", {})

    suggestions: list[dict] = []

    feature_names: list[str] = model_info.get("feature_names", []) or []
    feature_importances: dict[str, float] = model_info.get("feature_importances", {}) or {}
    n_features: int = model_info.get("n_features") or len(feature_names)
    val_auc = model_info.get("val_auc")
    wf_cv_auc = model_info.get("wf_cv_auc")

    labeled_count = db_info.get("labeled_samples")
    positive_rate = db_info.get("positive_rate")
    latest_snapshot_str = db_info.get("latest_snapshot_date")

    all_macro_regimes: list[str] = rd_info.get("all_macro_regimes", []) or []

    # --- Rule 1: Sample size --------------------------------------------------
    if labeled_count is not None and labeled_count < LABELED_COUNT_THRESHOLD:
        additional_per_fund = 5 * 2_000  # 5 hedge funds × ~500 holdings × 4 quarters
        projected_total = labeled_count + additional_per_fund
        suggestions.append(_make_suggestion(
            title="Expand tracked institutions to double labeled samples",
            rationale=(
                f"Current labeled sample count is {labeled_count:,}, below the {LABELED_COUNT_THRESHOLD:,} "
                f"threshold needed for stable gradient boosting. Adding 5 more hedge funds to the "
                f"tracking universe would yield approximately {additional_per_fund:,} additional labeled "
                f"rows (each 13F filing covers ~500 holdings × 4 quarters = 2,000 new rows per institution), "
                f"bringing the total to ~{projected_total:,}. More labeled data directly reduces variance "
                f"and narrows confidence intervals on AUC estimates."
            ),
            category="REVIEW",
            estimated_auc_delta="+0.005 to +0.020 (more data reduces variance)",
            effort="MEDIUM — requires identifying 5 suitable hedge funds, updating institution watchlist, "
                   "re-running the EDGAR scraper, and re-labeling with price_outcomes table",
        ))
        logger.info(f"Rule 1 fired: labeled_count={labeled_count} < {LABELED_COUNT_THRESHOLD}")

    # --- Rule 2: Overfitting gap ----------------------------------------------
    if val_auc is not None and wf_cv_auc is not None:
        try:
            gap = float(val_auc) - float(wf_cv_auc)
            if gap > OVERFITTING_GAP_THRESHOLD:
                suggestions.append(_make_suggestion(
                    title="Reduce feature count to close overfitting gap",
                    rationale=(
                        f"Validation AUC ({val_auc:.3f}) exceeds walk-forward cross-validation AUC "
                        f"({wf_cv_auc:.3f}) by {gap:.3f} — above the {OVERFITTING_GAP_THRESHOLD} "
                        f"threshold that indicates overfitting to held-out validation data. With a "
                        f"limited labeled set, too many features allow the model to memorize recent "
                        f"regimes. Dropping low-importance features (importance < {LOW_IMPORTANCE_THRESHOLD}) "
                        f"and applying stricter L1/L2 regularization should close this gap and improve "
                        f"out-of-sample reliability."
                    ),
                    category="REVIEW",
                    estimated_auc_delta="+0.005 to +0.030 on out-of-sample data (more reliable signal)",
                    effort="MEDIUM — audit feature importances, drop candidates, retrain with updated "
                           "regularization hyperparameters, re-evaluate walk-forward CV",
                ))
                logger.info(f"Rule 2 fired: val_auc={val_auc:.3f}, wf_cv_auc={wf_cv_auc:.3f}, gap={gap:.3f}")
        except (TypeError, ValueError) as e:
            logger.warning(f"Rule 2: could not compute overfitting gap: {e}")

    # --- Rule 3: Earnings feature gap -----------------------------------------
    has_earnings_feature = any(
        "earnings_surprise" in fn.lower() for fn in feature_names
    ) or any(
        "earnings_surprise" in fn.lower() for fn in ALL_KNOWN_FEATURES
    )
    # Treat as missing unless explicitly present in the loaded model's feature names
    has_earnings_in_model = any("earnings_surprise" in fn.lower() for fn in feature_names)
    if not has_earnings_in_model:
        suggestions.append(_make_suggestion(
            title="Add earnings surprise binary feature from R&D reports",
            rationale=(
                "The model does not currently include an earnings_surprise feature. R&D agents "
                "already flag BEAT/MISS results and store them in rd_ml_history.json under the "
                "'top_picks' field (which includes 'action': BUY/SELL and conviction scores). "
                "Encoding earnings surprise as +1 (BEAT) / -1 (MISS) at the filing date, keyed "
                "to the fiscal quarter end, would give the model direct access to fundamental "
                "momentum that correlates strongly with post-filing price moves."
            ),
            category="REVIEW",
            estimated_auc_delta="+0.003 to +0.015 (earnings surprise is a known return predictor)",
            effort="LOW — rd_ml_history.json top_picks field has 'action' (BUY/SELL) and conviction; "
                   "earnings BEAT filings should be labeled separately; join on ticker + quarter",
        ))
        logger.info("Rule 3 fired: earnings_surprise not in model feature names")

    # --- Rule 4: Low-importance features --------------------------------------
    if feature_importances and n_features and n_features > FEATURE_COUNT_THRESHOLD_FOR_DROP:
        low_importance_features = [
            name for name, imp in feature_importances.items()
            if imp < LOW_IMPORTANCE_THRESHOLD
        ]
        if low_importance_features:
            suggestions.append(_make_suggestion(
                title="Drop zero-weight features to reduce noise",
                rationale=(
                    f"The model has {n_features} features, {len(low_importance_features)} of which "
                    f"have importance < {LOW_IMPORTANCE_THRESHOLD}: "
                    f"{', '.join(low_importance_features[:10])}"
                    f"{'...' if len(low_importance_features) > 10 else ''}. "
                    f"Low-importance features add noise to the gradient boosting splits without "
                    f"contributing signal, increasing variance and slowing training. Dropping them "
                    f"reduces model complexity and typically improves generalization."
                ),
                category="IMPLEMENT",
                estimated_auc_delta="+0.002 to +0.008 (noise reduction)",
                effort="LOW — remove identified columns from feature list in training config, retrain",
            ))
            logger.info(
                f"Rule 4 fired: {len(low_importance_features)} features below importance "
                f"threshold (n_features={n_features})"
            )

    # --- Rule 5: Class imbalance ----------------------------------------------
    if positive_rate is not None:
        if positive_rate > IMBALANCE_HIGH_THRESHOLD or positive_rate < IMBALANCE_LOW_THRESHOLD:
            direction = "high" if positive_rate > IMBALANCE_HIGH_THRESHOLD else "low"
            suggestions.append(_make_suggestion(
                title=f"Address class imbalance — {positive_rate:.0%} positive rate",
                rationale=(
                    f"The labeled dataset has a {positive_rate:.1%} positive rate, which is "
                    f"{'above' if direction == 'high' else 'below'} the balanced range of "
                    f"{IMBALANCE_LOW_THRESHOLD:.0%}–{IMBALANCE_HIGH_THRESHOLD:.0%}. "
                    f"Extreme class imbalance biases gradient boosting toward the majority class, "
                    f"inflating accuracy while degrading precision/recall on the minority class. "
                    f"Setting class_weight='balanced' in the estimator or applying SMOTE "
                    f"oversampling during training would correct this without discarding data."
                ),
                category="REVIEW",
                estimated_auc_delta="+0.003 to +0.010",
                effort="LOW to MEDIUM — try class_weight='balanced' first (one-line change); "
                       "SMOTE requires imbalanced-learn package and train-set-only application",
            ))
            logger.info(f"Rule 5 fired: positive_rate={positive_rate:.3f}")

    # --- Rule 6: Data staleness -----------------------------------------------
    if latest_snapshot_str:
        try:
            # Try common date formats
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
                try:
                    latest_dt = datetime.strptime(latest_snapshot_str[:10], "%Y-%m-%d")
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"Cannot parse date: {latest_snapshot_str}")

            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            days_stale = (now_utc - latest_dt).days
            if days_stale > DATA_STALENESS_DAYS:
                suggestions.append(_make_suggestion(
                    title=f"Label builder needed — data is {days_stale} days stale",
                    rationale=(
                        f"The most recent training snapshot is dated {latest_snapshot_str[:10]}, "
                        f"which is {days_stale} days ago. Models trained on stale data may not "
                        f"capture current market regimes. Price outcomes can be labeled up to "
                        f"today minus 20 calendar days (the 20-day forward return window), so "
                        f"there are potentially {max(0, days_stale - 20)} days of unlabeled "
                        f"but labelable data sitting in the database. Running the label builder "
                        f"would expand the training set and inject fresher market regime data."
                    ),
                    category="REVIEW",
                    estimated_auc_delta="+0.005 to +0.015 (fresher market regime data)",
                    effort="MEDIUM — trigger label_builder.py with updated date range; "
                           "verify price_outcomes table covers the gap period; retrain",
                ))
                logger.info(f"Rule 6 fired: data is {days_stale} days stale (latest={latest_snapshot_str[:10]})")
        except Exception as e:
            logger.warning(f"Rule 6: could not parse latest snapshot date '{latest_snapshot_str}': {e}")

    # --- Rule 7: Options data (always DISCARD) --------------------------------
    suggestions.append(_make_suggestion(
        title="Add options market put/call ratio feature",
        rationale=(
            "Options market put/call ratios provide a sentiment signal that can lead equity "
            "price moves. However, this requires a paid options data feed not currently "
            "available in the Apex Capital data stack. The CBOE free tier provides only "
            "15-minute delayed data, which introduces look-ahead bias risk if not handled "
            "carefully. This idea is logged as considered but not worth pursuing at this stage."
        ),
        category="DISCARD",
        estimated_auc_delta="unknown — data unavailable",
        effort="HIGH — requires paid options data subscription; significant infrastructure work",
    ))
    logger.info("Rule 7 fired: options DISCARD suggestion added (always)")

    # --- Rule 8: Short-term momentum feature ----------------------------------
    has_momentum_5d = any("momentum_5d" in fn.lower() for fn in feature_names) or \
                      any("price_momentum_5d" in fn.lower() for fn in feature_names)
    if not has_momentum_5d:
        suggestions.append(_make_suggestion(
            title="Add short-term 5-day momentum feature",
            rationale=(
                "The current feature set includes price_momentum_20d but lacks a short-term "
                "counterpart. The 5-day price momentum captures fast-moving conviction changes "
                "that occur in the days immediately around a 13F filing date — when market "
                "participants react to the disclosed positions. This complements the 20-day "
                "signal by providing a more responsive, recent momentum indicator."
            ),
            category="IMPLEMENT",
            estimated_auc_delta="+0.002 to +0.008",
            effort="LOW — compute from price_outcomes table (price data already cached); "
                   "add as derived column in feature engineering pipeline",
        ))
        logger.info("Rule 8 fired: price_momentum_5d not in feature names")

    # --- Rule 9: Form 4 insider data (always REVIEW) --------------------------
    suggestions.append(_make_suggestion(
        title="Enrich insider features using SEC Form 4 real-time data",
        rationale=(
            "The current insider_buy_flag is a binary indicator that collapses rich Form 4 "
            "data into a single bit. Form 4 filings on SEC EDGAR provide exact dollar amounts, "
            "transaction timing relative to earnings, and officer type (CEO vs CFO vs director "
            "vs 10%-holder). CEO open-market purchases carry materially different signal than "
            "director grants or automatic 10b5-1 sales. Encoding these distinctions would "
            "significantly improve the insider feature's predictive power."
        ),
        category="REVIEW",
        estimated_auc_delta="+0.008 to +0.025 (insiders have material non-public advantage)",
        effort="MEDIUM — SEC EDGAR Form 4 endpoint is free (https://www.sec.gov/cgi-bin/browse-edgar); "
               "need new Form 4 XML parser and mapping to existing ticker/date index",
    ))
    logger.info("Rule 9 fired: Form 4 insider enrichment suggestion added (always)")

    # --- Rule 10: Regime-conditional training --------------------------------
    has_bullish = any(r in ("BULLISH", "BULL", "RISK_ON") for r in all_macro_regimes)
    has_bearish = any(r in ("BEARISH", "BEAR", "RISK_OFF") for r in all_macro_regimes)
    if has_bullish and has_bearish:
        regime_list = ", ".join(all_macro_regimes)
        suggestions.append(_make_suggestion(
            title="Train separate models per macro regime",
            rationale=(
                f"The R&D regime history shows both BULLISH and BEARISH macro regimes have "
                f"been observed ({regime_list}). Hedge fund stock-picking behavior differs "
                f"significantly across regimes: in bear markets, conviction buys are more "
                f"contrarian and mean-revert faster; in bull markets, momentum following "
                f"dominates. A single model averages away these regime-specific patterns. "
                f"Separate per-regime models (or a regime indicator as a hard split) would "
                f"allow each sub-model to specialize on the relevant behavioral pattern."
            ),
            category="REVIEW",
            estimated_auc_delta="+0.010 to +0.030 in out-of-sample regime-matched periods",
            effort="HIGH — requires regime-split training pipeline, per-regime model storage, "
                   "and a regime-detection router at inference time; validate each sub-model "
                   "has sufficient samples before splitting",
        ))
        logger.info(f"Rule 10 fired: both BULLISH and BEARISH regimes present ({regime_list})")
    else:
        logger.info(
            f"Rule 10 skipped: regime history has only {all_macro_regimes} "
            f"(need both BULLISH and BEARISH)"
        )

    logger.info(f"Rule-based engine generated {len(suggestions)} raw suggestions")
    return suggestions


# ---------------------------------------------------------------------------
# Suggestion Assembly
# ---------------------------------------------------------------------------

def assemble_suggestions(
    raw_suggestions: list[dict],
    existing_suggestions: list[dict],
) -> list[dict]:
    """
    Merge raw suggestions into the existing list, assigning IDs, timestamps,
    and status. Deduplicates by title (case-insensitive substring match).
    Returns only the NEW entries added this run.
    """
    existing_titles = build_existing_titles(existing_suggestions)
    now_iso = datetime.now(timezone.utc).isoformat()
    new_entries: list[dict] = []

    for raw in raw_suggestions:
        title = raw.get("title", "").strip()
        if not title:
            logger.warning("Skipping suggestion with empty title")
            continue

        if _is_duplicate(title, existing_titles):
            logger.info(f"Deduplicating suggestion (already exists): '{title}'")
            continue

        all_so_far = existing_suggestions + new_entries
        suggestion_id = next_suggestion_id(all_so_far)

        entry: dict[str, Any] = {
            "id": suggestion_id,
            "title": title,
            "rationale": raw.get("rationale", ""),
            "category": raw["category"],
            "estimated_auc_delta": raw.get("estimated_auc_delta", "unknown"),
            "effort": raw.get("effort", "unknown"),
            "status": "PENDING",
            "created_at": now_iso,
            "implemented_at": None,
        }
        new_entries.append(entry)
        existing_titles.add(title.lower())
        logger.info(f"New suggestion [{entry['category']}] {entry['id']}: {title}")

    return new_entries


# ---------------------------------------------------------------------------
# Email Digest
# ---------------------------------------------------------------------------

def load_last_email_info() -> dict:
    if not LAST_EMAIL_FILE.exists():
        return {"last_sent_date": None}
    try:
        with open(LAST_EMAIL_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read last email info: {e}")
        return {"last_sent_date": None}


def save_last_email_info(info: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LAST_EMAIL_FILE, "w") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save last email info: {e}")


def should_send_digest() -> bool:
    """Return True if we haven't sent a digest today yet."""
    info = load_last_email_info()
    last_date = info.get("last_sent_date")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return last_date != today


def build_digest_body(pending_review: list[dict]) -> str:
    """
    Build a plain-text email body listing PENDING REVIEW suggestions.
    Plain text — no HTML markup — for maximum compatibility.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        f"Apex Capital — ML Improvement Digest",
        f"Generated: {now_str}",
        f"Model: v{CURRENT_MODEL_VERSION}  |  Current AUC: {CURRENT_AUC}",
        f"",
        f"{len(pending_review)} suggestion(s) require your review:",
        f"=" * 60,
    ]

    for i, s in enumerate(pending_review, 1):
        lines += [
            f"",
            f"{i}. {s['title']}",
            f"   ID: {s['id']}",
            f"   Rationale: {s['rationale']}",
            f"   Estimated AUC delta: {s['estimated_auc_delta']}",
            f"   Effort: {s['effort']}",
        ]

    lines += [
        f"",
        f"=" * 60,
        f"",
        f"Reply to this email or mention it to Claude to approve/reject each item.",
        f"",
        f"Apex Capital ML Improvement Agent — runs 4x nightly (01:00, 03:00, 05:00, 07:00 UTC)",
    ]

    return "\n".join(lines)


def send_digest_email(suggestions: list[dict]) -> list[dict]:
    """
    If this is the first run today and there are PENDING REVIEW suggestions,
    send the digest email and mark those suggestions as EMAILED.
    Returns the (possibly modified) suggestions list.
    """
    if not should_send_digest():
        logger.info("Daily digest already sent today — skipping email")
        return suggestions

    pending_review = [
        s for s in suggestions
        if s.get("category") == "REVIEW" and s.get("status") == "PENDING"
    ]

    if not pending_review:
        logger.info("No PENDING REVIEW suggestions — no digest email needed")
        # Still mark today so we don't check again on subsequent runs
        save_last_email_info({"last_sent_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
        return suggestions

    n = len(pending_review)
    subject = f"\U0001f4ca Apex Capital — ML Improvement Digest ({n} items need your review)"
    body = build_digest_body(pending_review)

    logger.info(f"Sending digest email: {n} REVIEW suggestion(s)…")
    success = send_alert_email(subject, body)

    if success:
        logger.info("Digest email sent successfully")
        emailed_ids = {s["id"] for s in pending_review}
        for s in suggestions:
            if s["id"] in emailed_ids:
                s["status"] = "EMAILED"
        save_last_email_info({"last_sent_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    else:
        logger.error("Digest email failed — suggestions remain PENDING")

    return suggestions


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run() -> None:
    run_start = datetime.now(timezone.utc)
    logger.info(
        f"=== ML IMPROVEMENT AGENT START (rule-based) === "
        f"{run_start.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )

    # --- Load existing suggestions first (needed for dedup) -------------------
    existing_suggestions = load_suggestions()
    logger.info(f"Loaded {len(existing_suggestions)} existing suggestions")

    # --- Gather context -------------------------------------------------------
    # Try v4 first (most recent), fall back to v3 if v4 not yet trained
    _model_pkl = (DATA_DIR / "model_v5.pkl" if (DATA_DIR / "model_v5.pkl").exists()
                  else DATA_DIR / "model_v4.pkl" if (DATA_DIR / "model_v4.pkl").exists()
                  else DATA_DIR / "model_v3.pkl")
    logger.info(f"Extracting model features from {_model_pkl.name}…")
    model_info = extract_model_features(_model_pkl)
    logger.info(
        f"Model: AUC={model_info['auc']}, val_auc={model_info['val_auc']}, "
        f"wf_cv_auc={model_info['wf_cv_auc']}, n_features={model_info['n_features']}"
    )
    if model_info.get("load_error"):
        logger.warning(f"Model load issue: {model_info['load_error']}")

    logger.info("Querying historical_13f.db…")
    db_info = query_database_context(DATA_DIR / "historical_13f.db")
    logger.info(
        f"DB: total={db_info.get('total_samples')}, labeled={db_info.get('labeled_samples')}, "
        f"positive_rate={db_info.get('positive_rate')}, latest={db_info.get('latest_snapshot_date')}"
    )
    if db_info.get("db_error"):
        logger.warning(f"DB issue: {db_info['db_error']}")

    logger.info("Loading R&D regime history…")
    rd_info = load_rd_regime_context(DATA_DIR / "rd_ml_history.json")
    logger.info(
        f"R&D: {rd_info.get('n_entries')} entries, "
        f"regimes seen: {rd_info.get('all_macro_regimes')}"
    )
    if rd_info.get("load_error"):
        logger.warning(f"R&D load issue: {rd_info['load_error']}")

    logger.info("Scanning recent logs for ML warnings…")
    log_warnings = scan_log_warnings(LOGS_DIR)
    logger.info(f"Found {len(log_warnings)} ML-related log warning(s)")

    # --- Run rule-based suggestion engine -------------------------------------
    context = {
        "model_info": model_info,
        "db_info": db_info,
        "rd_info": rd_info,
        "log_warnings": log_warnings,
    }
    logger.info("Running rule-based suggestion engine…")
    raw_suggestions = generate_rule_based_suggestions(context)
    logger.info(f"Rule engine produced {len(raw_suggestions)} candidate suggestions")

    # --- Assemble and deduplicate ---------------------------------------------
    new_entries = assemble_suggestions(raw_suggestions, existing_suggestions)
    n_implement = sum(1 for s in new_entries if s["category"] == "IMPLEMENT")
    n_review = sum(1 for s in new_entries if s["category"] == "REVIEW")
    n_discard = sum(1 for s in new_entries if s["category"] == "DISCARD")
    logger.info(
        f"New suggestions this run: {len(new_entries)} "
        f"(IMPLEMENT={n_implement}, REVIEW={n_review}, DISCARD={n_discard})"
    )

    all_suggestions = existing_suggestions + new_entries

    # --- Persist before email (safe even if email fails) ----------------------
    try:
        save_suggestions(all_suggestions)
    except Exception as e:
        logger.error(f"Could not persist suggestions: {e} — continuing to email step")

    # --- Send digest if due ---------------------------------------------------
    all_suggestions = send_digest_email(all_suggestions)

    # --- Re-persist if email step modified statuses ---------------------------
    try:
        save_suggestions(all_suggestions)
    except Exception as e:
        logger.error(f"Could not persist updated suggestion statuses: {e}")

    # --- Summary --------------------------------------------------------------
    total = len(all_suggestions)
    by_status: dict[str, int] = {}
    for s in all_suggestions:
        key = f"{s['category']}/{s['status']}"
        by_status[key] = by_status.get(key, 0) + 1

    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    logger.info(
        f"=== ML IMPROVEMENT AGENT DONE === "
        f"total={total} new={len(new_entries)} elapsed={elapsed:.1f}s "
        f"breakdown={by_status}"
    )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Unhandled exception in ml_improvement_agent: {e}")
        sys.exit(1)
