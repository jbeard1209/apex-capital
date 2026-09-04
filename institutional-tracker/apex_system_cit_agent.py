#!/usr/bin/env python3
"""
APEX CAPITAL -- APEX SYSTEM CIT AGENT
======================================
Runs daily at 1 PM UTC (cron: 0 13 * * *).

PURPOSE
-------
Overall infrastructure and cross-pod health monitor. Checks GCP health,
database integrity, cron job activity, critical data file validity, email
buffer, and EDGAR cache freshness.

CHECKS
------
  A) GCP health          -- disk usage, memory, cron daemon
  B) Database health     -- holdings count, data freshness, file size
  C) All cron jobs       -- log freshness per scheduled job
  D) Critical data files -- existence and valid JSON
  E) Email buffer        -- overflow and loop detection
  F) EDGAR cache         -- freshness and completeness
  G) Improvement proposals -- research library, watchlist, IP stability

OUTPUT
------
  data/chairman_action_required.json -- appended on any finding (last 20 kept)
  logs/apex_system_cit_agent.log     -- via cron redirect
"""

import fcntl
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
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
logger = logging.getLogger("apex_system_cit")

# -- File paths ---------------------------------------------------------------
_CHAIRMAN_PATH   = "data/chairman_action_required.json"
_LOCK_PATH       = "data/.apex_system_cit.lock"
_DB_PATH         = "data/historical_13f.db"
_EMAIL_BUFFER    = "data/email_buffer.json"
# Where fe_edgar_fundamentals ACTUALLY writes — see fe_edgar_fundamentals.CACHE_DIR.
# The old path "data/edgar_cache" was a phantom: fe_edgar_fundamentals wrote to
# `fe_fundamentals_cache/` and never populated `edgar_cache/`, so this check filed
# "EDGAR cache empty -- fe_edgar_fundamentals may not be running" every day for
# ~1000 fresh files sitting one directory over. Classic wiring contract bug
# ([[wiring_contract_bugs]]).
_EDGAR_CACHE_DIR = "data/fe_fundamentals_cache"
_DATA_DIR        = "data"
_LOGS_DIR        = "logs"

# -- Thresholds ---------------------------------------------------------------
_DISK_CRITICAL_PCT      = 80
_DISK_ELEVATED_PCT      = 70
_MEMORY_LOW_MB          = 500
_DB_STALE_DAYS          = 120
_DB_LARGE_GB            = 10
_LOG_STALE_HOURS        = 36
_EMAIL_OVERFLOW         = 50
_EMAIL_LOOP_THRESHOLD   = 3
_EDGAR_STALE_DAYS       = 30
_CHAIRMAN_MAX_ENTRIES   = 20
_KNOWN_GCP_IP           = "34.182.202.196"
_GCP_METADATA_IP_URL    = ("http://metadata.google.internal/computeMetadata/v1/"
                           "instance/network-interfaces/0/access-configs/0/external-ip")

def _gcp_instance_external_ip():
    """This instance's external IP per GCP metadata, or None if not on GCP.

    Returns None rather than raising: "I am not the host that check is about"
    is a normal outcome, not an error, and the caller skips on it.
    """
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "5",
             "-H", "Metadata-Flavor: Google", _GCP_METADATA_IP_URL],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


# -- Critical data files that must exist and be valid JSON --------------------
_CRITICAL_FILES = [
    "data/fe_picks_queue.json",
    "data/fe_approved_picks.json",
    "data/fe_cio_slate.json",
    "data/agent_registry.json",
    "data/chairman_action_required.json",
]

# -- Cron log patterns: (cron_keyword, log_glob, label) ----------------------
# We check that cron-scheduled jobs leave fresh log traces.
_CRON_LOG_CHECKS = [
    ("ml_improvement_agent",      "logs/ml_improvement_*.log",           "ML improvement agent"),
    ("ml_data_watchdog",          "logs/ml_watchdog_*.log",              "ML data watchdog"),
    ("ml_drift_monitor",          "logs/ml_drift_*.log",                 "ML drift monitor"),
    ("ml_alpha_decay_monitor",    "logs/ml_decay_*.log",                 "ML alpha decay monitor"),
    ("ml_research_team_leader",   "logs/ml_team_leader_*.log",           "ML research team leader"),
    ("cit_pipeline_integrity",    "logs/pipeline_integrity_*.log",       "CIT pipeline integrity"),
    ("edgar_scrape",              "logs/edgar_scrape_*.log",             "EDGAR scraper"),
    ("fe_cit_agent",              "logs/fe_cit_agent_*.log",             "FE CIT agent"),
    ("fe_pipeline",               "logs/fe_pipeline_*.log",              "FE pipeline"),
    ("fe_watchlist_monitor",      "logs/fe_watchlist_monitor_*.log",     "FE watchlist monitor"),
    ("portfolio_risk_dashboard",  "logs/portfolio_risk_dashboard.log",   "Portfolio risk dashboard"),
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


def _newest_glob_age_hours(pattern):
    """Return age in hours of the most recently modified file matching pattern."""
    candidates = glob.glob(pattern)
    candidates = [p for p in candidates if os.path.isfile(p)]
    if not candidates:
        return float("inf")
    newest = min(candidates, key=lambda p: _file_age_hours(p))
    return _file_age_hours(newest)


def _latest_log(pattern):
    """Return age in hours of the most recently modified log matching pattern.
    Returns None if no match found (caller can treat as missing/stale)."""
    candidates = glob.glob(pattern)
    candidates = [p for p in candidates if os.path.isfile(p)]
    if not candidates:
        return None
    newest = min(candidates, key=lambda p: _file_age_hours(p))
    return _file_age_hours(newest)


def _is_weekday():
    return datetime.now().weekday() < 5


def _now_iso():
    return datetime.now().isoformat()


def _run_id():
    return "apex-sys-cit-%s" % datetime.now().strftime("%Y%m%d-%H%M")


def _raise_issue(proposals, severity, category, description, source="apex_system_cit_agent"):
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


# =============================================================================
# CHECK A -- GCP HEALTH
# =============================================================================

def check_gcp_health(proposals):
    logger.info("\n  [A] GCP health checks...")

    # Disk usage
    try:
        df_out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10).stdout
        for line in df_out.splitlines():
            if "/" in line and line.split()[-1] == "/":
                parts = line.split()
                usage_str = parts[4].rstrip("%")
                usage_pct = int(usage_str)
                logger.info("    Disk usage: %d%%", usage_pct)
                if usage_pct >= _DISK_CRITICAL_PCT:
                    _raise_issue(proposals, "HIGH", "BUG_FIX",
                                 "GCP disk usage critical -- %d%% used (threshold %d%%)"
                                 % (usage_pct, _DISK_CRITICAL_PCT))
                elif usage_pct >= _DISK_ELEVATED_PCT:
                    _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                                 "GCP disk usage elevated -- %d%% used (threshold %d%%)"
                                 % (usage_pct, _DISK_ELEVATED_PCT))
                else:
                    logger.info("    Disk OK.")
                break
    except Exception as e:
        logger.warning("    Could not check disk usage: %s", e)

    # Memory
    try:
        free_out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=10).stdout
        for line in free_out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                available_mb = int(parts[6]) if len(parts) > 6 else int(parts[1]) - int(parts[2])
                logger.info("    Memory available: %d MB", available_mb)
                if available_mb < _MEMORY_LOW_MB:
                    _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                                 "GCP memory low -- only %d MB available (threshold %d MB)"
                                 % (available_mb, _MEMORY_LOW_MB))
                else:
                    logger.info("    Memory OK.")
                break
    except Exception as e:
        logger.warning("    Could not check memory: %s", e)

    # Cron daemon
    try:
        cron_status = subprocess.run(
            ["systemctl", "is-active", "cron"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
        logger.info("    Cron daemon status: %s", cron_status)
        if cron_status != "active":
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "Cron daemon not active ('%s') -- all scheduled tasks are dead"
                         % cron_status)
        else:
            logger.info("    Cron daemon active -- OK.")
    except Exception as e:
        logger.warning("    Could not check cron daemon: %s", e)

    logger.info("    GCP health check done.")


# =============================================================================
# CHECK B -- DATABASE HEALTH
# =============================================================================

def check_database_health(proposals):
    logger.info("\n  [B] Database health...")

    if not os.path.exists(_DB_PATH):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "DB file missing -- %s does not exist" % _DB_PATH)
        return

    # File size check
    try:
        size_bytes = os.path.getsize(_DB_PATH)
        size_gb = size_bytes / (1024 ** 3)
        logger.info("    DB size: %.2f GB", size_gb)
        if size_gb > _DB_LARGE_GB:
            _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                         "DB large (%.1f GB) -- consider archiving old snapshots" % size_gb)
    except OSError as e:
        logger.warning("    Could not stat DB file: %s", e)

    try:
        conn = sqlite3.connect(_DB_PATH)
        c = conn.cursor()

        # Check holdings count
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='holdings'")
        if c.fetchone():
            c.execute("SELECT COUNT(*) FROM holdings")
            count = c.fetchone()[0]
            logger.info("    Holdings table rows: %d", count)
            if count == 0:
                _raise_issue(proposals, "HIGH", "BUG_FIX",
                             "13F holdings table is empty -- EDGAR scraper may not be running")
            else:
                # Check most recent filing date
                c.execute("SELECT MAX(filing_date) FROM holdings")
                latest = c.fetchone()[0]
                logger.info("    Latest filing_date: %s", latest)
                if latest:
                    try:
                        latest_dt = datetime.fromisoformat(latest)
                        days_old = (datetime.now() - latest_dt).days
                        if days_old > _DB_STALE_DAYS:
                            _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                                         "13F data stale -- latest filing_date %s is %d days ago "
                                         "(threshold %dd)" % (latest, days_old, _DB_STALE_DAYS))
                        else:
                            logger.info("    13F data current (%d days old).", days_old)
                    except Exception as e:
                        logger.warning("    Could not parse filing_date: %s", e)
        else:
            logger.info("    holdings table not found -- skipping count check.")

        conn.close()

    except sqlite3.Error as e:
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "DB read error -- %s: %s" % (_DB_PATH, str(e)[:80]))

    logger.info("    Database health check done.")


# =============================================================================
# CHECK C -- ALL CRON JOBS RUNNING
# =============================================================================

def check_cron_jobs(proposals):
    logger.info("\n  [C] Cron job log freshness...")

    if not _is_weekday():
        logger.info("    Weekend -- skipping stale log checks.")
        return

    stale_jobs = []
    for keyword, log_pattern, label in _CRON_LOG_CHECKS:
        age_hours = _newest_glob_age_hours(log_pattern)
        if age_hours == float("inf"):
            logger.info("    %s: no log found", label)
            # Only flag jobs that clearly should have logs (not weekend-only)
            # Skip EDGAR scraper (runs Mon/Thu only) on days other than Mon/Thu/Fri
            weekday = datetime.now().weekday()
            if keyword in ("edgar_scrape",) and weekday not in (0, 1, 3):
                continue
            stale_jobs.append((label, "no log found"))
        elif age_hours > _LOG_STALE_HOURS:
            logger.info("    %s: %.1fh old (STALE)", label, age_hours)
            stale_jobs.append((label, "%.0fh old" % age_hours))
        else:
            logger.info("    %s: %.1fh old -- OK", label, age_hours)

    if stale_jobs:
        for label, reason in stale_jobs:
            _raise_issue(proposals, "MEDIUM", "BUG_FIX",
                         "Cron job stale on weekday -- %s log %s (threshold %dh)"
                         % (label, reason, _LOG_STALE_HOURS))
    else:
        logger.info("    All cron job logs are current.")

    logger.info("    Cron job check done.")


# =============================================================================
# CHECK D -- CRITICAL DATA FILES
# =============================================================================

def check_critical_files(proposals):
    logger.info("\n  [D] Critical data file integrity...")

    for fpath in _CRITICAL_FILES:
        if not os.path.exists(fpath):
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "Critical file missing -- %s does not exist" % fpath)
            continue

        try:
            with open(fpath) as f:
                json.load(f)
            logger.info("    %s -- OK (valid JSON)", fpath)
        except Exception as e:
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "Critical file is corrupt JSON -- %s: %s" % (fpath, str(e)[:60]))

    logger.info("    Critical file check done.")


# =============================================================================
# CHECK E -- EMAIL BUFFER HEALTH
# =============================================================================

def check_email_buffer(proposals):
    logger.info("\n  [E] Email buffer health...")

    buffer = _load_json(_EMAIL_BUFFER)
    if not isinstance(buffer, list):
        logger.info("    email_buffer.json missing or not a list -- skipping.")
        return

    logger.info("    Email buffer size: %d", len(buffer))

    if len(buffer) > _EMAIL_OVERFLOW:
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Email buffer overflow -- %d unsent emails in buffer "
                     "(threshold %d); email sender may be broken"
                     % (len(buffer), _EMAIL_OVERFLOW))

    # Loop detection: a genuine loop is a BURST (an agent's bug regenerating the
    # same mail in seconds), not four scheduled re-runs over the trading day. The
    # detector used to key on subject alone and count over the whole buffer, so
    # every day it saw R&D and CoS mails from 4-6 cron fires and posted
    # PIPELINE_ALERT for a state that the 22:00 digest will collapse to one line.
    # See email_buffer.buffer_email's docstring for why dedupe is delayed until
    # flush; see [[cit_bug_fixer_alert_loop]] for the alert-storm that produced.
    #
    # Two fixes here:
    #   1. Key on `dedup_key or subject` — the same key flush uses. When the
    #      producer supplied an identity, honor it; the subject may be a display
    #      string that carries a count, not an identity.
    #   2. Require a burst window — 3+ enqueues under the same key within an
    #      hour, not spread across the whole day. Widely-spaced same-key entries
    #      are what flush is FOR and are not a defect.
    from datetime import datetime, timedelta

    _BURST_WINDOW = timedelta(hours=1)
    grouped: dict = {}
    for entry in buffer:
        if not isinstance(entry, dict):
            continue
        subj = entry.get("subject", "").strip()
        if not subj or subj.startswith("[IRM REVIEW]"):
            continue
        key = entry.get("dedup_key") or subj.lower()
        ts_raw = entry.get("buffered_at", "")
        try:
            ts = datetime.fromisoformat(ts_raw) if ts_raw else None
        except (TypeError, ValueError):
            ts = None
        grouped.setdefault(key, []).append((ts, subj))

    def _is_burst(items):
        # A burst is _EMAIL_LOOP_THRESHOLD+ entries whose timestamps span less
        # than one hour. Entries without a timestamp are conservative — treated
        # as unknown and not counted as burst evidence.
        stamps = sorted([ts for ts, _ in items if ts is not None])
        if len(stamps) < _EMAIL_LOOP_THRESHOLD:
            return False
        # Sliding window: any run of THRESHOLD stamps within _BURST_WINDOW.
        for i in range(len(stamps) - _EMAIL_LOOP_THRESHOLD + 1):
            if stamps[i + _EMAIL_LOOP_THRESHOLD - 1] - stamps[i] <= _BURST_WINDOW:
                return True
        return False

    loops = {k: len(items) for k, items in grouped.items() if _is_burst(items)}
    if loops:
        examples = "; ".join(
            '"%s" x%d' % ((k if len(k) < 50 else k[:47] + "..."), v)
            for k, v in list(loops.items())[:3]
        )
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "Email burst detected -- %d key(s) with %d+ enqueues within 1h: %s"
                     % (len(loops), _EMAIL_LOOP_THRESHOLD, examples[:120]))
    else:
        logger.info("    No email loops detected.")

    logger.info("    Email buffer check done.")


# =============================================================================
# CHECK F -- EDGAR CACHE FRESHNESS
# =============================================================================

def check_edgar_cache(proposals):
    logger.info("\n  [F] EDGAR cache freshness...")

    if not os.path.isdir(_EDGAR_CACHE_DIR):
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "EDGAR cache directory missing -- "
                     "fe_edgar_fundamentals may not be running")
        return

    cache_files = [
        f for f in os.listdir(_EDGAR_CACHE_DIR)
        if os.path.isfile(os.path.join(_EDGAR_CACHE_DIR, f))
    ]
    logger.info("    EDGAR cache files: %d", len(cache_files))

    if len(cache_files) == 0:
        _raise_issue(proposals, "HIGH", "BUG_FIX",
                     "EDGAR cache empty (0 files in data/edgar_cache/) -- "
                     "fe_edgar_fundamentals may not be running")
        return

    stale_count = 0
    for fname in cache_files:
        fpath = os.path.join(_EDGAR_CACHE_DIR, fname)
        if _file_age_days(fpath) > _EDGAR_STALE_DAYS:
            stale_count += 1

    if stale_count > 0:
        _raise_issue(proposals, "MEDIUM", "IMPROVEMENT",
                     "EDGAR cache partially stale -- %d of %d file(s) older than %dd; "
                     "fe_edgar_fundamentals may not be refreshing data"
                     % (stale_count, len(cache_files), _EDGAR_STALE_DAYS))
    else:
        logger.info("    EDGAR cache fresh -- OK.")

    logger.info("    EDGAR cache check done.")


# =============================================================================
# CHECK G -- IMPROVEMENT PROPOSALS
# =============================================================================

def check_improvement_proposals(proposals):
    logger.info("\n  [G] System improvement proposals...")

    # Deep research library completeness
    research_dir = "data/company_research"
    if not os.path.isdir(research_dir):
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "Deep research library directory missing -- "
                     "data/company_research/ does not exist")
    else:
        completed = []
        for item in os.listdir(research_dir):
            item_path = os.path.join(research_dir, item)
            if os.path.isdir(item_path) or item.endswith(".json"):
                completed.append(item)
        logger.info("    Company research profiles: %d", len(completed))
        if len(completed) < 5:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "Deep research library has only %d completed profile(s) -- "
                         "research scheduler may need priority review"
                         % len(completed))

    # FE watchlist check
    watchlist = _load_json("data/fe_watchlist.json")
    if watchlist is None:
        _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                     "FE watchlist missing (data/fe_watchlist.json) -- "
                     "add target-priced candidates")
    else:
        items = watchlist if isinstance(watchlist, list) else watchlist.get("watchlist", [])
        logger.info("    FE watchlist items: %d", len(items))
        if len(items) == 0:
            _raise_issue(proposals, "IMPROVEMENT", "IMPROVEMENT",
                         "FE watchlist is empty -- add target-priced candidates")

    # GCP IP stability check
    #
    # Ask GCP's metadata server for THIS instance's external IP. The old check
    # curl'd ifconfig.me, which answers from any host on the internet, so it
    # was really asking "what is my IP?" and comparing that to the VM's. Run
    # anywhere but the VM -- a laptop, most often -- it filed the operator's
    # home IP as a HIGH "GCP IP changed" alert; two such false alerts reached
    # the Chairman queue before this was caught. The metadata server is only
    # routable from inside a GCP instance, which collapses "am I the host this
    # check is about?" and "what is that host's IP?" into one question.
    try:
        current_ip = _gcp_instance_external_ip()
        if not current_ip:
            logger.info("    Not on the GCP VM (no metadata server) -- external IP "
                        "check skipped; only that host can answer it.")
        elif current_ip != _KNOWN_GCP_IP:
            logger.info("    External IP: %s (expected %s)", current_ip, _KNOWN_GCP_IP)
            _raise_issue(proposals, "HIGH", "BUG_FIX",
                         "GCP IP changed -- current external IP %s does not match "
                         "expected %s; update all SSH configs and health monitor"
                         % (current_ip, _KNOWN_GCP_IP))
        else:
            logger.info("    GCP IP stable (%s) -- OK.", current_ip)
    except Exception as e:
        logger.warning("    Could not check external IP: %s", e)

    logger.info("    Improvement proposals done.")


# =============================================================================
# OUTPUT -- update chairman queue
# =============================================================================

def _update_chairman_queue(proposals):
    # Delegates to chairman_queue.submit(). This used to do
    # `combined = combined[-_CHAIRMAN_MAX_ENTRIES:]`, which caps the *shared*
    # queue -- so this agent silently evicted other producers' open items
    # (one run took the file from 153 entries to 20 on 2026-07-31). An agent
    # may only ever cap its OWN entries.
    chairman_queue.submit(proposals, "apex_system_cit_agent",
                          path=_CHAIRMAN_PATH,
                          max_entries=_CHAIRMAN_MAX_ENTRIES)


# =============================================================================
# MAIN
# =============================================================================

def main():

    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("ApexSystemCIT")
    except Exception:
        pass
    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- APEX SYSTEM CIT AGENT")
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

        check_gcp_health(proposals)
        check_database_health(proposals)
        check_cron_jobs(proposals)
        check_critical_files(proposals)
        check_email_buffer(proposals)
        check_edgar_cache(proposals)
        check_improvement_proposals(proposals)

        # Grade any open hedge-exemption outcomes and auto-tighten the
        # carve-out if recent failure rate has crossed policy. Chairman
        # ratified the exemption 2026-09-04 on the condition that failures
        # produce a stricter policy — this is where that happens. Errors
        # swallowed: the tracker is bookkeeping, not a health signal.
        try:
            import hedge_exemption_outcomes as _heo
            _heo.grade_outcomes()
            _heo.maybe_auto_tighten()
        except Exception as e:
            logger.warning("  hedge_exemption_outcomes sweep failed: %s", e)

        if proposals:
            _update_chairman_queue(proposals)
            logger.info(
                "\n  DONE -- %d proposal(s) raised to chairman_action_required.json.",
                len(proposals)
            )
        else:
            logger.info("\n  ALL CLEAR -- no system issues detected.")

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
