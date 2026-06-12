#!/usr/bin/env python3
"""APEX CAPITAL — FE FRAUD WATCHDOG
Red-flag screen for FE picks. Filters obvious frauds and balance sheet disasters.
Returns (passed_picks, flagged_picks).
"""
from __future__ import annotations
import logging
logger = logging.getLogger("fe_fraud_watchdog")

RED_FLAGS = ["restatement", "going concern", "sec investigation", "material weakness",
             "auditor change", "fraud", "default", "bankruptcy"]

def fraud_check(ticker: str, picks: list) -> tuple[list, list]:
    """Screen picks for fraud red flags. Returns (clean, flagged)."""
    clean, flagged = [], []
    for pick in picks:
        flags = []
        # Check for known structural issues
        debt_equity = pick.get("debt_equity", 0) or 0
        if debt_equity > 15:
            flags.append(f"extreme leverage D/E={debt_equity:.1f}")
        mos = pick.get("margin_of_safety", 0) or 0
        if mos < -1.0:
            flags.append(f"extreme overvaluation MoS={mos*100:.0f}%")
        if flags:
            pick["fraud_flags"] = flags
            flagged.append(pick)
            logger.warning("[FraudWatchdog] %s flagged: %s", pick.get("ticker"), "; ".join(flags))
        else:
            clean.append(pick)

    # ── Fable 5 forensic sniff test — gated to very deep "bargains" ───────
    # A MoS > 60% on a screener often means the market knows something the
    # model doesn't. The LLM's knowledge of real accounting blowups helps here.
    try:
        from apex_llm import reason as _r, is_available
        if is_available("fe_fraud"):
            for pick in clean[:]:
                mos = pick.get("margin_of_safety", 0) or 0
                if mos < 0.60:
                    continue
                raw = _r(
                    "You are a forensic accounting analyst. Deep-value screeners often "
                    'flag "cheap" stocks the market correctly prices for distress. '
                    'Reply JSON only: {"suspicious": true|false, "reason": "one sentence"}',
                    f"{pick.get('ticker','?')} shows {mos*100:.0f}% margin of safety "
                    f"(IV {pick.get('intrinsic_value','?')} vs price {pick.get('current_price','?')}). "
                    f"D/E {pick.get('debt_equity','?')}.\n"
                    "Is this likely a value trap / distressed name the screener is misreading?",
                    agent_name="fe_fraud", max_tokens=110,
                )
                if raw:
                    import json as _j
                    try:
                        p = _j.loads(raw.strip())
                        if p.get("suspicious"):
                            pick["fraud_flags"] = [f"[LLM] {p.get('reason','suspicious deep-value setup')}"]
                            clean.remove(pick)
                            flagged.append(pick)
                            logger.warning("[FraudWatchdog LLM] %s flagged: %s",
                                           pick.get("ticker"), p.get("reason", ""))
                    except Exception:
                        pass
    except Exception:
        pass

    return clean, flagged

def run_prescreen(watchlist: list) -> list:
    """Pre-screen watchlist for obvious frauds before running agents."""
    return watchlist  # lightweight — full screen happens at pick level
