#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE CONTINUOUS IMPROVEMENT AGENT
=========================================================
Runs daily at 11 AM UTC (cron: 0 11 * * *).

PURPOSE
-------
Reviews the First Eagle pod's operational health and raises improvement
proposals to data/chairman_action_required.json exactly like the other
CIT agents do.

CHECKS
------
  a) Pick quality review        -- rejection rate, stall detection, borderline MoS
  b) Data quality checks        -- missing fields, EDGAR cache staleness, watchlist
  c) Module health              -- required scripts exist, cron wiring present
  d) Improvement proposals      -- fraud rate, moat coverage in approved picks

OUTPUT
------
  data/chairman_action_required.json -- appended on any finding (last 20 kept)
  logs/fe_cit_agent.log              -- via cron redirect
"""

import fcntl
import json
import logging
import os
import sys
from datetime import datetime, timedelta

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
logger = logging.getLogger("fe_cit_agent")

# -- File paths ---------------------------------------------------------------
_APPROVED_PATH     = "data/fe_approved_picks.json"
_REJECTED_PATH     = "data/fe_rejected_picks.json"
_BOARD_REPORT_PATH = "data/fe_board_report.json"
_WATCHLIST_PATH    = "data/fe_watchlist.json"
_EDGAR_CACHE_DIR   = "data/edgar_cache"
_CHAIRMAN_PATH     = "data/chairman_action_required.json"
_LOCK_PATH         = "data/.fe_cit_agent.lock"
_PIPELINE_LOG_PATH = "data/fe_pipeline_log.json"

# -- Thresholds ---------------------------------------------------------------
_REJECTION_RATE_FLOOR     = 0.80   # >80% rejection rate triggers flag
_NO_PICKS_DAYS            = 7      # no picks in last N days triggers stall flag
_LOW_MOS_THRESHOLD        = 0.27   # approved average MoS below this = borderline
_EDGAR_STALE_DAYS         = 10     # edgar cache files older than N days = stale
_WATCHLIST_STALE_DAYS     = 30     # watchlist target never updated after N days
_FRAUD_RATE_HIGH          = 0.30   # >30% fraud-flagged picks triggers flag
_BOARD_STALE_DAYS         = 8      # no board report in N days
_PIPELINE_LOG_STALE_HOURS = 25     # pipeline log older than this = real stall
_CHAIRMAN_MAX_ENTRIES     = 20

# -- Required FE scripts ------------------------------------------------------
_REQUIRED_SCRIPTS = [
    "fe_edgar_fundamentals.py",
    "fe_fraud_watchdog.py",
    "fe_intrinsic_value.py",
    "fe_pipeline.py",
    "fe_board.py",
]

# -- Required cron keywords (substring match against crontab output) ----------
_REQUIRED_CRON_KEYWORDS = [
    "fe_pipeline",
    "fe_board",
    "fe_position_monitor",
    "fe_watchlist_monitor",
]


# =============================================================================
# HELPERS
# =============================================================================

def _load_json(path):
    """Load JSON from path; return None on any error."""
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("  Could not load %s: %s", path, e)
    return None


def _file_age_days(path):
    """Return file mtime age in days; inf if missing."""
    try:
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 86400
    except OSError:
        return float("inf")


def _ts_age_days(ts):
    """Return age of an ISO timestamp in days; inf if blank/invalid."""
    if not ts:
        return float("inf")
    try:
        return (datetime.now() - datetime.fromisoformat(ts[:19])).total_seconds() / 86400
    except Exception:
        return float("inf")


def _now_iso():
    return datetime.now().isoformat()


def _run_id():
    return "fe-cit-%s" % datetime.now().strftime("%Y%m%d-%H%M")


def _raise_issue(proposals, severity, category, description, source="fe_cit_agent"):
    """Append a standardised proposal dict to the proposals list."""
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


def _get_crontab():
    """Return crontab output as a string, or empty string on failure."""
    try:
        import subprocess
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception:
        return ""


# =============================================================================
# CHECK A -- PICK QUALITY REVIEW
# =============================================================================

def check_pick_quality(proposals):
    logger.info("\n  [A] Pick quality review...")

    approved = _load_json(_APPROVED_PATH)
    rejected = _load_json(_REJECTED_PATH)

    if approved is None:
        approved = []
    if rejected is None:
        rejected = []

    # Normalise: approved/rejected files may be bare lists or dicts with a key
    if isinstance(approved, dict):
        approved = approved.get("approved_picks", [])
    if isinstance(rejected, dict):
        rejected = rejected.get("rejected_picks", [])

    now = datetime.now()
    window = timedelta(days=30)

    # Filter to last 30 days using reviewed_at or generated_at from pick_data
    def _pick_ts(p):
        ts = p.get("reviewed_at") or p.get("approved_at") or ""
        if not ts and isinstance(p.get("pick_data"), dict):
            ts = p["pick_data"].get("generated_at", "")
        return ts

    recent_approved = []
    recent_rejected = []
    for p in approved:
        ts = _pick_ts(p)
        if _ts_age_days(ts) <= 30 or not ts:
            recent_approved.append(p)
    for p in rejected:
        ts = _pick_ts(p)
        if _ts_age_days(ts) <= 30 or not ts:
            recent_rejected.append(p)

    total_recent = len(recent_approved) + len(recent_rejected)

    # Rejection rate check
    if total_recent > 0:
        rejection_rate = len(recent_rejected) / total_recent
        logger.info("    Rejection rate (30d): %.1f%% (%d/%d)",
                    rejection_rate * 100, len(recent_rejected), total_recent)
        if rejection_rate > _REJECTION_RATE_FLOOR:
            _raise_issue(
                proposals, "HIGH", "IMPROVEMENT",
                "FE board rejection rate high (%.0f%% over last 30 days) -- "
                "research quality may need improvement" % (rejection_rate * 100)
            )
    else:
        logger.info("    No picks in the 30-day window.")

    # Stall check: distinguish real stall from market-expensive 0-picks day
    # Real stall  = pipeline log not updated in >25h (pipeline did not run)
    # False alarm = pipeline log is fresh but picks are 0 (market too expensive)
    pipeline_log_age_hours = _file_age_days(_PIPELINE_LOG_PATH) * 24
    if pipeline_log_age_hours > _PIPELINE_LOG_STALE_HOURS:
        _raise_issue(
            proposals, "HIGH", "BUG_FIX",
            "FE pipeline stalled -- fe_pipeline_log.json is %.1fh old (>25h); "
            "pipeline did not run as scheduled" % pipeline_log_age_hours
        )
    else:
        # Pipeline ran — 0 picks just means market is expensive today
        all_picks = list(approved) + list(rejected)
        recent_7d = [p for p in all_picks
                     if _pick_ts(p) and _ts_age_days(_pick_ts(p)) <= 7]
        if recent_7d:
            logger.info("    %d pick(s) in last 7 days -- pipeline active.", len(recent_7d))
        else:
            logger.info(
                "    Pipeline healthy -- no undervalued candidates found today "
                "(market expensive, pipeline log is %.1fh old).", pipeline_log_age_hours
            )

    # Average MoS of approved picks (borderline check)
    if recent_approved:
        mos_values = []
        for p in recent_approved:
            # MoS may be on the entry directly or nested under pick_data
            mos = p.get("margin_of_safety")
            if mos is None and isinstance(p.get("pick_data"), dict):
                mos = p["pick_data"].get("margin_of_safety")
            if mos is not None:
                try:
                    mos_values.append(float(mos))
                except (TypeError, ValueError):
                    pass
        if mos_values:
            avg_mos = sum(mos_values) / len(mos_values)
            logger.info("    Average approved MoS: %.1f%%", avg_mos * 100)
            if avg_mos < _LOW_MOS_THRESHOLD:
                _raise_issue(
                    proposals, "MEDIUM", "IMPROVEMENT",
                    "FE approvals borderline -- average margin of safety %.1f%% "
                    "is barely above the 25%% threshold; conviction may be weak" % (avg_mos * 100)
                )
        else:
            logger.info("    No MoS data found in approved picks.")
    else:
        logger.info("    No recent approved picks to check MoS.")

    logger.info("    Pick quality check done.")


# =============================================================================
# CHECK B -- DATA QUALITY
# =============================================================================

def check_data_quality(proposals):
    logger.info("\n  [B] Data quality checks...")

    approved = _load_json(_APPROVED_PATH)
    if approved is None:
        approved = []
    if isinstance(approved, dict):
        approved = approved.get("approved_picks", [])

    # Missing required fields in approved picks
    required_fields = ["thesis", "intrinsic_value", "margin_of_safety"]
    bad_picks = []
    for p in approved:
        pick_data = p.get("pick_data") if isinstance(p.get("pick_data"), dict) else p
        ticker = pick_data.get("ticker") or p.get("ticker") or "UNKNOWN"
        missing = []
        for field in required_fields:
            val = pick_data.get(field)
            if val is None or val == "" or val == []:
                missing.append(field)
        if missing:
            bad_picks.append("%s(missing: %s)" % (ticker, ", ".join(missing)))

    if bad_picks:
        _raise_issue(
            proposals, "HIGH", "BUG_FIX",
            "FE approved picks have missing required fields -- %s" % "; ".join(bad_picks[:5])
        )
    else:
        logger.info("    All approved picks have required fields (or none exist yet).")

    # EDGAR cache staleness
    if not os.path.isdir(_EDGAR_CACHE_DIR):
        _raise_issue(
            proposals, "HIGH", "BUG_FIX",
            "EDGAR cache directory missing -- fe_edgar_fundamentals may not be running"
        )
    else:
        cache_files = [
            f for f in os.listdir(_EDGAR_CACHE_DIR)
            if os.path.isfile(os.path.join(_EDGAR_CACHE_DIR, f))
        ]
        if not cache_files:
            _raise_issue(
                proposals, "HIGH", "BUG_FIX",
                "EDGAR cache empty -- fe_edgar_fundamentals may not be running"
            )
        else:
            stale_count = 0
            for fname in cache_files:
                fpath = os.path.join(_EDGAR_CACHE_DIR, fname)
                if _file_age_days(fpath) > _EDGAR_STALE_DAYS:
                    stale_count += 1
            logger.info("    EDGAR cache: %d file(s), %d stale (>%dd).",
                        len(cache_files), stale_count, _EDGAR_STALE_DAYS)
            if stale_count > 0:
                _raise_issue(
                    proposals, "MEDIUM", "IMPROVEMENT",
                    "EDGAR cache stale -- %d of %d cache file(s) older than %d days; "
                    "fe_edgar_fundamentals may not be refreshing data" % (
                        stale_count, len(cache_files), _EDGAR_STALE_DAYS)
                )

    # Watchlist staleness
    watchlist_data = _load_json(_WATCHLIST_PATH)
    if watchlist_data is not None:
        items = watchlist_data if isinstance(watchlist_data, list) else watchlist_data.get("watchlist", [])
        stale_tickers = []
        for item in items:
            ticker = item.get("ticker", "?")
            added = item.get("added_date", "")
            if added and _ts_age_days(added) > _WATCHLIST_STALE_DAYS:
                stale_tickers.append(ticker)
        if stale_tickers:
            _raise_issue(
                proposals, "MEDIUM", "IMPROVEMENT",
                "FE watchlist has %d ticker(s) with target prices not updated in >%d days: %s" % (
                    len(stale_tickers), _WATCHLIST_STALE_DAYS, ", ".join(stale_tickers[:5]))
            )
        else:
            logger.info("    Watchlist items are current (or watchlist is empty).")
    else:
        logger.info("    fe_watchlist.json not found -- skipping watchlist staleness check.")

    logger.info("    Data quality check done.")


# =============================================================================
# CHECK C -- MODULE HEALTH
# =============================================================================

def check_module_health(proposals):
    logger.info("\n  [C] Module health...")

    # Required scripts exist
    missing_scripts = []
    for script in _REQUIRED_SCRIPTS:
        if not os.path.isfile(script):
            missing_scripts.append(script)
    if missing_scripts:
        _raise_issue(
            proposals, "HIGH", "BUG_FIX",
            "FE required script(s) missing: %s" % ", ".join(missing_scripts)
        )
    else:
        logger.info("    All required FE scripts present.")

    # Crontab wiring
    crontab_text = _get_crontab()
    missing_cron = []
    for keyword in _REQUIRED_CRON_KEYWORDS:
        if keyword not in crontab_text:
            missing_cron.append(keyword)
    if missing_cron:
        _raise_issue(
            proposals, "HIGH", "BUG_FIX",
            "FE crontab entries missing for: %s -- pipeline will not run automatically" % (
                ", ".join(missing_cron))
        )
    else:
        logger.info("    All required cron entries found.")

    logger.info("    Module health check done.")


# =============================================================================
# CHECK D -- IMPROVEMENT PROPOSALS
# =============================================================================

def check_improvement_proposals(proposals):
    logger.info("\n  [D] Improvement proposals...")

    approved = _load_json(_APPROVED_PATH)
    if approved is None:
        approved = []
    if isinstance(approved, dict):
        approved = approved.get("approved_picks", [])

    all_picks_raw = list(approved)
    rejected = _load_json(_REJECTED_PATH)
    if rejected:
        if isinstance(rejected, dict):
            rejected = rejected.get("rejected_picks", [])
        all_picks_raw += list(rejected)

    # Fraud rate check: fraction of all recent picks flagged HIGH fraud risk
    fraud_flagged = 0
    total_with_fraud_data = 0
    for p in all_picks_raw:
        pick_data = p.get("pick_data") if isinstance(p.get("pick_data"), dict) else p
        fraud = pick_data.get("fraud_risk") or pick_data.get("fraud_verdict") or ""
        if fraud:
            total_with_fraud_data += 1
            if str(fraud).upper() in ("HIGH", "BLOCKED"):
                fraud_flagged += 1

    if total_with_fraud_data > 0:
        fraud_rate = fraud_flagged / total_with_fraud_data
        logger.info("    Fraud rate in picks: %.1f%% (%d/%d)",
                    fraud_rate * 100, fraud_flagged, total_with_fraud_data)
        if fraud_rate > _FRAUD_RATE_HIGH:
            _raise_issue(
                proposals, "MEDIUM", "IMPROVEMENT",
                "FE universe generating many fraud-flagged tickers (%.0f%% rate) -- "
                "consider refining source universe" % (fraud_rate * 100)
            )
    else:
        logger.info("    No fraud risk data found in picks.")

    # Moat check: if all approved picks have moat_score = None or missing
    if approved:
        moat_present = 0
        for p in approved:
            pick_data = p.get("pick_data") if isinstance(p.get("pick_data"), dict) else p
            moat = pick_data.get("moat_score") or pick_data.get("moat")
            if moat is not None and str(moat).upper() not in ("NONE", "N/A", "UNKNOWN", ""):
                moat_present += 1
        if moat_present == 0:
            _raise_issue(
                proposals, "MEDIUM", "IMPROVEMENT",
                "FE approved picks lack moat scores -- screening criteria may be too loose; "
                "moat analysis should gate approval"
            )
        else:
            logger.info("    %d/%d approved pick(s) have moat data.", moat_present, len(approved))
    else:
        logger.info("    No approved picks to check moat data.")

    logger.info("    Improvement proposals check done.")


# =============================================================================
# OUTPUT -- update chairman queue
# =============================================================================

def _update_chairman_queue(proposals):
    """Merge new proposals into chairman_action_required.json (keep our last 20)."""
    # Delegates to chairman_queue.submit(). This used to do
    # `combined = combined[-_CHAIRMAN_MAX_ENTRIES:]`, which caps the *shared*
    # queue -- so this agent silently evicted other producers' open items
    # (one run took the file from 153 entries to 20 on 2026-07-31). An agent
    # may only ever cap its OWN entries.
    chairman_queue.submit(proposals, "fe_cit_agent",
                          path=_CHAIRMAN_PATH,
                          max_entries=_CHAIRMAN_MAX_ENTRIES)


# =============================================================================
# MAIN
# =============================================================================

def main():

    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("FECITAgent")
    except Exception:
        pass
    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- FE CONTINUOUS IMPROVEMENT AGENT")
    logger.info("  %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    # Single-instance lock
    lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another instance already running -- exiting.")
        lock_fh.close()
        sys.exit(0)

    try:
        proposals = []

        check_pick_quality(proposals)
        check_data_quality(proposals)
        check_module_health(proposals)
        check_improvement_proposals(proposals)

        if proposals:
            _update_chairman_queue(proposals)
            logger.info(
                "\n  DONE -- %d proposal(s) raised to chairman_action_required.json.",
                len(proposals)
            )
        else:
            logger.info("\n  ALL CLEAR -- no FE pod issues detected.")

        sys.exit(0)

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
