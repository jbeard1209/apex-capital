# =============================================================================
# APEX CAPITAL — PERFORMANCE LEDGER
# =============================================================================
# Tracks per-agent pick quality, outcome attribution, self-evaluations,
# and HR coaching notes that get injected at the start of each agent cycle.
#
# USAGE (from any agent)
# ──────────────────────
#   from performance_ledger import (
#       record_pick, record_outcome, record_self_evaluation,
#       get_agent_stats, write_coaching_note, get_coaching_note,
#   )
#
# DATA FILE
# ─────────
#   data/performance_ledger.json
#
# SCHEMA
# ──────
#   {
#     "picks":       [{"agent", "ticker", "conviction", "prediction_type", "ts"}],
#     "outcomes":    [{"ticker", "outcome", "pnl_pct", "agents", "ts"}],
#     "evaluations": [{"agent", "text", "ts"}],
#     "coaching":    {"AgentName": {"note": "...", "written_at": "...", "read": false}}
#   }
# =============================================================================

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("performance_ledger")

LEDGER_PATH = "data/performance_ledger.json"


# =============================================================================
# I/O HELPERS
# =============================================================================

def _load() -> dict:
    """Load ledger from disk; return empty structure on any failure."""
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH) as f:
                data = json.load(f)
            # Back-fill missing keys
            data.setdefault("picks",       [])
            data.setdefault("outcomes",    [])
            data.setdefault("evaluations", [])
            data.setdefault("coaching",    {})
            return data
        except Exception as e:
            logger.warning(f"[Ledger] Load failed: {e}")
    return {"picks": [], "outcomes": [], "evaluations": [], "coaching": {}}


def _save(ledger: dict) -> bool:
    """Write ledger to disk."""
    os.makedirs("data", exist_ok=True)
    try:
        with open(LEDGER_PATH, "w") as f:
            json.dump(ledger, f, indent=2, default=str)
        return True
    except Exception as e:
        logger.warning(f"[Ledger] Save failed: {e}")
        return False


# =============================================================================
# PICK RECORDING
# =============================================================================

def record_pick(
    agent: str,
    ticker: str,
    conviction: float,
    prediction_type: str,
) -> bool:
    """
    Record a stock pick made by an agent.

    Parameters
    ----------
    agent           : str   — agent name (e.g. "TechSectorAnalyst")
    ticker          : str   — ticker symbol (e.g. "NVDA")
    conviction      : float — conviction score (1–5 scale)
    prediction_type : str   — "BUY" | "SELL" | "TRIM" | "HOLD" | "WATCH"

    Returns True on success.
    """
    ledger = _load()
    ledger["picks"].append({
        "agent":           agent,
        "ticker":          ticker,
        "conviction":      conviction,
        "prediction_type": prediction_type,
        "acted_on":        None,   # filled by record_outcome
        "ts":              datetime.now().isoformat(),
    })
    # Keep picks list bounded (last 2000)
    if len(ledger["picks"]) > 2000:
        ledger["picks"] = ledger["picks"][-2000:]
    logger.debug(f"[Ledger] Pick recorded: {agent} → {ticker} ({prediction_type}, {conviction}★)")
    return _save(ledger)


# =============================================================================
# OUTCOME RECORDING
# =============================================================================

def record_outcome(
    ticker: str,
    outcome: str,
    pnl_pct: float,
) -> bool:
    """
    Record the outcome of a closed position and attribute it back to agents.

    Parameters
    ----------
    ticker  : str   — ticker symbol
    outcome : str   — "WIN" | "LOSS" | "BREAKEVEN"
    pnl_pct : float — realised P&L %

    Side effects: marks matching picks as acted_on=True/False based on whether
    they recommended the same direction as the trade.

    Returns True on success.
    """
    ledger = _load()

    # Find all picks for this ticker (most recent first)
    ticker_picks = [
        p for p in reversed(ledger["picks"])
        if p.get("ticker", "").upper() == ticker.upper()
        and p.get("acted_on") is None
    ]

    # Attribute outcome: picks that predicted the right direction
    attributed_agents = []
    for pick in ticker_picks:
        direction_match = (
            (pnl_pct > 0 and pick.get("prediction_type") in ("BUY",))
            or (pnl_pct < 0 and pick.get("prediction_type") in ("SELL", "TRIM"))
        )
        pick["acted_on"] = True   # this pick was acted on
        pick["outcome"]  = outcome
        pick["pnl_pct"]  = pnl_pct
        attributed_agents.append(pick.get("agent", "Unknown"))

    ledger["outcomes"].append({
        "ticker":           ticker,
        "outcome":          outcome,
        "pnl_pct":          pnl_pct,
        "agents":           attributed_agents,
        "picks_attributed": len(ticker_picks),
        "ts":               datetime.now().isoformat(),
    })

    # Keep outcomes bounded (last 1000)
    if len(ledger["outcomes"]) > 1000:
        ledger["outcomes"] = ledger["outcomes"][-1000:]

    logger.info(
        f"[Ledger] Outcome recorded: {ticker} {outcome} ({pnl_pct:+.1f}%) — "
        f"attributed to {len(attributed_agents)} agent(s)"
    )
    return _save(ledger)


# =============================================================================
# SELF-EVALUATION
# =============================================================================

def record_self_evaluation(agent: str, text: str) -> bool:
    """
    Record an agent's self-assessment for a given cycle.

    Parameters
    ----------
    agent : str — agent name
    text  : str — the evaluation text

    Returns True on success.
    """
    ledger = _load()
    ledger["evaluations"].append({
        "agent": agent,
        "text":  text,
        "ts":    datetime.now().isoformat(),
    })
    # Keep evaluations bounded (last 500)
    if len(ledger["evaluations"]) > 500:
        ledger["evaluations"] = ledger["evaluations"][-500:]
    logger.debug(f"[Ledger] Self-evaluation recorded for {agent}")
    return _save(ledger)


# =============================================================================
# AGENT STATS
# =============================================================================

def get_agent_stats(agent: str) -> dict:
    """
    Return performance statistics for a specific agent.

    Returns
    -------
    dict with keys:
      picks_made           : int
      picks_acted_on       : int
      win_rate             : float (0–1)
      avg_conviction_acted : float
      coaching_notes       : list[str]
    """
    ledger = _load()

    agent_picks = [p for p in ledger["picks"] if p.get("agent") == agent]
    acted_picks = [p for p in agent_picks if p.get("acted_on")]
    wins        = [p for p in acted_picks if p.get("outcome") == "WIN"]

    win_rate = (len(wins) / len(acted_picks)) if acted_picks else 0.0
    avg_conviction = (
        sum(p.get("conviction", 0) for p in acted_picks) / len(acted_picks)
        if acted_picks else 0.0
    )

    coaching = ledger["coaching"].get(agent, {})
    coaching_notes = [coaching["note"]] if coaching.get("note") else []

    return {
        "agent":               agent,
        "picks_made":          len(agent_picks),
        "picks_acted_on":      len(acted_picks),
        "win_rate":            round(win_rate, 3),
        "avg_conviction_acted": round(avg_conviction, 2),
        "coaching_notes":      coaching_notes,
    }


# =============================================================================
# COACHING NOTES
# =============================================================================

def write_coaching_note(agent: str, note: str) -> bool:
    """
    HR writes a coaching note for an agent.
    The note is read by the agent at the start of its next cycle.

    Parameters
    ----------
    agent : str — agent name
    note  : str — coaching text

    Returns True on success.
    """
    ledger = _load()
    ledger["coaching"][agent] = {
        "note":       note,
        "written_at": datetime.now().isoformat(),
        "read":       False,
    }
    logger.info(f"[Ledger] Coaching note written for {agent}")
    return _save(ledger)


def get_coaching_note(agent: str) -> Optional[str]:
    """
    Retrieve and mark-as-read the coaching note for an agent.

    Returns the note text if one exists and is unread, else None.
    Marks it as read so the same note is not returned twice.
    """
    ledger = _load()
    coaching = ledger.get("coaching", {}).get(agent)
    if not coaching or coaching.get("read"):
        return None

    note = coaching.get("note")
    if not note:
        return None

    # Mark as read
    ledger["coaching"][agent]["read"] = True
    ledger["coaching"][agent]["read_at"] = datetime.now().isoformat()
    _save(ledger)

    logger.debug(f"[Ledger] Coaching note read by {agent}")
    return note


# =============================================================================
# UTILITY — SUMMARY FOR HR / CIT
# =============================================================================

def get_all_agent_stats() -> dict:
    """
    Return stats for every agent that has at least one pick recorded.
    Useful for HR and CIT Director context.
    """
    ledger = _load()
    agents = {p["agent"] for p in ledger["picks"] if p.get("agent")}
    return {agent: get_agent_stats(agent) for agent in sorted(agents)}


def get_recent_outcomes(n: int = 20) -> list:
    """Return the last n outcome records."""
    ledger = _load()
    return ledger["outcomes"][-n:]
