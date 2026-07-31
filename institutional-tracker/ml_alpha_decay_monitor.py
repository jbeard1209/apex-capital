"""
ml_alpha_decay_monitor.py
=========================
Apex Capital ML Research Team
-------------------------------
Tracks per-feature alpha decay in model_v3.pkl by computing Spearman correlations
between each feature and outcome_20d across four rolling time windows. Flags
features whose predictive power has significantly eroded and writes suggestions
to ml_suggestions.json for the research team to act on.

Schedule: run after each model retrain or daily at market close.
Entry point: run()
"""

import json
import logging
import pickle
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats

from email_sender import send_alert_email

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
MODEL_PATH = DATA_DIR / "model_v3.pkl"
DB_PATH = DATA_DIR / "historical_13f.db"
REPORT_PATH = DATA_DIR / "alpha_decay_report.json"
SUGGESTIONS_PATH = DATA_DIR / "ml_suggestions.json"

MIN_SAMPLES_PER_WINDOW = 30
DECAY_THRESHOLD = 0.50        # W1 corr < 50% of W3/W4 corr → DECAYING
DEAD_THRESHOLD = 0.01         # |W1 corr| < 0.01 → DEAD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ml_alpha_decay_monitor")

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_model(path: Path) -> Optional[dict[str, Any]]:
    """Load pickled model bundle. Returns None if file missing or unpicklable."""
    if not path.exists():
        log.error("Model file not found: %s", path)
        return None
    try:
        with open(path, "rb") as fh:
            bundle = pickle.load(fh)
        log.info("Loaded model bundle from %s", path)
        return bundle
    except Exception as exc:
        log.error("Failed to unpickle model: %s", exc)
        return None


def extract_feature_info(bundle: dict[str, Any]) -> tuple[list[str], dict[str, float]]:
    """
    Extract feature_cols list and per-feature importances from the model bundle.
    Returns (feature_cols, importances_dict).
    Importances are normalized 0-1 (higher = more important in current model).
    """
    feature_cols: list[str] = bundle.get("feature_cols", [])

    if not feature_cols:
        # Fallback: try to infer from the LGB model directly
        lgb = bundle.get("models", {}).get("lgb") if isinstance(bundle.get("models"), dict) else None
        if lgb is not None and hasattr(lgb, "feature_name_"):
            feature_cols = lgb.feature_name_()
            log.info("Inferred %d features from lgb.feature_name_()", len(feature_cols))
        else:
            log.warning("No feature_cols found in bundle and no LGB model available.")
            return [], {}

    # Extract importances from LGB model if present
    importances: dict[str, float] = {}
    models = bundle.get("models", {})
    lgb_model = models.get("lgb") if isinstance(models, dict) else None

    if lgb_model is not None and hasattr(lgb_model, "feature_importances_"):
        raw = lgb_model.feature_importances_
        total = raw.sum() or 1.0
        lgb_feature_names = feature_cols
        if hasattr(lgb_model, "feature_name_"):
            lgb_feature_names = lgb_model.feature_name_()
        for fname, imp in zip(lgb_feature_names, raw):
            importances[fname] = float(imp / total)
        log.info("Extracted importances for %d features from LGB model.", len(importances))
    else:
        log.info("LGB model or feature_importances_ not available; importances will be empty.")

    log.info("Total feature columns: %d", len(feature_cols))
    return list(feature_cols), importances

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_snapshots(db_path: Path) -> pd.DataFrame:
    """
    Query pipeline_snapshots where outcome_20d IS NOT NULL, ordered by snapshot_date.
    Returns a DataFrame. Raises on connection failure.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        query = """
            SELECT *
            FROM pipeline_snapshots
            WHERE outcome_20d IS NOT NULL
            ORDER BY snapshot_date ASC
        """
        df = pd.read_sql_query(query, conn, parse_dates=["snapshot_date"])
        log.info("Loaded %d labelled snapshots from pipeline_snapshots.", len(df))
        return df
    finally:
        conn.close()


def split_windows(df: pd.DataFrame, reference_date: datetime) -> dict[str, pd.DataFrame]:
    """
    Split df into four time windows relative to reference_date:
      W1 = last 30 days
      W2 = 31–90 days ago
      W3 = 91–180 days ago
      W4 = 181–365 days ago
    snapshot_date column is expected to be datetime (UTC-naive or UTC).
    """
    ref = pd.Timestamp(reference_date)

    # Normalize to same tz-awareness
    if df["snapshot_date"].dt.tz is not None:
        ref = ref.tz_localize("UTC") if ref.tzinfo is None else ref.tz_convert("UTC")
    else:
        ref = ref.replace(tzinfo=None)

    cutoffs = {
        "W1": (ref - timedelta(days=30),  ref),
        "W2": (ref - timedelta(days=90),  ref - timedelta(days=31)),
        "W3": (ref - timedelta(days=180), ref - timedelta(days=91)),
        "W4": (ref - timedelta(days=365), ref - timedelta(days=181)),
    }

    windows: dict[str, pd.DataFrame] = {}
    for name, (start, end) in cutoffs.items():
        mask = (df["snapshot_date"] >= start) & (df["snapshot_date"] <= end)
        windows[name] = df.loc[mask].copy()
        log.info("Window %s: %d rows (%s → %s)", name, mask.sum(),
                 start.date(), end.date())
    return windows

# ---------------------------------------------------------------------------
# Correlation computation
# ---------------------------------------------------------------------------

def spearman_corr(series_x: pd.Series, series_y: pd.Series) -> Optional[float]:
    """
    Compute Spearman rank correlation between x and y after dropping NaN pairs.
    Returns None if fewer than MIN_SAMPLES_PER_WINDOW valid pairs exist.
    """
    combined = pd.concat([series_x.rename("x"), series_y.rename("y")], axis=1).dropna()
    if len(combined) < MIN_SAMPLES_PER_WINDOW:
        return None
    corr, _ = stats.spearmanr(combined["x"], combined["y"])
    return float(corr) if not np.isnan(corr) else None


def analyze_feature_decay(
    feature: str,
    windows: dict[str, pd.DataFrame],
    importances: dict[str, float],
) -> Optional[dict[str, Any]]:
    """
    Compute Spearman correlations for one feature across all windows.
    Returns a result dict or None if W1 has insufficient data.
    """
    corrs: dict[str, Optional[float]] = {}
    for wname, wdf in windows.items():
        if feature not in wdf.columns:
            corrs[wname] = None
            continue
        if wdf[feature].isnull().all():
            corrs[wname] = None
            continue
        corrs[wname] = spearman_corr(wdf[feature], wdf["outcome_20d"])

    corr_w1 = corrs.get("W1")

    # If W1 is insufficient, we cannot classify this feature
    if corr_w1 is None:
        return None

    abs_w1 = abs(corr_w1)
    status = "STABLE"

    # Dead feature check
    if abs_w1 < DEAD_THRESHOLD:
        status = "DEAD"
    else:
        # Decay check: W1 corr < 50% of corr in W3 or W4
        for ref_window in ("W3", "W4"):
            ref_corr = corrs.get(ref_window)
            if ref_corr is None:
                continue
            abs_ref = abs(ref_corr)
            if abs_ref < DEAD_THRESHOLD:
                continue  # reference window also dead — not meaningful comparison
            ratio = abs_w1 / abs_ref
            if ratio < DECAY_THRESHOLD:
                status = "DECAYING"
                break

    result: dict[str, Any] = {
        "feature": feature,
        "importance": importances.get(feature),
        "corr_W1": corr_w1,
        "corr_W2": corrs.get("W2"),
        "corr_W3": corrs.get("W3"),
        "corr_W4": corrs.get("W4"),
        "status": status,
    }

    # Add decay ratio (W1 vs best available reference)
    if status == "DECAYING":
        for ref_window in ("W3", "W4"):
            ref_corr = corrs.get(ref_window)
            if ref_corr is not None and abs(ref_corr) >= DEAD_THRESHOLD:
                result["decay_ratio"] = round(abs_w1 / abs(ref_corr), 4)
                result["decay_reference_window"] = ref_window
                break

    return result

# ---------------------------------------------------------------------------
# Report & suggestions writers
# ---------------------------------------------------------------------------

def write_report(
    results: list[dict[str, Any]],
    windows: dict[str, pd.DataFrame],
) -> None:
    """Serialize analysis results to alpha_decay_report.json."""
    decaying = [r for r in results if r["status"] == "DECAYING"]
    dead = [r for r in results if r["status"] == "DEAD"]
    stable = [r for r in results if r["status"] == "STABLE"]

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_sizes": {name: len(wdf) for name, wdf in windows.items()},
        "total_features_analyzed": len(results),
        "summary": {
            "decaying": len(decaying),
            "dead": len(dead),
            "stable": len(stable),
        },
        "decaying_features": _clean_for_json(decaying),
        "dead_features": _clean_for_json(dead),
        "stable_features": _clean_for_json(stable),
    }

    # ── Fable 5 decay interpretation — gated to actual decay ──────────────
    if decaying or dead:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("ml_improvement"):
                feats = ", ".join(
                    f"{r['feature']}({r.get('decay_ratio','?')})"
                    for r in (decaying + dead)[:6])
                raw = _r(
                    "You are a quant researcher diagnosing feature alpha decay in a "
                    "13F-based stock-selection model.",
                    f"{len(decaying)} decaying, {len(dead)} dead of {len(results)} features.\n"
                    f"Affected: {feats}\n"
                    "2 sentences: the most likely market-structural reason these signals "
                    "are fading, and whether to retrain, drop, or wait.",
                    agent_name="ml_improvement", max_tokens=120,
                )
                if raw:
                    report["llm_decay_read"] = raw.strip()
        except Exception:
            pass

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    log.info("Alpha decay report written → %s", REPORT_PATH)


def _clean_for_json(records: list[dict]) -> list[dict]:
    """Round float values for readability; handle None gracefully."""
    cleaned = []
    for rec in records:
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float):
                clean[k] = round(v, 6) if not np.isnan(v) else None
            else:
                clean[k] = v
        cleaned.append(clean)
    return cleaned


def load_suggestions() -> list[dict[str, Any]]:
    """Load existing suggestions from file, returning [] if missing/corrupt."""
    if not SUGGESTIONS_PATH.exists():
        return []
    try:
        with open(SUGGESTIONS_PATH) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        # Some bundles store suggestions under a key
        return data.get("suggestions", [])
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load suggestions file: %s", exc)
        return []


def save_suggestions(suggestions: list[dict[str, Any]]) -> None:
    SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SUGGESTIONS_PATH, "w") as fh:
        json.dump(suggestions, fh, indent=2, default=str)
    log.info("Suggestions written → %s", SUGGESTIONS_PATH)


def _title_already_exists(title: str, suggestions: list[dict]) -> bool:
    """
    Deduplicate: return True if a suggestion with a similar title already exists.
    'Similar' = feature name appears in the title of an existing suggestion
    within the last 7 days.
    """
    # Extract keywords (first 60 chars of title for fuzzy match)
    title_key = title[:60].lower()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for s in suggestions:
        existing_title = s.get("title", "").lower()
        # Check if key portion matches
        if title_key[:40] in existing_title or existing_title[:40] in title_key:
            # Check recency
            created_raw = s.get("created_at") or s.get("timestamp") or ""
            try:
                created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created > cutoff:
                    return True
            except (ValueError, TypeError):
                # No parseable date — treat as recent to be safe
                return True
    return False


def add_decay_suggestions(
    decaying_features: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
) -> int:
    """
    Append REVIEW suggestions for decaying features.
    Returns the number of new suggestions added.
    """
    added = 0
    for feat in decaying_features:
        name = feat["feature"]
        corr_w1 = feat.get("corr_W1")
        corr_w3 = feat.get("corr_W3")
        corr_w4 = feat.get("corr_W4")
        decay_ratio = feat.get("decay_ratio", 0.0)
        pct_drop = 1.0 - (decay_ratio or 0.0)

        title = f"Feature '{name}' showing alpha decay — correlation dropped {pct_drop:.0%}"

        if _title_already_exists(title, suggestions):
            log.debug("Skipping duplicate suggestion for feature '%s'.", name)
            continue

        # A conditional cannot live inside a format spec: everything after the
        # ':' is the spec, so ".4f if corr_w3 is not None else 'N/A'" is parsed
        # as a format spec and raises ValueError: Invalid format specifier. It
        # raised for W3/W4, and TypeError for W1 (.get() can return None there
        # too, which the original didn't guard at all). The raise is uncaught up
        # through run(), so the monitor died here — after writing its report,
        # before adding a single suggestion or sending its alert. It only ever
        # reached this line when `decaying` was non-empty, i.e. exactly when it
        # had something to say. Format first, interpolate after.
        def _f(v):
            return "N/A" if v is None else f"{v:.4f}"

        rationale = (
            f"Spearman correlation with outcome_20d has significantly weakened. "
            f"W1 (last 30d): {_f(corr_w1)}. "
            f"W3 (91–180d): {_f(corr_w3)}. "
            f"W4 (181–365d): {_f(corr_w4)}. "
            f"Decay ratio vs reference window: {decay_ratio:.2f} (threshold: {DECAY_THRESHOLD}). "
            f"Model importance: {feat.get('importance', 'N/A')}. "
            f"Recommend: (1) audit data pipeline for this feature, (2) consider removing or "
            f"replacing it before next retrain, (3) check if market regime change explains decay."
        )

        suggestion = {
            "id": f"alpha_decay_{name}_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            "category": "REVIEW",
            "title": title,
            "rationale": rationale,
            "estimated_auc_delta": "-0.001 to -0.010 if left in (adds noise)",
            "feature": name,
            "decay_ratio": decay_ratio,
            "corr_W1": corr_w1,
            "corr_W3": corr_w3,
            "corr_W4": corr_w4,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "PENDING",
            "source": "ml_alpha_decay_monitor",
        }
        suggestions.append(suggestion)
        added += 1
        log.info("Added suggestion for decaying feature '%s'.", name)

    return added

# ---------------------------------------------------------------------------
# Alert logic
# ---------------------------------------------------------------------------

def maybe_send_alert(decaying: list[dict], dead: list[dict]) -> None:
    """Send an email alert if there are newly decaying or dead features."""
    n_decaying = len(decaying)
    n_dead = len(dead)

    if n_decaying == 0 and n_dead == 0:
        return

    subject = f"[APEX ML] Alpha Decay Alert — {n_decaying} decaying, {n_dead} dead features"

    rows_decay = "".join(
        f"<tr><td>{r['feature']}</td>"
        f"<td>{r.get('corr_W1', 'N/A'):.4f}</td>"
        f"<td>{r.get('corr_W3', 'N/A') if r.get('corr_W3') is not None else 'N/A'}</td>"
        f"<td>{r.get('decay_ratio', 'N/A')}</td></tr>"
        for r in decaying
    )
    rows_dead = "".join(
        f"<tr><td>{r['feature']}</td><td>{r.get('corr_W1', 0):.4f}</td></tr>"
        for r in dead
    )

    body = f"""
    <h3 style="color:#f0a500;">Alpha Decay Report — {datetime.now().strftime('%Y-%m-%d')}</h3>
    <p><b>{n_decaying}</b> decaying features | <b>{n_dead}</b> dead features</p>

    <h4>Decaying Features</h4>
    <table border="1" cellpadding="6" style="border-collapse:collapse;font-size:13px;">
      <tr><th>Feature</th><th>Corr W1</th><th>Corr W3</th><th>Decay Ratio</th></tr>
      {rows_decay if rows_decay else "<tr><td colspan='4'>None</td></tr>"}
    </table>

    <h4>Dead Features (|corr| &lt; {DEAD_THRESHOLD})</h4>
    <table border="1" cellpadding="6" style="border-collapse:collapse;font-size:13px;">
      <tr><th>Feature</th><th>Corr W1</th></tr>
      {rows_dead if rows_dead else "<tr><td colspan='2'>None</td></tr>"}
    </table>

    <p style="color:#8b949e;font-size:12px;">
      See <code>data/alpha_decay_report.json</code> and <code>data/ml_suggestions.json</code>
      for full details and remediation suggestions.
    </p>
    """

    try:
        sent = send_alert_email(subject, body)
        if sent:
            log.info("Alert email sent: %s", subject)
        else:
            log.warning("Alert email returned False (not sent).")
    except Exception as exc:
        log.error("Failed to send alert email: %s", exc)

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> None:
    log.info("=== ml_alpha_decay_monitor starting ===")

    # 1. Load model
    bundle = load_model(MODEL_PATH)
    if bundle is None:
        log.error("Cannot proceed without model file. Exiting.")
        return

    feature_cols, importances = extract_feature_info(bundle)
    if not feature_cols:
        log.error("No feature columns found in model bundle. Exiting.")
        return

    log.info("Features to analyze: %d", len(feature_cols))

    # 2. Load snapshot data
    try:
        df = load_snapshots(DB_PATH)
    except FileNotFoundError as exc:
        log.error("%s. Exiting.", exc)
        return
    except Exception as exc:
        log.error("Unexpected error loading snapshots: %s", exc, exc_info=True)
        return

    if df.empty:
        log.warning("No labelled snapshots found (outcome_20d IS NOT NULL). Nothing to analyze.")
        return

    # 3. Split into time windows
    reference_date = datetime.now(timezone.utc)
    windows = split_windows(df, reference_date)

    # 4. Analyze each feature
    results: list[dict[str, Any]] = []
    skipped = 0
    for feature in feature_cols:
        result = analyze_feature_decay(feature, windows, importances)
        if result is None:
            log.debug("Feature '%s' skipped (insufficient W1 data).", feature)
            skipped += 1
            continue
        results.append(result)

    log.info(
        "Analysis complete: %d features analyzed, %d skipped (insufficient data).",
        len(results), skipped,
    )

    decaying = [r for r in results if r["status"] == "DECAYING"]
    dead = [r for r in results if r["status"] == "DEAD"]
    stable = [r for r in results if r["status"] == "STABLE"]

    log.info("Results — DECAYING: %d | DEAD: %d | STABLE: %d",
             len(decaying), len(dead), len(stable))

    # 5. Write report
    write_report(results, windows)

    # 6. Update ml_suggestions.json
    suggestions = load_suggestions()
    n_added = add_decay_suggestions(decaying, suggestions)
    if n_added > 0:
        save_suggestions(suggestions)
        log.info("Added %d new suggestions to ml_suggestions.json.", n_added)
    else:
        log.info("No new suggestions to add (all already present or no decaying features).")

    # 7. Send alert if warranted
    maybe_send_alert(decaying, dead)

    log.info("=== ml_alpha_decay_monitor complete ===")


if __name__ == "__main__":
    run()
