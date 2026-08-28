#!/usr/bin/env python3
"""
APEX CAPITAL -- APEX IMPROVEMENT BOARD
=======================================
Runs weekly on Sunday at 9 AM UTC (cron: 0 9 * * 0).

PURPOSE
-------
Cross-pod improvement idea generator. Reads the state of ALL pods and
synthesizes IMPROVEMENT proposals. Raises findings to chairman_action_required.json.

READS
-----
  - All *_cit_agent.log files for recent issue patterns
  - data/chairman_action_required.json for unresolved issue trends
  - Model validation reports for ML health trends
  - FE research library completeness and watchlist

GENERATES
---------
  IMPROVEMENT proposals for systemic patterns across all pods.

OUTPUT
------
  data/chairman_action_required.json -- appended on any finding (last 20 kept)
  logs/apex_improvement_board.log    -- via cron redirect
"""

import fcntl
import glob
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime

# Ages come from here, never from `datetime.now() - fromisoformat(ts[:19])`.
# chairman_action_required.json carries aware AND naive stamps under the same
# key; `age_days` aligns both against the right `now` instead of truncating.
from time_utils import age_days as _age_days

import chairman_queue

# -- Working directory --------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_SCRIPT_DIR)

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("apex_improvement_board")

# -- File paths ---------------------------------------------------------------
_CHAIRMAN_PATH      = "data/chairman_action_required.json"
_LOCK_PATH          = "data/.apex_improvement_board.lock"
_DATA_DIR           = "data"
_LOGS_DIR           = "logs"

# -- ML file paths ------------------------------------------------------------
_ST_MODEL_PATH      = "data/model.pkl"
_LT_MODEL_PATH      = "data/model_longterm.pkl"
_ST_VALIDATION      = "data/ml_validation_report.json"
_LT_VALIDATION      = "data/longterm_validation_report.json"
_FE_APPROVED_PATH   = "data/fe_approved_picks.json"
_FE_WATCHLIST_PATH  = "data/fe_watchlist.json"
_POSITIONS_PATH     = "data/portfolio_positions.json"
_RESEARCH_DIR       = "data/company_research"

# -- Thresholds ---------------------------------------------------------------
_ST_RETRAIN_DAYS        = 45
_LT_RETRAIN_DAYS        = 60
_ISSUE_RESOLUTION_FLOOR = 0.80   # 80% resolution rate expected
_RESEARCH_AGENTS_DAYS   = 30     # flag if no new FE research agent in N days
_CHAIRMAN_MAX_ENTRIES   = 20
_REVIEW_WINDOW_DAYS     = 30


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


def _now_iso():
    return datetime.now().isoformat()


def _run_id():
    return "apex-board-%s" % datetime.now().strftime("%Y%m%d-%H%M")


def _raise_issue(proposals, severity, category, description, source="apex_improvement_board"):
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


def _count_recent_log_issues(log_pattern, days=7):
    """Count WARNING/ERROR lines in log files modified within the last N days."""
    count = 0
    cutoff = datetime.now().timestamp() - (days * 86400)
    for fpath in glob.glob(log_pattern):
        if not os.path.isfile(fpath):
            continue
        if os.path.getmtime(fpath) < cutoff:
            continue
        try:
            with open(fpath, errors="replace") as f:
                for line in f:
                    if "WARNING" in line or "ERROR" in line or "[HIGH]" in line or "[MEDIUM]" in line:
                        count += 1
        except Exception:
            pass
    return count


# =============================================================================
# ANALYSIS 1 -- ML SHORT-TERM POD REVIEW
# =============================================================================

def review_ml_shortterm(proposals):
    logger.info("\n  [1] ML short-term pod review...")

    if not os.path.exists(_ST_MODEL_PATH):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "ML short-term model (model.pkl) is missing -- pod may be non-functional")
        return

    age_days = _file_age_days(_ST_MODEL_PATH)
    logger.info("    model.pkl: %.1f days old", age_days)

    if age_days > _ST_RETRAIN_DAYS:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "ML short-term model has been in production %.0f days without retraining -- "
                     "schedule a retraining cycle" % age_days)

    # Walk-forward trend from validation report
    val = _load_json(_ST_VALIDATION)
    if val:
        results = val.get("results", {})
        wf = None
        for key in ("test1_walk_forward", "walk_forward", "wf"):
            if key in results:
                d = results[key].get("details", {})
                wf = d.get("mean_auc") or d.get("auc")
                break
        if wf:
            logger.info("    ST walk-forward AUC: %.4f", wf)
            if wf < 0.54:
                _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                             "ML short-term walk-forward AUC %.4f is marginal -- "
                             "feature engineering review recommended before next production cycle" % wf)
    else:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "ML short-term has no validation report -- run a full validation "
                     "before next deployment")

    # Log issue count
    issue_count = _count_recent_log_issues("logs/ml_shortterm_cit_agent.log")
    if issue_count > 0:
        logger.info("    %d recent warning/error lines in ml_shortterm_cit_agent.log", issue_count)

    logger.info("    ML short-term review done.")


# =============================================================================
# ANALYSIS 2 -- ML LONG-TERM POD REVIEW
# =============================================================================

def review_ml_longterm(proposals):
    logger.info("\n  [2] ML long-term pod review...")

    if not os.path.exists(_LT_MODEL_PATH):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "ML long-term model (model_longterm.pkl) is missing -- pod may be non-functional")
        return

    age_days = _file_age_days(_LT_MODEL_PATH)
    logger.info("    model_longterm.pkl: %.1f days old", age_days)

    if age_days > _LT_RETRAIN_DAYS:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "ML long-term model has been in production %.0f days without retraining -- "
                     "schedule a retraining cycle (longer threshold: %dd)"
                     % (age_days, _LT_RETRAIN_DAYS))

    val = _load_json(_LT_VALIDATION)
    if val:
        results = val.get("results", {})
        wf = None
        for key in ("test1_walk_forward", "walk_forward", "wf"):
            if key in results:
                d = results[key].get("details", {})
                wf = d.get("mean_auc") or d.get("auc")
                break
        if wf:
            logger.info("    LT walk-forward AUC: %.4f", wf)
            if wf < 0.55:
                _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                             "ML long-term walk-forward AUC %.4f is below target -- "
                             "consider adding XBRL fundamentals or sector rotation features" % wf)
    else:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "ML long-term has no validation report -- run a full validation "
                     "before next deployment")

    logger.info("    ML long-term review done.")


# =============================================================================
# ANALYSIS 3 -- FIRST EAGLE POD REVIEW
# =============================================================================

def review_first_eagle(proposals):
    logger.info("\n  [3] First Eagle pod review...")

    # Research library completeness
    if not os.path.isdir(_RESEARCH_DIR):
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "FE company research library directory missing -- "
                     "deep research has not been set up")
    else:
        profiles = [
            item for item in os.listdir(_RESEARCH_DIR)
            if os.path.isdir(os.path.join(_RESEARCH_DIR, item))
            or item.endswith(".json")
        ]
        logger.info("    Research profiles: %d", len(profiles))

        watchlist = _load_json(_FE_WATCHLIST_PATH)
        if watchlist:
            items = watchlist if isinstance(watchlist, list) else watchlist.get("watchlist", [])
            universe_size = len(items)
            if universe_size > 0:
                coverage_pct = len(profiles) / universe_size * 100
                logger.info("    Research coverage: %d/%d = %.1f%% of watchlist",
                            len(profiles), universe_size, coverage_pct)
                _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                             "FE deep research has completed %d profile(s) -- %.1f%% of %d-ticker "
                             "watchlist universe; consider expanding research throughput"
                             % (len(profiles), coverage_pct, universe_size))
        if len(profiles) < 5:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "FE research library has only %d completed profile(s) -- "
                         "deep research scheduler may need priority review" % len(profiles))

    # FE approved picks freshness
    approved = _load_json(_FE_APPROVED_PATH)
    if approved:
        items = approved if isinstance(approved, list) else approved.get("approved_picks", [])
        recent = []
        for p in items:
            ts = (p.get("approved_at") or p.get("reviewed_at") or
                  (p.get("pick_data") or {}).get("generated_at", ""))
            days_old = _age_days(ts, default=None)
            if days_old is not None and days_old <= _REVIEW_WINDOW_DAYS:
                recent.append(p)
        logger.info("    FE approved picks in last 30d: %d", len(recent))

    # FE watchlist emptiness
    watchlist = _load_json(_FE_WATCHLIST_PATH)
    if watchlist is None:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "FE watchlist missing -- add target-priced candidates to "
                     "data/fe_watchlist.json")
    else:
        items = watchlist if isinstance(watchlist, list) else watchlist.get("watchlist", [])
        if not items:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "FE watchlist is empty -- add target-priced candidates")

    # FE CIT log issues in the last 7 days
    fe_issues = _count_recent_log_issues("logs/fe_cit_agent.log")
    logger.info("    FE CIT recent warnings: %d", fe_issues)
    if fe_issues > 5:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "FE CIT agent logged %d warnings/errors in last 7 days -- "
                     "review FE pod operational health" % fe_issues)

    logger.info("    First Eagle review done.")


# =============================================================================
# ANALYSIS 4 -- CHAIRMAN ISSUE RESOLUTION RATE
# =============================================================================

def review_issue_resolution(proposals):
    logger.info("\n  [4] Chairman issue resolution rate...")

    chairman = _load_json(_CHAIRMAN_PATH)
    if not isinstance(chairman, list) or not chairman:
        logger.info("    chairman_action_required.json empty -- skipping resolution check.")
        return

    # Filter to last 30 days.
    #
    # By AGE, not by comparing a parsed stamp against a naive `cutoff`.
    # chairman_action_required.json carries BOTH aware and naive `ts` values
    # under the same key, so neither spelling of the old comparison worked on
    # all of them: `fromisoformat(ts[:19])` threw the offset away and read the
    # row four hours too new, and dropping the slice would raise TypeError on
    # exactly the aware rows and silently discard them via the `except` — the
    # resolution RATE would then be computed over a denominator missing every
    # row a different producer wrote.
    recent = []
    for entry in chairman:
        age = _age_days(entry.get("ts", ""), default=None)
        if age is not None and age <= _REVIEW_WINDOW_DAYS:
            recent.append(entry)

    total = len(recent)
    if total == 0:
        logger.info("    No chairman issues in last 30 days -- OK.")
        return

    resolved = sum(
        1 for e in recent
        if e.get("status", "").lower() in ("resolved", "done", "closed", "approved", "complete")
    )
    awaiting = total - resolved
    resolution_rate = resolved / total if total > 0 else 1.0
    logger.info("    Issues (30d): %d total, %d resolved, %d awaiting (rate %.1f%%)",
                total, resolved, awaiting, resolution_rate * 100)

    # Count by source
    source_counts = Counter(e.get("source", "unknown") for e in recent)
    logger.info("    Issue sources: %s", dict(source_counts))

    if resolution_rate < _ISSUE_RESOLUTION_FLOOR:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "%d issues raised in last 30 days, %d resolved -- resolution rate "
                     "%.0f%% is below the %.0f%% target; chairman review recommended"
                     % (total, resolved, resolution_rate * 100, _ISSUE_RESOLUTION_FLOOR * 100))
    else:
        logger.info("    Resolution rate OK.")

    # Flag if any single source is generating many HIGH issues
    high_by_source = Counter(
        e.get("source", "unknown") for e in recent
        if e.get("severity") in ("HIGH", "CRITICAL")
    )
    for source, count in high_by_source.items():
        if count >= 5:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "High-severity issue cluster from '%s' -- %d HIGH/CRITICAL issues "
                         "in last 30 days; investigate root cause"
                         % (source, count))

    logger.info("    Issue resolution review done.")


# =============================================================================
# ANALYSIS 5 -- PORTFOLIO POSITION TRACKING
# =============================================================================

def review_portfolio_tracking(proposals):
    logger.info("\n  [5] Portfolio position tracking...")

    if not os.path.exists(_POSITIONS_PATH):
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "Portfolio positions file missing (data/portfolio_positions.json) -- "
                     "consider if position tracking is being used")
    else:
        positions = _load_json(_POSITIONS_PATH)
        if isinstance(positions, list) and len(positions) == 0:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "Portfolio positions table empty -- "
                         "consider if position tracking is actively maintained")
        elif isinstance(positions, dict):
            pos_list = positions.get("positions", [])
            if len(pos_list) == 0:
                _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                             "Portfolio positions list is empty -- "
                             "consider if position tracking is actively maintained")
            else:
                logger.info("    Portfolio has %d position(s).", len(pos_list))

    logger.info("    Portfolio tracking review done.")


# =============================================================================
# ANALYSIS 6 -- RESEARCH AGENT DIVERSITY (FE)
# =============================================================================

def review_research_agent_diversity(proposals):
    logger.info("\n  [6] FE research agent diversity...")

    # Check when FE research agent scripts were last modified
    fe_agents = glob.glob("agents/cos_fe_*.py") + glob.glob("agents/fe_*.py")
    if not fe_agents:
        fe_agents = glob.glob("cos_fe_*.py")

    if not fe_agents:
        logger.info("    No FE research agents found -- skipping.")
        return

    # Most recently modified FE agent
    newest_age = min(_file_age_days(f) for f in fe_agents)
    logger.info("    %d FE research agents; newest modified %.1f days ago",
                len(fe_agents), newest_age)

    if newest_age > _RESEARCH_AGENTS_DAYS:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "No new FE research agent types added or modified in %.0f days -- "
                     "consider expanding research coverage (last modification: %.0f days ago)"
                     % (_RESEARCH_AGENTS_DAYS, newest_age))

    logger.info("    Research agent diversity review done.")


# =============================================================================
# OUTPUT -- update chairman queue
# =============================================================================

def _update_chairman_queue(proposals):
    # Delegates to chairman_queue.submit(). This used to do
    # `combined = combined[-_CHAIRMAN_MAX_ENTRIES:]`, which caps the *shared*
    # queue -- so this agent silently evicted other producers' open items
    # (one run took the file from 153 entries to 20 on 2026-07-31). An agent
    # may only ever cap its OWN entries.
    chairman_queue.submit(proposals, "apex_improvement_board",
                          path=_CHAIRMAN_PATH,
                          max_entries=_CHAIRMAN_MAX_ENTRIES)


# =============================================================================
# MAIN
# =============================================================================

def main():
    # ── Inbox: process pending requests, meetings, broadcasts ──────────────────
    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("ApexImprovementBoard")
    except Exception:
        pass  # inbox is always non-fatal

    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- APEX IMPROVEMENT BOARD")
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

        review_ml_shortterm(proposals)
        review_ml_longterm(proposals)
        review_first_eagle(proposals)
        review_issue_resolution(proposals)
        review_portfolio_tracking(proposals)
        review_research_agent_diversity(proposals)

        if proposals:
            _update_chairman_queue(proposals)
            logger.info(
                "\n  DONE -- %d cross-pod improvement proposal(s) raised.",
                len(proposals)
            )
        else:
            logger.info("\n  ALL CLEAR -- no cross-pod improvement proposals this week.")

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
        main_inbox_only("ApexImprovementBoard")
        sys.exit(0)
    main()
