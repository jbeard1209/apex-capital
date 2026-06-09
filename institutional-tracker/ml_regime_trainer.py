#!/usr/bin/env python3
"""
APEX CAPITAL — ML REGIME TRAINER
==================================
Trains separate LightGBM classifiers for each macro regime (BULL / NEUTRAL / BEAR)
so predictions are calibrated to current market conditions rather than averaged
across all historical regimes.

Regime mapping (rd_macro_regime field in rd_ml_history.json):
  -1  → BEAR
   0  → NEUTRAL
   1  → BULL

Pipeline
--------
  1. Load pipeline_snapshots (outcome_20d IS NOT NULL) from historical_13f.db
  2. Build augmented feature matrix (same as ml_model_v3.py)
  3. Join with rd_ml_history.json to attach regime label to each row by date
  4. For each regime: filter, validate sample count, train LightGBM, evaluate
  5. Save per-regime .pkl models + regime_models.json registry
  6. Update ml_suggestions.json if a regime model beats the base model by > 0.01 AUC

Outputs
-------
  data/model_bull.pkl
  data/model_neutral.pkl
  data/model_bear.pkl
  data/regime_models.json
  data/ml_suggestions.json  (amended)

Usage
-----
  python3 ml_regime_trainer.py [--force]

GCP: Python 3.11, lightgbm, scikit-learn, pandas, numpy available.
"""

from __future__ import annotations

import json
import logging
import pickle
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

DB_PATH             = DATA_DIR / "historical_13f.db"
RD_ML_HISTORY_PATH  = DATA_DIR / "rd_ml_history.json"
REGIME_REGISTRY     = DATA_DIR / "regime_models.json"
SUGGESTIONS_FILE    = DATA_DIR / "ml_suggestions.json"
BASE_MODEL_META     = DATA_DIR / "model_v3_meta.json"
BASE_MODEL_PATH     = DATA_DIR / "model_v3.pkl"

REGIME_NAMES = {-1: "bear", 0: "neutral", 1: "bull"}
MIN_SAMPLES  = 200          # skip regimes with fewer rows
AUC_OUTPERFORM_DELTA = 0.01  # threshold to file an IMPLEMENT suggestion

# ---------------------------------------------------------------------------
# Logging — emit to stdout so GCP Cloud Run / Cloud Scheduler captures it
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_regime_trainer")


# ---------------------------------------------------------------------------
# Feature matrix construction (delegates to predictive_model package)
# ---------------------------------------------------------------------------

def _build_augmented_matrix() -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    """
    Build the augmented (base + macro features) matrix exactly as ml_model_v3.py
    does, then return (X_aug, y_binary, meta_df).

    meta_df is expected to contain a 'snapshot_date' column (date string or date
    object) that is used to join regime labels.
    """
    sys.path.insert(0, str(BASE_DIR))

    from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS
    from predictive_model.macro_features import add_macro_features, MACRO_FEATURE_COLS, get_macro_at_date

    logger.info("Building base feature matrix from historical_13f.db …")
    X, y, meta_df = build_feature_matrix(target="outcome_20d")

    if len(X) == 0:
        logger.warning("build_feature_matrix returned 0 rows — nothing to train on")
        return X, y, meta_df

    logger.info(
        f"Base matrix: {len(X)} rows × {X.shape[1]} features  "
        f"({meta_df['snapshot_date'].nunique()} unique dates)"
    )

    # -- macro features (date-accurate historical lookup) ---------------------
    logger.info("Fetching per-date macro features …")
    macro_vals = []
    for d in meta_df["snapshot_date"]:
        macro_vals.append(get_macro_at_date(d))
    macro_arr = np.array(
        [[row[c] for c in MACRO_FEATURE_COLS] for row in macro_vals],
        dtype=float,
    )

    X_aug = np.hstack([X, macro_arr])
    logger.info(
        f"Augmented matrix: {len(X_aug)} rows × {X_aug.shape[1]} features "
        f"({X.shape[1]} base + {len(MACRO_FEATURE_COLS)} macro)"
    )
    return X_aug, y, meta_df


# ---------------------------------------------------------------------------
# Regime label join
# ---------------------------------------------------------------------------

def _load_regime_by_date() -> dict[str, int]:
    """
    Parse rd_ml_history.json and return {date_string: regime_int} mapping.
    rd_macro_regime values are -1, 0, 1 (BEAR / NEUTRAL / BULL).
    Falls back gracefully if the file is missing or malformed.
    """
    if not RD_ML_HISTORY_PATH.exists():
        logger.warning(f"rd_ml_history.json not found at {RD_ML_HISTORY_PATH}")
        return {}

    try:
        with open(RD_ML_HISTORY_PATH, "r") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.error(f"Failed to load rd_ml_history.json: {exc}")
        return {}

    regime_map: dict[str, int] = {}

    if isinstance(data, list):
        for entry in data:
            date_val = entry.get("date")
            regime   = entry.get("rd_macro_regime") if "rd_macro_regime" in entry \
                       else entry.get("macro_regime")
            if date_val is not None and regime is not None:
                try:
                    regime_map[str(date_val)[:10]] = int(regime)
                except (ValueError, TypeError):
                    pass
    elif isinstance(data, dict):
        # Single-entry dict
        date_val = data.get("date")
        regime   = data.get("rd_macro_regime") or data.get("macro_regime")
        if date_val is not None and regime is not None:
            try:
                regime_map[str(date_val)[:10]] = int(regime)
            except (ValueError, TypeError):
                pass

    logger.info(f"Loaded {len(regime_map)} regime labels from rd_ml_history.json")
    return regime_map


def _attach_regime_labels(
    meta_df: pd.DataFrame,
    regime_map: dict[str, int],
) -> pd.Series:
    """
    Return a pd.Series of integer regime labels (-1 / 0 / 1) aligned with meta_df.
    Rows whose date has no match in regime_map are labelled as NEUTRAL (0).
    """
    def _lookup(d) -> int:
        key = str(d)[:10]
        return regime_map.get(key, 0)  # default to NEUTRAL if unknown

    regimes = meta_df["snapshot_date"].apply(_lookup)
    coverage = (meta_df["snapshot_date"].apply(lambda d: str(d)[:10]).isin(regime_map)).mean()
    logger.info(
        f"Regime coverage: {coverage:.1%} of rows matched to rd_ml_history  "
        f"(remainder defaulted to NEUTRAL)"
    )
    return regimes


# ---------------------------------------------------------------------------
# Per-regime training
# ---------------------------------------------------------------------------

def _train_regime_model(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    regime_name: str,
) -> tuple[object, float]:
    """
    Train a LightGBM classifier on the training split and evaluate on validation.
    Returns (fitted_model, val_auc).
    """
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise RuntimeError(
            "lightgbm is required for regime training but is not installed"
        ) from exc

    scaler  = StandardScaler()
    X_tr_s  = scaler.fit_transform(X_tr)
    X_val_s = scaler.transform(X_val)

    model = lgb.LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        max_depth=6,
        min_child_samples=10,
        class_weight="balanced",
        random_state=42,
        verbosity=-1,
    )

    model.fit(
        X_tr_s,
        y_tr,
        eval_set=[(X_val_s, y_val)],
        callbacks=[
            lgb.early_stopping(stopping_rounds=40, verbose=False),
            lgb.log_evaluation(period=-1),
        ],
    )

    if len(set(y_val)) < 2:
        logger.warning(f"  [{regime_name}] Validation set has only one class — AUC set to 0.5")
        val_auc = 0.5
    else:
        preds   = model.predict_proba(X_val_s)[:, 1]
        val_auc = float(roc_auc_score(y_val, preds))

    # Bundle scaler with the model so inference callers can scale inputs
    bundle = {
        "model":        model,
        "scaler":       scaler,
        "regime_name":  regime_name,
        "trained_at":   datetime.now(timezone.utc).isoformat(),
        "n_train":      len(X_tr),
        "n_val":        len(X_val),
        "val_auc":      round(val_auc, 4),
    }

    logger.info(f"  [{regime_name}] val AUC = {val_auc:.4f}  (n_train={len(X_tr)}, n_val={len(X_val)})")
    return bundle, val_auc


# ---------------------------------------------------------------------------
# Registry + suggestions helpers
# ---------------------------------------------------------------------------

def _save_registry(model_results: dict[str, dict]) -> None:
    """Write regime_models.json registry."""
    registry = {
        "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "models":     {},
        "fallback":   str(BASE_MODEL_PATH.relative_to(BASE_DIR)),
    }

    for regime_name, info in model_results.items():
        registry["models"][regime_name] = {
            "path":      str((DATA_DIR / f"model_{regime_name}.pkl").relative_to(BASE_DIR)),
            "n_samples": info["n_samples"],
            "val_auc":   info["val_auc"],
        }

    tmp = REGIME_REGISTRY.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(registry, fh, indent=2)
        tmp.replace(REGIME_REGISTRY)
        logger.info(f"Regime registry saved → {REGIME_REGISTRY}")
    except Exception as exc:
        logger.error(f"Failed to save regime registry: {exc}")
        tmp.unlink(missing_ok=True)
        raise


def _load_base_auc() -> float:
    """Read the v3 base model's validation AUC from model_v3_meta.json."""
    if not BASE_MODEL_META.exists():
        logger.warning("model_v3_meta.json not found — assuming base AUC = 0.59")
        return 0.59
    try:
        with open(BASE_MODEL_META, "r") as fh:
            meta = json.load(fh)
        auc = float(meta.get("ensemble_auc_val") or meta.get("stacked_auc_val") or 0.59)
        logger.info(f"Base model AUC from meta: {auc:.4f}")
        return auc
    except Exception as exc:
        logger.warning(f"Could not read base model AUC: {exc}")
        return 0.59


def _update_suggestions(model_results: dict[str, dict], base_auc: float) -> None:
    """
    Append IMPLEMENT suggestions to ml_suggestions.json for any regime model
    that outperforms the base by more than AUC_OUTPERFORM_DELTA.
    Deduplicates by title (case-insensitive substring match).
    """
    # Load existing suggestions
    existing: list[dict] = []
    if SUGGESTIONS_FILE.exists():
        try:
            with open(SUGGESTIONS_FILE, "r") as fh:
                existing = json.load(fh)
            if not isinstance(existing, list):
                existing = []
        except Exception as exc:
            logger.warning(f"Could not load ml_suggestions.json: {exc}")
            existing = []

    existing_titles = {s.get("title", "").lower() for s in existing}
    now_iso = datetime.now(timezone.utc).isoformat()

    # Generate next sequential ID
    import re
    year = datetime.now(timezone.utc).strftime("%Y")
    existing_nums: list[int] = []
    pattern = re.compile(rf"^mlsugg-{year}-(\d+)$")
    for s in existing:
        m = pattern.match(s.get("id", ""))
        if m:
            existing_nums.append(int(m.group(1)))
    next_num = max(existing_nums, default=0) + 1

    new_entries: list[dict] = []

    for regime_name, info in model_results.items():
        regime_auc = info["val_auc"]
        delta      = regime_auc - base_auc

        if delta <= AUC_OUTPERFORM_DELTA:
            continue

        title = f"Activate {regime_name} regime model — AUC {regime_auc:.4f} vs base {base_auc:.4f}"
        title_lower = title.lower()

        # Deduplicate
        if any(title_lower in existing.lower() or existing.lower() in title_lower
               for existing in existing_titles):
            logger.info(f"  Suggestion already exists (skipped): {title}")
            continue

        sugg_id = f"mlsugg-{year}-{next_num:03d}"
        next_num += 1

        entry = {
            "id":                  sugg_id,
            "title":               title,
            "rationale": (
                f"The {regime_name.upper()} regime model achieved val AUC {regime_auc:.4f}, "
                f"which is +{delta:.4f} above the base model's {base_auc:.4f}. "
                f"Training on {info['n_samples']} regime-matched samples filters out "
                f"cross-regime noise and calibrates predictions to current market conditions. "
                f"Activating this model when the rd_macro_regime signal matches "
                f"'{regime_name}' would lift expected AUC during that regime."
            ),
            "category":            "IMPLEMENT",
            "estimated_auc_delta": f"+{delta:.4f} in {regime_name.upper()} regime periods",
            "effort":              "LOW — regime_models.json registry already points to the trained model; "
                                   "update inference router in predictor.py to read regime from rd_ml_history",
            "status":              "PENDING",
            "created_at":          now_iso,
            "implemented_at":      None,
        }
        new_entries.append(entry)
        existing_titles.add(title_lower)
        logger.info(f"  New IMPLEMENT suggestion [{sugg_id}]: {title}")

    if not new_entries:
        logger.info("No regime models beat the base by > 0.01 AUC — no new suggestions added")
        return

    all_suggestions = existing + new_entries
    tmp = SUGGESTIONS_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(all_suggestions, fh, indent=2, default=str)
        tmp.replace(SUGGESTIONS_FILE)
        logger.info(
            f"ml_suggestions.json updated — added {len(new_entries)} IMPLEMENT suggestion(s)"
        )
    except Exception as exc:
        logger.error(f"Failed to save suggestions: {exc}")
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> dict:
    run_start = datetime.now(timezone.utc)
    logger.info("=" * 72)
    logger.info(
        f"ML REGIME TRAINER starting — {run_start.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    logger.info("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Build augmented feature matrix
    # ------------------------------------------------------------------
    try:
        X_aug, y_bin, meta_df = _build_augmented_matrix()
    except Exception as exc:
        logger.exception(f"Feature matrix construction failed: {exc}")
        return {"status": "FAILED", "reason": str(exc)}

    if len(X_aug) == 0:
        logger.warning("Empty feature matrix — aborting regime training")
        return {"status": "SKIPPED", "reason": "empty feature matrix"}

    # ------------------------------------------------------------------
    # 2. Load regime labels and attach to rows
    # ------------------------------------------------------------------
    regime_map     = _load_regime_by_date()
    regime_labels  = _attach_regime_labels(meta_df, regime_map)

    # Convert y to numpy int array
    y_arr = np.array(y_bin, dtype=int)

    # ------------------------------------------------------------------
    # 3. Load base model AUC for comparison
    # ------------------------------------------------------------------
    base_auc = _load_base_auc()

    # ------------------------------------------------------------------
    # 4. Train a model for each regime
    # ------------------------------------------------------------------
    model_results: dict[str, dict] = {}   # regime_name → {val_auc, n_samples}

    for regime_int, regime_name in REGIME_NAMES.items():
        mask = (regime_labels == regime_int).values
        n    = mask.sum()

        logger.info(f"--- Regime: {regime_name.upper()} (code={regime_int}) | {n} samples ---")

        if n < MIN_SAMPLES:
            logger.warning(
                f"Skipping {regime_name} regime — only {n} samples "
                f"(minimum {MIN_SAMPLES} required)"
            )
            continue

        X_regime = X_aug[mask]
        y_regime = y_arr[mask]

        # Time-ordered 80/20 split
        split    = int(n * 0.8)
        X_tr     = X_regime[:split]
        y_tr     = y_regime[:split]
        X_val    = X_regime[split:]
        y_val    = y_regime[split:]

        if len(set(y_tr)) < 2:
            logger.warning(
                f"Skipping {regime_name} regime — training split has only one class "
                f"(positive rate = {y_tr.mean():.3f})"
            )
            continue

        try:
            bundle, val_auc = _train_regime_model(X_tr, y_tr, X_val, y_val, regime_name)
        except Exception as exc:
            logger.error(f"Training failed for {regime_name} regime: {exc}")
            continue

        # Save model pkl
        model_path = DATA_DIR / f"model_{regime_name}.pkl"
        try:
            with open(model_path, "wb") as fh:
                pickle.dump(bundle, fh)
            logger.info(f"  Saved → {model_path}")
        except Exception as exc:
            logger.error(f"  Failed to save model_{regime_name}.pkl: {exc}")
            continue

        model_results[regime_name] = {
            "val_auc":   round(val_auc, 4),
            "n_samples": int(n),
        }

    if not model_results:
        logger.warning("No regime models were successfully trained")
        return {"status": "SKIPPED", "reason": "no regimes met the minimum sample threshold"}

    # ------------------------------------------------------------------
    # 5. Save regime registry
    # ------------------------------------------------------------------
    try:
        _save_registry(model_results)
    except Exception as exc:
        logger.error(f"Registry save failed: {exc}")

    # ------------------------------------------------------------------
    # 6. Update ml_suggestions.json if any regime model outperforms base
    # ------------------------------------------------------------------
    try:
        _update_suggestions(model_results, base_auc)
    except Exception as exc:
        logger.error(f"Suggestions update failed: {exc}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    summary = {
        "status":        "TRAINED",
        "trained_at":    run_start.isoformat(),
        "elapsed_sec":   round(elapsed, 1),
        "base_auc":      base_auc,
        "model_results": model_results,
        "registry":      str(REGIME_REGISTRY),
    }

    logger.info("=" * 72)
    logger.info(
        f"ML REGIME TRAINER complete — {len(model_results)} model(s) trained "
        f"in {elapsed:.1f}s"
    )
    for regime_name, info in model_results.items():
        delta = info["val_auc"] - base_auc
        logger.info(
            f"  {regime_name:8s} | AUC={info['val_auc']:.4f}  "
            f"delta vs base={delta:+.4f}  n={info['n_samples']:,}"
        )
    logger.info("=" * 72)

    return summary


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, default=str))
