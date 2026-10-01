# =============================================================================
# APEX CAPITAL — OPERATIONS DIRECTOR
# =============================================================================
# Non-voting honorary board member. Supervises all agents that do not fall
# under another board member's direct chain:
#   Trade Review Team, Creative Agent, Capital Efficiency Agent,
#   Report Generator, Premarket Check, Earnings Predictor
#
# At each board meeting the Operations Director delivers a "State of the Floor"
# brief synthesising the supervised agents' latest output.
# He speaks but does NOT vote.
# =============================================================================

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("operations_director")

SUPERVISED_AGENTS = [
    "Trade Review Team",
    "Creative Agent",
    "Capital Efficiency Agent",
    "Report Generator",
    "Premarket Check",
    "Earnings Predictor",
]

SUPERVISED_FILES = {
    "Trade Review Team":        "data/trade_review_report.json",
    "Creative Agent":           "data/thematic_positions.json",
    "Capital Efficiency Agent": "data/capital_efficiency_report.json",
    "Report Generator":         "data/chief_of_staff_report.json",   # closest proxy
    "Premarket Check":          "data/premarket_alerts.json",
    "Earnings Predictor":       "data/earnings_predictions.json",
}

OPS_REPORT_PATH = "data/ops_director_report.json"

#: Latest-state floor brief. Each run rewrites the complete picture of agent
#: floor status; morning_briefing reads the most recent brief. Older briefs
#: are superseded, not missed.
_WIRING_SNAPSHOT = {"ops_director_report.json"}


# Every status build_floor_brief can assign to an agent. The floor-status line
# is DERIVED from this tuple, because writing the buckets out by hand is exactly
# how ERROR came to be counted nowhere.
FLOOR_STATUSES = ("OK", "STALE", "NO_REPORT", "ERROR")

# Which statuses mean the floor is NOT reporting properly. Kept beside
# FLOOR_STATUSES so "is it a problem" and "is it counted" cannot drift apart.
FLOOR_PROBLEM_STATUSES = ("STALE", "NO_REPORT", "ERROR")


def count_by_status(agent_status: dict) -> dict:
    """Every supervised agent in exactly one bucket, plus residual and total.

    `unknown` is the conservation residual and should always be 0. Reported
    rather than asserted: a sixth status added later surfaces as a number
    nobody can explain, instead of crashing the floor brief.
    """
    out = {k: 0 for k in FLOOR_STATUSES}
    out["unknown"] = 0
    for v in (agent_status or {}).values():
        s = (v or {}).get("status")
        out[s if s in out and s != "unknown" else "unknown"] += 1
    out["total"] = len(agent_status or {})
    return out


def _load_report(path: str) -> dict:
    """Read an agent's report. Raises on unreadable/corrupt JSON — deliberately.

    This used to swallow every exception and return `{}`. A truncated or
    half-written report therefore produced an empty dict, which has no
    timestamp key, which took the `Timestamp unknown` branch and was recorded
    as **OK**. The one condition the caller's try/except was written to catch —
    a report it cannot read — was the one condition that could never reach it.
    A degraded read must not substitute a value that means "fine"
    (memory fallback-that-means-act).
    """
    with open(path) as f:
        return json.load(f)


def build_floor_brief() -> dict:
    """
    Reads the latest output from each supervised agent and produces a
    'state of the floor' dict for inclusion in board meeting reports.
    """
    brief = {
        "generated_at": datetime.now().isoformat(),
        "supervised_agents": SUPERVISED_AGENTS,
        "agent_status": {},
        "flags": [],
        "summary": "",
    }

    for agent_name in SUPERVISED_AGENTS:
        filepath = SUPERVISED_FILES.get(agent_name)
        if not filepath or not os.path.exists(filepath):
            brief["agent_status"][agent_name] = {
                "status": "NO_REPORT",
                "note": f"Output file not found: {filepath}",
            }
            brief["flags"].append(f"{agent_name}: no report on file")
            continue

        try:
            data = _load_report(filepath)
            # If the file contains a list (e.g. thematic_positions.json), wrap for uniform handling
            if isinstance(data, list):
                data = {"_list_data": data, "items": len(data)}
            # Determine staleness
            ts_key = next((k for k in ("generated_at", "timestamp", "run_at", "date") if k in data), None)
            stale = False
            age_h = None
            if ts_key:
                try:
                    dt = datetime.fromisoformat(str(data[ts_key]))
                    age_h = (datetime.now() - dt).total_seconds() / 3600
                    stale = age_h > 26
                except Exception:
                    pass

            status = "STALE" if stale else "OK"
            note = f"Last updated {age_h:.0f}h ago" if age_h is not None else "Timestamp unknown"
            if stale:
                brief["flags"].append(f"{agent_name}: report stale ({age_h:.0f}h old)")

            brief["agent_status"][agent_name] = {
                "status": status,
                "note": note,
                "data_keys": list(data.keys())[:8] if isinstance(data, dict) else [f"list[{len(data)}]"],
            }
        except Exception as e:
            brief["agent_status"][agent_name] = {"status": "ERROR", "note": str(e)}
            brief["flags"].append(f"{agent_name}: read error — {e}")

    # Check CoS meeting requests addressed to Operations Director
    try:
        from firm_channel import get_pending_tasks
        pending = get_pending_tasks("Operations Director")
        if pending:
            brief["flags"].append(f"{len(pending)} pending message(s) from supervised agents")
            brief["pending_messages"] = [
                {"from": t.get("from", "?"), "task": t.get("task", "")[:120]}
                for t in pending[:5]
            ]
    except Exception:
        pass

    # Four statuses are written above; three of them were counted. An agent
    # whose report failed to parse got status ERROR and appeared in none of the
    # numbers, so "Floor status: 3 OK, 3 stale, 0 missing" read as a full
    # account of a floor it did not add up to. The board reads this line as the
    # Operations Director's report on his own floor.
    c = count_by_status(brief["agent_status"])
    brief["status_counts"] = c
    parts = [f"{c['OK']} OK", f"{c['STALE']} stale", f"{c['NO_REPORT']} missing",
             f"{c['ERROR']} unreadable"]
    if c["unknown"]:
        parts.append(f"{c['unknown']} UNKNOWN-STATUS")
        logger.warning(
            f"  [OpsDir] {c['unknown']} agent(s) carry a status outside "
            f"{FLOOR_STATUSES} — they are in no bucket and the floor counts "
            "will not sum to the roster")
    brief["summary"] = (
        f"Floor status: {', '.join(parts)} of {c['total']} supervised. "
        f"{len(brief['flags'])} flag(s)."
    )

    os.makedirs("data", exist_ok=True)
    with open(OPS_REPORT_PATH, "w") as f:
        json.dump(brief, f, indent=2, default=str)

    return brief


def run() -> dict:
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — OPERATIONS DIRECTOR")
    logger.info(f"  Floor Review · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    brief = build_floor_brief()

    logger.info(f"  {brief['summary']}")
    for flag in brief["flags"]:
        logger.warning(f"  ⚠ {flag}")

    # ── Fable 5 ops triage — gated to 2+ flags ────────────────────────────
    if len(brief["flags"]) >= 2:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("operations"):
                raw = _r(
                    "You are Apex Capital's Operations Director triaging floor issues.",
                    f"Flags: {'; '.join(f[:80] for f in brief['flags'][:5])}\n"
                    "2 sentences: which flag is the real problem (vs noise), and what "
                    "single action fixes it fastest?",
                    agent_name="operations", max_tokens=110,
                )
                if raw:
                    brief["llm_triage"] = raw.strip()
                    try:
                        with open(OPS_REPORT_PATH, "w") as _f:
                            json.dump(brief, _f, indent=2, default=str)
                    except Exception:
                        pass
        except Exception:
            pass

    # Post to firm channel
    try:
        from firm_channel import post as _fc_post
        _prio = "HIGH" if len(brief["flags"]) > 2 else "NORMAL"
        _fc_post(
            "Operations Director", "FLOOR_BRIEF",
            brief["summary"] + (
                f" Flags: {'; '.join(brief['flags'][:3])}" if brief["flags"] else ""
            ),
            priority=_prio,
        )
    except Exception:
        pass

    logger.info(f"  Floor brief saved → {OPS_REPORT_PATH}")
    return brief


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('OperationsDirector'):
        raise SystemExit(0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s",
                        datefmt="%H:%M:%S")
    result = run()
    print(json.dumps(result, indent=2, default=str))
