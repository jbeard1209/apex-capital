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


def _safe_load(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


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
            data = _safe_load(filepath)
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

    n_ok    = sum(1 for v in brief["agent_status"].values() if v["status"] == "OK")
    n_stale = sum(1 for v in brief["agent_status"].values() if v["status"] == "STALE")
    n_miss  = sum(1 for v in brief["agent_status"].values() if v["status"] == "NO_REPORT")

    brief["summary"] = (
        f"Floor status: {n_ok} OK, {n_stale} stale, {n_miss} missing. "
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
