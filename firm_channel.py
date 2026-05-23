# =============================================================================
# APEX CAPITAL — FIRM CHANNEL
# =============================================================================
# A lightweight shared idea bus. Any agent (except IRM) can post observations,
# proposals, and alerts here throughout the day. The morning briefing reads
# recent posts to compile "From the Floor" — a live view of what every agent
# is thinking before the board meeting and pipeline run.
#
# IRM is explicitly excluded — their sealed evaluation chain must remain
# isolated from the shared communication layer.
#
# Usage (from any agent):
#   from firm_channel import post
#   post("Manager Agent", "TRADE_SIGNAL", "NVDA: 5★ conviction, all gates clear")
#   post("CIT Director", "PROPOSAL", "Kelly sizing would improve risk-adjusted returns")
#   post("R&D Team Leader", "SECTOR_OBS", "Semiconductors: RSI 72, momentum bullish")
#
# Categories:
#   PROPOSAL       — CIT / ThinkTank ideas for the board
#   TRADE_SIGNAL   — Manager Agent or Review Agent trade context
#   RISK_OBS       — Risk Manager observations (not confidential IRM work)
#   SECTOR_OBS     — R&D Team or Research Agent sector observations
#   PIPELINE_ALERT — Agent health / pipeline status (CoS, HR)
#   MARKET_NOTE    — Canary / macro conditions
#   IMPROVEMENT    — Any agent's ad-hoc improvement suggestion
#   EARNINGS       — Earnings predictor catalyst notes
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CHANNEL_PATH = "data/firm_channel.json"
MAX_ENTRIES  = 500     # rolling cap — oldest pruned when exceeded

# IRM is sealed. No firm-channel access.
_EXCLUDED_AGENTS = {"IRM", "Independent Risk Manager", "irm_review_agent"}

# Maps every agent to their direct supervisor for escalation routing.
# Any agent not listed here should escalate to CoS directly.
SUPERVISOR_MAP = {
    # R&D cluster
    "Research Team":              "R&D Team Leader",
    "Energy Research Team":       "R&D Team Leader",
    "Geopolitical Team":          "R&D Team Leader",
    "Historical Analyst":         "R&D Team Leader",
    "International Analyst (IMI)":"R&D Team Leader",
    "Earnings Predictor":         "R&D Team Leader",
    # CIT cluster
    "HR Agent":                   "CIT Director",
    "CIT Code Inspector":         "CIT Director",
    "CIT Pipeline Monitor":       "CIT Director",
    "CIT Strategy Analyst":       "CIT Director",
    "CIT Architecture Advisor":   "CIT Director",
    "CIT Data Quality Analyst":   "CIT Director",
    "CIT Proposal Validator":     "CIT Director",
    "CIT Self-Improvement Agent": "CIT Director",
    "CIT Security Monitor":       "CIT Director",
    # Operations cluster — reports to Operations Director
    "Trade Review Team":          "Operations Director",
    "Creative Agent":             "Operations Director",
    "Capital Efficiency Agent":   "Operations Director",
    "Report Generator":           "Operations Director",
    "Premarket Check":            "Operations Director",
    "Canary Agent":               "Manager Agent",
    # Board reports to CoS for coordination
    "Manager Agent":              "Chief of Staff",
    "Risk Manager":               "Chief of Staff",
    "R&D Team Leader":            "Chief of Staff",
    "CIT Director":               "Chief of Staff",
}

def get_supervisor(agent_name: str) -> str:
    """Return the direct supervisor for an agent, defaulting to Chief of Staff."""
    return SUPERVISOR_MAP.get(agent_name, "Chief of Staff")


def post(agent: str, category: str, content: str, priority: str = "NORMAL") -> bool:
    """
    Post a message to the firm channel.
    priority: NORMAL | HIGH | URGENT
    Returns True on success.
    """
    import fcntl
    if agent in _EXCLUDED_AGENTS:
        logger.warning(f"[FirmChannel] {agent} is excluded from the firm channel (sealed chain).")
        return False

    entry = {
        "ts":       datetime.now().isoformat(),
        "date":     datetime.now().strftime("%Y-%m-%d"),
        "agent":    agent,
        "category": category,
        "content":  content,
        "priority": priority,
    }

    os.makedirs("data", exist_ok=True)
    # Use an exclusive fcntl lock so concurrent agent writes don't interleave and corrupt the file.
    try:
        with open(CHANNEL_PATH, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
                existing.append(entry)
                # Prune oldest entries beyond cap
                if len(existing) > MAX_ENTRIES:
                    existing = existing[-MAX_ENTRIES:]
                f.seek(0)
                f.truncate()
                json.dump(existing, f, indent=2)
            finally:
                # Always release the lock even if json.dump raises.
                fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except Exception as e:
        logger.warning(f"[FirmChannel] Write failed: {e}")
        return False


def read_today() -> list[dict]:
    """Return all posts from today (calendar date), newest first."""
    today = datetime.now().strftime("%Y-%m-%d")
    return [e for e in reversed(_load()) if e.get("date") == today]


def read_recent(hours: float = 24) -> list[dict]:
    """Return all posts from the last N hours, newest first."""
    cutoff = datetime.now() - timedelta(hours=hours)
    result = []
    for e in reversed(_load()):
        try:
            if datetime.fromisoformat(e["ts"][:19]) >= cutoff:
                result.append(e)
        except Exception:
            pass
    return result


def read_by_category(category: str, hours: float = 24) -> list[dict]:
    """Return recent posts filtered by category."""
    return [e for e in read_recent(hours) if e.get("category") == category]


def read_by_agent(agent: str, hours: float = 48) -> list[dict]:
    """Return recent posts from a specific agent."""
    return [e for e in read_recent(hours) if e.get("agent") == agent]


def _load() -> list[dict]:
    if not os.path.exists(CHANNEL_PATH):
        return []
    try:
        with open(CHANNEL_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def post_task_request(
    from_agent: str,
    to_agent: str,
    task: str,
    priority: str = "HIGH",
    issue_id: str = None,
) -> dict:
    """Post a specific task request addressed to a named agent."""
    content = f"TASK REQUEST → {to_agent}: {task}"
    if issue_id:
        content += f" [Ref: {issue_id}]"
    return post(from_agent, "TASK_REQUEST", content, priority=priority)


def get_pending_tasks(agent_name: str) -> list:
    """Return TASK_REQUEST posts addressed to this agent from the last 48h."""
    recent = read_recent(hours=48)
    tasks  = [
        p for p in recent
        if p.get("category") == "TASK_REQUEST"
        and agent_name.lower() in p.get("content", "").lower()
    ]
    return tasks


def summary_for_briefing(hours: float = 20) -> dict:
    """
    Produce a structured summary of recent channel activity for the morning briefing.
    hours: look-back window (default 20h catches yesterday's activity through now)
    """
    recent = read_recent(hours)
    if not recent:
        return {"total": 0, "by_agent": {}, "by_category": {}, "urgent": [], "posts": []}

    by_agent    : dict[str, list] = {}
    by_category : dict[str, list] = {}
    urgent      : list[dict]      = []

    for e in recent:
        # Use .get() so a malformed entry missing "agent" or "category" never crashes the briefing.
        by_agent.setdefault(e.get("agent", "unknown"), []).append(e)
        by_category.setdefault(e.get("category", "UNKNOWN"), []).append(e)
        if e.get("priority") in ("HIGH", "URGENT"):
            urgent.append(e)

    return {
        "total":       len(recent),
        "by_agent":    {k: len(v) for k, v in by_agent.items()},
        "by_category": {k: len(v) for k, v in by_category.items()},
        "urgent":      urgent[:10],
        "posts":       recent[:50],    # latest 50 for the morning briefing display
    }


COS_MEETING_REQUESTS_PATH = "data/cos_meeting_requests.json"

def request_cos_meeting(from_agent: str, summary: str, urgency: str = "NORMAL") -> str:
    """
    Any agent can call this to formally request CoS attention.
    urgency: "NORMAL", "HIGH", or "EMERGENCY"
    Returns the request ID.
    """
    import fcntl, uuid
    os.makedirs("data", exist_ok=True)
    req_id = f"CMR-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "id": req_id,
        "ts": datetime.now().isoformat(),
        "from_agent": from_agent,
        "summary": summary,
        "urgency": urgency,
        "status": "PENDING",
    }
    try:
        with open(COS_MEETING_REQUESTS_PATH, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                # Lock must be released in finally so an exception mid-write never deadlocks.
                f.seek(0)
                try:
                    existing = json.load(f)
                except Exception:
                    existing = []
                existing.append(record)
                f.seek(0); f.truncate()
                json.dump(existing, f, indent=2, default=str)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.warning(f"  [FirmChannel] request_cos_meeting failed: {e}")
    return req_id

def get_cos_meeting_requests(status: str = "PENDING") -> list:
    """Return CoS meeting requests filtered by status."""
    try:
        if not os.path.exists(COS_MEETING_REQUESTS_PATH):
            return []
        with open(COS_MEETING_REQUESTS_PATH) as f:
            all_reqs = json.load(f)
        return [r for r in all_reqs if r.get("status") == status]
    except Exception:
        return []

def mark_cos_request_handled(req_id: str, resolution: str = "handled") -> None:
    """Mark a CoS meeting request as handled."""
    import fcntl
    try:
        with open(COS_MEETING_REQUESTS_PATH, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                # Lock must be released in finally so an exception mid-write never deadlocks.
                reqs = json.load(f)
                for r in reqs:
                    if r.get("id") == req_id:
                        r["status"] = "HANDLED"
                        r["resolution"] = resolution
                        r["handled_at"] = datetime.now().isoformat()
                f.seek(0); f.truncate()
                json.dump(reqs, f, indent=2, default=str)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.warning(f"[FirmChannel] mark_cos_request_handled failed for {req_id}: {e}")
