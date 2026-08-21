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

import ast
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
    # ML — there is ONE ML pod board (pod2), not three. ml_shortterm_board.py,
    # ml_longterm_board.py and ml_longterm_team_leader.py have never existed on
    # disk, so these three names could only ever fail to wake. They are not
    # decorative: agent_meeting.py:1453 lists MLShortTermBoard and
    # MLLongTermBoard as VOTERS in a firm-wide poll, log_review_agent grades
    # their coverage, and agent_meeting routes their escalations to the Chief of
    # Staff. Pointed at the board that actually exists; the wake appends
    # `--as <name>`, so each still reads its own inbox.
    # The real gap underneath — the long-term ML pod has six worker agents, no
    # board of its own, and data/ml_longterm_board_report.json has never been
    # written by anything — is a Chairman decision, not a rename. See the
    # standing queue item `ml-lt-cit-...-long_term_ml_output_`.
    "MLShortTermBoard":         ("ml_pod_board.py",                ["--inbox-only"]),
    "MLLongTermBoard":          ("ml_pod_board.py",                ["--inbox-only"]),
    "MLPodBoard":               ("ml_pod_board.py",                ["--inbox-only"]),
    "RDPodBoard":               ("rd_pod_board.py",                ["--inbox-only"]),
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
    "MLLongTermTeamLeader":     ("ml_research_team_leader.py",     ["--inbox-only"]),
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


# ── The contract, enforced ────────────────────────────────────────────────────
# Line 37 above has claimed "Scripts must support --inbox-only" since this file
# was written. On 2026-08-21 it was measured: of the 45 registry targets that
# exist on disk, 10 honoured the flag and 35 did not. Python does not object to
# an argument nobody reads, so the other 35 took "wake up and read your inbox"
# and ran their entire pipeline instead.
#
# Six of those reach the broker — board_meeting, canary_agent, deputy_board,
# deputy_monitor, head_agent, manager_agent. deputy_monitor's first act is to
# close any position down 7%. It is on neither host's crontab, so a wake is the
# *only* way it ever runs, and data/deputy_monitor_report.json on the VM shows
# one did, on 2026-08-19, live against the paper account.
#
# Two aggravating factors. wake_nonblocking() and wake_for_thread_reply() —
# which are the only paths anything actually calls — spawn with Popen, no
# timeout, stdout and stderr to DEVNULL, and never wrote the wake log; that is
# why data/wake_log.json has been `[]` since May 27 while the lock files record
# real wakes. And wake_for_thread_reply() deliberately bypasses the cooldown,
# so a chatty thread could start unbounded concurrent full pipeline runs.
#
# So the check is made here, once, and it fails closed: a target that cannot be
# shown to gate on the flag is not launched. Refusing is strictly better than
# what it replaces, because what it replaces is not a wake — it is a full
# unsupervised pipeline run wearing a wake's name.
_UNVERIFIED_TARGETS_LOG = os.path.join(BASE_DIR, "data", "wake_contract_breaches.json")


def _honours_inbox_only(script_path: str) -> "bool | None":
    """Does this script branch on --inbox-only before doing its work?

    AST, not a substring search: this module's own postmortem above says
    "--inbox-only" a dozen times, and every agent that mentions the flag in a
    docstring would pass a grep while ignoring it at runtime.

    Returns None for "could not look" (unreadable, unparseable) — a distinct
    fact from False, and reported as such rather than folded into it.
    """
    try:
        with open(script_path) as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        # Form A — the original hand-rolled guard:  "--inbox-only" in sys.argv
        if isinstance(node, ast.Compare) \
                and any(isinstance(op, ast.In) for op in node.ops) \
                and isinstance(node.left, ast.Constant) \
                and node.left.value == "--inbox-only":
            return True
        # Form B — the shared helper: agent_inbox.handle_wake_if_requested(...).
        # Both are real gates, so both count. Only the import name is checked,
        # because scripts alias it (`as _wake`) to keep the guard to two lines.
        if isinstance(node, ast.ImportFrom) and node.module == "agent_inbox" \
                and any(a.name == "handle_wake_if_requested" for a in node.names):
            return True
    return False


def _record_contract_breach(agent_name: str, script: str, verdict: "bool | None") -> None:
    """Append a refusal so an unwakeable agent is visible, not merely absent.

    memory `orphan_agents_blindness`: an agent that quietly never runs is the
    expensive kind of broken. A refusal that leaves no trace would trade a
    dangerous failure for an invisible one.
    """
    rows = []
    try:
        if os.path.exists(_UNVERIFIED_TARGETS_LOG):
            with open(_UNVERIFIED_TARGETS_LOG) as fh:
                rows = json.load(fh)
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    rows.append({
        "agent":   agent_name,
        "script":  script,
        "verdict": "IGNORES_FLAG" if verdict is False else "UNREADABLE",
        "ts":      datetime.now().isoformat(),
    })
    rows = rows[-200:]
    try:
        os.makedirs(os.path.dirname(_UNVERIFIED_TARGETS_LOG), exist_ok=True)
        tmp = _UNVERIFIED_TARGETS_LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(rows, fh, indent=2, default=str)
        os.replace(tmp, _UNVERIFIED_TARGETS_LOG)
    except Exception:
        pass


def _resolve_target(agent_name: str) -> "tuple[str, list, str]":
    """(script_path, extra_args, refusal_reason). refusal_reason == "" means go.

    One gate for all three wake paths. Previously each path did its own
    `if not os.path.exists(...): return` and none of them checked the contract,
    so the dangerous path and the safe path disagreed about what a wake was.
    """
    if agent_name not in AGENT_REGISTRY:
        return "", [], f"'{agent_name}' not in registry"
    script, extra_args = AGENT_REGISTRY[agent_name]
    script_path = os.path.join(BASE_DIR, script)
    if not os.path.exists(script_path):
        return script_path, extra_args, f"script not found: {script}"
    verdict = _honours_inbox_only(script_path)
    if verdict is not True:
        _record_contract_breach(agent_name, script, verdict)
        why = ("does not gate on --inbox-only, so waking it would run its full "
               "pipeline unsupervised" if verdict is False
               else "could not be parsed, so its --inbox-only support cannot be "
                    "verified")
        return script_path, extra_args, f"REFUSED: {script} {why}"
    return script_path, extra_args, ""


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
    script_path, extra_args, refusal = _resolve_target(agent_name)
    if refusal:
        logger.warning("[Wake] %s — %s", agent_name, refusal)
        _log_wake(agent_name, False, 0.0, refusal)
        return {"success": False, "skipped": True,
                "reason": refusal, "duration_s": 0}

    ok, elapsed = _cooldown_ok(agent_name)
    if not ok and not force:
        reason = f"cooldown: {elapsed:.0f}s since last wake (min {COOLDOWN_S}s)"
        logger.info("[Wake] Skipping %s — %s", agent_name, reason)
        return {"success": False, "skipped": True, "reason": reason, "duration_s": 0}

    script = os.path.basename(script_path)
    cmd = [sys.executable, script_path] + extra_args + ["--as", agent_name]
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
    """Fire-and-forget wake — spawns subprocess and returns immediately.

    This is the path agent_meeting.py actually calls, so everything true of a
    wake in practice is true here: the contract gate, and the log.
    """
    script_path, extra_args, refusal = _resolve_target(agent_name)
    if refusal:
        logger.warning("[Wake] %s — %s", agent_name, refusal)
        _log_wake(agent_name, False, 0.0, refusal)
        return
    ok, elapsed = _cooldown_ok(agent_name)
    if not ok:
        _log_wake(agent_name, False, 0.0,
                  f"cooldown: {elapsed:.0f}s < {COOLDOWN_S}s")
        return
    cmd = [sys.executable, script_path] + extra_args + ["--as", agent_name]
    if request_id:
        cmd += ["--request-id", request_id]
    _record_wake(agent_name)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=BASE_DIR)
        logger.info("[Wake] Non-blocking wake fired for %s", agent_name)
        _log_wake(agent_name, True, 0.0, "nonblocking: spawned, exit not observed")
    except Exception as e:
        logger.warning("[Wake] Non-blocking wake failed for %s: %s", agent_name, e)
        _log_wake(agent_name, False, 0.0, f"spawn failed: {e}")


def wake_for_thread_reply(agent_name: str, thread_id: str) -> None:
    """
    Wake an agent to handle a reply in an active conversation thread.
    Bypasses the normal cooldown — when agents are mid-conversation they
    should respond as quickly as needed without artificial delay.
    Called automatically by reply_to_request() in agent_meeting.py.
    """
    script_path, extra_args, refusal = _resolve_target(agent_name)
    if refusal:
        logger.warning("[Wake] %s — %s", agent_name, refusal)
        _log_wake(agent_name, False, 0.0, refusal)
        return
    cmd = [sys.executable, script_path] + extra_args \
        + ["--as", agent_name, "--thread-id", thread_id]
    _record_wake(agent_name)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=BASE_DIR)
        logger.info("[Wake] Thread-reply wake (no cooldown) fired for %s [thread=%s]",
                    agent_name, thread_id)
        _log_wake(agent_name, True, 0.0, f"thread-reply {thread_id}: spawned")
    except Exception as e:
        logger.warning("[Wake] Thread-reply wake failed for %s: %s", agent_name, e)
        _log_wake(agent_name, False, 0.0, f"thread-reply spawn failed: {e}")


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
