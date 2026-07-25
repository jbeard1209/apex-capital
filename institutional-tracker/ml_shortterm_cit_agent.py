#!/usr/bin/env python3
"""
APEX CAPITAL -- ML SHORT-TERM CONTINUOUS IMPROVEMENT AGENT
===========================================================
Runs daily at noon UTC (cron: 0 12 * * *).

PURPOSE
-------
Monitors the ML short-term pod (model.pkl, outcome_20d target, ml_chain*.py
pipeline) and raises improvement proposals to chairman_action_required.json.

CHECKS
------
  A) Model health       -- existence, age, walk-forward score
  B) Prediction health  -- output file freshness and validity
  C) Pipeline run check -- log file activity
  D) Data drift check   -- feature importance drift
  E) Improvement proposals -- retraining schedule, declining performance

OUTPUT
------
  data/chairman_action_required.json -- appended on any finding (last 20 kept)
  logs/ml_shortterm_cit_agent.log    -- via cron redirect
"""

import fcntl
import glob
import json
import logging
import os
import sys
from datetime import datetime, timedelta

# -- Working directory --------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_SCRIPT_DIR)

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ml_shortterm_cit")

# -- File paths ---------------------------------------------------------------
_MODEL_PATH         = "data/model.pkl"
_VALIDATION_PATH    = "data/ml_validation_report.json"
_CHAIRMAN_PATH      = "data/chairman_action_required.json"
_LOCK_PATH          = "data/.ml_shortterm_cit.lock"
_LOGS_DIR           = "logs"
_DATA_DIR           = "data"

# -- Thresholds ---------------------------------------------------------------
_MODEL_STALE_DAYS           = 60
_MODEL_RETRAIN_SUGGESTION   = 30
_WALK_FORWARD_FLOOR         = 0.52
_PREDICTION_STALE_HOURS     = 48
_LOG_STALE_HOURS            = 48
_CHAIRMAN_MAX_ENTRIES       = 20

# -- Log file patterns for ML short-term --------------------------------------
_LOG_PATTERNS = [
    "logs/ml_chain*.log",
    "logs/ml_shortterm*.log",
    "logs/ml_runner*.log",
    "logs/ml_team_leader*.log",
    "logs/overnight_ml*.log",
]

# -- Prediction/signal file patterns in data/ ---------------------------------
_PRED_PATTERNS = [
    "data/*prediction*",
    "data/*signal*",
    "data/rd_ml_bridge_signals.json",
    "data/alt_data_signals.json",
]


# =============================================================================
# HELPERS
# =============================================================================

def _load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("  Could not load %s: %s", path, e)
    return None


def _file_age_days(path):
    try:
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 86400
    except OSError:
        return float("inf")


def _file_age_hours(path):
    try:
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 3600
    except OSError:
        return float("inf")


def _is_weekday():
    return datetime.now().weekday() < 5


def _now_iso():
    return datetime.now().isoformat()


def _run_id():
    return "ml-st-cit-%s" % datetime.now().strftime("%Y%m%d-%H%M")


def _raise_issue(proposals, severity, category, description, source="ml_shortterm_cit_agent"):
    run_id = _run_id()
    slug = description[:30].lower().replace(" ", "_").replace("-", "_")
    entry = {
        "id":          "%s-%s" % (run_id, slug[:20]),
        "severity":    severity,
        "category":    category,
        "description": description,
        "source":      source,
        "ts":          _now_iso(),
        "status":      "awaiting_chairman",
    }
    proposals.append(entry)
    logger.warning("  [%s] %s: %s", severity, category, description[:80])


def _newest_matching_file(patterns):
    """Return (path, age_hours) of the most recently modified file matching any pattern."""
    candidates = []
    for pat in patterns:
        for fpath in glob.glob(pat):
            if os.path.isfile(fpath):
                candidates.append(fpath)
    if not candidates:
        return None, float("inf")
    newest = min(candidates, key=lambda p: _file_age_hours(p))
    return newest, _file_age_hours(newest)


# =============================================================================
# CHECK A -- MODEL HEALTH
# =============================================================================

def check_model_health(proposals):
    logger.info("\n  [A] Short-term model health...")

    # Existence
    if not os.path.exists(_MODEL_PATH):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Short-term model file missing -- data/model.pkl does not exist")
        logger.info("    Model file missing -- skipping age/validation checks.")
        return

    age_days = _file_age_days(_MODEL_PATH)
    logger.info("    model.pkl age: %.1f days", age_days)

    if age_days > _MODEL_STALE_DAYS:
        _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                     "Short-term model stale -- last trained %.0f days ago (threshold %dd)"
                     % (age_days, _MODEL_STALE_DAYS))

    # Walk-forward score
    val = _load_json(_VALIDATION_PATH)
    if val is None:
        logger.info("    ml_validation_report.json not found -- skipping walk-forward check.")
    else:
        # Probe multiple known key structures
        wf_score = None
        results = val.get("results", {})
        for key in ("test1_walk_forward", "walk_forward", "wf"):
            if key in results:
                details = results[key].get("details", {})
                wf_score = details.get("mean_auc") or details.get("auc")
                break
        if wf_score is None:
            wf_score = val.get("walk_forward_auc") or val.get("auc")

        if wf_score is not None:
            logger.info("    Walk-forward AUC: %.4f (floor %.2f)", wf_score, _WALK_FORWARD_FLOOR)
            if wf_score < _WALK_FORWARD_FLOOR:
                _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                             "Short-term model walk-forward below threshold -- AUC %.4f < %.2f"
                             % (wf_score, _WALK_FORWARD_FLOOR))
        else:
            logger.info("    Walk-forward score not found in validation report.")

    logger.info("    Model health check done.")


# =============================================================================
# CHECK B -- PREDICTION OUTPUT HEALTH
# =============================================================================

def check_prediction_health(proposals):
    logger.info("\n  [B] Short-term prediction output health...")

    newest_path, age_hours = _newest_matching_file(_PRED_PATTERNS)

    if newest_path is None:
        if _is_weekday():
            _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                         "Short-term ML predictions stale -- no prediction/signal files found in data/")
        else:
            logger.info("    No prediction files found (weekend -- skipping flag).")
        return

    logger.info("    Newest signal/prediction file: %s (%.1fh old)", newest_path, age_hours)

    if age_hours > _PREDICTION_STALE_HOURS and _is_weekday():
        _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                     "Short-term ML predictions stale -- newest file %s is %.0fh old"
                     % (os.path.basename(newest_path), age_hours))

    # Validate JSON structure of newest file
    data = _load_json(newest_path)
    if data is None:
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Short-term prediction file corrupt (invalid JSON): %s"
                     % os.path.basename(newest_path))
    else:
        entry_count = len(data) if isinstance(data, list) else len(data) if isinstance(data, dict) else 0
        if entry_count == 0:
            _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                         "Short-term prediction file has 0 entries: %s"
                         % os.path.basename(newest_path))
        else:
            logger.info("    Prediction file valid, %d entries.", entry_count)

    logger.info("    Prediction health check done.")


# =============================================================================
# CHECK C -- PIPELINE RUN CHECK
# =============================================================================

def check_pipeline_run(proposals):
    logger.info("\n  [C] Short-term pipeline run check (log activity)...")

    newest_log, age_hours = _newest_matching_file(_LOG_PATTERNS)

    if newest_log is None:
        if _is_weekday():
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "Short-term ML pipeline not running -- no ml_chain/ml_shortterm "
                         "log files found in logs/")
        else:
            logger.info("    No ML short-term logs found (weekend -- skipping HIGH flag).")
        return

    logger.info("    Newest ML short-term log: %s (%.1fh old)", newest_log, age_hours)

    if age_hours > _LOG_STALE_HOURS and _is_weekday():
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Short-term ML pipeline not running -- newest log %s is %.0fh old"
                     % (os.path.basename(newest_log), age_hours))
    else:
        logger.info("    Pipeline log activity looks current.")

    logger.info("    Pipeline run check done.")


# =============================================================================
# CHECK D -- DATA DRIFT CHECK
# =============================================================================

def check_data_drift(proposals):
    logger.info("\n  [D] Feature importance drift check...")

    if not os.path.exists(_MODEL_PATH):
        logger.info("    model.pkl missing -- skipping drift check.")
        return

    val = _load_json(_VALIDATION_PATH)
    if val is None:
        logger.info("    No validation report -- skipping drift check.")
        return

    # Extract feature importances if present
    features = val.get("features") or val.get("feature_importances")
    if not features:
        logger.info("    No feature importance data in validation report -- skipping.")
        return

    # Look for a prior validation report snapshot to compare against
    prior_path = "data/ml_validation_report_prior.json"
    if not os.path.exists(prior_path):
        logger.info("    No prior validation snapshot -- saving current as baseline.")
        try:
            import shutil
            shutil.copy(_VALIDATION_PATH, prior_path)
        except Exception as e:
            logger.warning("    Could not save baseline: %s", e)
        return

    prior = _load_json(prior_path)
    if prior is None:
        logger.info("    Prior report unreadable -- skipping drift check.")
        return

    prior_features = prior.get("features") or prior.get("feature_importances")
    if not prior_features:
        logger.info("    Prior report has no feature list -- skipping drift check.")
        return

    # Compare top feature (first in list or top importance key)
    if isinstance(features, list) and isinstance(prior_features, list):
        current_top = features[0] if features else None
        prior_top = prior_features[0] if prior_features else None
        if current_top and prior_top and current_top != prior_top:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "Feature importance drift detected -- top feature changed from "
                         "'%s' to '%s'; consider retraining" % (prior_top, current_top))
        else:
            logger.info("    Top feature stable: %s", current_top)
    else:
        logger.info("    Feature importance format not comparable -- skipping drift check.")

    logger.info("    Data drift check done.")


# =============================================================================
# CHECK E -- IMPROVEMENT PROPOSALS
# =============================================================================

def check_improvement_proposals(proposals):
    logger.info("\n  [E] Improvement proposals...")

    if not os.path.exists(_MODEL_PATH):
        logger.info("    Model file missing -- skipping improvement proposals.")
        return

    age_days = _file_age_days(_MODEL_PATH)

    # Retraining cycle suggestion
    if age_days > _MODEL_RETRAIN_SUGGESTION:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "Short-term model due for retraining cycle -- model.pkl is %.0f days old "
                     "(review after 30 days)" % age_days)

    # Declining performance: compare walk-forward scores across runs
    val = _load_json(_VALIDATION_PATH)
    prior_path = "data/ml_validation_report_prior.json"
    prior = _load_json(prior_path)

    if val and prior:
        def _extract_wf(report):
            results = report.get("results", {})
            for key in ("test1_walk_forward", "walk_forward", "wf"):
                if key in results:
                    d = results[key].get("details", {})
                    score = d.get("mean_auc") or d.get("auc")
                    if score is not None:
                        return float(score)
            return report.get("walk_forward_auc") or report.get("auc")

        current_wf = _extract_wf(val)
        prior_wf   = _extract_wf(prior)

        if current_wf is not None and prior_wf is not None:
            delta = current_wf - prior_wf
            logger.info("    Walk-forward trend: %.4f -> %.4f (delta %.4f)", prior_wf, current_wf, delta)
            if delta < -0.01:
                _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                             "Short-term model performance declining -- walk-forward AUC "
                             "dropped %.4f (%.4f -> %.4f); review features" % (delta, prior_wf, current_wf))
        else:
            logger.info("    Could not extract walk-forward scores for trend check.")

    logger.info("    Improvement proposals done.")


# =============================================================================
# OUTPUT -- update chairman queue
# =============================================================================

def _update_chairman_queue(proposals):
    existing = []
    raw = _load_json(_CHAIRMAN_PATH)
    if isinstance(raw, list):
        existing = raw

    combined = existing + proposals
    combined = combined[-_CHAIRMAN_MAX_ENTRIES:]

    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_CHAIRMAN_PATH, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    logger.info("  Updated %s (%d entries total).", _CHAIRMAN_PATH, len(combined))


# =============================================================================
# MAIN
# =============================================================================

def main():
    # ── Inbox: process pending requests, meetings, broadcasts ──────────────────
    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("MLShortTermCITAgent")
    except Exception:
        pass  # inbox is always non-fatal

    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- ML SHORT-TERM CIT AGENT")
    logger.info("  %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    os.makedirs(_DATA_DIR, exist_ok=True)
    os.makedirs(_LOGS_DIR, exist_ok=True)

    lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another instance already running -- exiting.")
        lock_fh.close()
        sys.exit(0)

    try:
        proposals = []

        check_model_health(proposals)
        check_prediction_health(proposals)
        check_pipeline_run(proposals)
        check_data_drift(proposals)
        check_improvement_proposals(proposals)

        if proposals:
            _update_chairman_queue(proposals)
            logger.info(
                "\n  DONE -- %d proposal(s) raised to chairman_action_required.json.",
                len(proposals)
            )
        else:
            logger.info("\n  ALL CLEAR -- no ML short-term pod issues detected.")

        sys.exit(0)

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    if "--inbox-only" in sys.argv or "--thread-id" in sys.argv:
        import logging as _log_io
        _log_io.basicConfig(level=_log_io.INFO,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%H:%M:%S")
        from agent_inbox import main_inbox_only
        main_inbox_only("MLShortTermCITAgent")
        sys.exit(0)
    main()
