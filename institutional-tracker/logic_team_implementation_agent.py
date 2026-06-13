#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — LOGIC TEAM IMPLEMENTATION AGENT
# =============================================================================
#
# The Logic Team finds coherence failures. This agent acts on them.
#
# Unlike the CIT Implementation Agent (which writes code), this agent's job
# is operational resolution: it triages open Logic Team findings, determines
# which ones can be auto-resolved vs. require board action, and executes
# the resolutions it's authorized to take.
#
# Authorized auto-resolutions:
#   - Agent health gaps: spawn replacement agents via Chief of Staff
#   - Stale chairman items: draft summary and flag for dismissal
#   - Capital idle too long: escalate to board with specific deployment plan
#   - Pipeline dark: trigger rd_team_leader.py run immediately
#   - Board governance gaps: trigger board_meeting.py with agenda
#
# Everything else is escalated to the board as a proposal with a specific
# recommended action and an owner (CIT, HR, IRM, Chairman).
#
# Schedule: runs after each Logic Team cycle (wired into logic_team.py
#           and crontab after logic_team standalone runs)
#
# Usage:
#   python3 logic_team_implementation_agent.py           # full triage cycle
#   python3 logic_team_implementation_agent.py --report  # show open items
# =============================================================================

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lt_impl")

BASE_DIR         = Path(__file__).parent
DATA_DIR         = BASE_DIR / "data"
LT_RESOLUTIONS   = DATA_DIR / "logic_team_resolutions.json"
LT_FINDINGS      = DATA_DIR / "logic_team_findings.json"
CIT_PENDING      = DATA_DIR / "cit_pending_proposals.json"
FIRM_CHANNEL     = DATA_DIR / "firm_channel.json"
IMPL_LOG         = DATA_DIR / "lt_implementation_log.json"

os.chdir(BASE_DIR)


def _load(path: Path, default=None):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _log_action(action: str, finding_key: str, result: str, auto_resolved: bool = False):
    log = _load(IMPL_LOG, {"actions": []})
    if not isinstance(log, dict):
        log = {"actions": []}
    log["actions"].append({
        "ts":           datetime.now().isoformat(),
        "finding_key":  finding_key,
        "action":       action,
        "result":       result,
        "auto_resolved": auto_resolved,
    })
    log["actions"] = log["actions"][-200:]
    log["last_run"] = datetime.now().isoformat()
    _save(IMPL_LOG, log)


def _post_to_channel(message: str, priority: str = "NORMAL"):
    try:
        fc = _load(FIRM_CHANNEL, [])
        if not isinstance(fc, list):
            fc = []
        fc.append({
            "ts":       datetime.now().isoformat(),
            "date":     datetime.now().strftime("%Y-%m-%d"),
            "agent":    "LT Implementation Agent",
            "category": "LT_RESOLUTION",
            "priority": priority,
            "content":  message,
        })
        if len(fc) > 500:
            fc = fc[-500:]
        _save(FIRM_CHANNEL, fc)
    except Exception as e:
        logger.warning(f"  Channel post failed: {e}")


def _escalate_to_board(title: str, description: str, recommended_owner: str,
                       severity: str = "MEDIUM", proposed_fix: str = "") -> bool:
    """Submit a resolution proposal to the board queue."""
    try:
        pending = _load(CIT_PENDING, [])
        if not isinstance(pending, list):
            pending = []
        existing = {p.get("title","") for p in pending}
        if title in existing:
            return False
        pending.append({
            "id":           f"LTIA-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "source":       "LT Implementation Agent",
            # Must be a category the deputy/full board recognizes. "OPERATIONAL_FIX"
            # is in none of deputy_board's scope sets, so it skipped the deputy
            # fast-path and only the (often-stalled) full board would pick it up.
            # "IMPROVEMENT" is in DEPUTY_CATEGORIES; HIGH/CRITICAL still escalate
            # to the full board per deputy_board._should_deputy_vote().
            "category":     "IMPROVEMENT",
            "severity":     severity,
            "confidence":   85,
            "title":        title,
            "description":  description,
            "proposed_fix": proposed_fix or description,
            "owner":        recommended_owner,
            "created_at":   datetime.now().isoformat(),
            "status":       "pending",
        })
        _save(CIT_PENDING, pending)
        return True
    except Exception as e:
        logger.warning(f"  Board escalation failed: {e}")
        return False


# =============================================================================
# COOLDOWN — stop auto-actions from re-firing every cycle (the "noise" trap)
# =============================================================================
# An auto-action (re-running a pipeline, reconciling a ledger) is only useful
# the first time. Re-running it every 15 minutes is exactly the kind of useless
# churn the CIT used to produce. We persist a per-rule cooldown clock and skip
# subprocess actions that ran inside the window.

def _cooldowns() -> dict:
    log = _load(IMPL_LOG, {})
    cd = log.get("cooldowns", {})
    return cd if isinstance(cd, dict) else {}


def _within_cooldown(rule: str, hours: float) -> bool:
    last = _cooldowns().get(rule)
    if not last:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(last)) < timedelta(hours=hours)
    except Exception:
        return False


def _mark_cooldown(rule: str):
    log = _load(IMPL_LOG, {"actions": []})
    if not isinstance(log, dict):
        log = {"actions": []}
    cd = log.get("cooldowns", {})
    if not isinstance(cd, dict):
        cd = {}
    cd[rule] = datetime.now().isoformat()
    log["cooldowns"] = cd
    _save(IMPL_LOG, log)


def _run_script(script: str, args: list = None, timeout: int = 180) -> tuple:
    """Run a project script as a subprocess. Returns (ok, detail)."""
    args = args or []
    path = BASE_DIR / script
    if not path.exists():
        return False, f"missing_script:{script}"
    try:
        r = subprocess.run([sys.executable, str(path), *args],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, "ok"
        return False, f"exit{r.returncode}:{(r.stderr or r.stdout)[:120].strip()}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, f"error:{e}"


# =============================================================================
# RESOLUTION HANDLERS
# Two kinds:
#   AUTO  — the agent fixes it directly (rerun a dark pipeline, reconcile a
#           broken ledger). These actually resolve the finding.
#   BOARD — the agent cannot fix it alone (governance votes, capital policy,
#           risk-signal contradictions). These escalate with a *specific*
#           recommended action and owner — never a vague "review this".
# Each handler returns a short result string for the action log.
# =============================================================================

# ---- AUTO-ACTION HANDLERS ----------------------------------------------------

def _auto_rerun_research(entry: dict) -> str:
    """Dark/stale research pipeline — rerun the R&D team leader to produce picks."""
    if _within_cooldown("rerun_research", hours=24):
        return "skipped:cooldown_24h"
    logger.info("  → Re-running rd_team_leader.py to break the research dark window...")
    ok, detail = _run_script("rd_team_leader.py", timeout=240)
    _mark_cooldown("rerun_research")
    if ok:
        _post_to_channel(
            f"🔧 LT auto-action: research pipeline was dark/stale "
            f"('{entry.get('title','?')[:50]}') — re-ran rd_team_leader.py to regenerate picks.",
            priority="HIGH")
        return "auto_reran_research_pipeline"
    return f"research_rerun_failed:{detail}"


def _auto_rerun_pod_pipeline(entry: dict) -> str:
    """Pod pipeline stale / never run / zero picks — rerun that pod's pipeline."""
    rule = entry.get("rule", "")
    pod = "fi" if rule.startswith("fi") else "fe" if rule.startswith("fe") else None
    if not pod:
        return "no_pod_match"
    script = f"{pod}_pipeline.py"
    cd_key = f"rerun_{pod}_pipeline"
    if _within_cooldown(cd_key, hours=24):
        return "skipped:cooldown_24h"
    logger.info(f"  → Re-running {script} to refresh {pod.upper()} pod picks...")
    ok, detail = _run_script(script, timeout=300)
    _mark_cooldown(cd_key)
    if ok:
        _post_to_channel(
            f"🔧 LT auto-action: {pod.upper()} pipeline was stale/empty "
            f"('{entry.get('title','?')[:50]}') — re-ran {script}.", priority="NORMAL")
        return f"auto_reran_{script}"
    return f"{pod}_rerun_failed:{detail}"


def _auto_reconcile_positions(entry: dict) -> str:
    """Open positions missing review coverage — rerun review_agent to re-sync."""
    if _within_cooldown("reconcile_reviews", hours=12):
        return "skipped:cooldown_12h"
    logger.info("  → Re-running review_agent.py to re-sync position coverage...")
    ok, detail = _run_script("review_agent.py", args=["--quiet"], timeout=180)
    _mark_cooldown("reconcile_reviews")
    if ok:
        _post_to_channel(
            f"🔧 LT auto-action: positions lacked review coverage "
            f"('{entry.get('title','?')[:50]}') — re-ran review_agent.py to restore "
            f"stop-loss / exit monitoring.", priority="HIGH")
        return "auto_reconciled_reviews"
    return f"review_rerun_failed:{detail}"


def _auto_reconcile_pod_ledger(entry: dict) -> str:
    """Pod positions_value=0 despite holdings — rerun pod_ledger to recompute."""
    if _within_cooldown("reconcile_pod_ledger", hours=12):
        return "skipped:cooldown_12h"
    logger.info("  → Re-running pod_ledger.py to recompute pod values...")
    ok, detail = _run_script("pod_ledger.py", timeout=120)
    _mark_cooldown("reconcile_pod_ledger")
    if ok:
        _post_to_channel(
            f"🔧 LT auto-action: pod value tracking was broken "
            f"('{entry.get('title','?')[:50]}') — re-ran pod_ledger.py to recompute "
            f"positions_value and pod P&L.", priority="NORMAL")
        return "auto_reconciled_pod_ledger"
    return f"pod_ledger_rerun_failed:{detail}"


def _auto_train_model(entry: dict) -> str:
    """No trained model exists — train and persist one (model.pkl + model_meta.json)."""
    if _within_cooldown("train_model", hours=24):
        return "skipped:cooldown_24h"
    logger.info("  → Training ML model (predictive_model/ml_model.py)...")
    ok, detail = _run_script("predictive_model/ml_model.py", timeout=600)
    _mark_cooldown("train_model")
    if ok:
        _post_to_channel(
            f"🔧 LT auto-action: no trained model existed "
            f"('{entry.get('title','?')[:50]}') — ran ml_model.py to train + persist "
            f"model.pkl/model_meta.json. Real AUC now available to monitors.",
            priority="HIGH")
        return "auto_trained_model"
    return f"model_train_failed:{detail}"


def _auto_start_ml_tracking(entry: dict) -> str:
    """ML pod live picks not tracked — start the live test runner so AUC can be validated."""
    if _within_cooldown("start_ml_tracking", hours=24):
        return "skipped:cooldown_24h"
    logger.info("  → Running ml_live_test_runner.py --enter to begin live ML validation...")
    ok, detail = _run_script("ml_live_test_runner.py", args=["--enter"], timeout=180)
    _mark_cooldown("start_ml_tracking")
    if ok:
        _post_to_channel(
            f"🔧 LT auto-action: ML pod had no live picks tracked "
            f"('{entry.get('title','?')[:50]}') — started ml_live_test_runner --enter. "
            f"Evaluate after 20 trading days.", priority="NORMAL")
        return "auto_started_ml_tracking"
    return f"ml_tracking_failed:{detail}"


# ---- BOARD-ESCALATION HANDLERS ----------------------------------------------

def _board_idle_capital(entry: dict) -> str:
    title = f"[LTIA] Decide on idle pod capital: {entry.get('title','?')[:45]}"
    submitted = _escalate_to_board(
        title,
        f"Logic Team flagged idle capital: '{entry.get('title','?')}' "
        f"(first seen {entry.get('first_seen','?')[:10]}, {entry.get('times_seen',1)}x). "
        f"This needs a decision, not a rerun.",
        recommended_owner="Board + relevant pod CIO",
        severity="MEDIUM",
        proposed_fix="Vote one of: (a) document an explicit reason the pod is held flat "
                     "and a re-deploy trigger; or (b) if it's a pipeline failure, assign CIT "
                     "to fix the pod pipeline this week.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_governance(entry: dict) -> str:
    title = f"[LTIA] Governance action: {entry.get('title','?')[:50]}"
    submitted = _escalate_to_board(
        title,
        f"Governance check failed: '{entry.get('title','?')}' "
        f"(open {entry.get('days_open',0)}d, {entry.get('times_seen',1)}x).",
        recommended_owner="Chairman + Board",
        severity="HIGH",
        proposed_fix="Run `python3 board_meeting.py` with this item as the lead agenda line; "
                     "record the vote in board_meeting_report.json so this finding can auto-close.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_cro_paralysis(entry: dict) -> str:
    title = "[LTIA] CRO extended block needs a vote with explicit exit criteria"
    submitted = _escalate_to_board(
        title,
        f"CRO paralysis: '{entry.get('title','?')}'. A perpetual buy-block with no "
        f"exit condition is a strategy vacuum.",
        recommended_owner="Board + CRO + IRM",
        severity="HIGH",
        proposed_fix="Board must define the exact metric+threshold that steps CRO down to "
                     "ELEVATED (e.g. 'VIX < 22 for 3 sessions AND IRM flags == 0'), or accept "
                     "the posture with a named review date. 'Keep reviewing' is not an answer.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_risk_contradiction(entry: dict) -> str:
    title = f"[LTIA] Reconcile contradicting risk signals: {entry.get('title','?')[:40]}"
    submitted = _escalate_to_board(
        title,
        f"Risk monitors disagree: '{entry.get('title','?')}'. The board cannot act on "
        f"signals that point opposite directions.",
        recommended_owner="IRM + CRO",
        severity=entry.get("severity", "MEDIUM"),
        proposed_fix="IRM and CRO each confirm their inputs are fresh (check timestamps on "
                     "irm_daily_check.json and risk_bulletin.json). Whichever is running on "
                     "stale data gets re-run; if both are fresh, board picks the governing signal.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_agent_health(entry: dict) -> str:
    rule = entry.get("rule", "")
    if rule == "sector_coverage_gap":
        owner, fix = ("Chief of Staff",
                      "Spawn a research agent for each uncovered sector via the Agent Factory in "
                      "chief_of_staff_agent.py, then confirm it registers as 'active'.")
    elif rule in ("suspicious_mass_retirement",):
        owner, fix = ("HR + Chairman",
                      "Cross-check each retirement date against CRO=HIGH windows; restore agents "
                      "whose zero-pick streaks fell entirely inside a firm-wide buy block (amnesty).")
    elif rule == "pip_during_cro_high":
        owner, fix = ("HR",
                      "Pause PIP timers while CRO is HIGH/CRITICAL — an agent can't clear a PIP that "
                      "requires approved picks when all buys are blocked. Resume when CRO <= ELEVATED.")
    else:
        owner, fix = ("HR", "Review agent-health finding and act.")
    title = f"[LTIA] Agent-health fix: {entry.get('title','?')[:48]}"
    submitted = _escalate_to_board(title,
                                   f"Agent-health finding: '{entry.get('title','?')}'.",
                                   recommended_owner=owner, severity="MEDIUM", proposed_fix=fix)
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_loss_action(entry: dict) -> str:
    title = f"[LTIA] Loss needs corrective action, not just documentation"
    submitted = _escalate_to_board(
        title,
        f"Performance finding: '{entry.get('title','?')}'. Attribution without action is "
        f"just paperwork.",
        recommended_owner="Board + Review Agent",
        severity="HIGH",
        proposed_fix="Board approves specific recovery trades (e.g. cover the QQQ short per the "
                     "Review Agent, deploy idle pod capital when CRO permits) and logs them under "
                     "loss_attribution.fixes_applied_today so this finding closes.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_ml_suspend(entry: dict) -> str:
    title = "[LTIA] Suspend ML-driven buys — model AUC below random"
    submitted = _escalate_to_board(
        title,
        f"ML finding: '{entry.get('title','?')}'. An AUC below 0.50 means the model loses to "
        f"a coin flip; any capital on its signals has negative expectancy.",
        recommended_owner="Board + ML pod",
        severity="HIGH",
        proposed_fix="Vote to suspend ML-sourced buy recommendations immediately and assign the "
                     "ML pod to retrain before re-enabling. Document the suspension in the board log.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_wiring_fix(entry: dict) -> str:
    """A producer/consumer wiring contract is broken — route to CIT to fix the code."""
    title = f"[LTIA] Wiring contract broken — code fix needed: {entry.get('title','?')[:40]}"
    submitted = _escalate_to_board(
        title,
        f"Self-detected wiring violation: '{entry.get('title','?')}'. Something is produced but "
        f"no consumer handles it (dead-letter / blind monitor).",
        recommended_owner="CIT",
        severity="HIGH",
        proposed_fix="CIT: align the producer and consumer identifiers (rule/queue/category/file), "
                     "then add a TestWiringContracts case. Reproduce with `python3 wiring_audit.py`.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_backlog_bloat(entry: dict) -> str:
    title = "[LTIA] CIT backlog bloat — tighten proposal quality or action path"
    submitted = _escalate_to_board(
        title,
        f"Backlog finding: '{entry.get('title','?')}'. Proposals are accumulating faster than "
        f"the boards act on them.",
        recommended_owner="CIT Director + Deputy Board",
        severity="MEDIUM",
        proposed_fix="Sample 20 open BUG_FIX items: if mostly low-value/duplicate, raise the CIT "
                     "confidence/precision gate; if valuable, widen deputy-board auto-implement so "
                     "they actually get done. Write site now caps cit_backlog.json at 500.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_email_overflow(entry: dict) -> str:
    title = "[LTIA] Email buffer overflowing — tighten sender volume"
    submitted = _escalate_to_board(
        title,
        f"Communication finding: '{entry.get('title','?')}'. The Chairman can't absorb this volume.",
        recommended_owner="Operations Director",
        severity="MEDIUM",
        proposed_fix="In email_buffer.py, suppress LOW-priority senders from the digest and/or "
                     "raise digest frequency. Target < 20 buffered emails between flushes.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _board_communication_silence(entry: dict) -> str:
    title = "[LTIA] Expected agents silent — check they're still running"
    submitted = _escalate_to_board(
        title,
        f"Communication finding: '{entry.get('title','?')}'. Silent agents are invisible agents.",
        recommended_owner="Operations Director",
        severity="LOW",
        proposed_fix="Check crontab and the latest log for each silent agent; restart any that "
                     "have stopped, or wire its output to the firm channel if it's running mute.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


# =============================================================================
# RULE → HANDLER MAPPING
# Keys are CANONICAL rule names. Dynamic rules (idle_pod_<id>,
# pod_value_mismatch_<id>, <pod>_pipeline_stale, <pod>_never_run,
# <pod>_zero_picks) are folded onto these keys by _normalize_rule().
# =============================================================================

RESOLUTION_HANDLERS = {
    # --- auto-fixable operational findings ---
    "zero_picks":              _auto_rerun_research,
    "stale_research_report":   _auto_rerun_research,
    "complete_inactivity":     _auto_rerun_research,
    "fi_pipeline_not_running": _auto_rerun_pod_pipeline,
    "pod_pipeline_stale":      _auto_rerun_pod_pipeline,
    "pod_never_run":           _auto_rerun_pod_pipeline,
    "pod_zero_picks":          _auto_rerun_pod_pipeline,
    "positions_without_reviews": _auto_reconcile_positions,
    "pod_value_mismatch":      _auto_reconcile_pod_ledger,
    "ml_model_not_tracked":    _auto_start_ml_tracking,
    "ml_no_live_validation":   _auto_start_ml_tracking,
    "ml_model_never_trained":  _auto_train_model,

    # --- needs a human/board decision ---
    "idle_pod":                _board_idle_capital,
    "cro_extended_no_review":  _board_governance,
    "board_overdue":           _board_governance,
    "stale_chairman_actions":  _board_governance,
    "cro_paralysis":           _board_cro_paralysis,
    "cro_high_fully_deployed": _board_cro_paralysis,
    "irm_cro_contradiction":   _board_risk_contradiction,
    "cro_canary_contradiction": _board_risk_contradiction,
    "canary_red_positions_open": _board_risk_contradiction,
    "sector_coverage_gap":     _board_agent_health,
    "suspicious_mass_retirement": _board_agent_health,
    "pip_during_cro_high":     _board_agent_health,
    "loss_no_attribution":     _board_loss_action,
    "loss_attribution_no_fixes": _board_loss_action,
    "ml_below_random":         _board_ml_suspend,
    "ml_auc_mediocre":         _board_ml_suspend,
    "email_buffer_overflow":   _board_email_overflow,
    "agent_communication_silence": _board_communication_silence,
    "wiring_contract_violation": _board_wiring_fix,
    "cit_backlog_bloat":         _board_backlog_bloat,
}


def _normalize_rule(rule: str) -> str:
    """Fold dynamic, instance-suffixed rule names onto their canonical handler key.

    The Logic Team emits per-instance rules like 'idle_pod_pod3_ml',
    'pod_value_mismatch_fi', 'fe_pipeline_stale', 'fi_never_run', 'fe_zero_picks'.
    Without this mapping those never match a handler — the bug that left the
    implementation log empty.
    """
    if not rule:
        return rule
    if rule in RESOLUTION_HANDLERS:
        return rule
    if rule.startswith("idle_pod_"):
        return "idle_pod"
    if rule.startswith("pod_value_mismatch_"):
        return "pod_value_mismatch"
    for pod in ("fi", "fe"):
        if rule == f"{pod}_pipeline_stale":
            return "pod_pipeline_stale"
        if rule == f"{pod}_never_run":
            return "pod_never_run"
        if rule == f"{pod}_zero_picks":
            return "pod_zero_picks"
    return rule


# =============================================================================
# MAIN TRIAGE LOOP
# =============================================================================

def run_triage() -> dict:
    logger.info("=" * 55)
    logger.info("  LT IMPLEMENTATION AGENT — TRIAGE")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    tracker = _load(LT_RESOLUTIONS, {"open": {}})
    open_findings = tracker.get("open", {})

    if not open_findings:
        logger.info("  No open Logic Team findings — nothing to triage ✓")
        return {"triaged": 0, "actions_taken": 0}

    logger.info(f"  {len(open_findings)} open finding(s) to triage")

    actions_taken = 0
    results = []

    for key, entry in sorted(open_findings.items(),
                              key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW"].index(
                                  x[1].get("severity","LOW"))):
        rule     = entry.get("rule", key.split("::")[-1])
        severity = entry.get("severity","?")
        title    = entry.get("title","?")
        days_open = entry.get("days_open", 0) or (
            datetime.now() - datetime.fromisoformat(
                entry.get("first_seen", datetime.now().isoformat()))).days

        logger.info(f"  [{severity}] {title[:55]} (open {days_open}d)")

        # Make sure the entry carries its own rule so handlers can branch on it
        # (the resolution tracker stores 'rule'; fall back to the key tail).
        entry.setdefault("rule", rule)
        entry.setdefault("days_open", days_open)
        canonical = _normalize_rule(rule)
        handler = RESOLUTION_HANDLERS.get(canonical)
        if handler:
            result = handler(entry)
            logger.info(f"    → {result}")
            _log_action(f"handler:{rule}", key, result)
            results.append({"key": key, "action": rule, "result": result})
            actions_taken += 1
        else:
            # No specific handler — escalate generically if HIGH/CRITICAL and open >2 days
            if severity in ("HIGH","CRITICAL") and days_open >= 2:
                title_prop = f"[LTIA] Unresolved {severity} finding: {title[:50]}"
                submitted = _escalate_to_board(
                    title_prop,
                    f"Logic Team finding open for {days_open} days with no handler: '{title}'. "
                    f"Rule: {rule}. Times seen: {entry.get('times_seen',1)}.",
                    recommended_owner="CIT",
                    severity=severity,
                )
                result = "generic_board_escalation" if submitted else "already_in_queue"
                _log_action("generic_escalation", key, result)
                results.append({"key": key, "action": "generic_escalation", "result": result})
                actions_taken += 1

    if actions_taken:
        _post_to_channel(
            f"LT Implementation Agent triaged {len(open_findings)} finding(s), "
            f"took {actions_taken} action(s). "
            + (f"Results: " + "; ".join(f"{r['action']}→{r['result']}" for r in results[:3]))
        )

    return {"triaged": len(open_findings), "actions_taken": actions_taken, "results": results}


def print_report():
    tracker = _load(LT_RESOLUTIONS, {})
    open_f  = tracker.get("open", {})
    resolved = tracker.get("resolved", [])
    log      = _load(IMPL_LOG, {})
    actions  = log.get("actions", [])

    print(f"\n{'='*60}")
    print(f"  LT IMPLEMENTATION AGENT REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"\n  Open findings: {len(open_f)}  |  Resolved lifetime: {len(resolved)}")
    print(f"  Actions taken (last 10 runs): {len(actions)}")

    if open_f:
        print("\n  OPEN:")
        for key, e in sorted(open_f.items(),
                             key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW"].index(
                                 x[1].get("severity","LOW"))):
            days = (datetime.now() - datetime.fromisoformat(
                e.get("first_seen", datetime.now().isoformat()))).days
            print(f"    [{e.get('severity','?')}] {e.get('title','?')[:60]} ({days}d open)")

    if resolved:
        print(f"\n  RECENTLY RESOLVED (last 5):")
        for e in resolved[-5:]:
            print(f"    ✅ {e.get('title','?')[:60]}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        run_triage()
