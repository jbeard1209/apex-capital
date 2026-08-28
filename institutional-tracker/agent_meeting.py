"""
agent_meeting.py  —  Apex Capital Agent-to-Agent Meeting System
================================================================
Agents can request information from each other and conduct "ad-hoc meetings"
without waiting for their next scheduled cron run.

PUBLIC API
----------
  request_info(from_agent, to_agent, topic, context, urgency, callback_hint)
      → request_id str

  get_info_requests(agent_name)          → list of open requests
  fulfill_info_request(request_id, fulfilling_agent, result, confidence)
  get_fulfilled_responses(requesting_agent, max_age_hours)  → list
  dismiss_request(request_id)            → none

  call_ad_hoc_meeting(initiator, participants, agenda, urgency)
      → meeting_id str

  get_my_meetings(agent_name)            → list of meetings pending my input
  add_meeting_response(meeting_id, agent_name, response, action_items)
  get_meeting_summary(meeting_id)        → dict

GOVERNANCE VETO (3-WAY: HR / CoS / IRM)
-----------------------------------------
  submit_supervisor_proposal(supervisor, flagged_agent, proposal, rationale)
      → proposal_id str

  cast_veto_vote(proposal_id, voter, vote, reasoning)
      → {"approved": bool, "votes": dict, "action": str}

  get_open_proposals()                   → list
  get_proposal(proposal_id)              → dict

DESIGN
------
All state lives in data/agent_meetings.json  (requests + meetings)
            and data/governance_proposals.json  (veto votes)
Both are append-only logs with soft-delete ("dismissed"/"resolved" status).
All writes are atomic (write to .tmp, rename).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

_BASE   = os.path.dirname(os.path.abspath(__file__))
_MEET   = os.path.join(_BASE, "data", "agent_meetings.json")
_PROP   = os.path.join(_BASE, "data", "governance_proposals.json")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat()

def _uid() -> str:
    return uuid.uuid4().hex[:12]

def _age_hours(ts: str) -> float:
    """Age in hours; `inf` when the stamp is missing or unreadable.

    `inf` is fail-closed here: ~25 call sites in this module use it as
    `_age_hours(x) <= max_age_hours` to keep a record in scope, so an
    unreadable stamp drops the record rather than admitting it forever.
    """
    from time_utils import age_hours
    return age_hours(ts, default=float("inf"))

def _load(path: str) -> list:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _load_dict(path: str) -> dict:
    """Like _load but for dict-based JSON files (e.g. agent_status)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save(path: str, data: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_ = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _firm_post(from_agent: str, category: str, content: str, priority: str = "NORMAL") -> None:
    """Best-effort firm_channel post — never raises."""
    try:
        from firm_channel import post
        post(from_agent, category, content, priority=priority)
    except Exception:
        pass

def _firm_task(from_agent: str, to_agent: str, task: str, priority: str = "HIGH") -> None:
    try:
        from firm_channel import post_task_request
        post_task_request(from_agent, to_agent, task, priority=priority)
    except Exception:
        pass

# =============================================================================
# SECTION 1 — INFO REQUESTS  (agent A asks agent B a question)
# =============================================================================

def request_info(
    from_agent:    str,
    to_agent:      str,
    topic:         str,
    context:       str = "",
    urgency:       str = "NORMAL",   # NORMAL | HIGH | CRITICAL
    callback_hint: str = "",         # e.g. "needed before next trading cycle"
) -> str:
    """
    Agent A asks agent B for specific information.
    Returns a request_id the caller can poll with get_fulfilled_responses().

    Example:
        rid = request_info(
            "Manager Agent", "RDTeamLeader",
            topic="Current conviction score for NVDA",
            context="Considering a position increase before earnings",
            urgency="HIGH",
            callback_hint="needed within next trading cycle"
        )
    """
    records = _load(_MEET)
    req_id  = _uid()
    record  = {
        "type":          "INFO_REQUEST",
        "request_id":    req_id,
        "from_agent":    from_agent,
        "to_agent":      to_agent,
        "topic":         topic,
        "context":       context,
        "urgency":       urgency,
        "callback_hint": callback_hint,
        "status":        "open",
        "filed_at":      _now(),
        "fulfilled_at":  None,
        "result":        None,
        "confidence":    None,
        "fulfilling_agent": None,
    }
    records.append(record)
    _save(_MEET, records)

    # Notify via firm_channel task so the recipient sees it in their inbox
    msg = (
        f"INFO REQUEST from {from_agent}: '{topic}'. "
        f"Context: {context[:200]}. "
        f"Urgency: {urgency}."
        + (f" Needed: {callback_hint}." if callback_hint else "")
        + f" [request_id: {req_id}]"
    )
    _firm_task(from_agent, to_agent, msg,
               priority="HIGH" if urgency in ("HIGH", "CRITICAL") else "NORMAL")

    # For HIGH/CRITICAL requests, wake the target agent immediately so they
    # don't have to wait for their next scheduled cron run.
    if urgency in ("HIGH", "CRITICAL"):
        try:
            from wake_agent import wake_nonblocking as _wnb
            _wnb(to_agent, request_id=req_id)
        except Exception:
            pass

    return req_id


def get_info_requests(agent_name: str, max_age_hours: float = 48.0) -> list:
    """Return open INFO_REQUESTs addressed to this agent."""
    records = _load(_MEET)
    return [
        r for r in records
        if r.get("type")     == "INFO_REQUEST"
        and r.get("to_agent") == agent_name
        and r.get("status")   == "open"
        and _age_hours(r.get("filed_at", "")) <= max_age_hours
    ]


def fulfill_info_request(
    request_id:       str,
    fulfilling_agent: str,
    result:           str,
    confidence:       str = "HIGH",   # HIGH | MEDIUM | LOW
) -> bool:
    """
    Agent B answers a pending info request.
    Returns True if the request was found and marked fulfilled.

    Example:
        fulfill_info_request(
            req_id, "RDTeamLeader",
            result="NVDA conviction score: 4★ — accelerating EPS, strong moat.",
            confidence="HIGH"
        )
    """
    records = _load(_MEET)
    found   = False
    req_rec = None
    for r in records:
        if r.get("request_id") == request_id and r.get("status") == "open":
            r["status"]          = "fulfilled"
            r["fulfilled_at"]    = _now()
            r["result"]          = result
            r["confidence"]      = confidence
            r["fulfilling_agent"] = fulfilling_agent
            req_rec = r
            found   = True
            break
    if found:
        _save(_MEET, records)
        if req_rec:
            _firm_post(
                fulfilling_agent, "INFO_RESPONSE",
                f"INFO RESPONSE to {req_rec['from_agent']}: "
                f"[{confidence} confidence] {result[:300]} "
                f"[request_id: {request_id}]",
                priority="HIGH" if req_rec.get("urgency") in ("HIGH", "CRITICAL") else "NORMAL",
            )
    return found


def get_fulfilled_responses(requesting_agent: str, max_age_hours: float = 24.0) -> list:
    """Return fulfilled responses to this agent's recent info requests."""
    records = _load(_MEET)
    return [
        r for r in records
        if r.get("type")       == "INFO_REQUEST"
        and r.get("from_agent") == requesting_agent
        and r.get("status")     == "fulfilled"
        and _age_hours(r.get("fulfilled_at", "")) <= max_age_hours
    ]


def dismiss_request(request_id: str, reason: str = "resolved") -> None:
    """Mark a request dismissed (e.g. agent obtained info another way)."""
    records = _load(_MEET)
    for r in records:
        if r.get("request_id") == request_id:
            r["status"]       = "dismissed"
            r["dismissed_at"] = _now()
            r["dismiss_reason"] = reason
    _save(_MEET, records)


# =============================================================================
# SECTION 2 — AD-HOC MEETINGS  (multi-agent discussion, one record per topic)
# =============================================================================

def call_ad_hoc_meeting(
    initiator:    str,
    participants: list[str],
    agenda:       str,
    urgency:      str = "NORMAL",
    context:      str = "",
) -> str:
    """
    Initiator calls an ad-hoc meeting with one or more agents.
    Each participant is expected to add a response via add_meeting_response().

    Example:
        mid = call_ad_hoc_meeting(
            "Chief of Staff",
            ["CITDirector", "IndependentRiskManager"],
            agenda="CIT negative outcome rate: agree on corrective action",
            urgency="HIGH",
        )
    """
    records    = _load(_MEET)
    meeting_id = _uid()
    record     = {
        "type":         "AD_HOC_MEETING",
        "meeting_id":   meeting_id,
        "initiator":    initiator,
        "participants": list(participants),
        "agenda":       agenda,
        "urgency":      urgency,
        "context":      context,
        "status":       "open",        # open | concluded | cancelled
        "called_at":    _now(),
        "concluded_at": None,
        "responses":    {},            # {agent_name: {response, action_items, ts}}
        "conclusion":   None,
    }
    records.append(record)
    _save(_MEET, records)

    # Notify all participants via firm_channel
    for participant in participants:
        _firm_task(
            initiator, participant,
            f"AD-HOC MEETING REQUESTED [{urgency}]: {agenda}. "
            f"Called by {initiator}. "
            + (f"Context: {context[:200]}. " if context else "")
            + f"Please add your response via add_meeting_response(). "
            f"[meeting_id: {meeting_id}]",
            priority="HIGH" if urgency in ("HIGH", "CRITICAL") else "NORMAL",
        )
    return meeting_id


def get_my_meetings(agent_name: str, max_age_hours: float = 48.0) -> list:
    """Return open AD_HOC_MEETINGs where this agent is a participant or initiator
    and hasn't yet submitted a response."""
    records = _load(_MEET)
    result  = []
    for r in records:
        if r.get("type") != "AD_HOC_MEETING":
            continue
        if r.get("status") != "open":
            continue
        if _age_hours(r.get("called_at", "")) > max_age_hours:
            continue
        is_participant = (
            agent_name == r.get("initiator")
            or agent_name in r.get("participants", [])
        )
        has_responded = agent_name in r.get("responses", {})
        if is_participant and not has_responded:
            result.append(r)
    return result


def add_meeting_response(
    meeting_id:   str,
    agent_name:   str,
    response:     str,
    action_items: list[str] | None = None,
) -> dict:
    """
    Agent adds their input to an ad-hoc meeting.
    Returns {"concluded": bool, "waiting_on": [agents...]}
    """
    records = _load(_MEET)
    for r in records:
        if r.get("meeting_id") != meeting_id:
            continue
        r.setdefault("responses", {})[agent_name] = {
            "response":     response,
            "action_items": action_items or [],
            "ts":           _now(),
        }
        # Check if all participants have responded
        all_agents   = set(r.get("participants", [])) | {r.get("initiator")}
        responded    = set(r["responses"].keys())
        waiting_on   = list(all_agents - responded)
        if not waiting_on:
            r["status"]       = "concluded"
            r["concluded_at"] = _now()
            # Build summary conclusion
            r["conclusion"] = _summarize_meeting(r)
            _firm_post(
                r["initiator"], "MEETING_CONCLUDED",
                f"Meeting concluded: {r['agenda'][:100]} "
                f"[meeting_id: {meeting_id}]. "
                f"All {len(all_agents)} participant(s) responded.",
                priority="NORMAL",
            )
        _save(_MEET, records)
        return {"concluded": not waiting_on, "waiting_on": waiting_on}
    return {"concluded": False, "waiting_on": [], "error": "meeting not found"}


def _summarize_meeting(record: dict) -> str:
    lines = [f"MEETING CONCLUDED: {record['agenda']}"]
    for agent, resp in record.get("responses", {}).items():
        lines.append(f"  {agent}: {resp['response'][:200]}")
        for item in resp.get("action_items", []):
            lines.append(f"    → ACTION: {item}")
    return "\n".join(lines)


def get_meeting_summary(meeting_id: str) -> dict:
    records = _load(_MEET)
    for r in records:
        if r.get("meeting_id") == meeting_id:
            return r
    return {}


def get_open_meetings(max_age_hours: float = 72.0) -> list:
    records = _load(_MEET)
    return [
        r for r in records
        if r.get("type")   == "AD_HOC_MEETING"
        and r.get("status") == "open"
        and _age_hours(r.get("called_at", "")) <= max_age_hours
    ]


# =============================================================================
# SECTION 3 — GOVERNANCE PROPOSALS  (3-way HR / CoS / IRM veto)
# =============================================================================
# Flow:
#   1. A supervisor submits a proposal to fix a flagged agent.
#   2. CoS, the IRM, and the head of HR each cast a vote (APPROVE / VETO).
#   3. If ANY voter vetoes, the proposal is rejected and a resolution meeting
#      is called automatically among the three.
#   4. If all three approve, Claude implements the fix.
# =============================================================================

_VETO_VOTERS = {"Chief of Staff", "IndependentRiskManager", "HRPerformanceOfficer"}


def submit_supervisor_proposal(
    supervisor:    str,
    flagged_agent: str,
    proposal:      str,
    rationale:     str = "",
    hr_issue_ref:  str = "",   # e.g. firm_channel post ID or HR memo ref
) -> str:
    """
    Supervisor submits a corrective-action proposal for a flagged agent.
    Notifies CoS, IRM, and HR head to review and vote.

    Returns proposal_id.
    """
    records    = _load(_PROP)
    prop_id    = _uid()
    record     = {
        "proposal_id":   prop_id,
        "supervisor":    supervisor,
        "flagged_agent": flagged_agent,
        "proposal":      proposal,
        "rationale":     rationale,
        "hr_issue_ref":  hr_issue_ref,
        "status":        "pending",   # pending | approved | vetoed | disputed
        "filed_at":      _now(),
        "resolved_at":   None,
        "votes":         {},          # {voter: {vote, reasoning, ts}}
        "resolution_meeting_id": None,
    }
    records.append(record)
    _save(_PROP, records)

    # Notify all three approvers
    for voter in _VETO_VOTERS:
        _firm_task(
            supervisor, voter,
            f"GOVERNANCE PROPOSAL requires your review: "
            f"{supervisor} proposes fix for {flagged_agent}: "
            f"\"{proposal[:200]}\". "
            f"Rationale: {rationale[:150]}. "
            f"APPROVE or VETO via cast_veto_vote(). "
            f"[proposal_id: {prop_id}]",
            priority="HIGH",
        )

    _firm_post(
        supervisor, "GOVERNANCE_PROPOSAL",
        f"Proposal filed for {flagged_agent} by {supervisor}: {proposal[:150]} "
        f"[proposal_id: {prop_id}] — awaiting CoS + IRM + HR approval.",
        priority="HIGH",
    )
    return prop_id


def cast_veto_vote(
    proposal_id: str,
    voter:       str,
    vote:        str,    # "APPROVE" or "VETO"
    reasoning:   str = "",
) -> dict:
    """
    CoS, IRM, or HR head casts their vote on a pending proposal.
    Returns {"approved": bool, "status": str, "votes": dict,
             "waiting_on": [agents], "resolution_meeting_id": str|None}

    If all three approve  → status = "approved" (ready to implement)
    If any voter vetoes   → status = "vetoed", resolution meeting called
    """
    if voter not in _VETO_VOTERS:
        return {"error": f"{voter} is not a recognized veto voter. "
                         f"Valid voters: {sorted(_VETO_VOTERS)}"}
    vote = vote.upper()
    if vote not in ("APPROVE", "VETO"):
        return {"error": "vote must be APPROVE or VETO"}

    records = _load(_PROP)
    for r in records:
        if r.get("proposal_id") != proposal_id:
            continue
        if r.get("status") not in ("pending", "disputed"):
            return {"error": f"Proposal already {r['status']} — cannot re-vote"}

        r.setdefault("votes", {})[voter] = {
            "vote":      vote,
            "reasoning": reasoning,
            "ts":        _now(),
        }

        votes_cast  = r["votes"]
        all_voted   = _VETO_VOTERS.issubset(set(votes_cast.keys()))
        any_veto    = any(v["vote"] == "VETO" for v in votes_cast.values())
        waiting_on  = sorted(_VETO_VOTERS - set(votes_cast.keys()))

        if any_veto and all_voted:
            r["status"]      = "vetoed"
            r["resolved_at"] = _now()
            # Auto-call a resolution meeting among the three voters
            mid = call_ad_hoc_meeting(
                initiator    = "Chief of Staff",
                participants = list(_VETO_VOTERS - {"Chief of Staff"}),
                agenda       = (
                    f"VETO RESOLUTION for proposal [{proposal_id}]: "
                    f"{r['supervisor']}'s plan for {r['flagged_agent']}. "
                    f"Vetoed by: {[k for k,v in votes_cast.items() if v['vote']=='VETO']}. "
                    f"Must agree on alternative corrective action."
                ),
                urgency = "HIGH",
                context = f"Original proposal: {r['proposal'][:300]}",
            )
            r["resolution_meeting_id"] = mid
            _firm_post(
                "Governance System", "GOVERNANCE_VETO",
                f"PROPOSAL VETOED [{proposal_id}] — resolution meeting called "
                f"[meeting_id: {mid}]. Participants: {sorted(_VETO_VOTERS)}.",
                priority="HIGH",
            )
        elif any_veto and not all_voted:
            # Any single VETO immediately triggers resolution meeting
            r["status"]      = "vetoed"
            r["resolved_at"] = _now()
            mid2 = call_ad_hoc_meeting(
                initiator    = "Chief of Staff",
                participants = list(_VETO_VOTERS - {"Chief of Staff"}),
                agenda       = (
                    "VETO RESOLUTION for proposal [" + proposal_id + "]: "
                    + r['supervisor'] + "'s plan for " + r['flagged_agent'] + ". "
                    + "Vetoed by: " + voter + ". Remaining voters not yet in. "
                    + "Must agree on alternative corrective action."
                ),
                urgency = "HIGH",
                context = "Original proposal: " + r['proposal'][:300],
            )
            r["resolution_meeting_id"] = mid2
            _firm_post(
                voter, "GOVERNANCE_VETO",
                "VETO cast by " + voter + " on proposal [" + proposal_id + "] -- "
                + "resolution meeting called immediately [meeting_id: " + mid2 + "]. "
                + "Remaining voters: " + str(waiting_on) + ".",
                priority="HIGH",
            )
        elif all_voted and not any_veto:
            r["status"]      = "approved"
            r["resolved_at"] = _now()
            _firm_post(
                "Governance System", "GOVERNANCE_APPROVED",
                f"PROPOSAL APPROVED [{proposal_id}]: {r['supervisor']}'s fix for "
                f"{r['flagged_agent']} approved unanimously by CoS + IRM + HR. "
                f"Ready for implementation. Proposal: {r['proposal'][:200]}",
                priority="HIGH",
            )
        else:
            _firm_post(
                voter, "GOVERNANCE_VOTE",
                f"{voter} voted {vote} on proposal [{proposal_id}]. "
                f"Still waiting on: {waiting_on}.",
                priority="NORMAL",
            )

        _save(_PROP, records)
        return {
            "approved":              r["status"] == "approved",
            "status":                r["status"],
            "votes":                 r["votes"],
            "waiting_on":            waiting_on,
            "resolution_meeting_id": r.get("resolution_meeting_id"),
        }

    return {"error": f"proposal {proposal_id} not found"}


def get_open_proposals(max_age_hours: float = 168.0) -> list:
    """Return proposals in pending/disputed/vetoed state within the last week."""
    records = _load(_PROP)
    return [
        r for r in records
        if r.get("status") in ("pending", "disputed", "vetoed")
        and _age_hours(r.get("filed_at", "")) <= max_age_hours
    ]


def get_approved_proposals(max_age_hours: float = 48.0) -> list:
    """Return recently approved proposals ready for Claude to implement."""
    records = _load(_PROP)
    return [
        r for r in records
        if r.get("status") == "approved"
        and _age_hours(r.get("resolved_at", "")) <= max_age_hours
    ]


def get_proposal(proposal_id: str) -> dict:
    records = _load(_PROP)
    for r in records:
        if r.get("proposal_id") == proposal_id:
            return r
    return {}


def mark_proposal_implemented(proposal_id: str, implementation_notes: str = "") -> None:
    """Claude calls this after implementing an approved proposal."""
    records = _load(_PROP)
    for r in records:
        if r.get("proposal_id") == proposal_id:
            r["status"]                = "implemented"
            r["implemented_at"]        = _now()
            r["implementation_notes"]  = implementation_notes
    _save(_PROP, records)
    _firm_post(
        "Claude", "GOVERNANCE_IMPLEMENTED",
        f"Proposal [{proposal_id}] implemented. Notes: {implementation_notes[:200]}",
        priority="NORMAL",
    )


# =============================================================================
# SECTION 4 — CONVENIENCE: research request helpers  (Manager ↔ R&D Director)
# =============================================================================

def manager_request_research(
    topic:   str,
    context: str = "",
    urgency: str = "HIGH",
) -> str:
    """
    Shortcut: Manager Agent requests specific research from the RD Team Leader.
    Returns request_id.
    """
    return request_info(
        from_agent    = "Manager Agent",
        to_agent      = "RDTeamLeader",
        topic         = topic,
        context       = context,
        urgency       = urgency,
        callback_hint = "needed before next trading cycle",
    )


def rd_leader_delegate_to_team(
    topic:     str,
    context:   str  = "",
    requester: str  = "Manager Agent",
    urgency:   str  = "HIGH",
) -> list[str]:
    """
    RD Team Leader fans out a research request to all available sub-agents.
    Returns list of request_ids.
    """
    _RESEARCH_AGENTS = [
        "ResearchTeam",
        "EnergyResearchTeam",
        "MLResearchTeamLeader",
    ]
    ids = []
    for agent in _RESEARCH_AGENTS:
        rid = request_info(
            from_agent    = "RDTeamLeader",
            to_agent      = agent,
            topic         = topic,
            context       = f"Delegated from {requester}. {context}",
            urgency       = urgency,
            callback_hint = "return to RD Team Leader for consolidation",
        )
        ids.append(rid)
    return ids




# =============================================================================
# SECTION 5 — BROADCASTS  (one agent → all agents)
# =============================================================================

_BC_PATH = os.path.join(_BASE, "data", "agent_broadcasts.json")

def broadcast(
    sender:    str,
    message:   str,
    severity:  str = "INFO",     # INFO | WARNING | CRITICAL
    category:  str = "general",  # market | risk | governance | ops | general
    recipients: list[str] | None = None,  # None = all agents
) -> str:
    """
    Publish a message all agents will see at the start of their next run.
    If severity=CRITICAL, auto-wakes all recipient agents immediately.

    Returns broadcast_id.

    Example:
        broadcast("Canary", "Market entered RED zone — SPY -3.2%",
                  severity="CRITICAL", category="market")
    """
    from wake_agent import AGENT_REGISTRY, wake_nonblocking as _wnb
    records = _load(_BC_PATH)
    bc_id   = _uid()
    record  = {
        "broadcast_id": bc_id,
        "sender":       sender,
        "message":      message,
        "severity":     severity,
        "category":     category,
        "recipients":   recipients or list(AGENT_REGISTRY.keys()),
        "sent_at":      _now(),
        "read_by":      [],
    }
    records.append(record)
    _save(_BC_PATH, records)

    _firm_post(
        sender, f"BROADCAST_{severity}",
        f"[{severity}] {message[:200]}",
        priority="URGENT" if severity == "CRITICAL" else
                 "HIGH"   if severity == "WARNING"  else "NORMAL",
    )

    # For CRITICAL broadcasts, wake all recipients immediately
    if severity == "CRITICAL":
        for agent in record["recipients"]:
            try:
                _wnb(agent)
            except Exception:
                pass

    return bc_id


def get_broadcasts(
    recipient:    str | None = None,
    max_age_hours: float = 6.0,
    unread_only:  bool = True,
) -> list:
    """Return broadcasts addressed to this agent (or all if recipient=None)."""
    records = _load(_BC_PATH)
    if records is None:
        return []
    result = []
    for r in records:
        if _age_hours(r.get("sent_at", "")) > max_age_hours:
            continue
        if recipient and recipient not in r.get("recipients", []):
            continue
        if unread_only and recipient and recipient in r.get("read_by", []):
            continue
        result.append(r)
    return result


def mark_broadcast_read(broadcast_id: str, agent_name: str) -> None:
    records = _load(_BC_PATH)
    if records is None:
        return
    for r in records:
        if r.get("broadcast_id") == broadcast_id:
            r.setdefault("read_by", [])
            if agent_name not in r["read_by"]:
                r["read_by"].append(agent_name)
    _save(_BC_PATH, records)


# =============================================================================
# SECTION 6 — TASK DELEGATIONS  (named owner + deliverable + deadline)
# =============================================================================

_DELG_PATH = os.path.join(_BASE, "data", "agent_delegations.json")

def delegate_task(
    from_agent:  str,
    to_agent:    str,
    task:        str,
    deliverable: str = "",     # what the output should look like
    due_by:      str = "",     # ISO datetime string or human description
    priority:    str = "NORMAL",
    context:     str = "",
) -> str:
    """
    Formally delegate a task to another agent with a named deliverable.
    Stronger than an info request: has acceptance criteria and completion tracking.

    Returns delegation_id.

    Example:
        delegate_task(
            "Chief of Staff", "CITDirector",
            task="Audit all CITSelfImprovement proposals from last 30 days",
            deliverable="List of proposals with evidence quality score (1-5)",
            due_by="2026-05-26T09:00:00",
            priority="HIGH",
        )
    """
    records    = _load(_DELG_PATH)
    if records is None:
        records = []
    delg_id    = _uid()
    record     = {
        "delegation_id": delg_id,
        "from_agent":    from_agent,
        "to_agent":      to_agent,
        "task":          task,
        "deliverable":   deliverable,
        "due_by":        due_by,
        "priority":      priority,
        "context":       context,
        "status":        "pending",    # pending | acknowledged | in_progress | completed | failed
        "filed_at":      _now(),
        "acknowledged_at": None,
        "completed_at":  None,
        "result":        None,
    }
    records.append(record)
    _save(_DELG_PATH, records)

    msg = (
        f"TASK DELEGATED to {to_agent} from {from_agent}: {task}. "
        + (f"Deliverable: {deliverable}. " if deliverable else "")
        + (f"Due: {due_by}. "             if due_by      else "")
        + f"Priority: {priority}. [delegation_id: {delg_id}]"
    )
    _firm_task(from_agent, to_agent, msg, priority=priority)

    # Auto-wake for HIGH/CRITICAL delegations
    if priority in ("HIGH", "CRITICAL"):
        try:
            from wake_agent import wake_nonblocking as _wnb2
            _wnb2(to_agent, request_id=delg_id)
        except Exception:
            pass

    return delg_id


def get_my_delegations(
    agent_name:    str,
    max_age_hours: float = 72.0,
    status_filter: list[str] | None = None,
) -> list:
    """Return delegations assigned to this agent."""
    records = _load(_DELG_PATH)
    if records is None:
        return []
    statuses = status_filter or ["pending", "acknowledged"]
    return [
        r for r in records
        if r.get("to_agent") == agent_name
        and r.get("status") in statuses
        and _age_hours(r.get("filed_at", "")) <= max_age_hours
    ]


def acknowledge_delegation(delegation_id: str, agent_name: str, note: str = "") -> None:
    records = _load(_DELG_PATH)
    if records is None:
        return
    for r in records:
        if r.get("delegation_id") == delegation_id and r.get("to_agent") == agent_name:
            r["status"]           = "acknowledged"
            r["acknowledged_at"]  = _now()
            if note:
                r["ack_note"] = note
    _save(_DELG_PATH, records)


def complete_delegation(
    delegation_id: str,
    completing_agent: str,
    result: str,
    artifacts: list[str] | None = None,
) -> None:
    """Mark a delegation complete and notify the delegating agent."""
    records = _load(_DELG_PATH)
    if records is None:
        return
    for r in records:
        if r.get("delegation_id") == delegation_id:
            r["status"]       = "completed"
            r["completed_at"] = _now()
            r["result"]       = result
            if artifacts:
                r["artifacts"] = artifacts
            _firm_post(
                completing_agent, "DELEGATION_COMPLETE",
                f"Task completed by {completing_agent} for {r['from_agent']}: "
                f"{r['task'][:80]}. Result: {result[:200]} [delegation_id: {delegation_id}]",
                priority="HIGH" if r.get("priority") in ("HIGH","CRITICAL") else "NORMAL",
            )
    _save(_DELG_PATH, records)



# =============================================================================
# SECTION 7 — CONVERSATION THREADS  (back-and-forth replies on any request)
# =============================================================================
# Any agent can reply to an open info request to ask for clarification,
# push back, or continue a dialogue.  Replies wake the other party
# immediately with no cooldown (via wake_for_thread_reply).

def reply_to_request(
    request_id: str,
    from_agent:  str,
    message:     str,
) -> bool:
    """
    Post a reply in the thread of an existing info request or meeting.
    The other party is woken immediately (no cooldown) to continue the dialogue.

    Returns True if the request was found and updated.

    Example — requester asks a follow-up:
        reply_to_request(req_id, "Manager Agent",
                         "Can you break that down by sector?")

    Example — fulfiller asks for clarification before answering:
        reply_to_request(req_id, "MLShortTermBoard",
                         "Do you mean intra-day or close-to-close return?")
    """
    records = _load(_MEET)
    if records is None:
        return False
    for r in records:
        if r.get("request_id") != request_id and r.get("meeting_id") != request_id:
            continue
        r.setdefault("thread", [])
        r["thread"].append({
            "from":    from_agent,
            "message": message,
            "at":      _now(),
        })
        r["last_activity"] = _now()
        _save(_MEET, records)

        # Determine who to wake: if requester replies, wake the target; and vice versa
        other = None
        if r.get("type") == "INFO_REQUEST":
            if from_agent == r.get("from_agent"):
                other = r.get("to_agent")
            else:
                other = r.get("from_agent")
        elif r.get("type") == "AD_HOC_MEETING":
            # Wake all participants except the sender
            participants = [p for p in r.get("participants", []) if p != from_agent]
            for p in participants:
                try:
                    from wake_agent import wake_for_thread_reply as _wtr
                    _wtr(p, thread_id=request_id)
                except Exception:
                    pass
            return True

        if other:
            try:
                from wake_agent import wake_for_thread_reply as _wtr
                _wtr(other, thread_id=request_id)
            except Exception:
                pass
        return True
    return False


def get_thread(request_id: str) -> list:
    """Return the full message thread for a request/meeting."""
    records = _load(_MEET)
    if not records:
        return []
    for r in records:
        if r.get("request_id") == request_id or r.get("meeting_id") == request_id:
            return r.get("thread", [])
    return []


def resolve_thread(request_id: str, resolved_by: str) -> None:
    """Mark a conversation thread as resolved."""
    records = _load(_MEET)
    if not records:
        return
    for r in records:
        if r.get("request_id") == request_id or r.get("meeting_id") == request_id:
            r["thread_resolved"]   = True
            r["thread_resolved_by"] = resolved_by
            r["thread_resolved_at"] = _now()
    _save(_MEET, records)


def get_my_threads(
    agent_name:    str,
    max_age_hours: float = 48.0,
    unresolved_only: bool = True,
) -> list:
    """Return all active conversation threads this agent is part of."""
    records = _load(_MEET)
    if not records:
        return []
    result = []
    for r in records:
        if _age_hours(r.get("filed_at", r.get("scheduled_at", ""))) > max_age_hours:
            continue
        is_participant = (
            r.get("from_agent") == agent_name
            or r.get("to_agent") == agent_name
            or agent_name in r.get("participants", [])
        )
        if not is_participant:
            continue
        thread = r.get("thread", [])
        if not thread:
            continue
        if unresolved_only and r.get("thread_resolved"):
            continue
        result.append(r)
    return result


# =============================================================================
# SECTION 8 — HR / BOARD PERFORMANCE REVIEWS + PIP SYSTEM
# =============================================================================
# HR (or any agent with grounds) can flag an agent for a board performance
# review.  The board then meets WITH the agent (blunt, direct) to identify
# a path forward — usually a PIP.  For serious issues, the board also
# convenes privately to assess structural remedies (demotion, role change,
# increased supervision).

_REVIEW_PATH = os.path.join(_BASE, "data", "agent_performance_reviews.json")
_PIP_PATH    = os.path.join(_BASE, "data", "agent_pips.json")

# Agents that are directly supervised by the Board (no intermediate supervisor)
_BOARD_SUPERVISED = {
    "CITDirector", "Chief of Staff", "IndependentRiskManager",
    "HRPerformanceOfficer", "HRDevelopmentOfficer", "CROAgent",
    "OperationsDirector", "HeadAgent", "Manager Agent", "ManagerAgent",
}

_BOARD_MEMBERS = ["Board", "Chief of Staff", "IndependentRiskManager"]


def flag_for_board_review(
    reporter:       str,
    flagged_agent:  str,
    issues:         list[str],
    severity:       str = "MODERATE",   # MILD | MODERATE | SERIOUS | CRITICAL
    evidence:       str = "",
    recommended_action: str = "",
) -> str:
    """
    Flag an agent for a board performance review.

    Severity guide:
        MILD      — coaching conversation, no PIP required
        MODERATE  — board + agent meeting, PIP likely
        SERIOUS   — board + agent meeting AND private board session; PIP required
        CRITICAL  — immediate private board session; potential demotion/removal

    Returns review_id.

    Example:
        flag_for_board_review(
            "HRPerformanceOfficer", "CITDirector",
            issues=["Stale reports 3 sessions running",
                    "Proposals lack evidence quality (avg score 1.8/5)"],
            severity="MODERATE",
            evidence="data/hr_report.json sections 3.1–3.4",
            recommended_action="PIP: improve evidence quality to avg 3.5+ within 14 days",
        )
    """
    records    = _load(_REVIEW_PATH) or []
    review_id  = _uid()
    record     = {
        "review_id":           review_id,
        "reporter":            reporter,
        "flagged_agent":       flagged_agent,
        "issues":              issues,
        "severity":            severity,
        "evidence":            evidence,
        "recommended_action":  recommended_action,
        "filed_at":            _now(),
        "status":              "pending",     # pending | meeting_scheduled | pip_issued | resolved | escalated
        "board_meeting_id":    None,
        "private_meeting_id":  None,
        "pip_id":              None,
    }
    records.append(record)
    _save(_REVIEW_PATH, records)

    # ── Post to firm channel so the record is visible ──────────────────────
    _firm_post(
        reporter, "PERFORMANCE_REVIEW",
        f"[{severity}] {reporter} has flagged {flagged_agent} for board review. "
        f"Issues: {'; '.join(issues[:2])}{'...' if len(issues)>2 else ''}. "
        f"[review_id: {review_id}]",
        priority="HIGH" if severity in ("SERIOUS","CRITICAL") else "NORMAL",
    )

    # ── MILD / MODERATE / SERIOUS: open meeting WITH the flagged agent ─────
    if severity in ("MILD", "MODERATE", "SERIOUS"):
        all_participants = _BOARD_MEMBERS + [flagged_agent, reporter]
        # Deduplicate while preserving order
        seen = set(); participants = []
        for p in all_participants:
            if p not in seen:
                seen.add(p); participants.append(p)

        _nl = chr(10)
        _issues_str = _nl.join("  • " + i for i in issues)
        _ev_str  = ("Evidence: " + (evidence if isinstance(evidence, str) else "; ".join(str(e) for e in evidence))) if evidence else ""
        _rec_str = ("Recommendation: " + recommended_action) if recommended_action else ""
        _pip_note = "A PIP should be issued at the end of this meeting." if severity in ("MODERATE", "SERIOUS") else ""
        agenda = (
            "PERFORMANCE REVIEW — " + flagged_agent + _nl
            + "Filed by: " + reporter + " | Severity: " + severity + _nl
            + "Issues:" + _nl + _issues_str + _nl
            + (_ev_str + _nl if _ev_str else "")
            + (_rec_str + _nl if _rec_str else "")
            + _nl
            + "NOTE TO BOARD: Be direct and specific. " + flagged_agent + " is present "
            + "to participate in building a solution, not just to receive feedback. "
            + "Agree on concrete targets and a timeline. "
            + _pip_note
        )
        mtg_id = call_ad_hoc_meeting(
            initiator    = "Board",
            participants = participants,
            agenda       = agenda,
            urgency      = "HIGH" if severity in ("SERIOUS","CRITICAL") else "NORMAL",
            context      = f"Board performance review. review_id={review_id}",
        )
        record["board_meeting_id"] = mtg_id
        records[-1] = record
        _save(_REVIEW_PATH, records)

    # ── SERIOUS / CRITICAL: also call a PRIVATE board meeting ─────────────
    if severity in ("SERIOUS", "CRITICAL"):
        private_agenda = (
            "PRIVATE BOARD SESSION — " + flagged_agent + " (NOT PRESENT)" + _nl
            + "Filed by: " + reporter + " | Severity: " + severity + _nl
            + "Issues:" + _nl + _issues_str + _nl + _nl
            + "The board should independently assess:" + _nl
            + "  1. Root cause — systemic or individual failure?" + _nl
            + "  2. Structural remedies: increased supervision, role reduction, "
            + "demotion, or removal" + _nl
            + "  3. Whether the agent's current mandate is appropriately scoped" + _nl
            + "  4. Interim measures pending PIP outcome" + _nl + _nl
            + "Decision must be logged as a governance proposal via "
            + "submit_supervisor_proposal() for 3-way veto sign-off."
        )
        private_mtg_id = call_ad_hoc_meeting(
            initiator    = "Board",
            participants = _BOARD_MEMBERS,
            agenda       = private_agenda,
            urgency      = "HIGH",
            context      = f"PRIVATE. review_id={review_id}. {flagged_agent} excluded.",
        )
        record["private_meeting_id"] = private_mtg_id
        records[-1] = record
        _save(_REVIEW_PATH, records)

    return review_id


def create_pip(
    issuing_agent:  str,
    flagged_agent:  str,
    issues:         list[str],
    targets:        list[str],           # concrete, measurable improvement goals
    timeline_days:  int  = 14,
    review_id:      str  = "",
    context:        str  = "",
) -> str:
    """
    Issue a Performance Improvement Plan (PIP) to an agent.

    Example:
        create_pip(
            "Board", "CITDirector",
            issues=["Evidence quality avg 1.8/5 (threshold 3.5)",
                    "Stale reports: 3 consecutive sessions"],
            targets=["Evidence quality avg >= 3.5/5 within 14 days",
                     "Zero stale reports for 7 consecutive sessions",
                     "File at least 1 self-improvement proposal per session with 3+ data points"],
            timeline_days=14,
        )
    """
    pips        = _load(_PIP_PATH) or []
    pip_id      = _uid()
    review_date = _now()  # will be set to now + timeline_days conceptually
    pip = {
        "pip_id":          pip_id,
        "flagged_agent":   flagged_agent,
        "issued_by":       issuing_agent,
        "review_id":       review_id,
        "issues":          issues,
        "targets":         targets,
        "timeline_days":   timeline_days,
        "created_at":      _now(),
        "status":          "active",     # active | met | failed | extended
        "progress_notes":  [],
        "outcome":         None,
        "outcome_notes":   "",
        "context":         context,
    }
    pips.append(pip)
    _save(_PIP_PATH, pips)

    # Notify the flagged agent via firm channel + task
    _nl = chr(10)
    pip_text = (
        "PERFORMANCE IMPROVEMENT PLAN issued to " + flagged_agent + " by " + issuing_agent + "." + _nl
        + "Timeline: " + str(timeline_days) + " days." + _nl
        + "Issues:" + _nl + _nl.join("  • " + i for i in issues) + _nl
        + "Targets:" + _nl + _nl.join("  • " + t for t in targets) + _nl
        + "[pip_id: " + pip_id + "]"
    )
    _firm_task(issuing_agent, flagged_agent, pip_text, priority="HIGH")
    _firm_post(
        issuing_agent, "PIP_ISSUED",
        f"PIP issued to {flagged_agent}: {timeline_days}-day plan. "
        f"{len(targets)} target(s). [pip_id: {pip_id}]",
        priority="HIGH",
    )

    # Update the linked review record
    if review_id:
        reviews = _load(_REVIEW_PATH) or []
        for rv in reviews:
            if rv.get("review_id") == review_id:
                rv["pip_id"] = pip_id
                rv["status"] = "pip_issued"
        _save(_REVIEW_PATH, reviews)

    return pip_id


def get_my_pip(agent_name: str) -> dict | None:
    """Return the active PIP for this agent, if any."""
    pips = _load(_PIP_PATH)
    if not pips:
        return None
    active = [p for p in pips
              if p.get("flagged_agent") == agent_name
              and p.get("status") == "active"]
    return active[-1] if active else None


def add_pip_progress(
    pip_id:    str,
    from_agent: str,
    note:       str,
    metric:     str = "",   # e.g. "evidence_quality_avg: 2.4/5"
) -> None:
    """
    Log a progress update on a PIP.  Both the flagged agent and the
    issuing board/supervisor can add notes.

    Example (flagged agent self-reporting):
        add_pip_progress(pip_id, "CITDirector",
                         note="Improved evidence sourcing — 3 proposals filed with 3+ data points",
                         metric="evidence_quality_avg: 3.2/5 (up from 1.8)")
    """
    pips = _load(_PIP_PATH) or []
    for p in pips:
        if p.get("pip_id") == pip_id:
            p.setdefault("progress_notes", []).append({
                "from":    from_agent,
                "note":    note,
                "metric":  metric,
                "at":      _now(),
            })
    _save(_PIP_PATH, pips)


def close_pip(
    pip_id:    str,
    closed_by: str,
    outcome:   str,         # "met" | "failed" | "extended"
    outcome_notes: str = "",
    extend_days: int = 0,
) -> None:
    """
    Close a PIP after the review period.

    outcome="met"      — agent met all targets; PIP closed successfully
    outcome="failed"   — targets not met; typically triggers escalation /
                         private board session / demotion discussion
    outcome="extended" — partial progress; extend by extend_days
    """
    pips = _load(_PIP_PATH) or []
    for p in pips:
        if p.get("pip_id") == pip_id:
            if outcome == "extended" and extend_days > 0:
                p["status"]        = "active"
                p["timeline_days"] += extend_days
                p.setdefault("progress_notes", []).append({
                    "from": closed_by,
                    "note": f"PIP extended by {extend_days} days. {outcome_notes}",
                    "at":   _now(),
                })
            else:
                p["status"]        = outcome   # "met" or "failed"
                p["outcome"]       = outcome
                p["outcome_notes"] = outcome_notes
                p["closed_at"]     = _now()
                p["closed_by"]     = closed_by

            _firm_post(
                closed_by, f"PIP_{outcome.upper()}",
                f"PIP for {p['flagged_agent']} closed: {outcome.upper()}. "
                + (outcome_notes[:100] if outcome_notes else "")
                + f" [pip_id: {pip_id}]",
                priority="HIGH" if outcome == "failed" else "NORMAL",
            )
            # Escalate automatically on failure
            if outcome == "failed":
                flag_for_board_review(
                    reporter       = closed_by,
                    flagged_agent  = p["flagged_agent"],
                    issues         = [f"PIP FAILED: targets not met within {p['timeline_days']} days"] + p.get("issues", []),
                    severity       = "SERIOUS",
                    evidence       = f"pip_id={pip_id}. " + outcome_notes[:200],
                    recommended_action="Convene private board session to assess structural remedies.",
                )
    _save(_PIP_PATH, pips)


def get_open_reviews(max_age_hours: float = 168.0) -> list:
    """Return open performance reviews (pending or pip_issued)."""
    records = _load(_REVIEW_PATH) or []
    return [
        r for r in records
        if r.get("status") in ("pending", "meeting_scheduled", "pip_issued")
        and _age_hours(r.get("filed_at", "")) <= max_age_hours
    ]


# =============================================================================
# SECTION 9 — BULLETIN BOARD  (lightweight "FYI" posts, no response needed)
# =============================================================================
# Lower overhead than a broadcast, lower urgency than a request.
# Any agent can post an observation; any agent can read the board.
# Think of it as a team notice board: "Hey, I noticed X" without demanding action.

_BULLETIN_PATH = os.path.join(_BASE, "data", "agent_bulletin.json")

def post_bulletin(
    agent:    str,
    message:  str,
    topic:    str  = "general",  # market | risk | ml | ops | research | general
    ttl_hours: float = 24.0,
) -> str:
    """
    Post a lightweight observation to the shared bulletin board.
    No response expected; agents read it in their inbox pass.

    Example:
        post_bulletin("Canary", "VIX crossed 25 — elevated vol regime beginning",
                      topic="risk", ttl_hours=8.0)
    """
    records  = _load(_BULLETIN_PATH) or []
    bull_id  = _uid()
    records.append({
        "bulletin_id": bull_id,
        "agent":       agent,
        "message":     message,
        "topic":       topic,
        "posted_at":   _now(),
        "ttl_hours":   ttl_hours,
        "read_by":     [],
    })
    # Prune expired entries at write time
    records = [r for r in records if _age_hours(r.get("posted_at","")) <= r.get("ttl_hours", 24)]
    _save(_BULLETIN_PATH, records)
    return bull_id


def get_bulletin(
    topics:       list[str] | None = None,
    max_age_hours: float = 24.0,
    agent_name:   str | None = None,   # if set, exclude posts by this agent
) -> list:
    """Return active bulletin posts, optionally filtered by topic."""
    records = _load(_BULLETIN_PATH) or []
    result  = []
    for r in records:
        age = _age_hours(r.get("posted_at",""))
        if age > min(max_age_hours, r.get("ttl_hours", 24)):
            continue
        if topics and r.get("topic") not in topics:
            continue
        if agent_name and r.get("agent") == agent_name:
            continue
        result.append(r)
    return result


def mark_bulletin_read(bulletin_id: str, agent_name: str) -> None:
    records = _load(_BULLETIN_PATH) or []
    for r in records:
        if r.get("bulletin_id") == bulletin_id:
            r.setdefault("read_by", [])
            if agent_name not in r["read_by"]:
                r["read_by"].append(agent_name)
    _save(_BULLETIN_PATH, records)


# =============================================================================
# SECTION 10 — CONSENSUS VOTING  (structured multi-agent polls)
# =============================================================================
# When a decision needs input from multiple agents — trade sizing, policy
# changes, model promotion — create a poll.  Agents vote; result is computed
# when quorum is reached or deadline passes.

_VOTE_PATH = os.path.join(_BASE, "data", "agent_votes.json")

def create_poll(
    initiator:    str,
    question:     str,
    options:      list[str],            # e.g. ["BUY", "HOLD", "SELL"]
    voters:       list[str] | None = None,  # None = all registered agents
    quorum:       float = 0.5,          # fraction of voters needed
    deadline_hrs: float = 4.0,
    context:      str  = "",
) -> str:
    """
    Create a consensus poll.  Voters cast ballots; result tallied on quorum
    or deadline.

    Example:
        poll_id = create_poll(
            "Board", "Should we increase NVDA allocation above 10%?",
            options=["YES — increase to 12%", "HOLD at 10%", "REDUCE below 8%"],
            voters=["CROAgent", "FIBoard", "FEBoard", "MLShortTermBoard", "MLLongTermBoard"],
            quorum=0.6, deadline_hrs=6.0,
        )
    """
    from wake_agent import AGENT_REGISTRY
    records   = _load(_VOTE_PATH) or []
    poll_id   = _uid()
    all_voters = voters or list(AGENT_REGISTRY.keys())
    records.append({
        "poll_id":      poll_id,
        "initiator":    initiator,
        "question":     question,
        "options":      options,
        "voters":       all_voters,
        "quorum":       quorum,
        "deadline_hrs": deadline_hrs,
        "created_at":   _now(),
        "ballots":      {},          # {agent_name: {"option": ..., "rationale": ...}}
        "result":       None,
        "closed":       False,
    })
    _save(_VOTE_PATH, records)

    # Notify all voters via firm_channel
    _firm_post(
        initiator, "POLL_OPEN",
        f"POLL by {initiator}: {question[:100]} | Options: {', '.join(options)} "
        f"| Quorum: {int(quorum*100)}% | Deadline: {deadline_hrs}h [poll_id: {poll_id}]",
        priority="HIGH",
    )
    # Only wake voters immediately for urgent polls (deadline < 1 hour)
    # For normal polls, voters will see it in their next scheduled inbox check
    if deadline_hrs < 1.0:
        for voter in all_voters:
            try:
                from wake_agent import wake_nonblocking as _wnb
                _wnb(voter)
            except Exception:
                pass
    return poll_id


def cast_vote(
    poll_id:   str,
    agent:     str,
    option:    str,
    rationale: str = "",
) -> dict:
    """
    Cast a ballot on an open poll.  Returns {result, closed, tallies}.

    Example:
        cast_vote(poll_id, "CROAgent", "HOLD at 10%",
                  rationale="Risk-adjusted return doesn't justify higher concentration")
    """
    records = _load(_VOTE_PATH) or []
    for poll in records:
        if poll.get("poll_id") != poll_id:
            continue
        if poll.get("closed"):
            return {"error": "poll already closed", "result": poll.get("result")}
        if option not in poll.get("options", []):
            return {"error": f"invalid option {option!r}"}

        poll["ballots"][agent] = {"option": option, "rationale": rationale, "at": _now()}

        # Tally
        votes_cast = len(poll["ballots"])
        total      = len(poll["voters"])
        quorum_n   = max(1, int(poll["quorum"] * total))
        tallies    = {}
        for b in poll["ballots"].values():
            tallies[b["option"]] = tallies.get(b["option"], 0) + 1

        result = None
        if votes_cast >= quorum_n:
            winner = max(tallies, key=tallies.get)
            result = {"winner": winner, "tallies": tallies, "votes_cast": votes_cast,
                      "total_voters": total}
            poll["result"] = result
            poll["closed"] = True
            poll["closed_at"] = _now()
            _firm_post(
                "PollSystem", "POLL_RESULT",
                f"POLL CLOSED: {poll['question'][:80]} | Winner: {winner} "
                f"({tallies[winner]}/{votes_cast} votes) [poll_id: {poll_id}]",
                priority="HIGH",
            )

        _save(_VOTE_PATH, records)
        return {"result": result, "closed": poll["closed"],
                "tallies": tallies, "votes_cast": votes_cast}
    # Poll not found
    return {"status": "not_found", "error": f"poll {poll_id!r} not found"}

    return {"error": "poll not found"}


def get_open_polls(agent_name: str | None = None) -> list:
    """Return polls that are open and (optionally) include this agent as a voter."""
    records = _load(_VOTE_PATH) or []
    result  = []
    for poll in records:
        if poll.get("closed"):
            continue
        if _age_hours(poll.get("created_at","")) > poll.get("deadline_hrs", 4) + 1:
            continue
        if agent_name and agent_name not in poll.get("voters", []) and poll.get("voters"):
            continue
        if agent_name and agent_name in poll.get("ballots", {}):
            continue  # already voted
        result.append(poll)
    return result


# =============================================================================
# SECTION 11 — AGENT STATUS REGISTRY  (health + availability)
# =============================================================================
# Each agent updates its status at the start of every run.  Others can
# check before routing a message — no point waking a degraded agent.

_STATUS_PATH = os.path.join(_BASE, "data", "agent_status.json")


def update_agent_status(
    agent_name:  str,
    status:      str  = "OK",       # OK | DEGRADED | ERROR | OFFLINE
    last_output: str  = "",         # brief summary of last run result
    metrics:     dict | None = None,
) -> None:
    """
    Call at the start/end of each agent's run to register availability.
    agent_inbox.run_inbox() calls this automatically.

    Example:
        update_agent_status("CITDirector", status="OK",
                            last_output="Reviewed 12 proposals; 2 approved",
                            metrics={"proposals_reviewed": 12, "approved": 2})
    """
    registry = _load_dict(_STATUS_PATH)
    if isinstance(registry, list):
        registry = {}   # migrate old format
    registry[agent_name] = {
        "status":      status,
        "last_run":    _now(),
        "last_output": last_output[:200],
        "metrics":     metrics or {},
    }
    _save(_STATUS_PATH, registry)


def get_agent_status(agent_name: str | None = None) -> dict:
    """
    Return status for one agent or all agents.
    Returns {"status": ..., "last_run": ..., "last_output": ...}
    """
    registry = _load_dict(_STATUS_PATH)
    if isinstance(registry, list):
        registry = {}
    if agent_name:
        return registry.get(agent_name, {"status": "UNKNOWN", "last_run": None})
    return registry


def get_stale_agents(threshold_hours: float = 26.0) -> list[str]:
    """Return agent names that haven't updated their status in threshold_hours."""
    registry = _load_dict(_STATUS_PATH)
    if isinstance(registry, list):
        return []
    stale = []
    for name, info in registry.items():
        lr = info.get("last_run","")
        if not lr or _age_hours(lr) > threshold_hours:
            stale.append(name)
    return stale


# --- LIVENESS CORROBORATION (registry-staleness is NOT proof of death) -------
# The status registry (data/agent_status.json) is only stamped by
# update_agent_status(), which fires on inbox-driven runs. But almost every
# agent runs via cron (main.py 6×/day, canary, the boards, the pods), and that
# path never calls it — so the registry goes stale for exactly the busiest
# agents while they run fine. That produced a chronic false "31 agents OFFLINE"
# in the weekly health report (Manager Agent looked 63 days dead while trading
# every cycle). Before branding an agent OFFLINE we now corroborate against the
# ONE thing cron runs DO leave behind: dated files in logs/. If an agent has a
# fresh log, it's alive regardless of what the registry says.
#
# Maps registry agent name → underscore-delimited log-prefix tokens that
# evidence a run. Matching is contiguous-token (see _prefix_matches), so "rd"
# corroborates "catchup_rd_team_leader" but not "pipeline_integrity". Agents
# that only run inside the main pipeline map to the pipeline's own logs
# (main/run/cron/canary) — those firing IS that agent running.
_AGENT_LOG_EVIDENCE = {
    "Canary":                 ["canary"],
    "CITDirector":            ["cit"],
    "CITPipelineIntegrity":   ["pipeline_integrity", "cit"],
    "ApexSystemCIT":          ["sentinel", "cit"],
    "Manager Agent":          ["main", "run", "cron"],
    "AlpacaTrader":           ["main", "run", "cron"],
    "CROAgent":               ["risk_manager", "main", "cron"],
    "CapitalEfficiencyAgent": ["main", "run", "cron"],
    "SignalEngine":           ["main", "rd", "cron"],
    "ReviewAgent":            ["main", "run", "cron"],
    "TradeReviewTeam":        ["main", "run", "cron"],
    "LogReviewAgent":         ["main", "run", "cron"],
    "EarningsPredictor":      ["main", "run", "cron"],
    "Chief of Staff":         ["briefing", "main", "cron"],
    "MorningBriefing":        ["briefing"],
    "PremarketCheck":         ["premarket"],
    "ResearchTeam":           ["rd", "rd_triggered"],
    "EnergyResearchTeam":     ["etf_research", "etf_board"],
    "InternationalAnalyst":   ["rd", "main"],
    "HRPerformanceOfficer":   ["hr"],
    "IndependentRiskManager": ["irm_daily", "risk_manager"],
    "Board":                  ["board", "rd_board"],
    "DeputyBoard":            ["board", "rd_board"],
    "FEBoard":                ["fe", "fe_pipeline", "etf_board"],
    "FECIOAgent":             ["fe", "fe_pipeline"],
    "FIBoard":                ["fi", "fi_pipeline"],
    "MLShortTermBoard":       ["ml_board"],
    "MLLongTermBoard":        ["ml_board"],
    "MLResearchTeamLeader":   ["ml_runner_daily", "ml_live", "ml_overnight"],
    "MLSTExpRunner":          ["ml_overnight", "ml_runner_daily"],
    "MLLTExpRunner":          ["ml_overnight", "lt_impl", "ml_runner_daily"],
    # AuditTestAgent_V2: intentionally UNMAPPED — a test scaffold with no live
    # log. If it's stale it should surface; that's a true signal, not noise.
}


def _prefix_matches(candidate: str, fresh_prefix: str) -> bool:
    """True if `candidate` is a contiguous underscore-token run inside a log
    prefix. Wrapping both in underscores makes 'rd' match 'catchup_rd_team'
    but not 'board_rd'-only-as-substring accidents like 'fi' in 'notify'."""
    return f"_{candidate}_" in f"_{fresh_prefix}_"


def fresh_log_prefixes(threshold_hours: float = 26.0) -> set:
    """Underscore-delimited prefixes of logs/ files modified within the window.
    A log file `canary_20260729.log` → prefix `canary`; runs on whichever host
    calls this (Mac or VM) so it reflects THAT host's activity."""
    import re as _re
    cutoff = time.time() - threshold_hours * 3600
    prefixes = set()
    logdir = os.path.join(_BASE, "logs")
    try:
        for fn in os.listdir(logdir):
            if not fn.endswith(".log"):
                continue
            try:
                if os.path.getmtime(os.path.join(logdir, fn)) < cutoff:
                    continue
            except OSError:
                continue
            # strip trailing timestamp chunks (_20260729, _0945, .log)
            stem = _re.sub(r"(_\d{4,})+$", "", fn[:-4])
            if stem:
                prefixes.add(stem)
    except OSError:
        pass
    return prefixes


def agents_with_fresh_logs(threshold_hours: float = 26.0) -> set:
    """Registry agent names whose log-evidence prefixes are fresh — i.e.
    demonstrably alive regardless of a stale status-registry entry."""
    fresh = fresh_log_prefixes(threshold_hours)
    alive = set()
    for agent, cands in _AGENT_LOG_EVIDENCE.items():
        if any(_prefix_matches(c, fp) for c in cands for fp in fresh):
            alive.add(agent)
    return alive


# =============================================================================
# SECTION 12 — ACTION ITEMS  (tracked deliverables from meetings / decisions)
# =============================================================================
# When a meeting or review produces commitments, they get logged as action
# items with owner, deadline, and completion tracking.

_ACTIONS_PATH = os.path.join(_BASE, "data", "agent_action_items.json")


def create_action_item(
    owner:        str,
    description:  str,
    due_by:       str  = "",
    created_by:   str  = "system",
    source_id:    str  = "",   # meeting_id / proposal_id / review_id
    priority:     str  = "NORMAL",
) -> str:
    """
    Create a tracked action item owned by a specific agent.

    Example:
        create_action_item(
            "CITDirector",
            "Implement fcntl try/finally fix in cit_director.py:2170",
            due_by="2026-05-28", created_by="Board",
            source_id=meeting_id, priority="HIGH",
        )
    """
    items  = _load(_ACTIONS_PATH) or []
    act_id = _uid()
    items.append({
        "action_id":   act_id,
        "owner":       owner,
        "description": description,
        "due_by":      due_by,
        "created_by":  created_by,
        "source_id":   source_id,
        "priority":    priority,
        "created_at":  _now(),
        "status":      "open",       # open | in_progress | done | overdue
        "completed_at": None,
        "completion_notes": "",
    })
    _save(_ACTIONS_PATH, items)
    _firm_task(created_by, owner,
               f"ACTION ITEM: {description[:120]} "
               + (f"Due: {due_by}. " if due_by else "")
               + f"[action_id: {act_id}]",
               priority=priority)
    return act_id


def complete_action_item(
    action_id: str,
    agent:     str,
    notes:     str = "",
) -> None:
    """Mark an action item as completed."""
    items = _load(_ACTIONS_PATH) or []
    for item in items:
        if item.get("action_id") == action_id and item.get("owner") == agent:
            item["status"]          = "done"
            item["completed_at"]    = _now()
            item["completion_notes"] = notes
            _firm_post(agent, "ACTION_DONE",
                       f"Action item completed by {agent}: {item['description'][:80]} "
                       + (f"Notes: {notes[:100]}" if notes else "")
                       + f" [action_id: {action_id}]",
                       priority="NORMAL")
    _save(_ACTIONS_PATH, items)


def get_my_action_items(
    agent_name:    str,
    max_age_hours: float = 168.0,
    open_only:     bool  = True,
) -> list:
    """Return action items owned by this agent."""
    items = _load(_ACTIONS_PATH) or []
    return [
        i for i in items
        if i.get("owner") == agent_name
        and (not open_only or i.get("status") in ("open", "in_progress", "overdue"))
        and _age_hours(i.get("created_at","")) <= max_age_hours
    ]


# =============================================================================
# SECTION 13 — ESCALATION CHAINS  (unanswered requests auto-escalate)
# =============================================================================
# HIGH/CRITICAL requests that go unanswered for escalation_hrs trigger an
# automatic escalation to the target's supervisor or CoS.
# run_escalation_check() should be called periodically (e.g. by CoS or Canary).

# Maps agent → their supervisor for escalation routing
_ESCALATION_CHAIN: dict[str, str] = {
    "MLShortTermBoard":     "Chief of Staff",
    "MLLongTermBoard":      "Chief of Staff",
    "MLResearchTeamLeader": "Chief of Staff",
    "MLLongTermTeamLeader": "Chief of Staff",
    "ResearchTeam":         "RDTeamLeader",
    "EnergyResearchTeam":   "RDTeamLeader",
    "RDTeamLeader":         "Chief of Staff",
    "FIBoard":              "Chief of Staff",
    "FEBoard":              "Chief of Staff",
    "ETFPodBoard":          "Chief of Staff",
    "CITDirector":          "Chief of Staff",
    "CITWatchdog":          "CITDirector",
    "CITPipelineMonitor":   "CITDirector",
    "CITAutoImplementer":   "CITDirector",
    "DeputyMonitor":        "Chief of Staff",
    "DeputyBoard":          "Chief of Staff",
    "Canary":               "IndependentRiskManager",
    "CROAgent":             "IndependentRiskManager",
    "CapitalEfficiencyAgent": "IndependentRiskManager",
    "Manager Agent":        "Chief of Staff",
    "ManagerAgent":         "Chief of Staff",
    "ReviewAgent":          "Chief of Staff",
    "TradeReviewTeam":      "Chief of Staff",
    "EarningsPredictor":    "RDTeamLeader",
    "InternationalAnalyst": "RDTeamLeader",
    "SignalEngine":         "Manager Agent",
    "HeadAgent":            "Chief of Staff",
    "OperationsDirector":   "Chief of Staff",
    "MorningBriefing":      "Chief of Staff",
    "ApexImprovementBoard": "Chief of Staff",
    "HRPerformanceOfficer": "Board",
    "HRDevelopmentOfficer": "Board",
    "IndependentRiskManager": "Board",
    "Board":                "Chief of Staff",
}

def run_escalation_check(escalation_hrs: float = 2.0) -> list[str]:
    """
    Scan open HIGH/CRITICAL requests older than escalation_hrs.
    For each unanswered one, file an info request to the target's supervisor
    and wake them.  Returns list of escalated request_ids.

    Call this from CoS or Canary's run function periodically.

    Example:
        escalated = run_escalation_check(escalation_hrs=2.0)
        if escalated:
            logger.warning("Escalated %d unanswered requests", len(escalated))
    """
    records    = _load(_MEET) or []
    escalated  = []

    for r in records:
        if r.get("type") != "INFO_REQUEST":
            continue
        if r.get("status") != "open":
            continue
        if r.get("escalated"):
            continue
        urgency = r.get("urgency","NORMAL")
        if urgency not in ("HIGH","CRITICAL"):
            continue
        age = _age_hours(r.get("filed_at",""))
        if age < escalation_hrs:
            continue

        target     = r.get("to_agent","")
        supervisor = _ESCALATION_CHAIN.get(target, "Chief of Staff")
        req_id     = r.get("request_id","?")

        # File escalation notice to supervisor
        escalation_req = request_info(
            "EscalationSystem", supervisor,
            topic=(f"ESCALATION: {target} has not answered a {urgency} request "
                   f"for {age:.1f}h. Request: '{r.get('topic','')[:80]}' "
                   f"[original_req: {req_id}]"),
            urgency="HIGH",
            context=(f"Original requester: {r.get('from_agent')}. "
                     f"Filed at: {r.get('filed_at','')}"),
            callback_hint=f"Please ensure {target} responds or handle it yourself",
        )

        r["escalated"]    = True
        r["escalated_at"] = _now()
        r["escalated_to"] = supervisor
        escalated.append(req_id)

    if escalated:
        _save(_MEET, records)

    return escalated


# =============================================================================
# SECTION 14 — CC / SUBSCRIPTIONS  (standing interest in message topics)
# =============================================================================
# Agents can register standing subscriptions to be CC'd on messages
# matching a topic, urgency threshold, or sender pattern.

_SUBS_PATH = os.path.join(_BASE, "data", "agent_subscriptions.json")

def subscribe(
    agent:         str,
    topic_pattern: str,          # substring match against topic/category fields
    min_urgency:   str = "HIGH", # only CC on messages at or above this urgency
    msg_types:     list[str] | None = None,  # None = all types
) -> str:
    """
    Register a standing subscription.  When a message matching topic_pattern
    and min_urgency is posted, agent gets CC'd into their inbox.

    Example:
        subscribe("IndependentRiskManager", topic_pattern="risk",
                  min_urgency="NORMAL", msg_types=["INFO_REQUEST","BROADCAST"])
        subscribe("Chief of Staff", topic_pattern="",   # matches everything
                  min_urgency="CRITICAL")
    """
    subs   = _load(_SUBS_PATH) or []
    sub_id = _uid()
    subs.append({
        "sub_id":        sub_id,
        "agent":         agent,
        "topic_pattern": topic_pattern,
        "min_urgency":   min_urgency,
        "msg_types":     msg_types or ["INFO_REQUEST","BROADCAST","DELEGATION","POLL_OPEN"],
        "created_at":    _now(),
        "active":        True,
    })
    _save(_SUBS_PATH, subs)
    return sub_id


def unsubscribe(sub_id: str) -> None:
    subs = _load(_SUBS_PATH) or []
    for s in subs:
        if s.get("sub_id") == sub_id:
            s["active"] = False
    _save(_SUBS_PATH, subs)


def get_cc_recipients(
    topic:     str,
    urgency:   str,
    msg_type:  str,
    exclude:   list[str] | None = None,
) -> list[str]:
    """
    Return agents subscribed to receive a CC on a message with these attributes.
    Called internally by request_info/broadcast/delegate_task.
    """
    _URGENCY_RANK = {"NORMAL": 0, "HIGH": 1, "CRITICAL": 2}
    subs   = _load(_SUBS_PATH) or []
    result = []
    excl   = set(exclude or [])
    for s in subs:
        if not s.get("active"):
            continue
        if s["agent"] in excl:
            continue
        pattern = s.get("topic_pattern","")
        if pattern and pattern.lower() not in topic.lower():
            continue
        if msg_type not in s.get("msg_types", [msg_type]):
            continue
        req_rank = _URGENCY_RANK.get(s.get("min_urgency","HIGH"), 1)
        msg_rank = _URGENCY_RANK.get(urgency, 0)
        if msg_rank < req_rank:
            continue
        result.append(s["agent"])
    return list(set(result))


# =============================================================================
# SECTION 15 — CLEANUP  (TTL-based garbage collection for all stores)
# =============================================================================

def run_cleanup(
    max_age_requests:  float = 72.0,
    max_age_meetings:  float = 168.0,
    max_age_proposals: float = 336.0,
    max_age_broadcasts:float = 12.0,
    max_age_delegations:float= 168.0,
    max_age_bulletins: float = 24.0,
    max_age_votes:     float = 48.0,
) -> dict:
    """
    Garbage-collect all stale records from all communication stores.
    Call periodically (e.g. from CoS or a nightly cron).

    Returns counts of records removed from each store.
    """
    removed = {}

    stores = [
        (_MEET,        max_age_requests,   "meetings"),
        (_PROP,        max_age_proposals,  "proposals"),
        (_BC_PATH,     max_age_broadcasts, "broadcasts"),
        (_DELG_PATH,   max_age_delegations,"delegations"),
        (_BULLETIN_PATH, max_age_bulletins,"bulletins"),
        (_VOTE_PATH,   max_age_votes,      "votes"),
    ]

    for path, max_age, label in stores:
        records = _load(path) or []
        if isinstance(records, dict):
            continue   # status registry uses dict format
        ts_keys = ("filed_at","sent_at","created_at","posted_at","scheduled_at")
        before  = len(records)
        keep    = []
        for r in records:
            ts = next((r[k] for k in ts_keys if k in r), "")
            if ts and _age_hours(ts) > max_age:
                continue
            keep.append(r)
        if len(keep) < before:
            _save(path, keep)
            removed[label] = before - len(keep)

    return removed



# =============================================================================
# ── SECTION 16: Inbox Health Digest  (#11) ──────────────────────────────────
# =============================================================================

def get_inbox_digest(agent_name: str | None = None) -> dict:
    """
    Return a summary of the current inbox health across the system.
    If agent_name is provided, filter to messages involving that agent.
    Used by CoS to generate the daily 9am board digest.
    """
    meets     = _load(_MEET)   or []
    props     = _load(_PROP)   or []
    delegates = _load(_DELG_PATH) or []
    bulletins = _load(_BULLETIN_PATH) or []
    votes     = _load(_VOTE_PATH) or []

    now = datetime.now().astimezone()

    # Open info requests
    open_reqs = [r for r in meets if r.get("type") == "INFO_REQUEST"
                 and r.get("status") == "open"]
    if agent_name:
        open_reqs = [r for r in open_reqs
                     if r.get("to_agent") == agent_name or r.get("from_agent") == agent_name]

    # Escalated (age > 2h)
    escalated = [r for r in open_reqs if _age_hours(r.get("filed_at","")) > 2.0]

    # Stale threads (open > 24h)
    stale = [r for r in open_reqs if _age_hours(r.get("filed_at","")) > 24.0]

    # Open delegations
    open_delg = [d for d in delegates if d.get("status") in ("pending","acknowledged")]
    if agent_name:
        open_delg = [d for d in open_delg
                     if d.get("assigned_to") == agent_name or d.get("delegated_by") == agent_name]

    # Open polls
    open_polls = [v for v in votes if v.get("status") == "open"]

    # Active bulletins
    active_bulletins = [b for b in bulletins if b.get("status") == "active"]

    # Unimplemented proposals
    open_props = [p for p in props if p.get("status") in ("pending","approved")]

    return {
        "generated_at":        datetime.now().isoformat(),
        "open_requests":       len(open_reqs),
        "escalated_requests":  len(escalated),
        "stale_threads_24h":   len(stale),
        "open_delegations":    len(open_delg),
        "open_polls":          len(open_polls),
        "active_bulletins":    len(active_bulletins),
        "open_proposals":      len(open_props),
        "top_escalated": [
            {"id": r.get("request_id",""), "topic": r.get("topic",""), "to": r.get("to_agent",""),
             "age_hours": round(_age_hours(r.get("filed_at","")), 1)}
            for r in sorted(escalated, key=lambda x: _age_hours(x.get("filed_at","")), reverse=True)[:5]
        ],
    }


def post_inbox_digest_to_board(posted_by: str = "Chief of Staff") -> str:
    """
    Generate and post a daily inbox health digest to firm_channel.
    Returns the digest summary string.
    Call from CoS morning run.
    """
    digest = get_inbox_digest()
    lines = [
        "📬 DAILY INBOX HEALTH DIGEST",
        f"  Open requests:    {digest['open_requests']}",
        f"  Escalated (>2h):  {digest['escalated_requests']}",
        f"  Stale (>24h):     {digest['stale_threads_24h']}",
        f"  Open delegations: {digest['open_delegations']}",
        f"  Open polls:       {digest['open_polls']}",
        f"  Active bulletins: {digest['active_bulletins']}",
        f"  Open proposals:   {digest['open_proposals']}",
    ]
    if digest["top_escalated"]:
        lines.append("  Top escalated:")
        for item in digest["top_escalated"]:
            lines.append(f"    • [{item['id'][:8]}] {item['topic'][:50]} → {item['to']} ({item['age_hours']}h)")
    summary = "\n".join(lines)
    _firm_post(posted_by, "INBOX_DIGEST", summary, priority="NORMAL")
    return summary


# =============================================================================
# ── SECTION 17: Private Direct Messages  (#12) ──────────────────────────────
# =============================================================================

_DM_PATH = os.path.join(_BASE, "agent_dms.json")


def send_dm(from_agent: str, to_agent: str, subject: str, body: str,
            urgency: str = "NORMAL") -> str:
    """
    Send a private direct message to a single agent.
    Unlike broadcasts, DMs are only visible to the recipient (and sender).
    Returns dm_id.
    """
    dm_id = f"dm-{_uid()}"
    record = {
        "dm_id":      dm_id,
        "from_agent": from_agent,
        "to_agent":   to_agent,
        "subject":    subject,
        "body":       body,
        "urgency":    urgency.upper(),
        "sent_at":    datetime.now().isoformat(),
        "read":       False,
        "private":    True,
    }
    records = _load(_DM_PATH) or []
    records.append(record)
    _save(_DM_PATH, records)
    return dm_id


def get_my_dms(agent_name: str, include_sent: bool = False,
               unread_only: bool = False, max_age_hours: float = 168.0) -> list:
    """
    Return DMs addressed to agent_name (optionally including sent).
    Only the recipient and sender can read a DM — all others are filtered out.
    """
    records = _load(_DM_PATH) or []
    out = []
    for r in records:
        if _age_hours(r.get("sent_at", "")) > max_age_hours:
            continue
        is_recipient = r.get("to_agent") == agent_name
        is_sender    = r.get("from_agent") == agent_name and include_sent
        if not (is_recipient or is_sender):
            continue
        if unread_only and r.get("read"):
            continue
        out.append(r)
    return sorted(out, key=lambda x: x.get("sent_at", ""))


def mark_dm_read(dm_id: str, agent_name: str) -> bool:
    """Mark a DM as read. Only the recipient may mark it read. Returns True if found."""
    records = _load(_DM_PATH) or []
    for r in records:
        if r.get("dm_id") == dm_id and r.get("to_agent") == agent_name:
            r["read"] = True
            r["read_at"] = datetime.now().isoformat()
            _save(_DM_PATH, records)
            return True
    return False


# =============================================================================
# ── SECTION 18: Structured Compute Requests  (#13) ──────────────────────────
# =============================================================================

_COMPUTE_PATH = os.path.join(_BASE, "agent_compute_requests.json")


def request_compute(from_agent: str, to_agent: str, computation_spec: dict,
                    callback_meeting_id: str = "", context: str = "") -> str:
    """
    Ask another agent to run a specific computation and post results back
    to a designated meeting/thread.

    computation_spec: dict describing what to compute, e.g.:
        {"type": "feature_shap", "model": "shortterm", "n_top": 10}
        {"type": "backtest", "tickers": ["AAPL"], "horizon_days": 20}
        {"type": "signal_refresh", "universe": "top50", "as_of": "today"}

    Returns compute_request_id.
    """
    req_id = f"cr-{_uid()}"
    record = {
        "compute_request_id": req_id,
        "from_agent":         from_agent,
        "to_agent":           to_agent,
        "computation_spec":   computation_spec,
        "callback_meeting_id":callback_meeting_id,
        "context":            context,
        "status":             "pending",
        "filed_at":           datetime.now().isoformat(),
        "fulfilled_at":       None,
        "result_summary":     None,
        "result_artifacts":   [],
    }
    records = _load(_COMPUTE_PATH) or []
    records.append(record)
    _save(_COMPUTE_PATH, records)
    return req_id


def fulfill_compute_request(compute_request_id: str, fulfilling_agent: str,
                             result_summary: str, artifacts: list | None = None) -> dict:
    """
    Mark a compute request as fulfilled and optionally post to callback meeting.
    """
    records = _load(_COMPUTE_PATH) or []
    for r in records:
        if r.get("compute_request_id") == compute_request_id:
            r["status"]          = "fulfilled"
            r["fulfilled_by"]    = fulfilling_agent
            r["fulfilled_at"]    = datetime.now().isoformat()
            r["result_summary"]  = result_summary
            r["result_artifacts"]= artifacts or []
            _save(_COMPUTE_PATH, records)
            # Post to callback meeting if specified
            cb = r.get("callback_meeting_id", "")
            if cb:
                try:
                    reply_to_request(
                        request_id     = cb,
                        replying_agent = fulfilling_agent,
                        content        = f"[COMPUTE RESULT] {result_summary}",
                    )
                except Exception:
                    pass
            return {"status": "fulfilled", "compute_request_id": compute_request_id}
    return {"status": "not_found", "compute_request_id": compute_request_id}


def get_pending_compute_requests(agent_name: str) -> list:
    """Return pending compute requests assigned to agent_name."""
    records = _load(_COMPUTE_PATH) or []
    return [r for r in records
            if r.get("to_agent") == agent_name and r.get("status") == "pending"]


# =============================================================================
# ── SECTION 19: Agent Health Heartbeat Report  (#14) ────────────────────────
# =============================================================================

def _flag_suspect_health_report(offline_agents: list, n_revived: int) -> None:
    """Raise a Chairman-queue item when the health report looks self-inconsistent
    (firm pipeline demonstrably alive, yet many agents still 'offline'). Deduped
    once/day. This is the guard against 'a report ran, was wrong, nobody read it'."""
    path = os.path.join(_BASE, "data", "chairman_action_required.json")
    actions = _load_dict(path)
    if not isinstance(actions, list):
        actions = _load(path) or []
    if not isinstance(actions, list):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    tag = f"[HealthReportSelfCheck] {today}"
    if any((a.get("description") or "").startswith(tag) for a in actions
           if a.get("status") == "open"):
        return
    syms = ", ".join(sorted(offline_agents)[:8])
    actions.append({
        "id": f"HRSC-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "source": "Agent Health Self-Check", "severity": "MEDIUM", "status": "open",
        "resolution_note": "", "ts": datetime.now().isoformat(),
        "description": (f"{tag} — weekly health report still lists {len(offline_agents)} "
                        f"agent(s) OFFLINE ({syms}) AFTER log-corroboration revived "
                        f"{n_revived}, yet the main pipeline logs are fresh (firm is "
                        "alive). Likely a missing entry in agent_meeting._AGENT_LOG_EVIDENCE "
                        "or a genuinely retired agent — NOT a mass outage. Reconcile the "
                        "alias map rather than trusting the offline count."),
    })
    try:
        with open(path, "w") as fh:
            json.dump(actions, fh, indent=2, default=str)
    except OSError:
        pass


def generate_agent_health_report(posted_by: str = "Chief of Staff") -> dict:
    """
    Generate a weekly agent health report showing OK/DEGRADED/OFFLINE agents,
    stale agents, open escalations, and top failure reasons.
    Posts a bulletin and broadcasts to the board.
    Returns the full report dict.
    """
    status_map  = get_agent_status()
    stale_agents= get_stale_agents(threshold_hours=26.0)
    all_meets   = _load(_MEET)   or []
    digest      = get_inbox_digest()

    # Categorize agents
    ok_agents       = []
    degraded_agents = []
    offline_agents  = []

    for agent, info in status_map.items():
        state = info.get("status", "UNKNOWN")
        if state == "OK":
            ok_agents.append(agent)
        elif state in ("DEGRADED", "ERROR", "WARNING"):
            degraded_agents.append({"agent": agent, "status": state,
                                     "last_seen": info.get("last_updated",""),
                                     "note": info.get("last_action","")})
        else:
            offline_agents.append(agent)

    for agent in stale_agents:
        if agent not in [d["agent"] for d in degraded_agents] and agent not in offline_agents:
            offline_agents.append(agent)

    # --- CORROBORATE before branding OFFLINE ---------------------------------
    # The registry only knows about inbox-driven runs; cron-driven agents look
    # dead in it while running fine. Reclassify any "offline" agent that left a
    # fresh log as OK (revived_by_logs), so the report stops crying wolf.
    alive_by_log = agents_with_fresh_logs(threshold_hours=26.0)
    revived = [a for a in offline_agents if a in alive_by_log]
    if revived:
        offline_agents = [a for a in offline_agents if a not in alive_by_log]
        for a in revived:
            if a not in ok_agents:
                ok_agents.append(a)

    # --- SELF-CHECK: the report must not be silently wrong --------------------
    # This whole bug persisted for ~2 months because a report ran, was wrong,
    # and nobody read it. So the report now audits ITSELF: if it's still about
    # to declare a pile of agents OFFLINE while the firm is demonstrably alive
    # (pipeline logs fresh), that's more likely a corroboration/mapping gap than
    # a real mass outage — flag it to the Chairman queue instead of burying it.
    firm_is_alive = bool(fresh_log_prefixes(26.0) & {
        "main", "run", "cron", "canary"})
    health_self_check = {
        "firm_pipeline_alive": firm_is_alive,
        "offline_after_corroboration": len(offline_agents),
        "revived_by_logs": sorted(revived),
        "suspect_report": firm_is_alive and len(offline_agents) >= 5,
    }
    if health_self_check["suspect_report"]:
        try:
            _flag_suspect_health_report(offline_agents, len(revived))
        except Exception:
            pass

    # Escalation patterns
    escalated = [r for r in all_meets
                 if r.get("type") == "INFO_REQUEST" and r.get("status") == "open"
                 and _age_hours(r.get("filed_at","")) > 2.0]
    escalation_targets = {}
    for r in escalated:
        t = r.get("to_agent","unknown")
        escalation_targets[t] = escalation_targets.get(t, 0) + 1
    top_escalation_targets = sorted(escalation_targets.items(), key=lambda x: x[1], reverse=True)[:5]

    report = {
        "generated_at":          datetime.now().isoformat(),
        "agents_ok":             len(ok_agents),
        "agents_degraded":       len(degraded_agents),
        "agents_offline":        len(offline_agents),
        "ok_list":               sorted(ok_agents),
        "degraded_list":         degraded_agents,
        "offline_list":          sorted(offline_agents),
        "revived_by_logs":       sorted(revived),
        "self_check":            health_self_check,
        "open_escalations":      digest["escalated_requests"],
        "stale_threads":         digest["stale_threads_24h"],
        "top_escalation_targets":top_escalation_targets,
    }

    # Post as a bulletin (7-day TTL, ops topic)
    lines = [
        "🏥 WEEKLY AGENT HEALTH REPORT",
        f"  OK: {len(ok_agents)} | DEGRADED: {len(degraded_agents)} | OFFLINE: {len(offline_agents)}",
        f"  Open escalations: {digest['escalated_requests']}  Stale threads: {digest['stale_threads_24h']}",
    ]
    if revived:
        lines.append(f"  Revived by fresh logs (registry was stale): {len(revived)} "
                     f"— {', '.join(sorted(revived)[:6])}")
    if health_self_check["suspect_report"]:
        lines.append("  ⚠️ SELF-CHECK: offline count looks inconsistent with a live "
                     "pipeline — flagged to Chairman (check alias map, not a mass outage).")
    if degraded_agents:
        lines.append("  DEGRADED agents: " + ", ".join(d["agent"] for d in degraded_agents))
    if offline_agents:
        lines.append("  OFFLINE agents: " + ", ".join(sorted(offline_agents)[:5]))
    if top_escalation_targets:
        targets_str = ", ".join(f"{a}({n})" for a,n in top_escalation_targets)
        lines.append(f"  Top escalation targets: {targets_str}")

    bulletin_text = "\n".join(lines)
    try:
        post_bulletin(
            author      = posted_by,
            topic       = "ops",
            title       = "Weekly Agent Health Report",
            body        = bulletin_text,
            urgency     = "NORMAL",
            ttl_hours   = 168.0,
        )
    except Exception:
        pass

    _firm_post(posted_by, "AGENT_HEALTH", bulletin_text, priority="NORMAL")
    return report

# =============================================================================
# CLI  — quick inspection / simulation
# =============================================================================

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print("\n=== Open Info Requests ===")
        meets = _load(_MEET)
        reqs  = [r for r in meets if r.get("type") == "INFO_REQUEST" and r.get("status") == "open"]
        print(f"  {len(reqs)} open request(s)")
        for r in reqs[:5]:
            print(f"  [{r['urgency']}] {r['from_agent']} → {r['to_agent']}: {r['topic'][:60]}")

        print("\n=== Open Meetings ===")
        meetings = [r for r in meets if r.get("type") == "AD_HOC_MEETING" and r.get("status") == "open"]
        print(f"  {len(meetings)} open meeting(s)")
        for m in meetings[:5]:
            responded   = set(m.get("responses", {}).keys())
            all_agents  = set(m.get("participants", [])) | {m.get("initiator")}
            waiting     = all_agents - responded
            print(f"  [{m['urgency']}] {m['initiator']}: {m['agenda'][:60]}")
            print(f"    waiting on: {sorted(waiting)}")

        print("\n=== Governance Proposals ===")
        props = _load(_PROP)
        open_props = [p for p in props if p.get("status") in ("pending","disputed","vetoed")]
        print(f"  {len(open_props)} open proposal(s)")
        for p in open_props[:5]:
            votes = p.get("votes", {})
            print(f"  [{p['status']}] {p['supervisor']} → {p['flagged_agent']}: {p['proposal'][:50]}")
            print(f"    votes: {[(v, d['vote']) for v,d in votes.items()]}")

    elif cmd == "simulate":
        print("\n>>> SIMULATION: Manager needs fresh NVDA research")
        rid = manager_request_research(
            topic   = "NVDA conviction score and recent thesis changes",
            context = "Considering 15% position increase before Q2 earnings call",
            urgency = "HIGH",
        )
        print(f"  Manager filed request: {rid}")

        print("\n>>> RD Director fans out to sub-agents")
        sub_ids = rd_leader_delegate_to_team(
            topic   = "NVDA: earnings trajectory, institutional ownership trend, moat strength",
            context = f"Fulfilling Manager request {rid}",
        )
        print(f"  Delegated to {len(sub_ids)} sub-agent(s): {sub_ids}")

        print("\n>>> ResearchTeam fulfills sub-request")
        if sub_ids:
            fulfill_info_request(
                sub_ids[0], "ResearchTeam",
                result=(
                    "NVDA conviction: 5★. EPS accelerating 3Q+. "
                    "Institutional ownership up 8% QoQ. "
                    "Data-center moat deepening via CUDA ecosystem lock-in. "
                    "Thesis intact — no red flags."
                ),
                confidence="HIGH",
            )

        print("\n>>> RD Director fulfills Manager's original request")
        fulfill_info_request(
            rid, "RDTeamLeader",
            result=(
                "NVDA thesis confirmed strong: 5★ conviction. "
                "Moat deepening, earnings accelerating. "
                "15% increase is within normal sizing; no IRM objection expected."
            ),
            confidence="HIGH",
        )
        responses = get_fulfilled_responses("Manager Agent", max_age_hours=1.0)
        print(f"  Manager can now see {len(responses)} response(s)")

        print("\n>>> SIMULATION: Governance veto — supervisor proposes CITDirector fix")
        prop_id = submit_supervisor_proposal(
            supervisor    = "Chief of Staff",
            flagged_agent = "CITDirector",
            proposal      = "Increase CITDirector run cadence from 6x to 8x daily and add a minimum-evidence filter requiring 2+ data points per proposal",
            rationale     = "HR flagged stale report; multiple NEGATIVE CIT outcomes traced to insufficient evidence quality",
        )
        print(f"  Proposal filed: {prop_id}")

        print("\n>>> All three voters approve")
        for voter in ["Chief of Staff", "IndependentRiskManager", "HRPerformanceOfficer"]:
            result = cast_veto_vote(prop_id, voter, "APPROVE", f"{voter} approves — fixes identified root cause")
            print(f"  {voter}: {result['status']} | waiting on: {result['waiting_on']}")

        print("\n>>> Check proposal status")
        p = get_proposal(prop_id)
        print(f"  Final status: {p['status']}")

        print("\n>>> SIMULATION: One voter vetoes")
        prop2 = submit_supervisor_proposal(
            supervisor    = "CITDirector",
            flagged_agent = "CITSelfImprovement",
            proposal      = "Auto-approve all CITSelfImprovement proposals without backlog review",
            rationale     = "Speed up improvement cycle",
        )
        cast_veto_vote(prop2, "Chief of Staff", "APPROVE", "Sounds reasonable")
        veto_result = cast_veto_vote(prop2, "IndependentRiskManager", "VETO",
                                     "Auto-approval bypasses quality gate — creates risk of unreviewed changes")
        cast_veto_vote(prop2, "HRPerformanceOfficer", "APPROVE", "Efficiency improvement")
        print(f"  After IRM veto: {veto_result['status']}")
        print(f"  Resolution meeting: {veto_result['resolution_meeting_id']}")

        print("\nSimulation complete.")
