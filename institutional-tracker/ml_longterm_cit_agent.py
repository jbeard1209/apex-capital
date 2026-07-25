#!/usr/bin/env python3
"""
APEX CAPITAL -- ML LONG-TERM CONTINUOUS IMPROVEMENT AGENT
==========================================================
Runs daily at noon UTC (cron: 0 12 * * *).

PURPOSE
-------
Monitors the ML long-term pod (model_longterm.pkl, outcome_365d target,
ml_chain_lt*.py / ml_longterm*.py pipeline) and raises improvement proposals
to chairman_action_required.json.

CHECKS
------
  A) Model health       -- existence, age, walk-forward score
  B) Label coverage     -- outcome_365d coverage in pipeline_snapshots
  C) Prediction health  -- output file freshness and validity
  D) Improvement proposals -- retraining, fundamental feature gap

OUTPUT
------
  data/chairman_action_required.json -- appended on any finding (last 20 kept)
  logs/ml_longterm_cit_agent.log     -- via cron redirect
"""

import fcntl
import glob
import json
import logging
import os
import sqlite3
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
logger = logging.getLogger("ml_longterm_cit")

# -- File paths ---------------------------------------------------------------
_MODEL_PATH         = "data/model_longterm.pkl"
_VALIDATION_PATH    = "data/longterm_validation_report.json"
_DB_PATH            = "data/historical_13f.db"
_CHAIRMAN_PATH      = "data/chairman_action_required.json"
_LOCK_PATH          = "data/.ml_longterm_cit.lock"
_LOGS_DIR           = "logs"
_DATA_DIR           = "data"

# -- Thresholds ---------------------------------------------------------------
_MODEL_STALE_DAYS           = 90
_MODEL_RETRAIN_SUGGESTION   = 60
_WALK_FORWARD_FLOOR         = 0.535
_LABEL_COVERAGE_FLOOR       = 0.50     # 50% minimum outcome_365d labeled
_LABEL_CUTOFF_MAX_DAYS      = 400      # labels older than this are too stale
_PREDICTION_STALE_HOURS     = 48
_LOG_STALE_HOURS            = 48
_CHAIRMAN_MAX_ENTRIES       = 20

# -- Long-term log and prediction file patterns -------------------------------
_LOG_PATTERNS = [
    "logs/ml_chain_lt*.log",
    "logs/ml_longterm*.log",
    "logs/label_builder*.log",
]

_PRED_PATTERNS = [
    "data/ml_longterm_team_brief.json",
    "data/ml_longterm_board_report.json",
    "data/ml_longterm_experiment_queue.json",
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
    return "ml-lt-cit-%s" % datetime.now().strftime("%Y%m%d-%H%M")


def _raise_issue(proposals, severity, category, description, source="ml_longterm_cit_agent"):
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
    logger.info("\n  [A] Long-term model health...")

    if not os.path.exists(_MODEL_PATH):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Long-term model file missing -- data/model_longterm.pkl does not exist")
        logger.info("    Model file missing -- skipping age/validation checks.")
        return

    age_days = _file_age_days(_MODEL_PATH)
    logger.info("    model_longterm.pkl age: %.1f days", age_days)

    if age_days > _MODEL_STALE_DAYS:
        _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                     "Long-term model stale -- last trained %.0f days ago (threshold %dd)"
                     % (age_days, _MODEL_STALE_DAYS))

    # Walk-forward score
    val = _load_json(_VALIDATION_PATH)
    if val is None:
        logger.info("    longterm_validation_report.json not found -- skipping walk-forward check.")
    else:
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
            logger.info("    Walk-forward AUC: %.4f (floor %.3f)", wf_score, _WALK_FORWARD_FLOOR)
            if wf_score < _WALK_FORWARD_FLOOR:
                _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                             "Long-term model walk-forward below threshold -- AUC %.4f < %.3f"
                             % (wf_score, _WALK_FORWARD_FLOOR))
        else:
            logger.info("    Walk-forward score not found in validation report.")

    logger.info("    Model health check done.")


# =============================================================================
# CHECK B -- LABEL COVERAGE
# =============================================================================

def check_label_coverage(proposals):
    logger.info("\n  [B] Long-term label coverage (outcome_365d)...")

    if not os.path.exists(_DB_PATH):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "DB file missing -- %s does not exist" % _DB_PATH)
        return

    try:
        conn = sqlite3.connect(_DB_PATH)
        c = conn.cursor()

        # Check if pipeline_snapshots table has outcome_365d
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_snapshots'")
        if not c.fetchone():
            logger.info("    pipeline_snapshots table not found -- skipping label coverage check.")
            conn.close()
            return

        c.execute("PRAGMA table_info(pipeline_snapshots)")
        cols = [row[1] for row in c.fetchall()]
        if "outcome_365d" not in cols:
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "outcome_365d column missing from pipeline_snapshots -- "
                         "long-term labels have not been built")
            conn.close()
            return

        c.execute("SELECT COUNT(*) FROM pipeline_snapshots")
        total = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_365d IS NOT NULL")
        labeled = c.fetchone()[0]

        c.execute("SELECT MAX(snapshot_date) FROM pipeline_snapshots WHERE outcome_365d IS NOT NULL")
        latest_labeled = c.fetchone()[0]

        conn.close()

        if total == 0:
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "pipeline_snapshots is empty -- no data to train on")
            return

        coverage_pct = labeled / total
        logger.info("    outcome_365d coverage: %d/%d rows = %.1f%%",
                    labeled, total, coverage_pct * 100)

        if coverage_pct < _LABEL_COVERAGE_FLOOR:
            _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                         "outcome_365d label coverage below %.0f%% (%d/%d rows = %.1f%%) -- "
                         "model training data may be thin"
                         % (_LABEL_COVERAGE_FLOOR * 100, labeled, total, coverage_pct * 100))

        # Check if latest labeled date is too old
        if latest_labeled:
            try:
                latest_dt = datetime.fromisoformat(latest_labeled)
                days_since_label = (datetime.now() - latest_dt).days
                logger.info("    Latest labeled snapshot_date: %s (%d days ago)",
                            latest_labeled, days_since_label)
                if days_since_label > _LABEL_CUTOFF_MAX_DAYS:
                    _raise_issue(proposals, "HIGH", "BUG_FIX",
                                 "outcome_365d labels too old -- latest labeled snapshot_date "
                                 "%s is %d days ago (threshold %dd); "
                                 "forward prices may be stale"
                                 % (latest_labeled, days_since_label, _LABEL_CUTOFF_MAX_DAYS))
            except Exception as e:
                logger.warning("    Could not parse latest_labeled date: %s", e)

    except sqlite3.Error as e:
        logger.warning("    DB error during label coverage check: %s", e)
        _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                     "Long-term label coverage check failed -- DB error: %s" % str(e)[:60])

    logger.info("    Label coverage check done.")


# =============================================================================
# CHECK C -- PREDICTION HEALTH
# =============================================================================

def check_prediction_health(proposals):
    logger.info("\n  [C] Long-term prediction output health...")

    newest_path, age_hours = _newest_matching_file(_PRED_PATTERNS)

    if newest_path is None:
        if _is_weekday():
            _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                         "Long-term ML output files stale -- no ml_longterm output found in data/")
        else:
            logger.info("    No long-term output files found (weekend -- skipping flag).")
        return

    logger.info("    Newest long-term output: %s (%.1fh old)", newest_path, age_hours)

    if age_hours > _PREDICTION_STALE_HOURS and _is_weekday():
        _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                     "Long-term ML output stale -- newest file %s is %.0fh old"
                     % (os.path.basename(newest_path), age_hours))

    data = _load_json(newest_path)
    if data is None:
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Long-term output file corrupt (invalid JSON): %s"
                     % os.path.basename(newest_path))
    else:
        entry_count = len(data) if isinstance(data, (list, dict)) else 0
        if entry_count == 0:
            _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                         "Long-term output file has 0 entries: %s"
                         % os.path.basename(newest_path))
        else:
            logger.info("    Output file valid, %d entries.", entry_count)

    logger.info("    Prediction health check done.")


# =============================================================================
# CHECK D -- IMPROVEMENT PROPOSALS
# =============================================================================

def check_improvement_proposals(proposals):
    logger.info("\n  [D] Long-term improvement proposals...")

    if not os.path.exists(_MODEL_PATH):
        logger.info("    Model file missing -- skipping improvement proposals.")
        return

    age_days = _file_age_days(_MODEL_PATH)

    # Retraining cycle suggestion
    if age_days > _MODEL_RETRAIN_SUGGESTION:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "Long-term model due for retraining cycle -- model_longterm.pkl "
                     "is %.0f days old (review every %dd)" % (age_days, _MODEL_RETRAIN_SUGGESTION))

    # Check if model predates fundamental features
    # Fundamental features were added in ml_chain_lt_r1 (logged in ml_chain_lt_r1.log)
    # We check via the validation report features list
    val = _load_json(_VALIDATION_PATH)
    if val:
        features = val.get("features", [])
        fundamental_features = [
            "rd_regime_bull", "rd_vix", "rd_spy_rsi",
            "insider_buy_flag", "earnings_surprise_flag"
        ]
        has_fundamentals = any(f in features for f in fundamental_features)
        if not has_fundamentals and features:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "Long-term model predates fundamental features -- "
                         "retrain recommended with rd_regime_bull, insider_buy_flag, "
                         "earnings_surprise_flag features")
        elif has_fundamentals:
            logger.info("    Model includes fundamental features -- OK.")

    # SEC EDGAR XBRL coverage suggestion
    edgar_cache = "data/edgar_cache"
    if os.path.isdir(edgar_cache):
        cache_count = len([f for f in os.listdir(edgar_cache) if os.path.isfile(os.path.join(edgar_cache, f))])
        logger.info("    EDGAR cache has %d files.", cache_count)
        if cache_count > 0:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "Consider whether SEC EDGAR XBRL expanded coverage could improve "
                         "long-term model feature set -- %d EDGAR cache files available"
                         % cache_count)

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
        _run_inbox("MLLongTermCITAgent")
    except Exception:
        pass  # inbox is always non-fatal

    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- ML LONG-TERM CIT AGENT")
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
        check_label_coverage(proposals)
        check_prediction_health(proposals)
        check_improvement_proposals(proposals)

        if proposals:
            _update_chairman_queue(proposals)
            logger.info(
                "\n  DONE -- %d proposal(s) raised to chairman_action_required.json.",
                len(proposals)
            )
        else:
            logger.info("\n  ALL CLEAR -- no ML long-term pod issues detected.")

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
        main_inbox_only("MLLongTermCITAgent")
        sys.exit(0)
    main()
