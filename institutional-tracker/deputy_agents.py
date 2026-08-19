# =============================================================================
# APEX CAPITAL — DEPUTY AGENTS
# =============================================================================
# Lightweight deputy versions of key board members that handle only
# time-critical real-world actions while their principal is in a board meeting.
#
# USAGE
# ─────
#   from deputy_agents import activate_deputy_if_needed, DeputyManager, DeputyCoS
#
#   # In main.py at pipeline start:
#   if activate_deputy_if_needed("Manager Agent"):
#       DeputyManager().run()
#       sys.exit(0)   # or return — skip full pipeline
#
# WHAT DEPUTIES DO
# ────────────────
#   DeputyManager:
#     • Enforces stop-loss rules via alpaca_trader.enforce_risk_rules()
#     • Executes any pending IMMEDIATE board actions from the last meeting record
#     • Does NOT make new buy decisions
#
#   DeputyCoS:
#     • Logs that board meeting is active
#     • Defers routine work
#     • Monitors for CRITICAL canary alerts and escalates if needed
#
# STATE FILE
# ──────────
#   data/board_meeting_active.json  — written by board_meeting.run_board_meeting()
#                                     at start; deleted at end.
#   {"active": true, "started_at": "<ISO timestamp>"}
# =============================================================================

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("deputy_agents")

BOARD_ACTIVE_PATH = "data/board_meeting_active.json"


# =============================================================================
# STATE CHECK
# =============================================================================

def is_board_meeting_active() -> bool:
    """
    Returns True if a board meeting is currently in progress.
    Reads data/board_meeting_active.json written by run_board_meeting().
    Considers meetings stale if they started more than 3 hours ago
    (guards against a crash that left the file behind).
    """
    if not os.path.exists(BOARD_ACTIVE_PATH):
        return False
    try:
        with open(BOARD_ACTIVE_PATH) as f:
            data = json.load(f)
        if not data.get("active"):
            return False
        # Stale guard — board meetings should not take more than 3 hours
        started_at = data.get("started_at", "")
        if started_at:
            dt = datetime.fromisoformat(started_at)
            age_h = (datetime.now() - dt).total_seconds() / 3600
            if age_h > 3:
                logger.warning(
                    f"  [Deputy] board_meeting_active.json is {age_h:.1f}h old — "
                    "treating as stale, ignoring deputy mode."
                )
                return False
        return True
    except Exception:
        return False


# =============================================================================
# DEPUTY MANAGER
# =============================================================================

class DeputyManager:
    """
    Handles stop-loss enforcement and pending IMMEDIATE board actions
    while the Manager Agent's principal is in a board meeting.
    Does NOT make new buy decisions.
    """

    name = "Deputy Manager Agent"

    def run(self) -> dict:
        logger.info("  [DeputyManager] Activated — board meeting in progress.")
        logger.info("  [DeputyManager] Scope: stop-loss enforcement + pending IMMEDIATE board actions only.")
        logger.info("  [DeputyManager] NO new buy decisions will be made.")

        results = {
            "activated_at":    datetime.now().isoformat(),
            "stop_loss_result": None,
            "board_actions":    [],
            "errors":           [],
        }

        # ── 1. Enforce stop-loss rules ────────────────────────────────────
        try:
            from alpaca_trader import AlpacaTrader
            trader = AlpacaTrader()
            sl_result = trader.enforce_risk_rules(
                entry_dates={},   # will be loaded from state inside enforce_risk_rules if supported
                exclude_tickers=set(),
            )
            results["stop_loss_result"] = sl_result
            logger.info(f"  [DeputyManager] Stop-loss enforcement complete: {sl_result}")
        except Exception as e:
            err = f"Stop-loss enforcement error: {e}"
            logger.warning(f"  [DeputyManager] {err}")
            results["errors"].append(err)

        # ── 2. Execute pending IMMEDIATE board actions from last meeting ──
        try:
            meeting_path = "data/board_meeting_report.json"
            if os.path.exists(meeting_path):
                with open(meeting_path) as f:
                    meeting_record = json.load(f)

                consensus = meeting_record.get("consensus", {})
                immediate_actions = [
                    a for a in consensus.get("actions", [])
                    if a.get("proceed")
                    and a.get("urgency") == "IMMEDIATE"
                    and a.get("executor") == "Manager Agent"
                ]

                if immediate_actions:
                    logger.info(
                        f"  [DeputyManager] {len(immediate_actions)} pending IMMEDIATE "
                        f"board action(s) found — executing..."
                    )
                    try:
                        from alpaca_trader import AlpacaTrader as _AT
                        _trader = _AT()
                        for action in immediate_actions:
                            aid = action.get("id", "")
                            # Parse action ID: VERB_TICKER
                            parts = aid.upper().split("_")
                            if len(parts) >= 2:
                                verb   = parts[0]
                                ticker = parts[1]
                                try:
                                    if verb in ("CLOSE", "SELL"):
                                        order = _trader.submit_sell(ticker, reason="deputy_board_action")
                                        results["board_actions"].append({
                                            "id": aid, "status": "executed", "order": str(order)
                                        })
                                        logger.info(f"  [DeputyManager] Executed board action: {aid}")
                                    else:
                                        results["board_actions"].append({
                                            "id": aid, "status": "skipped",
                                            "reason": f"Deputy only executes SELL/CLOSE, not {verb}"
                                        })
                                except Exception as _ae:
                                    err = f"Board action {aid} failed: {_ae}"
                                    logger.warning(f"  [DeputyManager] {err}")
                                    results["errors"].append(err)
                                    results["board_actions"].append({
                                        "id": aid, "status": "error", "error": str(_ae)
                                    })
                    except Exception as _tr_e:
                        err = f"Board action execution failed: {_tr_e}"
                        logger.warning(f"  [DeputyManager] {err}")
                        results["errors"].append(err)
                else:
                    logger.info("  [DeputyManager] No pending IMMEDIATE board actions found.")
            else:
                logger.info("  [DeputyManager] No board meeting record found — skipping board actions.")
        except Exception as e:
            err = f"Board action loading error: {e}"
            logger.warning(f"  [DeputyManager] {err}")
            results["errors"].append(err)

        logger.info(
            f"  [DeputyManager] Run complete. "
            f"Errors: {len(results['errors'])} | Board actions: {len(results['board_actions'])}"
        )
        return results


# =============================================================================
# DEPUTY CHIEF OF STAFF
# =============================================================================

class DeputyCoS:
    """
    Logs that board meeting is active, defers routine work,
    and monitors for CRITICAL canary alerts.
    """

    name = "Deputy Chief of Staff"

    def run(self) -> dict:
        logger.info("  [DeputyCoS] Activated — board meeting in progress.")
        logger.info("  [DeputyCoS] Routine Chief of Staff work deferred.")
        logger.info("  [DeputyCoS] Monitoring for CRITICAL canary alerts only.")

        results = {
            "activated_at":   datetime.now().isoformat(),
            "canary_level":   None,
            "escalated":      False,
        }

        # ── Check for CRITICAL canary alerts ─────────────────────────────
        try:
            alert_path = "data/canary_alert.json"
            if os.path.exists(alert_path):
                with open(alert_path) as f:
                    alert = json.load(f)
                level = alert.get("level", "GREEN")
                results["canary_level"] = level
                if alert.get("active") and level == "RED":
                    logger.warning(
                        "  [DeputyCoS] CRITICAL canary alert detected during board meeting! "
                        "Escalating to DeputyManager."
                    )
                    DeputyManager().run()
                    results["escalated"] = True
                else:
                    logger.info(f"  [DeputyCoS] Canary level: {level} — no escalation needed.")
        except Exception as e:
            logger.warning(f"  [DeputyCoS] Canary check error: {e}")

        logger.info("  [DeputyCoS] Deferred work will resume after board meeting ends.")
        return results


class DeputyCRO:
    """
    Deputy Chief Risk Officer.
    While the CRO is in a board meeting: enforces stop-losses, updates the
    risk bulletin with current VaR, and flags any new CRITICAL-level position
    breaches so the CRO can act immediately after the meeting.
    """

    name = "Deputy CRO"

    def run(self) -> dict:
        logger.info("  [DeputyCRO] Activated — board meeting in progress.")
        results = {"activated_at": datetime.now().isoformat(), "actions": []}

        try:
            from alpaca_trader import AlpacaTrader
            trader = AlpacaTrader()

            # Enforce stop-losses
            sl_result = trader.enforce_risk_rules()
            results["actions"].append(f"Stop-loss enforcement: {sl_result}")
            logger.info(f"  [DeputyCRO] Stop-loss check complete: {sl_result}")

            # Snapshot current concentration.
            #
            # `weight_pct` is not a column get_positions() builds — it never
            # was. `row.get("weight_pct", 0)` therefore returned 0 for every
            # position, `0 > 10` is never true, and the Deputy CRO's
            # concentration alert could not fire. Weight has to be computed
            # against the account, so if the account read fails there is no
            # denominator and the honest answer is "not checked", not "0%".
            positions = trader.get_positions()
            portfolio_value = float(trader.get_account().get("portfolio_value", 0) or 0)
            if positions.empty or portfolio_value <= 0:
                if not positions.empty:
                    logger.warning("  [DeputyCRO] No portfolio value — "
                                   "concentration NOT checked for %d position(s)",
                                   len(positions))
                    results["actions"].append(
                        "Concentration NOT checked — no portfolio value")
            else:
                new_breaches = []
                for _, row in positions.iterrows():
                    w = abs(float(row["market_value"])) / portfolio_value * 100
                    if w > 10:
                        new_breaches.append(f"{row['ticker']} {w:.1f}%")
                if new_breaches:
                    breach_note = f"Concentration alert: {', '.join(new_breaches)}"
                    results["actions"].append(breach_note)
                    logger.warning(f"  [DeputyCRO] {breach_note}")
                    try:
                        from firm_channel import post as _fc_post
                        _fc_post("Deputy CRO", "RISK_ALERT", breach_note, priority="HIGH")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"  [DeputyCRO] {e}")

        return results


class DeputyCITDirector:
    """
    Deputy CIT Director.
    While the CIT Director is in a board meeting: runs the code inspector only
    (lightweight), monitors firm channel for URGENT items, defers full analysis.
    """

    name = "Deputy CIT Director"

    def run(self) -> dict:
        logger.info("  [DeputyCIT] Activated — board meeting in progress.")
        logger.info("  [DeputyCIT] Running lightweight code inspection only.")
        results = {"activated_at": datetime.now().isoformat(), "inspections": []}

        try:
            from cit_director import CITCodeInspector
            inspector = CITCodeInspector()
            findings = inspector.analyze()
            critical = [f for f in findings if f.get("severity") == "HIGH"]
            results["inspections"] = findings[:5]
            if critical:
                logger.warning(f"  [DeputyCIT] {len(critical)} HIGH severity code issues found.")
                try:
                    from firm_channel import post as _fc_post
                    _fc_post(
                        "Deputy CIT Director", "CODE_ALERT",
                        f"Code inspection during board meeting: {len(critical)} HIGH severity "
                        f"issue(s) found. Full CIT session will follow.",
                        priority="HIGH",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"  [DeputyCIT] Code inspection error: {e}")

        return results


class DeputyRDLeader:
    """
    Deputy R&D Team Leader.
    While the R&D Leader is in a board meeting: maintains current research
    assignments (no new sector scans), runs the IMI international desk check,
    and defers the full briefing cycle.
    """

    name = "Deputy R&D Team Leader"

    def run(self) -> dict:
        logger.info("  [DeputyRD] Activated — board meeting in progress.")
        logger.info("  [DeputyRD] Maintaining assignments. Running IMI desk check.")
        results = {"activated_at": datetime.now().isoformat(), "intl_sentiment": None}

        try:
            import international_analyst
            intl = international_analyst.run()
            sentiment = intl.get("sentiment", "NEUTRAL")
            results["intl_sentiment"] = sentiment
            logger.info(f"  [DeputyRD] IMI check complete. Sentiment: {sentiment}")
            if intl.get("sentiment_score", 0) <= -2:
                logger.warning("  [DeputyRD] RISK_OFF global signal — flagging for R&D Leader post-meeting.")
                try:
                    from firm_channel import post as _fc_post
                    _fc_post(
                        "Deputy R&D Leader", "INTL_MARKETS",
                        f"RISK_OFF signal detected during board meeting (score {intl.get('sentiment_score',0):+d}). "
                        f"R&D Leader will review post-meeting.",
                        priority="HIGH",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"  [DeputyRD] IMI check error: {e}")

        return results


class DeputyOperationsDirector:
    """
    Deputy Operations Director.
    While the Ops Director is in a board meeting: checks for urgent messages
    from supervised agents and flags anything critical to the board queue.
    """

    name = "Deputy Operations Director"

    def run(self) -> dict:
        logger.info("  [DeputyOpsDir] Activated — board meeting in progress.")
        results = {"activated_at": datetime.now().isoformat(), "flags": []}

        try:
            from operations_director import build_floor_brief
            brief = build_floor_brief()
            results["summary"] = brief.get("summary", "")
            results["flags"]   = brief.get("flags", [])
            if brief["flags"]:
                logger.warning(f"  [DeputyOpsDir] {len(brief['flags'])} floor flag(s) during board meeting.")
                try:
                    from firm_channel import post as _fc_post
                    _fc_post(
                        "Deputy Operations Director", "FLOOR_BRIEF",
                        f"Floor check during board meeting: {brief['summary']}",
                        priority="HIGH" if len(brief["flags"]) > 2 else "NORMAL",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"  [DeputyOpsDir] Floor brief error: {e}")
            results["error"] = str(e)

        return results


class DeputyIRM:
    """
    Deputy Independent Risk Manager.
    While the IRM is in a board meeting: runs a lightweight daily check,
    monitors for governance breaches, and flags any sealed findings that
    need urgent Chairman attention.
    The IRM's governance independence is preserved — deputy does not report
    to the board or firm channel; output goes only to the Chairman.
    """

    name = "Deputy IRM"

    def run(self) -> dict:
        logger.info("  [DeputyIRM] Activated — board meeting in progress.")
        logger.info("  [DeputyIRM] Maintaining IRM independence — no firm channel posting.")
        results = {"activated_at": datetime.now().isoformat(), "flags": []}

        try:
            from irm_review_agent import run_irm_daily
            irm_result = run_irm_daily(send_email=False)
            score = irm_result.get("risk_score", 0) if isinstance(irm_result, dict) else 0
            results["risk_score"] = score
            logger.info(f"  [DeputyIRM] Lightweight IRM check complete. Risk score: {score}/10")
            if score >= 8:
                logger.warning(f"  [DeputyIRM] Risk score {score}/10 — flagging for IRM principal post-meeting.")
                try:
                    from email_sender import send_alert_email
                    send_alert_email(
                        "🔒 IRM Deputy Alert — High Risk Score During Board Meeting",
                        f"IRM deputy detected risk score {score}/10 during board meeting. "
                        f"IRM principal should review immediately after meeting ends."
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"  [DeputyIRM] Lightweight check error: {e}")
            results["error"] = str(e)

        return results


# =============================================================================
# TOP-LEVEL ACTIVATION HELPER
# =============================================================================

def activate_deputy_if_needed(agent_name: str) -> bool:
    """
    Check if a board meeting is active and activate deputy mode if so.

    Parameters
    ----------
    agent_name : str — name of the calling agent (e.g. "Manager Agent")

    Returns True if board meeting is active (caller should run deputy mode).
    Returns False if normal operation should proceed.

    Side effects when True:
      • Logs "{agent_name} deputy activated — board meeting in progress"
      • Posts to firm channel
    """
    if not is_board_meeting_active():
        return False

    logger.info(f"  [Deputy] {agent_name} deputy activated — board meeting in progress")

    # Post to firm channel
    try:
        from firm_channel import post as _fc_post
        _fc_post(
            agent_name,
            "PIPELINE_ALERT",
            f"{agent_name} deputy activated — board meeting in progress. "
            "Routine work deferred. Stop-loss enforcement and CRITICAL alerts only.",
            priority="HIGH",
        )
    except Exception as _fc_e:
        logger.debug(f"  [Deputy] Firm channel post failed (non-fatal): {_fc_e}")

    return True


# =============================================================================
# Entry point (for testing)
# =============================================================================

if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        active = is_board_meeting_active()
        print(f"Board meeting active: {active}")
    elif "--deputy-manager" in sys.argv:
        result = DeputyManager().run()
        print(json.dumps(result, indent=2, default=str))
    elif "--deputy-cos" in sys.argv:
        result = DeputyCoS().run()
        print(json.dumps(result, indent=2, default=str))
    elif "--deputy-cro" in sys.argv:
        result = DeputyCRO().run()
        print(json.dumps(result, indent=2, default=str))
    elif "--deputy-cit" in sys.argv:
        result = DeputyCITDirector().run()
        print(json.dumps(result, indent=2, default=str))
    elif "--deputy-rd" in sys.argv:
        result = DeputyRDLeader().run()
        print(json.dumps(result, indent=2, default=str))
    elif "--deputy-ops" in sys.argv:
        result = DeputyOperationsDirector().run()
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Usage: python3 deputy_agents.py [--check | --deputy-manager | --deputy-cos | --deputy-cro | --deputy-cit | --deputy-rd | --deputy-ops]")
