#!/usr/bin/env python3
"""
APEX CAPITAL — ML RESEARCH TEAM LEADER
========================================
Daily orchestrator for the five ML Research Team agents.  Runs them in
sequence, collects their outputs, and emails a unified daily briefing to
the Chairman.

Agents orchestrated (in order):
  1. ml_improvement_agent   → data/ml_suggestions.json
  2. ml_drift_monitor       → data/drift_report.json
  3. ml_data_watchdog       → data/data_quality_report.json
  4. ml_alpha_decay_monitor → data/alpha_decay_report.json
  5. ml_alt_data_agent      → data/alt_data_signals.json
  6. ml_rd_bridge_agent     → data/rd_ml_bridge_signals.json  (R&D ↔ ML feature bridge)

(ml_regime_trainer runs weekly via a separate cron — not in the daily loop.)

Daily brief is sent once per 7 AM UTC run.  A single-instance lock via
fcntl.flock prevents double-sends if the cron fires multiple overlapping
instances.

Email deduplication guard: data/ml_team_last_brief.json stores the last
send date (UTC).  If we already sent today we skip the email.

Usage
-----
  python3 ml_research_team_leader.py

GCP: Python 3.11, all ML packages available.
"""

from __future__ import annotations

import fcntl
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

LOCK_FILE          = BASE_DIR / ".ml_team_leader.lock"
LAST_BRIEF_FILE    = DATA_DIR / "ml_team_last_brief.json"
DRIFT_REPORT       = DATA_DIR / "drift_report.json"
DATA_QUALITY_REPORT = DATA_DIR / "data_quality_report.json"
SUGGESTIONS_FILE   = DATA_DIR / "ml_suggestions.json"
ALT_DATA_SIGNALS   = DATA_DIR / "alt_data_signals.json"
ALPHA_DECAY_REPORT = DATA_DIR / "alpha_decay_report.json"

# ---------------------------------------------------------------------------
# Logging — stdout + rotating file
# ---------------------------------------------------------------------------
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOGS_DIR / "ml_team_leader.log", encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("ml_research_team_leader")


# ---------------------------------------------------------------------------
# Agent imports — each agent exposes a top-level run() function.
# We import lazily inside the orchestration loop so that an import failure
# for one agent doesn't kill the entire run.
# ---------------------------------------------------------------------------

def _import_agent_run(module_name: str) -> Callable[[], None]:
    """
    Dynamically import `run` from `module_name`.
    Raises ImportError with a clear message if the module cannot be loaded.
    """
    import importlib
    sys.path.insert(0, str(BASE_DIR))
    module = importlib.import_module(module_name)
    run_fn = getattr(module, "run", None)
    if run_fn is None or not callable(run_fn):
        raise ImportError(f"Module '{module_name}' does not expose a callable run()")
    return run_fn


# Agent definitions: (display_name, module_name)
# The run function is resolved lazily per agent.
AGENT_DEFS: list[tuple[str, str]] = [
    ("ml_improvement_agent",  "ml_improvement_agent"),
    ("ml_drift_monitor",      "ml_drift_monitor"),
    ("ml_data_watchdog",      "ml_data_watchdog"),
    ("ml_alpha_decay_monitor","ml_alpha_decay_monitor"),
    ("ml_alt_data_agent",     "ml_alt_data_agent"),
    ("ml_rd_bridge_agent",    "ml_rd_bridge_agent"),   # R&D ↔ ML feature bridge
]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all_agents() -> dict[str, str]:
    """
    Execute each agent's run() function in sequence.
    Returns {agent_name: "OK"} or {agent_name: "ERROR: <message>"}.

    SystemExit is caught SEPARATELY and deliberately. These agents are imported
    and called in-process, but several were written as standalone scripts and
    still say `sys.exit(0)` where a library would `return` — ml_drift_monitor
    has eight such lines inside run(), ml_data_watchdog two. `sys.exit` raises
    SystemExit, which derives from BaseException and NOT from Exception, so the
    handler below never saw it: the first agent to hit a routine skip condition
    ("model isn't deployed yet", "fewer than 50 recent samples") terminated this
    whole process. Verified: with ml_drift_monitor raising SystemExit(0) at
    position 2 of 6, run_all_agents never returns and the four agents after it —
    ml_data_watchdog, ml_alpha_decay_monitor, ml_alt_data_agent,
    ml_rd_bridge_agent — never run. send_daily_brief never runs either.

    The exit code was ZERO, so cron, the log sweep and every health check read
    it as a clean success. That is the `fallback-that-means-act` shape: the
    degraded path returns the all-clear. Both conditions above are live right
    now (no model_v3.pkl on the Mac; zero labelled rows inside the 60-day window
    on the VM), so this is not a hypothetical.

    KeyboardInterrupt is the one BaseException that must still stop everything —
    an operator pressing ^C is asking for the run to end, not for the next agent.
    """
    results: dict[str, str] = {}

    for agent_name, module_name in AGENT_DEFS:
        logger.info(f"--- Running {agent_name} ---")
        try:
            run_fn = _import_agent_run(module_name)
            status = run_fn()
            # Agents converted away from sys.exit now RETURN their status, so a
            # non-zero return is the same failure the old sys.exit(1) reported.
            # Recording it as "OK" would trade a process kill for a silent one.
            if isinstance(status, int) and status != 0:
                results[agent_name] = f"ERROR: run() returned status {status}"
                logger.error("  %s returned non-zero status %s", agent_name, status)
                continue
            results[agent_name] = "OK"
            logger.info(f"  {agent_name} completed successfully")
        except KeyboardInterrupt:
            raise
        except SystemExit as exc:
            code = exc.code if exc.code is not None else 0
            msg = f"ERROR: called sys.exit({code}) inside run() — treated as a stop"
            results[agent_name] = msg
            logger.error(
                "  %s tried to exit the process (code %s). Contained — the "
                "remaining %d agent(s) still run. Fix the agent to return "
                "instead of exiting.",
                agent_name, code,
                len(AGENT_DEFS) - 1 - [n for n, _ in AGENT_DEFS].index(agent_name))
        except Exception as exc:
            msg = f"ERROR: {exc}"
            results[agent_name] = msg
            logger.error(f"  {agent_name} failed: {exc}", exc_info=True)

    return results


# ---------------------------------------------------------------------------
# JSON report loaders — all tolerant, never raise
# ---------------------------------------------------------------------------

def _load_json(path: Path, description: str) -> dict | list | None:
    """Load and return JSON from path. Returns None on any failure."""
    if not path.exists():
        logger.debug(f"{description}: file not found at {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning(f"Failed to load {description} ({path}): {exc}")
        return None


def _read_drift_report() -> dict[str, Any]:
    data = _load_json(DRIFT_REPORT, "drift_report")
    if data is None or not isinstance(data, dict):
        return {"status": "UNAVAILABLE", "recent_auc": None, "drift": None}
    return {
        "status":       data.get("status", "UNKNOWN"),
        "recent_auc":   data.get("recent_auc"),
        "historical_auc": data.get("historical_auc"),
        "drift":        data.get("drift"),
        "baseline_auc": data.get("baseline_auc"),
        "recent_n":     data.get("recent_n"),
        "timestamp":    data.get("timestamp"),
    }


def _read_data_quality() -> dict[str, Any]:
    data = _load_json(DATA_QUALITY_REPORT, "data_quality_report")
    if data is None or not isinstance(data, dict):
        return {"overall_status": "UNAVAILABLE", "issues": []}
    return {
        "overall_status": data.get("overall_status") or data.get("status", "UNKNOWN"),
        "issues":         data.get("issues") or data.get("warnings") or [],
        "checked_at":     data.get("checked_at") or data.get("timestamp"),
        "summary":        data.get("summary"),
    }


def _read_suggestions() -> dict[str, Any]:
    data = _load_json(SUGGESTIONS_FILE, "ml_suggestions")
    if data is None or not isinstance(data, list):
        return {"total_pending": 0, "implement": [], "review": []}

    pending = [s for s in data if s.get("status") == "PENDING"]
    implement = [s for s in pending if s.get("category") == "IMPLEMENT"]
    review    = [s for s in pending if s.get("category") == "REVIEW"]

    return {
        "total_pending": len(pending),
        "implement":     implement,
        "review":        review,
    }


def _read_alt_data() -> dict[str, Any]:
    data = _load_json(ALT_DATA_SIGNALS, "alt_data_signals")
    if data is None or not isinstance(data, dict):
        return {
            "market_news_sentiment":    None,
            "insider_buy_pressure":     None,
            "consumer_sentiment_surprise": None,
        }
    return {
        "market_news_sentiment":       data.get("market_news_sentiment"),
        "insider_buy_pressure":        data.get("insider_buy_pressure"),
        "consumer_sentiment_surprise": data.get("consumer_sentiment_surprise"),
        "signal_date":                 data.get("signal_date") or data.get("date"),
        "raw":                         data,
    }


def _read_alpha_decay() -> dict[str, Any]:
    data = _load_json(ALPHA_DECAY_REPORT, "alpha_decay_report")
    if data is None or not isinstance(data, dict):
        return {"decaying": [], "dead": [], "checked_at": None}

    features = data.get("features") or data.get("feature_status") or {}
    decaying: list[str] = []
    dead:     list[str] = []

    if isinstance(features, dict):
        for feature, status in features.items():
            s = str(status).upper() if isinstance(status, str) else \
                str(status.get("status", "")).upper() if isinstance(status, dict) else ""
            if s == "DECAYING":
                decaying.append(feature)
            elif s == "DEAD":
                dead.append(feature)
    elif isinstance(features, list):
        for item in features:
            if not isinstance(item, dict):
                continue
            name   = item.get("feature") or item.get("name", "")
            status = str(item.get("status", "")).upper()
            if status == "DECAYING":
                decaying.append(name)
            elif status == "DEAD":
                dead.append(name)

    return {
        "decaying":    decaying,
        "dead":        dead,
        "checked_at":  data.get("checked_at") or data.get("timestamp"),
        "total_features_checked": data.get("total_features_checked"),
    }


# ---------------------------------------------------------------------------
# Email: single-send guard
# ---------------------------------------------------------------------------

def _already_sent_today() -> bool:
    """Return True if we have already sent the brief for today (UTC)."""
    data = _load_json(LAST_BRIEF_FILE, "ml_team_last_brief")
    if data is None or not isinstance(data, dict):
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return data.get("last_sent_date") == today


def _mark_sent() -> None:
    """Record today's UTC date as the last brief send date."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "last_sent_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "last_sent_at":   datetime.now(timezone.utc).isoformat(),
    }
    tmp = LAST_BRIEF_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(record, fh, indent=2)
        tmp.replace(LAST_BRIEF_FILE)
        logger.info(f"Brief send record saved → {LAST_BRIEF_FILE}")
    except Exception as exc:
        logger.warning(f"Could not save brief send record: {exc}")


# ---------------------------------------------------------------------------
# Email body builder
# ---------------------------------------------------------------------------

def _fmt_auc(v: Any) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_signal(v: Any, precision: int = 3) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):+.{precision}f}"
    except (TypeError, ValueError):
        return str(v)


def build_brief_html(
    agent_results: dict[str, str],
    drift:         dict[str, Any],
    data_quality:  dict[str, Any],
    suggestions:   dict[str, Any],
    alt_data:      dict[str, Any],
    alpha_decay:   dict[str, Any],
) -> str:
    """
    Assemble the full HTML body for the daily ML Research Team brief.
    Uses inline styles for maximum email client compatibility.
    """
    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today_str = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")

    # ── palette ──────────────────────────────────────────────────────────────
    BG      = "#0d1117"
    SURFACE = "#161b22"
    BORDER  = "#30363d"
    TEXT    = "#c9d1d9"
    MUTED   = "#8b949e"
    GREEN   = "#3fb950"
    YELLOW  = "#d29922"
    RED     = "#f85149"
    BLUE    = "#58a6ff"

    def _status_color(s: str) -> str:
        s = s.upper()
        if s in ("OK", "PASS", "HEALTHY", "NORMAL"):
            return GREEN
        if s in ("WARNING", "WARN", "DEGRADED"):
            return YELLOW
        if s in ("ALERT", "ERROR", "FAIL", "CRITICAL", "DEAD"):
            return RED
        return MUTED

    def _agent_badge(result: str) -> str:
        if result == "OK":
            return f'<span style="color:{GREEN};font-weight:bold;">OK</span>'
        return f'<span style="color:{RED};font-weight:bold;">{result}</span>'

    def _h2(text: str) -> str:
        return (
            f'<h2 style="color:{BLUE};font-size:16px;margin:28px 0 10px;'
            f'border-bottom:1px solid {BORDER};padding-bottom:6px;">{text}</h2>'
        )

    def _row(label: str, value: str) -> str:
        return (
            f'<tr>'
            f'<td style="padding:5px 20px 5px 0;color:{MUTED};font-size:13px;">{label}</td>'
            f'<td style="padding:5px 0;font-size:13px;color:{TEXT};">{value}</td>'
            f'</tr>'
        )

    parts: list[str] = []

    # ── outer wrapper ─────────────────────────────────────────────────────────
    parts.append(f"""
<html>
<body style="background:{BG};color:{TEXT};font-family:Arial,sans-serif;padding:24px;margin:0;">
<div style="max-width:720px;margin:0 auto;">

  <div style="border-left:4px solid {BLUE};padding-left:16px;margin-bottom:24px;">
    <h1 style="color:{BLUE};margin:0 0 4px;font-size:20px;">
      📊 Apex Capital — ML Research Team Daily Brief
    </h1>
    <p style="color:{MUTED};margin:0;font-size:12px;">{today_str} · Generated {now_str}</p>
  </div>
""")

    # ── 1. Agent Status ───────────────────────────────────────────────────────
    parts.append(_h2("1 · Agent Status"))
    parts.append(f'<table style="border-collapse:collapse;width:100%;">')
    for agent_name, result in agent_results.items():
        parts.append(_row(agent_name, _agent_badge(result)))
    parts.append("</table>")

    ok_count  = sum(1 for v in agent_results.values() if v == "OK")
    err_count = len(agent_results) - ok_count
    summary_color = GREEN if err_count == 0 else (YELLOW if err_count <= 2 else RED)
    parts.append(
        f'<p style="color:{summary_color};font-size:13px;margin-top:8px;">'
        f'{ok_count}/{len(agent_results)} agents ran successfully'
        + (f' — {err_count} error(s) require attention' if err_count else '')
        + '</p>'
    )

    # ── 2. Model Health (drift report) ────────────────────────────────────────
    parts.append(_h2("2 · Model Health"))
    drift_status = drift.get("status", "UNAVAILABLE")
    sc = _status_color(drift_status)
    parts.append(f'<table style="border-collapse:collapse;">')
    parts.append(_row("Status", f'<span style="color:{sc};font-weight:bold;">{drift_status}</span>'))
    parts.append(_row("Recent AUC (last 60d)",   _fmt_auc(drift.get("recent_auc"))))
    parts.append(_row("Historical AUC (61–180d)", _fmt_auc(drift.get("historical_auc"))))
    drift_val = drift.get("drift")
    if drift_val is not None:
        try:
            dv = float(drift_val)
            dcolor = GREEN if dv >= 0 else (YELLOW if dv > -0.015 else RED)
            parts.append(_row("Drift", f'<span style="color:{dcolor};">{dv:+.4f}</span>'))
        except (TypeError, ValueError):
            parts.append(_row("Drift", str(drift_val)))
    parts.append(_row("Baseline AUC (training)", _fmt_auc(drift.get("baseline_auc"))))
    if drift.get("timestamp"):
        parts.append(_row("Last checked", str(drift["timestamp"])[:19]))
    parts.append("</table>")

    # ── 3. Data Quality ───────────────────────────────────────────────────────
    parts.append(_h2("3 · Data Quality"))
    dq_status = data_quality.get("overall_status", "UNAVAILABLE")
    sc = _status_color(dq_status)
    parts.append(f'<table style="border-collapse:collapse;">')
    parts.append(_row("Overall status", f'<span style="color:{sc};font-weight:bold;">{dq_status}</span>'))
    if data_quality.get("summary"):
        parts.append(_row("Summary", str(data_quality["summary"])))
    if data_quality.get("checked_at"):
        parts.append(_row("Checked at", str(data_quality["checked_at"])[:19]))
    parts.append("</table>")

    issues = data_quality.get("issues") or []
    if issues:
        parts.append(
            f'<p style="color:{YELLOW};font-size:13px;margin:8px 0 4px;">Issues detected:</p>'
        )
        parts.append(f'<ul style="margin:0;padding-left:20px;color:{TEXT};font-size:13px;">')
        for issue in issues[:10]:
            parts.append(f"<li>{issue}</li>")
        if len(issues) > 10:
            parts.append(f"<li style='color:{MUTED};'>… and {len(issues)-10} more</li>")
        parts.append("</ul>")
    else:
        parts.append(
            f'<p style="color:{MUTED};font-size:13px;margin-top:6px;">No issues reported.</p>'
        )

    # ── 4. Pending ML Suggestions ─────────────────────────────────────────────
    parts.append(_h2("4 · Pending ML Suggestions"))
    total_pending  = suggestions.get("total_pending", 0)
    implement_list = suggestions.get("implement", [])
    review_list    = suggestions.get("review", [])

    color_total = RED if total_pending > 5 else (YELLOW if total_pending > 0 else GREEN)
    parts.append(
        f'<p style="color:{color_total};font-size:14px;font-weight:bold;margin-bottom:10px;">'
        f'{total_pending} pending item(s): {len(implement_list)} IMPLEMENT · {len(review_list)} REVIEW'
        f'</p>'
    )

    def _sugg_block(sugg_list: list[dict], label: str, color: str) -> None:
        if not sugg_list:
            return
        parts.append(
            f'<p style="color:{color};font-size:13px;font-weight:bold;margin:10px 0 4px;">'
            f'{label} ({len(sugg_list)})</p>'
        )
        for s in sugg_list:
            parts.append(
                f'<div style="background:{SURFACE};border:1px solid {BORDER};'
                f'border-left:3px solid {color};padding:10px 12px;margin-bottom:8px;'
                f'border-radius:4px;">'
            )
            parts.append(
                f'<p style="margin:0 0 4px;font-size:13px;font-weight:bold;color:{TEXT};">'
                f'{s.get("title","(no title)")}</p>'
            )
            sid = s.get("id","")
            delta = s.get("estimated_auc_delta", "")
            effort = s.get("effort","")
            parts.append(
                f'<p style="margin:0;font-size:11px;color:{MUTED};">'
                f'ID: {sid}'
                + (f' · Est. AUC delta: {delta}' if delta else '')
                + (f' · Effort: {effort}' if effort else '')
                + '</p>'
            )
            parts.append("</div>")

    _sugg_block(implement_list, "IMPLEMENT", GREEN)
    _sugg_block(review_list,    "REVIEW",    YELLOW)

    if total_pending == 0:
        parts.append(f'<p style="color:{GREEN};font-size:13px;">No pending suggestions.</p>')

    # ── 5. Alt Data Signals ───────────────────────────────────────────────────
    parts.append(_h2("5 · Alt Data Signals"))
    parts.append(f'<table style="border-collapse:collapse;">')

    mns = alt_data.get("market_news_sentiment")
    ibp = alt_data.get("insider_buy_pressure")
    css = alt_data.get("consumer_sentiment_surprise")

    def _signal_color(v: Any) -> str:
        if v is None:
            return MUTED
        try:
            fv = float(v)
            return GREEN if fv > 0.1 else (RED if fv < -0.1 else MUTED)
        except (TypeError, ValueError):
            return MUTED

    parts.append(_row(
        "Market news sentiment",
        f'<span style="color:{_signal_color(mns)};">{_fmt_signal(mns)}</span>'
    ))
    parts.append(_row(
        "Insider buy pressure",
        f'<span style="color:{_signal_color(ibp)};">{_fmt_signal(ibp)}</span>'
    ))
    parts.append(_row(
        "Consumer sentiment surprise",
        f'<span style="color:{_signal_color(css)};">{_fmt_signal(css)}</span>'
    ))
    if alt_data.get("signal_date"):
        parts.append(_row("Signal date", str(alt_data["signal_date"])[:10]))
    parts.append("</table>")

    # ── 6. Alpha Decay Alerts ────────────────────────────────────────────────
    parts.append(_h2("6 · Alpha Decay Alerts"))
    decaying = alpha_decay.get("decaying", [])
    dead     = alpha_decay.get("dead", [])
    total_features = alpha_decay.get("total_features_checked")

    if not decaying and not dead:
        header_color = GREEN
        header_text  = "No features flagged."
    else:
        header_color = RED if dead else YELLOW
        header_text  = (
            f'{len(decaying)} DECAYING, {len(dead)} DEAD'
            + (f' (of {total_features} checked)' if total_features else '')
        )

    parts.append(
        f'<p style="color:{header_color};font-size:13px;font-weight:bold;">{header_text}</p>'
    )

    if dead:
        parts.append(f'<p style="color:{RED};font-size:13px;margin:4px 0 2px;">DEAD features:</p>')
        parts.append(f'<ul style="margin:0;padding-left:20px;color:{RED};font-size:13px;">')
        for f in dead:
            parts.append(f"<li>{f}</li>")
        parts.append("</ul>")

    if decaying:
        parts.append(
            f'<p style="color:{YELLOW};font-size:13px;margin:8px 0 2px;">DECAYING features:</p>'
        )
        parts.append(f'<ul style="margin:0;padding-left:20px;color:{YELLOW};font-size:13px;">')
        for f in decaying:
            parts.append(f"<li>{f}</li>")
        parts.append("</ul>")

    if alpha_decay.get("checked_at"):
        parts.append(
            f'<p style="color:{MUTED};font-size:11px;margin-top:6px;">'
            f'Last checked: {str(alpha_decay["checked_at"])[:19]}</p>'
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    parts.append(f"""
  <hr style="border:none;border-top:1px solid {BORDER};margin:32px 0 16px;">
  <p style="color:{MUTED};font-size:12px;line-height:1.6;">
    <strong style="color:{TEXT};">IMPLEMENT</strong> items will be executed at next Claude session
    check-in.<br>
    <strong style="color:{TEXT};">REVIEW</strong> items require Chairman approval — reply to approve
    or reply <em>DISCARD</em> to dismiss.
  </p>
  <p style="color:#6e7681;font-size:11px;margin-top:8px;">
    Apex Capital ML Research Team · Daily brief · Runs 07:00 UTC
  </p>

</div>
</body>
</html>
""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Send daily brief
# ---------------------------------------------------------------------------

def send_daily_brief(agent_results: dict[str, str]) -> None:
    """
    Compile and email the unified daily brief — once per day only.
    """
    if _already_sent_today():
        logger.info("Daily brief already sent today — skipping email")
        return

    # Gather report data (always succeeds — loaders are tolerant)
    drift        = _read_drift_report()
    data_quality = _read_data_quality()
    suggestions  = _read_suggestions()
    alt_data     = _read_alt_data()
    alpha_decay  = _read_alpha_decay()

    html_body = build_brief_html(
        agent_results=agent_results,
        drift=drift,
        data_quality=data_quality,
        suggestions=suggestions,
        alt_data=alt_data,
        alpha_decay=alpha_decay,
    )

    subject = "📊 Apex Capital — ML Research Team Daily Brief"

    logger.info(f"Sending daily brief: '{subject}'")
    try:
        from email_sender import send_alert_email
        success = send_alert_email(subject, html_body)
    except Exception as exc:
        logger.error(f"send_alert_email raised an exception: {exc}", exc_info=True)
        success = False

    if success:
        logger.info("Daily brief sent successfully")
        _mark_sent()
    else:
        logger.error("Daily brief FAILED to send — will retry on next run if still today")


# ---------------------------------------------------------------------------
# Main entry point — single-instance lock via fcntl.flock
# ---------------------------------------------------------------------------

def run() -> None:
    run_start = datetime.now(timezone.utc)
    logger.info("=" * 72)
    logger.info(
        f"ML RESEARCH TEAM LEADER starting — "
        f"{run_start.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    logger.info("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # -- Single-instance lock -------------------------------------------------
    # fcntl.flock is advisory; a second invocation will block until the first
    # completes and releases the lock, then see the "already sent today" guard.
    lock_fh = None
    try:
        lock_fh = open(LOCK_FILE, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning(
            "Another instance of ml_research_team_leader is already running "
            "(could not acquire lock) — exiting to prevent double-send"
        )
        if lock_fh:
            lock_fh.close()
        return
    except Exception as exc:
        logger.warning(f"Lock acquisition failed ({exc}) — proceeding without lock")

    try:
        # -- Run all agents ---------------------------------------------------
        logger.info("Running all ML Research Team agents …")
        agent_results = run_all_agents()

        ok_count  = sum(1 for v in agent_results.values() if v == "OK")
        err_count = len(agent_results) - ok_count
        logger.info(
            f"Agents complete: {ok_count} OK, {err_count} error(s) — "
            f"{agent_results}"
        )

        # -- Compile and send daily brief ------------------------------------
        send_daily_brief(agent_results)

    finally:
        # Always release the lock
        if lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass

    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    logger.info("=" * 72)
    logger.info(f"ML RESEARCH TEAM LEADER complete — elapsed {elapsed:.1f}s")
    logger.info("=" * 72)


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('MLResearchTeamLeader'):
        raise SystemExit(0)
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as exc:
        logger.exception(f"Unhandled exception in ml_research_team_leader: {exc}")
        sys.exit(1)
