#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RESEARCH ORCHESTRATOR
==================================================
Manages the persistent research library.
Runs 6-agent deep research on one company at a time.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_QUEUE_FILE = BASE_DIR / "data" / "fe_research_queue.json"
ADR_UNIVERSE_FILE   = BASE_DIR / "data" / "adr_universe.json"
FE_WATCHLIST_FILE   = BASE_DIR / "data" / "fe_watchlist.json"

RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

RESEARCH_FRESH_DAYS = 90  # data older than this triggers refresh


def get_research_status(ticker: str) -> dict:
    """
    Check which research agents have completed for this ticker.
    Returns completion status and timestamps.
    """
    ticker = ticker.upper().strip()
    res_dir = RESEARCH_DIR / ticker

    status = {
        "ticker": ticker,
        "financial_forensics": False,
        "moat": False,
        "management": False,
        "risk": False,
        "valuation": False,
        "esg_controversy": False,
        "earnings_quality_deep": False,
        "business_model_analyzer": False,
        "activist_monitor": False,
        "supply_chain_risk": False,
        "synthesis": False,
        "complete": False,
        "last_updated": None,
        "age_days": None,
    }

    if not res_dir.exists():
        return status

    files = {
        "financial_forensics":   res_dir / "financial_forensics.json",
        "moat":                  res_dir / "moat_analysis.json",
        "management":            res_dir / "management_quality.json",
        "risk":                  res_dir / "risk_assessment.json",
        "valuation":             res_dir / "valuation_models.json",
        "esg_controversy":       res_dir / "esg_controversy.json",
        "earnings_quality_deep": res_dir / "earnings_quality_deep.json",
        "business_model_analyzer": res_dir / "business_model_analysis.json",
        "activist_monitor":      res_dir / "activist_monitor.json",
        "supply_chain_risk":     res_dir / "supply_chain_risk.json",
        "synthesis":             res_dir / "synthesis_memo.json",
    }

    newest_mtime = None
    for key, fpath in files.items():
        if fpath.exists():
            # Check file has valid content and no error
            try:
                with open(fpath) as f:
                    data = json.load(f)
                if not data.get("error"):
                    status[key] = True
                    mtime = datetime.fromtimestamp(fpath.stat().st_mtime, tz=timezone.utc)
                    if newest_mtime is None or mtime > newest_mtime:
                        newest_mtime = mtime
            except Exception:
                pass

    core_complete = all(status[k] for k in [
        "financial_forensics", "moat", "management", "risk", "valuation",
        "esg_controversy", "earnings_quality_deep", "business_model_analyzer",
        "activist_monitor", "supply_chain_risk",
    ])
    status["complete"] = core_complete and status["synthesis"]

    if newest_mtime:
        status["last_updated"] = newest_mtime.isoformat()
        status["age_days"] = (datetime.now(timezone.utc) - newest_mtime).days

    return status


def _is_stale(ticker: str) -> bool:
    """Returns True if research is missing or older than RESEARCH_FRESH_DAYS."""
    status = get_research_status(ticker)
    if not status["complete"]:
        return True
    age = status.get("age_days")
    if age is None:
        return True
    return age > RESEARCH_FRESH_DAYS


def build_research_queue() -> list:
    """
    Build prioritized queue from all FE agent watchlists + ADR universe.
    Priority: partially researched > agent nomination count > cheapness (P/B).
    """
    all_tickers = set()

    # Read FE agent watchlists
    agent_nominations = {}  # ticker -> count of agents nominating it
    agents_dir = BASE_DIR / "agents"
    agent_pattern = str(agents_dir / "cos_fe_*.py")
    agent_files = glob.glob(agent_pattern)
    # Also include main FE agent
    fe_main = agents_dir / "cos_firsteaglevalueagent.py"
    if fe_main.exists():
        agent_files.append(str(fe_main))

    for fpath in agent_files:
        try:
            with open(fpath) as f:
                content = f.read()
            # Extract _RAW_WATCHLIST tickers
            match = re.search(r"_RAW_WATCHLIST\s*=\s*\[([^\]]+)\]", content, re.DOTALL)
            if match:
                list_content = match.group(1)
                tickers_found = re.findall(r'"([A-Z0-9.\-]{1,10})"', list_content)
                for tk in tickers_found:
                    tk = tk.upper()
                    all_tickers.add(tk)
                    agent_nominations[tk] = agent_nominations.get(tk, 0) + 1
        except Exception as e:
            logger.warning(f"[Orchestrator] Could not parse agent file {fpath}: {e}")

    # Read ADR universe
    try:
        if ADR_UNIVERSE_FILE.exists():
            with open(ADR_UNIVERSE_FILE) as f:
                adr_data = json.load(f)
            if isinstance(adr_data, list):
                for item in adr_data:
                    if isinstance(item, str):
                        all_tickers.add(item.upper())
                    elif isinstance(item, dict):
                        tk = item.get("ticker", "")
                        if tk:
                            all_tickers.add(tk.upper())
    except Exception as e:
        logger.warning(f"[Orchestrator] Could not load ADR universe: {e}")

    # Read fe_watchlist.json (quality target-price watchlist)
    try:
        if FE_WATCHLIST_FILE.exists():
            with open(FE_WATCHLIST_FILE) as f:
                wl_data = json.load(f)
            wl_entries = wl_data if isinstance(wl_data, list) else wl_data.get("watchlist", [])
            added = 0
            for item in wl_entries:
                if isinstance(item, str):
                    tk = item.upper()
                elif isinstance(item, dict):
                    tk = item.get("ticker", "").upper()
                else:
                    continue
                if tk:
                    all_tickers.add(tk)
                    # Watchlist tickers get +1 nomination so they're weighted in
                    agent_nominations[tk] = agent_nominations.get(tk, 0) + 1
                    added += 1
            logger.info(f"[Orchestrator] fe_watchlist.json: added {added} tickers to universe")
    except Exception as e:
        logger.warning(f"[Orchestrator] Could not load fe_watchlist.json: {e}")

    # Build prioritized queue
    queue = []
    for ticker in all_tickers:
        status  = get_research_status(ticker)
        partial = any(status[k] for k in ["financial_forensics", "moat", "management", "risk", "valuation"])
        stale   = _is_stale(ticker)
        nominations = agent_nominations.get(ticker, 0)

        # Priority score (higher = run sooner)
        priority = 0
        if partial and not status["complete"]:
            priority += 100  # incomplete research -- finish it
        elif stale:
            priority += 50   # stale -- needs refresh
        priority += nominations * 10

        if stale or (partial and not status["complete"]):
            queue.append({
                "ticker": ticker,
                "priority": priority,
                "nominations": nominations,
                "partial_research": partial,
                "status": status,
            })

    queue.sort(key=lambda x: x["priority"], reverse=True)

    # Save queue to disk
    try:
        with open(RESEARCH_QUEUE_FILE, "w") as f:
            json.dump(queue, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"[Orchestrator] Could not save research queue: {e}")

    logger.info(f"[Orchestrator] Research queue built: {len(queue)} companies need research")
    return queue


def research_one_company(ticker: str, force_refresh: bool = False) -> dict:
    """
    Run full 6-agent research team on a single company.
    Skips agents that have already completed (unless force_refresh or data > 90 days old).
    Returns synthesis memo when done.
    """
    ticker = ticker.upper().strip()
    logger.info(f"[Orchestrator] Starting research for {ticker}")
    started_at = datetime.now(timezone.utc).isoformat()

    # Create research directory
    res_dir = RESEARCH_DIR / ticker
    res_dir.mkdir(parents=True, exist_ok=True)

    # Check current status
    status = get_research_status(ticker)
    age_days = status.get("age_days") or 999

    def _should_run(key: str) -> bool:
        if force_refresh:
            return True
        if not status[key]:
            return True  # never run
        if age_days > RESEARCH_FRESH_DAYS:
            return True  # stale
        return False

    # Track results for status file
    agent_results = {}
    errors = []

    # Import EDGAR fundamentals once for CIK lookup
    cik = None
    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        f = get_edgar_fundamentals(ticker)
        if f:
            cik = f.get("cik")
    except Exception as e:
        logger.warning(f"[Orchestrator] EDGAR fetch for CIK: {e}")

    # ── Agent 1: Financial Forensics ─────────────────────────────────────────
    if _should_run("financial_forensics"):
        logger.info(f"[Orchestrator] {ticker} -- Running financial_forensics agent...")
        try:
            from fe_research_agents.financial_forensics import run_financial_forensics
            result = run_financial_forensics(ticker)
            agent_results["financial_forensics"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("forensics_score"),
                "verdict": result.get("forensics_verdict"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} financial_forensics: "
                f"score={result.get('forensics_score')} verdict={result.get('forensics_verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} financial_forensics FAILED: {e}")
            errors.append(f"financial_forensics: {e}")
            agent_results["financial_forensics"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- financial_forensics already fresh, skipping")
        agent_results["financial_forensics"] = {"skipped": True, "reason": "already fresh"}

    # ── Agent 2: Moat Research ───────────────────────────────────────────────
    if _should_run("moat"):
        logger.info(f"[Orchestrator] {ticker} -- Running moat_researcher agent...")
        try:
            from fe_research_agents.moat_researcher import run_moat_research
            result = run_moat_research(ticker)
            agent_results["moat"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("moat_score"),
                "width": result.get("moat_width"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} moat: "
                f"width={result.get('moat_width')} score={result.get('moat_score')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} moat FAILED: {e}")
            errors.append(f"moat: {e}")
            agent_results["moat"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- moat already fresh, skipping")
        agent_results["moat"] = {"skipped": True, "reason": "already fresh"}

    # ── Agent 3: Management Research ─────────────────────────────────────────
    if _should_run("management"):
        logger.info(f"[Orchestrator] {ticker} -- Running management_researcher agent...")
        try:
            from fe_research_agents.management_researcher import run_management_research
            result = run_management_research(ticker, cik=cik)
            agent_results["management"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("management_score"),
                "verdict": result.get("management_verdict"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} management: "
                f"score={result.get('management_score')} verdict={result.get('management_verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} management FAILED: {e}")
            errors.append(f"management: {e}")
            agent_results["management"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- management already fresh, skipping")
        agent_results["management"] = {"skipped": True, "reason": "already fresh"}

    # ── Agent 4: Risk Research ───────────────────────────────────────────────
    if _should_run("risk"):
        logger.info(f"[Orchestrator] {ticker} -- Running risk_researcher agent...")
        try:
            from fe_research_agents.risk_researcher import run_risk_research
            result = run_risk_research(ticker, cik=cik)
            agent_results["risk"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("risk_score"),
                "level": result.get("risk_level"),
                "going_concern": result.get("going_concern_flag"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} risk: "
                f"score={result.get('risk_score')} level={result.get('risk_level')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} risk FAILED: {e}")
            errors.append(f"risk: {e}")
            agent_results["risk"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- risk already fresh, skipping")
        agent_results["risk"] = {"skipped": True, "reason": "already fresh"}

    # ── Agent 5: Valuation Research ──────────────────────────────────────────
    if _should_run("valuation"):
        logger.info(f"[Orchestrator] {ticker} -- Running valuation_researcher agent...")
        try:
            from fe_research_agents.valuation_researcher import run_valuation_research
            result = run_valuation_research(ticker)
            agent_results["valuation"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "mos": result.get("margin_of_safety"),
                "verdict": result.get("verdict"),
                "conservative_iv": result.get("conservative_iv"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} valuation: "
                f"MoS={result.get('margin_of_safety')} verdict={result.get('verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} valuation FAILED: {e}")
            errors.append(f"valuation: {e}")
            agent_results["valuation"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- valuation already fresh, skipping")
        agent_results["valuation"] = {"skipped": True, "reason": "already fresh"}

    # -- Agent 6: ESG Controversy -------------------------------------------
    if _should_run("esg_controversy"):
        logger.info(f"[Orchestrator] {ticker} -- Running esg_controversy agent...")
        try:
            from fe_research_agents.esg_controversy import run_esg_controversy
            result = run_esg_controversy(ticker, cik=cik)
            agent_results["esg_controversy"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("esg_score"),
                "verdict": result.get("esg_verdict"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} esg_controversy: "
                f"score={result.get('esg_score')} verdict={result.get('esg_verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} esg_controversy FAILED: {e}")
            errors.append(f"esg_controversy: {e}")
            agent_results["esg_controversy"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- esg_controversy already fresh, skipping")
        agent_results["esg_controversy"] = {"skipped": True, "reason": "already fresh"}

    # -- Agent 7: Earnings Quality Deep ------------------------------------
    if _should_run("earnings_quality_deep"):
        logger.info(f"[Orchestrator] {ticker} -- Running earnings_quality_deep agent...")
        try:
            from fe_research_agents.earnings_quality_deep import run_earnings_quality_deep
            result = run_earnings_quality_deep(ticker)
            agent_results["earnings_quality_deep"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("earnings_quality_score"),
                "verdict": result.get("earnings_quality_verdict"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} earnings_quality_deep: "
                f"score={result.get('earnings_quality_score')} verdict={result.get('earnings_quality_verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} earnings_quality_deep FAILED: {e}")
            errors.append(f"earnings_quality_deep: {e}")
            agent_results["earnings_quality_deep"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- earnings_quality_deep already fresh, skipping")
        agent_results["earnings_quality_deep"] = {"skipped": True, "reason": "already fresh"}

    # -- Agent 8: Business Model Analyzer ----------------------------------
    if _should_run("business_model_analyzer"):
        logger.info(f"[Orchestrator] {ticker} -- Running business_model_analyzer agent...")
        try:
            from fe_research_agents.business_model_analyzer import run_business_model_analysis
            result = run_business_model_analysis(ticker)
            agent_results["business_model_analyzer"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("business_model_score"),
                "verdict": result.get("business_model_verdict"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} business_model_analyzer: "
                f"score={result.get('business_model_score')} verdict={result.get('business_model_verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} business_model_analyzer FAILED: {e}")
            errors.append(f"business_model_analyzer: {e}")
            agent_results["business_model_analyzer"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- business_model_analyzer already fresh, skipping")
        agent_results["business_model_analyzer"] = {"skipped": True, "reason": "already fresh"}

    # -- Agent 9: Activist Investor Monitor --------------------------------
    if _should_run("activist_monitor"):
        logger.info(f"[Orchestrator] {ticker} -- Running activist_monitor agent...")
        try:
            from fe_research_agents.activist_investor_monitor import run_activist_monitor
            result = run_activist_monitor(ticker, cik=cik)
            agent_results["activist_monitor"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "signal": result.get("signal"),
                "activist_present": result.get("activist_present"),
                "value_investor_present": result.get("value_investor_present"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} activist_monitor: "
                f"signal={result.get('signal')} activist={result.get('activist_present')} "
                f"value_investor={result.get('value_investor_present')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} activist_monitor FAILED: {e}")
            errors.append(f"activist_monitor: {e}")
            agent_results["activist_monitor"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- activist_monitor already fresh, skipping")
        agent_results["activist_monitor"] = {"skipped": True, "reason": "already fresh"}

    # -- Agent 10: Supply Chain Risk ---------------------------------------
    if _should_run("supply_chain_risk"):
        logger.info(f"[Orchestrator] {ticker} -- Running supply_chain_risk agent...")
        try:
            from fe_research_agents.supply_chain_risk import run_supply_chain_risk
            result = run_supply_chain_risk(ticker, cik=cik)
            agent_results["supply_chain_risk"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "score": result.get("supply_chain_risk_score"),
                "verdict": result.get("supply_chain_verdict"),
                "error": result.get("error"),
            }
            logger.info(
                f"[Orchestrator] {ticker} supply_chain_risk: "
                f"score={result.get('supply_chain_risk_score')} verdict={result.get('supply_chain_verdict')}"
            )
        except Exception as e:
            logger.error(f"[Orchestrator] {ticker} supply_chain_risk FAILED: {e}")
            errors.append(f"supply_chain_risk: {e}")
            agent_results["supply_chain_risk"] = {"error": str(e)}
    else:
        logger.info(f"[Orchestrator] {ticker} -- supply_chain_risk already fresh, skipping")
        agent_results["supply_chain_risk"] = {"skipped": True, "reason": "already fresh"}

    # ── Agent 11 (was 6): Synthesis ─────────────────────────────────────────────
    # Always run synthesis to synthesize whatever is available
    logger.info(f"[Orchestrator] {ticker} -- Running synthesis agent...")
    try:
        from fe_research_agents.synthesis_agent import run_synthesis
        synthesis = run_synthesis(ticker)
        agent_results["synthesis"] = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "verdict": synthesis.get("overall_verdict"),
            "conviction": synthesis.get("conviction_level"),
            "ready_for_board": synthesis.get("ready_for_board"),
            "error": synthesis.get("error"),
        }
        logger.info(
            f"[Orchestrator] {ticker} synthesis: "
            f"verdict={synthesis.get('overall_verdict')} "
            f"conviction={synthesis.get('conviction_level')} "
            f"ready_for_board={synthesis.get('ready_for_board')}"
        )
    except Exception as e:
        logger.error(f"[Orchestrator] {ticker} synthesis FAILED: {e}")
        errors.append(f"synthesis: {e}")
        agent_results["synthesis"] = {"error": str(e)}
        synthesis = {}

    # ── Write research_status.json ────────────────────────────────────────────
    status_after = get_research_status(ticker)
    status_record = {
        "ticker": ticker,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "agents": agent_results,
        "status": status_after,
        "errors": errors,
    }

    status_file = res_dir / "research_status.json"
    try:
        with open(status_file, "w") as f:
            json.dump(status_record, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"[Orchestrator] Could not write status file: {e}")

    logger.info(
        f"[Orchestrator] {ticker} research complete. "
        f"Complete={status_after['complete']} | Errors={len(errors)}"
    )
    return synthesis


def run_scheduler(companies_per_run: int = 3):
    # ── Inbox: process pending requests, meetings, broadcasts ──────────────────
    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("FEResearchOrchestrator")
    except Exception:
        pass  # inbox is always non-fatal

    """
    Called by cron. Picks the top N companies from the queue and runs deep
    research on each sequentially.  Default is 3 per cron invocation.

    With two daily cron slots (2am + 8pm UTC) this yields 6 companies per day.
    """
    logger.info(
        f"[Orchestrator Scheduler] Starting -- building research queue "
        f"(companies_per_run={companies_per_run})"
    )

    queue = build_research_queue()

    if not queue:
        logger.info("[Orchestrator Scheduler] Queue is empty -- all companies have fresh research")
        return

    batch = queue[:companies_per_run]
    logger.info(
        f"[Orchestrator Scheduler] Queue has {len(queue)} companies. "
        f"Running top {len(batch)}: {[b['ticker'] for b in batch]}"
    )

    last_result = {}
    for i, target in enumerate(batch, start=1):
        ticker = target["ticker"]
        logger.info(
            f"[Orchestrator Scheduler] [{i}/{len(batch)}] Starting {ticker} "
            f"(priority={target['priority']}, nominations={target['nominations']}, "
            f"partial={target['partial_research']})"
        )

        result = research_one_company(ticker)
        last_result = result

        verdict = result.get("overall_verdict", "unknown")
        ready   = result.get("ready_for_board", False)
        mos     = result.get("margin_of_safety")
        if mos is not None:
            logger.info(
                f"[Orchestrator Scheduler] [{i}/{len(batch)}] {ticker} done. "
                f"verdict={verdict} | ready_for_board={ready} | MoS={mos*100:.1f}%"
            )
        else:
            logger.info(
                f"[Orchestrator Scheduler] [{i}/{len(batch)}] {ticker} done. "
                f"verdict={verdict} | ready_for_board={ready}"
            )

    remaining = max(0, len(queue) - len(batch))
    logger.info(
        f"[Orchestrator Scheduler] Batch complete. "
        f"Ran {len(batch)} companies. Remaining in queue: {remaining}"
    )
    return last_result


if __name__ == "__main__":
    import sys as _sys_io
    if "--inbox-only" in _sys_io.argv or "--thread-id" in _sys_io.argv:
        import logging as _log_io
        _log_io.basicConfig(level=_log_io.INFO,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%H:%M:%S")
        from agent_inbox import main_inbox_only
        main_inbox_only("FEResearchOrchestrator")
        _sys_io.exit(0)
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if len(sys.argv) > 1:
        ticker = sys.argv[1]
        force  = "--force" in sys.argv
        r = research_one_company(ticker, force_refresh=force)
        print(json.dumps(r, indent=2, default=str))
    else:
        run_scheduler()
