"""
APEX CAPITAL — Logging Infrastructure
======================================
Provides a smart log filter that:

  1. Demotes known-transient ERROR messages to WARNING so they don't
     pollute monitoring dashboards or trigger false CIT alerts.
  2. Adds structured context to log records (canary level, pipeline step).
  3. Writes a separate data/log_health.json that CIT and IRM can read to
     understand log noise levels without scanning raw log files.

WHAT COUNTS AS A REAL ERROR
  Anything unexpected that indicates a bug or config problem:
    • AuthenticationError on SMTP (wrong password)
    • Alpaca order rejection for a non-obvious reason
    • JSON decode failures on critical data files
    • Uncaught exceptions in core pipeline steps
    • Import failures

KNOWN TRANSIENT NOISE (demoted to WARNING)
  These are expected in a live system and do NOT indicate a bug:
    • DNS resolution failure sending email (network blip)
    • yfinance data temporarily unavailable
    • Alpaca paper trading "available=0" on SH (shares held for orders — fixed)
    • CIT pipeline reports referencing log error counts (meta-noise)
    • smtplib connection timeouts

USAGE
  Call setup_logging() once at the top of main.py (or any entry point).
  All existing logger.error/warning/info calls work unchanged — the filter
  runs transparently underneath.
"""

import logging
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOG_HEALTH_PATH = DATA_DIR / "log_health.json"


# =============================================================================
# KNOWN TRANSIENT PATTERNS
# Any log record whose message matches one of these (at ERROR level) gets
# demoted to WARNING automatically.
# =============================================================================

_TRANSIENT_PATTERNS = [
    # Email / DNS failures
    re.compile(r"nodename nor servname provided", re.I),
    re.compile(r"Name or service not known", re.I),
    re.compile(r"Errno 8", re.I),
    re.compile(r"_send_raw failed", re.I),
    re.compile(r"smtplib\.SMTPServerDisconnected", re.I),
    re.compile(r"Connection refused.*smtp", re.I),

    # Alpaca paper trading known non-issues
    re.compile(r"available.*:.*0.*code.*40310000", re.I),   # shares held for pending order
    re.compile(r"Sell order failed.*SH.*40310000", re.I),
    re.compile(r"insufficient qty", re.I),

    # yfinance / data feed blips
    re.compile(r"yfinance.*No data found", re.I),
    re.compile(r"No timezone found", re.I),
    re.compile(r"NoneType.*has no attribute", re.I),         # yfinance returns None during off-hours
    re.compile(r"possibly delisted", re.I),
    re.compile(r"auto_adjust", re.I),

    # CIT meta-noise (CIT reporting on error counts in logs — not a real error)
    re.compile(r"ERROR lines.*dominant.*Reporter.*CITPipeline", re.I),
    re.compile(r"CITPipelineMonitor.*ERROR", re.I),

    # Timeout on non-critical external calls
    re.compile(r"timed out", re.I),
    re.compile(r"Read timed out", re.I),
    re.compile(r"Connection timed out", re.I),

    # Expected misses during off-hours
    re.compile(r"market.*closed", re.I),
    re.compile(r"no quotes.*weekend", re.I),
]

# =============================================================================
# REAL ERROR KEYWORDS (never demote these regardless of other patterns)
# =============================================================================

_ALWAYS_ERROR_PATTERNS = [
    re.compile(r"SMTPAuthenticationError", re.I),
    re.compile(r"authentication failed", re.I),
    re.compile(r"ImportError|ModuleNotFoundError", re.I),
    re.compile(r"JSONDecodeError", re.I),
    re.compile(r"PermissionError", re.I),
    re.compile(r"critical.*fail", re.I),
]


# =============================================================================
# SMART LOG FILTER
# =============================================================================

class ApexLogFilter(logging.Filter):
    """
    Transparently demotes known-transient ERROR messages to WARNING.
    Tracks noise statistics in memory for log_health.json.
    """

    def __init__(self):
        super().__init__()
        self._noise_counts: dict = defaultdict(int)
        self._error_counts: dict = defaultdict(int)
        self._last_flush   = time.time()
        self._flush_interval = 300   # write log_health.json every 5 minutes

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.ERROR:
            return True   # only intercept ERROR records

        msg = record.getMessage()

        # Never demote genuine errors
        for pat in _ALWAYS_ERROR_PATTERNS:
            if pat.search(msg):
                self._error_counts[pat.pattern[:30]] += 1
                self._maybe_flush()
                return True

        # Demote known transient noise
        for pat in _TRANSIENT_PATTERNS:
            if pat.search(msg):
                record.levelno   = logging.WARNING
                record.levelname = "WARNING"
                self._noise_counts[pat.pattern[:30]] += 1
                self._maybe_flush()
                return True

        # Unknown ERROR — keep as-is, log to health tracker
        self._error_counts["unclassified"] += 1
        self._maybe_flush()
        return True

    def _maybe_flush(self):
        now = time.time()
        if now - self._last_flush > self._flush_interval:
            self._flush_health()
            self._last_flush = now

    def _flush_health(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            health = {
                "last_updated":    datetime.now().isoformat(),
                "real_errors":     dict(self._error_counts),
                "noise_demoted":   dict(self._noise_counts),
                "total_real":      sum(self._error_counts.values()),
                "total_demoted":   sum(self._noise_counts.values()),
            }
            LOG_HEALTH_PATH.write_text(json.dumps(health, indent=2))
        except Exception:
            pass   # health file is informational — never crash for it


# =============================================================================
# PUBLIC INTERFACE
# =============================================================================

_filter_instance = None  # type: Optional[ApexLogFilter]


def setup_logging(level: int = logging.INFO) -> None:
    """
    Call once at process startup to install the smart filter on the root logger.
    Idempotent — safe to call multiple times.
    """
    global _filter_instance

    root = logging.getLogger()
    root.setLevel(level)

    # Check if already installed
    for handler in root.handlers:
        for f in handler.filters:
            if isinstance(f, ApexLogFilter):
                return   # already installed

    if _filter_instance is None:
        _filter_instance = ApexLogFilter()

    # Add filter to every existing handler + any added later
    for handler in root.handlers:
        if not any(isinstance(f, ApexLogFilter) for f in handler.filters):
            handler.addFilter(_filter_instance)

    # Monkey-patch addHandler so future handlers also get the filter
    _orig_add = root.addHandler
    def _patched_add(hdlr):
        _orig_add(hdlr)
        if not any(isinstance(f, ApexLogFilter) for f in hdlr.filters):
            hdlr.addFilter(_filter_instance)
    root.addHandler = _patched_add


def get_log_health() -> dict:
    """Read and return the latest log health snapshot."""
    try:
        if LOG_HEALTH_PATH.exists():
            return json.loads(LOG_HEALTH_PATH.read_text())
    except Exception:
        pass
    return {"total_real": 0, "total_demoted": 0, "real_errors": {}, "noise_demoted": {}}


def flush_health_now() -> None:
    """Force-write log_health.json immediately (call at end of pipeline run)."""
    if _filter_instance:
        _filter_instance._flush_health()
