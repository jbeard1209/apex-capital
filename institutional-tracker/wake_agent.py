"""
wake_agent.py  —  On-Demand Agent Wake Dispatcher
===================================================
Wakes any named agent to handle its inbox (info requests, meetings, delegations)
without running its full pipeline.  Called automatically by agent_meeting.py
when a HIGH/CRITICAL urgency request is filed.

Usage:
    python3 wake_agent.py <AgentName>
    python3 wake_agent.py <AgentName> --request-id <rid>

Each agent script is called with --inbox-only.  A per-agent lockfile prevents
concurrent wakes.  3-minute cooldown between wakes of the same agent.
Thread replies (reply_to_request) bypass the cooldown entirely.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("wake_agent")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LOCK_DIR   = os.path.join(BASE_DIR, "data", "wake_locks")
WAKE_LOG   = os.path.join(BASE_DIR, "data", "wake_log.json")
COOLDOWN_S = 180   # 3 minutes between wakes of the same agent
TIMEOUT_S  = 90    # max seconds an inbox-only run may take

# ── Agent registry ─────────────────────────────────────────────────────────────
# Maps canonical agent name → (script, [extra_args])
# Scripts must support --inbox-only flag (see agent_inbox.py for the handler)
AGENT_REGISTRY: dict[str, tuple[str, list]] = {
    # Core governance
    "Chief of Staff":           ("chief_of_staff_agent.py",        ["--inbox-only"]),
    "IndependentRiskManager":   ("irm_review_agent.py",            ["--inbox-only"]),
    "HRPerformanceOfficer":     ("hr_agent.py",                    ["--inbox-only"]),
    "HRDevelopmentOfficer":     ("hr_agent.py",                    ["--inbox-only"]),
    # Trading
    "Manager Agent":            ("manager_agent.py",               ["--inbox-only"]),
    "ManagerAgent":             ("manager_agent.py",               ["--inbox-only"]),
    # Research
    "RDTeamLeader":             ("rd_team_leader.py",              ["--inbox-only"]),
    "ResearchTeam":             ("research_team.py",               ["--inbox-only"]),
    "EnergyResearchTeam":       ("energy_research_team.py",        ["--inbox-only"]),
    "MLResearchTeamLeader":     ("ml_research_team_leader.py",     ["--inbox-only"]),
    # CIT
    "CITDirector":              ("cit_director.py",                ["--session", "morning", "--inbox-only"]),
    "CITCodeInspector":         ("cit_director.py",                ["--inbox-only"]),
    "CITPipelineMonitor":       ("cit_pipeline_integrity_agent.py",["--inbox-only"]),
    "CITSelfImprovement":       ("cit_director.py",                ["--inbox-only"]),
    "CITStrategyAnalyst":       ("cit_director.py",                ["--inbox-only"]),
    "CITArchitectureAdvisor":   ("cit_director.py",                ["--inbox-only"]),
    "CITDataQualityAnalyst":    ("cit_director.py",                ["--inbox-only"]),
    # Board / risk
    "Board":                    ("board_meeting.py",               ["--inbox-only"]),
    "CROAgent":                 ("irm_review_agent.py",            ["--inbox-only"]),
    # FI / FE pods
    "FIBoard":                  ("fi_board.py",                    ["--inbox-only"]),
    "FEBoard":                  ("fe_board.py",                    ["--inbox-only"]),
    # ML
    "MLShortTermBoard":         ("ml_shortterm_board.py",          ["--inbox-only"]),
    "MLLongTermBoard":          ("ml_longterm_board.py",           ["--inbox-only"]),
    # Monitoring
    "Canary":                   ("canary_agent.py",                ["--inbox-only"]),
    "DeputyMonitor":            ("deputy_monitor.py",              ["--inbox-only"]),
    # Boards & directors
    "ApexImprovementBoard":     ("apex_improvement_board.py",      ["--inbox-only"]),
    "DeputyBoard":              ("deputy_board.py",                ["--inbox-only"]),
    "ETFPodBoard":              ("etf_pod_board.py",               ["--inbox-only"]),
    "OperationsDirector":       ("operations_director.py",         ["--inbox-only"]),
    "HeadAgent":                ("head_agent.py",                  ["--inbox-only"]),
    # FE pod agents
    "FECIOAgent":               ("fe_cio_agent.py",                ["--inbox-only"]),
    "FEResearchOrchestrator":   ("fe_research_orchestrator.py",    ["--inbox-only"]),
    # Research specialists
    "EarningsPredictor":        ("earnings_predictor.py",          ["--inbox-only"]),
    "ETFResearchAgent":         ("etf_research_agent.py",          ["--inbox-only"]),
    "InternationalAnalyst":     ("international_analyst.py",       ["--inbox-only"]),
    "RDGeopoliticalTeam":       ("rd_geopolitical_team.py",        ["--inbox-only"]),
    "RDHistoricalAnalyst":      ("rd_historical_analyst.py",       ["--inbox-only"]),
    "PodMacroAgent":            ("pod_macro_agent.py",             ["--inbox-only"]),
    # ML specialists
    "MLLongTermTeamLeader":     ("ml_longterm_team_leader.py",     ["--inbox-only"]),
    "MLRDBridgeAgent":          ("ml_rd_bridge_agent.py",          ["--inbox-only"]),
    "MLShortTermCITAgent":      ("ml_shortterm_cit_agent.py",      ["--inbox-only"]),
    "MLLongTermCITAgent":       ("ml_longterm_cit_agent.py",       ["--inbox-only"]),
    # CIT extras
    "CITAutoImplementer":       ("cit_auto_implementer.py",        ["--inbox-only"]),
    "CITWatchdog":              ("cit_watchdog.py",                ["--inbox-only"]),
    # Risk / review / capital
    "PositionRiskMonitor":      ("position_risk_monitor.py",     ["--inbox-only"]),
    "LogReviewAgent":           ("log_review_agent.py",           ["--inbox-only"]),
    "CapitalEfficiencyAgent":   ("capital_efficiency_agent.py",    ["--inbox-only"]),
    "ReviewAgent":              ("review_agent.py",                ["--inbox-only"]),
    "TradeReviewTeam":          ("trade_review_team.py",           ["--inbox-only"]),
    "SignalEngine":             ("signal_engine.py",               ["--inbox-only"]),
    "PremarketCheck":           ("premarket_check.py",             ["--inbox-only"]),
    "MorningBriefing":          ("morning_briefing.py",            ["--inbox-only"]),
    # Strategic projects #30-33
    "CryptoSignalAgent":        ("crypto_signal_agent.py",         ["--inbox-only"]),
    "TCAAgent":                 ("tca_agent.py",                   ["--inbox-only"]),
    "QuiverSignalAgent":        ("quiver_signal_agent.py",          ["--inbox-only"]),
    "AlpacaWSListener":         ("alpaca_ws_listener.py",           ["--inbox-only"]),
}


def _lock_path(agent_name: str) -> str:
    os.makedirs(LOCK_DIR, exist_ok=True)
    safe = agent_name.replace(" ", "_").replace("/", "_")
    return os.path.join(LOCK_DIR, f"{safe}.lock")


def _cooldown_ok(agent_name: str) -> tuple[bool, float]:
    """Returns (ok, seconds_since_last_wake)."""
    lp = _lock_path(agent_name)
    if not os.path.exists(lp):
        return True, float("inf")
    try:
        with open(lp) as f:
            data = json.load(f)
        last = data.get("last_wake", "")
        if not last:
            return True, float("inf")
        elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        return elapsed >= COOLDOWN_S, elapsed
    except Exception:
        return True, float("inf")


def _record_wake(agent_name: str) -> None:
    lp = _lock_path(agent_name)
    try:
        with open(lp, "w") as f:
            json.dump({"agent": agent_name, "last_wake": datetime.now().isoformat()}, f)
    except Exception:
        pass


def _log_wake(agent_name: str, success: bool, duration_s: float, note: str = "") -> None:
    entries = []
    if os.path.exists(WAKE_LOG):
        try:
            with open(WAKE_LOG) as f:
                entries = json.load(f)
        except Exception:
            pass
    entries.append({
        "agent":      agent_name,
        "woken_at":   datetime.now().isoformat(),
        "success":    success,
        "duration_s": round(duration_s, 1),
        "note":       note,
    })
    entries = entries[-200:]   # keep last 200 entries
    try:
        tmp = WAKE_LOG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f, indent=2, default=str)
        os.replace(tmp, WAKE_LOG)
    except Exception:
        pass


def wake(agent_name: str, request_id: str = "", force: bool = False) -> dict:
    """
    Wake a named agent for inbox-only processing.
    Returns {"success": bool, "skipped": bool, "reason": str, "duration_s": float}
    """
    if agent_name not in AGENT_REGISTRY:
        return {"success": False, "skipped": True,
                "reason": f"'{agent_name}' not in registry", "duration_s": 0}

    ok, elapsed = _cooldown_ok(agent_name)
    if not ok and not force:
        reason = f"cooldown: {elapsed:.0f}s since last wake (min {COOLDOWN_S}s)"
        logger.info("[Wake] Skipping %s — %s", agent_name, reason)
        return {"success": False, "skipped": True, "reason": reason, "duration_s": 0}

    script, extra_args = AGENT_REGISTRY[agent_name]
    script_path = os.path.join(BASE_DIR, script)
    if not os.path.exists(script_path):
        return {"success": False, "skipped": False,
                "reason": f"script not found: {script}", "duration_s": 0}

    cmd = [sys.executable, script_path] + extra_args
    if request_id:
        cmd += ["--request-id", request_id]

    logger.info("[Wake] Waking %s (%s)...", agent_name, script)
    _record_wake(agent_name)

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            cwd=BASE_DIR,
        )
        duration = time.monotonic() - t0
        success  = result.returncode == 0
        note     = (result.stderr[-300:] if not success and result.stderr else "")
        if success:
            logger.info("[Wake] %s completed in %.1fs", agent_name, duration)
        else:
            logger.warning("[Wake] %s failed (exit %d): %s",
                           agent_name, result.returncode, note[:100])
        _log_wake(agent_name, success, duration, note)
        return {"success": success, "skipped": False,
                "reason": "ok" if success else f"exit {result.returncode}",
                "duration_s": duration}
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - t0
        logger.warning("[Wake] %s timed out after %.0fs", agent_name, duration)
        _log_wake(agent_name, False, duration, "timeout")
        return {"success": False, "skipped": False,
                "reason": f"timeout after {TIMEOUT_S}s", "duration_s": duration}
    except Exception as e:
        duration = time.monotonic() - t0
        _log_wake(agent_name, False, duration, str(e))
        return {"success": False, "skipped": False, "reason": str(e), "duration_s": duration}


def wake_nonblocking(agent_name: str, request_id: str = "") -> None:
    """Fire-and-forget wake — spawns subprocess and returns immediately."""
    if agent_name not in AGENT_REGISTRY:
        return
    ok, elapsed = _cooldown_ok(agent_name)
    if not ok:
        return
    script, extra_args = AGENT_REGISTRY[agent_name]
    script_path = os.path.join(BASE_DIR, script)
    if not os.path.exists(script_path):
        return
    cmd = [sys.executable, script_path] + extra_args
    if request_id:
        cmd += ["--request-id", request_id]
    _record_wake(agent_name)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=BASE_DIR)
        logger.info("[Wake] Non-blocking wake fired for %s", agent_name)
    except Exception as e:
        logger.debug("[Wake] Non-blocking wake failed for %s: %s", agent_name, e)


def wake_for_thread_reply(agent_name: str, thread_id: str) -> None:
    """
    Wake an agent to handle a reply in an active conversation thread.
    Bypasses the normal cooldown — when agents are mid-conversation they
    should respond as quickly as needed without artificial delay.
    Called automatically by reply_to_request() in agent_meeting.py.
    """
    if agent_name not in AGENT_REGISTRY:
        return
    script, extra_args = AGENT_REGISTRY[agent_name]
    script_path = os.path.join(BASE_DIR, script)
    if not os.path.exists(script_path):
        return
    cmd = [sys.executable, script_path] + extra_args + ["--thread-id", thread_id]
    _record_wake(agent_name)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=BASE_DIR)
        logger.info("[Wake] Thread-reply wake (no cooldown) fired for %s [thread=%s]",
                    agent_name, thread_id)
    except Exception as e:
        logger.debug("[Wake] Thread-reply wake failed for %s: %s", agent_name, e)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Wake an agent for inbox-only processing")
    parser.add_argument("agent",        nargs="?", help="Agent name (use quotes for spaces)")
    parser.add_argument("--request-id", default="", help="Specific request ID to handle")
    parser.add_argument("--force",      action="store_true", help="Ignore cooldown")
    parser.add_argument("--list",       action="store_true", help="List registered agents")
    args = parser.parse_args()

    if args.list:
        print("Registered agents:")
        for name, (script, extra) in sorted(AGENT_REGISTRY.items()):
            print(f"  {name:<35} → {script}")
        sys.exit(0)

    if not args.agent:
        parser.print_help()
        sys.exit(1)

    r = wake(args.agent, request_id=args.request_id, force=args.force)
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["success"] or r["skipped"] else 1)
