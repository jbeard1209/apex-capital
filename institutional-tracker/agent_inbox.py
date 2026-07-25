"""
agent_inbox.py  —  Shared inbox handler for all agents
=======================================================
Every agent calls run_inbox(agent_name) at the start of its run.
One import, one call — the rest is handled here.

Also provides the --inbox-only entry point: when an agent is woken by
wake_agent.py, it calls main_inbox_only(agent_name) instead of its
full pipeline, handles pending items, and exits cleanly.

WHAT IT HANDLES
---------------
1. Fulfilled info responses  — log them so the agent is aware
2. Pending info requests     — attempt to auto-answer from known data files;
                               otherwise log for manual handling
3. Pending ad-hoc meetings   — add a default acknowledgement response
4. Governance proposal votes — if this agent is a veto voter, prompt a vote
                               (logged as needing human review via Claude)
5. Incoming broadcasts       — log and optionally act on critical ones
6. Pending delegations       — log the task, mark acknowledged

All actions are non-fatal — a broken inbox never crashes a pipeline run.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Data file registry: what each agent "knows" ───────────────────────────────
# Used to auto-answer info requests from existing report files.
_AGENT_DATA_FILES: dict[str, list[str]] = {
    "Chief of Staff":         ["data/chief_of_staff_report.json", "data/firm_channel.json"],
    "RDTeamLeader":           ["data/research_team_report.json",  "data/sector_assignments.json"],
    "Manager Agent":          ["data/manager_decisions.json",     "data/research_team_report.json"],
    "ManagerAgent":           ["data/manager_decisions.json"],
    "CITDirector":            ["data/cit_morning_report.json",    "data/cit_evening_report.json"],
    "IndependentRiskManager": ["data/irm_daily_check.json"],
    "HRPerformanceOfficer":   ["data/hr_report.json"],
    "HRDevelopmentOfficer":   ["data/hr_report.json"],
    "CROAgent":               ["data/risk_manager_report.json"],
    "FIBoard":                ["data/fi_board_report.json"],
    "FEBoard":                ["data/fe_board_report.json"],
    "MLShortTermBoard":       ["data/ml_shortterm_board_report.json"],
    "MLLongTermBoard":        ["data/ml_longterm_board_report.json"],
    "Canary":                 ["data/canary_alert.json"],
    "DeputyMonitor":          ["data/deputy_monitor_state.json"],
    "ResearchTeam":           ["data/research_team_report.json"],
    "EnergyResearchTeam":     ["data/energy_research_report.json"],
    "MLResearchTeamLeader":   ["data/ml_research_report.json"],
}


def _load(path: str):
    full = os.path.join(BASE_DIR, path)
    if not os.path.exists(full):
        return None
    try:
        with open(full) as f:
            return json.load(f)
    except Exception:
        return None


def _age_hours(ts: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(ts[:19])).total_seconds() / 3600
    except Exception:
        return float("inf")


def _auto_answer_from_files(agent_name: str, topic: str) -> tuple[str | None, str]:
    """Try to produce an answer from this agent's known data files."""
    for path in _AGENT_DATA_FILES.get(agent_name, []):
        data = _load(path)
        if data is None:
            continue
        ts   = data.get("generated_at") or data.get("timestamp") or ""
        age  = _age_hours(ts) if ts else float("inf")
        if age > 72:
            continue   # too stale to be useful
        # Build a summary from whatever data we have
        summary_parts = []
        for key in ("summary", "verdict", "overall_status", "risk_level",
                    "level", "total_picks", "recommendations"):
            val = data.get(key)
            if val is not None:
                summary_parts.append(f"{key}: {str(val)[:100]}")
        if summary_parts:
            answer = (
                f"{agent_name} responding from {path} ({age:.1f}h old): "
                + "; ".join(summary_parts[:4])
            )
            confidence = "HIGH" if age < 6 else "MEDIUM" if age < 24 else "LOW"
            return answer, confidence
    return None, "LOW"


# =============================================================================
# PUBLIC API
# =============================================================================

def run_inbox(agent_name: str, quiet: bool = False) -> dict:
    """
    Call this at the start of any agent's run to handle its pending inbox items.
    Returns a summary dict; never raises.

    Example (one line per agent):
        from agent_inbox import run_inbox
        run_inbox("CITDirector")
    """
    summary = {
        "agent":         agent_name,
        "responses":     0,
        "requests":      0,
        "meetings":      0,
        "broadcasts":    0,
        "delegations":   0,
        "proposals":     0,
        "threads":       0,
        "active_pip":    False,
        "bulletins":     0,
        "polls":         0,
        "action_items":  0,
    }
    # Register this agent as active
    try:
        from agent_meeting import update_agent_status as _uas
        _uas(agent_name, status="OK")
    except Exception:
        pass

    try:
        from agent_meeting import (
            get_info_requests,
            get_fulfilled_responses,
            fulfill_info_request,
            get_my_meetings,
            add_meeting_response,
            get_open_proposals,
            get_my_threads,
            get_my_pip,
            get_open_polls,
            get_bulletin,
            mark_bulletin_read,
            get_my_action_items,
            update_agent_status,
        )
    except ImportError:
        return summary

    # ── 1. Fulfilled responses to my prior requests ───────────────────────────
    try:
        responses = get_fulfilled_responses(agent_name, max_age_hours=12.0)
        for resp in responses:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] ✅ Response from %s [%s confidence]: %s",
                    agent_name, resp.get("fulfilling_agent", "?"),
                    resp.get("confidence", "?"),
                    str(resp.get("result", ""))[:120],
                )
            summary["responses"] += 1
    except Exception as e:
        logger.debug("non-fatal inbox responses: %s", e)

    # ── 2. Info requests addressed to me ─────────────────────────────────────
    try:
        requests = get_info_requests(agent_name, max_age_hours=24.0)
        for req in requests:
            req_id = req["request_id"]
            topic  = req.get("topic", "")
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 📥 Info request from %s: %s",
                    agent_name, req.get("from_agent", "?"), topic[:80],
                )
            # Attempt auto-answer
            answer, conf = _auto_answer_from_files(agent_name, topic)
            if answer:
                fulfill_info_request(req_id, agent_name, answer, confidence=conf)
                if not quiet:
                    logger.info("  [INBOX:%s] 📤 Auto-answered [%s]: %s",
                                agent_name, conf, answer[:80])
            else:
                # Can't auto-answer — log it so the agent's full run can address it
                if not quiet:
                    logger.info(
                        "  [INBOX:%s] ⏳ Cannot auto-answer '%s' — will address in full run",
                        agent_name, topic[:60],
                    )
            summary["requests"] += 1
    except Exception as e:
        logger.debug("non-fatal inbox requests: %s", e)

    # ── 3. Ad-hoc meetings pending my response ────────────────────────────────
    try:
        meetings = get_my_meetings(agent_name, max_age_hours=48.0)
        for mtg in meetings:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 🤝 Meeting: %s [%s urgency]",
                    agent_name, mtg.get("agenda", "")[:80], mtg.get("urgency", "?"),
                )
            # Acknowledge so the meeting can progress; full response on next scheduled run
            add_meeting_response(
                mtg["meeting_id"], agent_name,
                response=(
                    f"{agent_name} acknowledges this meeting. "
                    f"Full analysis will be provided on next scheduled run."
                ),
                action_items=[f"Review at next scheduled cycle: {mtg.get('agenda','')[:60]}"],
            )
            summary["meetings"] += 1
    except Exception as e:
        logger.debug("non-fatal inbox meetings: %s", e)

    # ── 4. Governance proposals pending my vote (veto voters only) ────────────
    _VETO_VOTERS = {"Chief of Staff", "IndependentRiskManager", "HRPerformanceOfficer"}
    if agent_name in _VETO_VOTERS:
        try:
            proposals = get_open_proposals(max_age_hours=168.0)
            for prop in proposals:
                votes = prop.get("votes", {})
                if agent_name in votes:
                    continue   # already voted
                if not quiet:
                    logger.info(
                        "  [INBOX:%s] ⚖️  Governance proposal pending vote: "
                        "[%s] proposes fix for %s: %s",
                        agent_name,
                        prop.get("supervisor", "?"),
                        prop.get("flagged_agent", "?"),
                        prop.get("proposal", "")[:80],
                    )
                    logger.info(
                        "  [INBOX:%s] → Cast vote via: "
                        "cast_veto_vote('%s', '%s', 'APPROVE'/'VETO', reasoning)",
                        agent_name, prop["proposal_id"], agent_name,
                    )
                summary["proposals"] += 1
        except Exception as e:
            logger.debug("non-fatal inbox proposals: %s", e)

    # ── 5. Broadcasts ─────────────────────────────────────────────────────────
    try:
        from agent_meeting import get_broadcasts
        broadcasts = get_broadcasts(recipient=agent_name, max_age_hours=6.0)
        for bc in broadcasts:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 📢 Broadcast from %s [%s]: %s",
                    agent_name, bc.get("sender", "?"),
                    bc.get("severity", "?"),
                    bc.get("message", "")[:100],
                )
            summary["broadcasts"] += 1
    except Exception as e:
        logger.debug("non-fatal inbox broadcasts: %s", e)

    # ── 6. Delegations addressed to me ────────────────────────────────────────
    try:
        from agent_meeting import get_my_delegations, acknowledge_delegation
        delegations = get_my_delegations(agent_name)
        for delg in delegations:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 📋 Delegation from %s: %s (due: %s)",
                    agent_name, delg.get("from_agent", "?"),
                    delg.get("task", "")[:80],
                    delg.get("due_by", "unspecified"),
                )
            acknowledge_delegation(delg["delegation_id"], agent_name)
            summary["delegations"] += 1
    except Exception as e:
        logger.debug("non-fatal inbox delegations: %s", e)

    # ── 7. Bulletin board posts from other agents ────────────────────────────
    try:
        posts = get_bulletin(max_age_hours=6.0, agent_name=agent_name)
        for post in posts:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 📌 Bulletin [%s] from %s: %s",
                    agent_name, post.get("topic","?"),
                    post.get("agent","?"), post.get("message","")[:100],
                )
            mark_bulletin_read(post.get("bulletin_id",""), agent_name)
            summary["bulletins"] = summary.get("bulletins", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox bulletins: %s", e)

    # ── 8. Open polls awaiting my vote ────────────────────────────────────
    try:
        polls = get_open_polls(agent_name=agent_name)
        for poll in polls:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 🗳️  Poll from %s: %s | Options: %s",
                    agent_name, poll.get("initiator","?"),
                    poll.get("question","")[:80],
                    ", ".join(poll.get("options",[]))[:60],
                )
                logger.info(
                    "  [INBOX:%s] → Vote via: cast_vote('%s', '%s', '<option>', reasoning)",
                    agent_name, poll.get("poll_id","?"), agent_name,
                )
            summary["polls"] = summary.get("polls", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox polls: %s", e)

    # ── 9. Open action items ──────────────────────────────────────────────
    try:
        action_items = get_my_action_items(agent_name, max_age_hours=168.0)
        for item in action_items:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] ✅ Action item [%s] from %s: %s (due: %s)",
                    agent_name, item.get("priority","?"),
                    item.get("created_by","?"),
                    item.get("description","")[:80],
                    item.get("due_by","unset"),
                )
            summary["action_items"] = summary.get("action_items", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox action_items: %s", e)

    # ── 10. Active conversation threads (replies needing attention) ───────
    try:
        from agent_meeting import get_my_threads
        threads = get_my_threads(agent_name, max_age_hours=48.0, unresolved_only=True)
        for t in threads:
            # Find the latest message in the thread NOT from this agent
            thread_msgs = t.get("thread", [])
            unread = [m for m in thread_msgs if m.get("from") != agent_name]
            if not unread:
                continue
            latest = unread[-1]
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 💬 Thread reply from %s: %s",
                    agent_name, latest.get("from", "?"),
                    latest.get("message", "")[:100],
                )
            summary["threads"] = summary.get("threads", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox threads: %s", e)

    # ── 8. Active PIP (Performance Improvement Plan) ──────────────────────
    try:
        from agent_meeting import get_my_pip
        pip = get_my_pip(agent_name)
        if pip and not quiet:
            logger.info(
                "  [INBOX:%s] 📋 ACTIVE PIP (issued by %s, %d-day plan): targets: %s",
                agent_name,
                pip.get("issued_by", "?"),
                pip.get("timeline_days", 0),
                "; ".join(pip.get("targets", [])[:2]),
            )
            summary["active_pip"] = True
    except Exception as e:
        logger.debug("non-fatal inbox pip: %s", e)

    total = sum(v for k, v in summary.items() if k not in ("agent", "active_pip"))
    if total and not quiet:
        pip_note = " [ACTIVE PIP]" if summary.get("active_pip") else ""
        logger.info(
            "  [INBOX:%s] Summary: %d item(s) processed "
            "(responses=%d, requests=%d, meetings=%d, broadcasts=%d, "
            "delegations=%d, proposals=%d, threads=%d)%s",
            agent_name, total,
            summary["responses"], summary["requests"], summary["meetings"],
            summary["broadcasts"], summary["delegations"], summary["proposals"],
            summary.get("threads", 0), pip_note,
        )
    return summary


def main_inbox_only(agent_name: str) -> None:
    """
    Entry point for --inbox-only mode.
    Agents call this when woken by wake_agent.py instead of running full pipeline.
    """
    logger.info("[InboxOnly] %s woken for inbox processing", agent_name)
    summary = run_inbox(agent_name, quiet=False)
    # ── 7. Bulletin board posts from other agents ────────────────────────────
    try:
        posts = get_bulletin(max_age_hours=6.0, agent_name=agent_name)
        for post in posts:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 📌 Bulletin [%s] from %s: %s",
                    agent_name, post.get("topic","?"),
                    post.get("agent","?"), post.get("message","")[:100],
                )
            mark_bulletin_read(post.get("bulletin_id",""), agent_name)
            summary["bulletins"] = summary.get("bulletins", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox bulletins: %s", e)

    # ── 8. Open polls awaiting my vote ────────────────────────────────────
    try:
        polls = get_open_polls(agent_name=agent_name)
        for poll in polls:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 🗳️  Poll from %s: %s | Options: %s",
                    agent_name, poll.get("initiator","?"),
                    poll.get("question","")[:80],
                    ", ".join(poll.get("options",[]))[:60],
                )
                logger.info(
                    "  [INBOX:%s] → Vote via: cast_vote('%s', '%s', '<option>', reasoning)",
                    agent_name, poll.get("poll_id","?"), agent_name,
                )
            summary["polls"] = summary.get("polls", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox polls: %s", e)

    # ── 9. Open action items ──────────────────────────────────────────────
    try:
        action_items = get_my_action_items(agent_name, max_age_hours=168.0)
        for item in action_items:
            if not quiet:
                logger.info(
                    "  [INBOX:%s] ✅ Action item [%s] from %s: %s (due: %s)",
                    agent_name, item.get("priority","?"),
                    item.get("created_by","?"),
                    item.get("description","")[:80],
                    item.get("due_by","unset"),
                )
            summary["action_items"] = summary.get("action_items", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox action_items: %s", e)

    # ── 10. Active conversation threads (replies needing attention) ───────
    try:
        from agent_meeting import get_my_threads
        threads = get_my_threads(agent_name, max_age_hours=48.0, unresolved_only=True)
        for t in threads:
            # Find the latest message in the thread NOT from this agent
            thread_msgs = t.get("thread", [])
            unread = [m for m in thread_msgs if m.get("from") != agent_name]
            if not unread:
                continue
            latest = unread[-1]
            if not quiet:
                logger.info(
                    "  [INBOX:%s] 💬 Thread reply from %s: %s",
                    agent_name, latest.get("from", "?"),
                    latest.get("message", "")[:100],
                )
            summary["threads"] = summary.get("threads", 0) + 1
    except Exception as e:
        logger.debug("non-fatal inbox threads: %s", e)

    # ── 8. Active PIP (Performance Improvement Plan) ──────────────────────
    try:
        from agent_meeting import get_my_pip
        pip = get_my_pip(agent_name)
        if pip and not quiet:
            logger.info(
                "  [INBOX:%s] 📋 ACTIVE PIP (issued by %s, %d-day plan): targets: %s",
                agent_name,
                pip.get("issued_by", "?"),
                pip.get("timeline_days", 0),
                "; ".join(pip.get("targets", [])[:2]),
            )
            summary["active_pip"] = True
    except Exception as e:
        logger.debug("non-fatal inbox pip: %s", e)

    total = sum(v for k, v in summary.items() if k not in ("agent", "active_pip"))
    logger.info("[InboxOnly] %s done — %d item(s) handled", agent_name, total)
