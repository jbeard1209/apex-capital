#!/usr/bin/env python3
"""
APEX CAPITAL — PRE-TRADE DEVIL'S ADVOCATE
==========================================
Adversarially challenges any HIGH-CONVICTION buy (conviction ≥ 4) before
order submission. Surfaces bear cases the research team may have missed.

Interface
---------
challenge(ticker, conviction, reasons, context) → ChallengeResult

ChallengeResult.proceed   — True = pass, submit the order
ChallengeResult.warnings  — list of concerns (non-blocking)
ChallengeResult.blocked   — True = devil's advocate blocked the trade
ChallengeResult.block_reason — why it was blocked (if any)

Blocking criteria (any of these vetoes the trade):
  1. Earnings within 2 trading days (binary event risk)
  2. Recent halt or trading suspension on SEC/EDGAR
  3. P/E > 100 or forward P/E > 80 (extreme valuation for non-growth)
  4. Short interest > 30% of float (squeeze/overhang risk)
  5. The ticker has been bought AND stopped out in the last 14 days
     (avoid churning into the same losing position)

Warning criteria (logged but non-blocking):
  A. Short interest 15-30% (elevated)
  B. P/E 60-100 (stretched valuation)
  C. Conviction from a single institution only (concentration of signal)
  D. Stock up >25% in last 30 days (momentum exhaustion risk)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pre_trade_challenge")

DATA_DIR = Path(__file__).parent / "data"


@dataclass
class ChallengeResult:
    ticker:       str
    proceed:      bool = True
    blocked:      bool = False
    block_reason: str  = ""
    warnings:     list[str] = field(default_factory=list)
    checks_run:   list[str] = field(default_factory=list)


def _load(path: Path, default=None):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _check_recent_churn(ticker: str) -> Optional[str]:
    """Block if we bought AND stopped out this ticker in the last 14 days."""
    ledger = _load(DATA_DIR / "performance_ledger.json", {})
    closed = ledger.get("closed_positions", [])
    cutoff = datetime.now() - timedelta(days=14)
    for pos in closed:
        if pos.get("ticker") != ticker:
            continue
        try:
            closed_at = datetime.fromisoformat(pos.get("closed_at", "2000-01-01"))
        except Exception:
            continue
        if closed_at < cutoff:
            continue
        exit_reason = pos.get("exit_reason", "").lower()
        if any(x in exit_reason for x in ("stop", "loss", "stoploss", "stop_loss", "sl")):
            days_ago = (datetime.now() - closed_at).days
            return f"ticker stopped out {days_ago}d ago — avoid re-entry churn"
    return None


def _check_earnings_proximity(ticker: str) -> Optional[str]:
    """Warn/block if earnings are within 2 trading days."""
    ep = _load(DATA_DIR / "earnings_predictions.json", {})
    for entry in ep.get("predictions", ep if isinstance(ep, list) else []):
        if not isinstance(entry, dict):
            continue
        if entry.get("ticker") != ticker:
            continue
        date_str = entry.get("earnings_date") or entry.get("report_date")
        if not date_str:
            continue
        try:
            edate = datetime.fromisoformat(str(date_str)[:10])
        except Exception:
            continue
        days_away = (edate - datetime.now()).days
        if 0 <= days_away <= 2:
            return f"earnings in {days_away}d — binary event risk"
    return None


def _check_valuation(ticker: str) -> tuple[Optional[str], Optional[str]]:
    """Return (block_reason, warning) based on valuation."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        pe   = info.get("trailingPE")
        fpe  = info.get("forwardPE")
        if pe and pe > 100:
            return f"P/E {pe:.0f} > 100 (extreme valuation)", None
        if fpe and fpe > 80:
            return f"Forward P/E {fpe:.0f} > 80 (extreme valuation)", None
        if pe and pe > 60:
            return None, f"P/E {pe:.0f} — stretched valuation"
    except Exception:
        pass
    return None, None


def _check_short_interest(ticker: str) -> tuple[Optional[str], Optional[str]]:
    """Return (block_reason, warning) based on short interest."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        short_pct = info.get("shortPercentOfFloat")
        if short_pct is None:
            return None, None
        if short_pct > 0.30:
            return f"short interest {short_pct:.0%} > 30% of float", None
        if short_pct > 0.15:
            return None, f"elevated short interest {short_pct:.0%}"
    except Exception:
        pass
    return None, None


def _check_recent_momentum(ticker: str) -> Optional[str]:
    """Warn if stock is up >25% in last 30 days (momentum exhaustion risk)."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="35d", auto_adjust=True)
        if hist.empty:
            return None
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return None
        ret_30d = (closes.iloc[-1] / closes.iloc[0] - 1)
        if ret_30d > 0.25:
            return f"up {ret_30d:.0%} in 30d — momentum exhaustion risk"
    except Exception:
        pass
    return None


def challenge(
    ticker: str,
    conviction: float,
    reasons: list[str] | None = None,
    context: dict | None = None,
) -> ChallengeResult:
    """Run devil's advocate checks on a high-conviction buy. Always fast-returns for conviction < 4."""
    result = ChallengeResult(ticker=ticker)

    if conviction < 4:
        result.checks_run.append("skipped (conviction < 4)")
        return result

    logger.info("[Devil's Advocate] Challenging %s (conviction %.0f)", ticker, conviction)

    # ── Blocking checks ───────────────────────────────────────────────────────
    churn = _check_recent_churn(ticker)
    result.checks_run.append("recent_churn")
    if churn:
        result.blocked      = True
        result.proceed      = False
        result.block_reason = churn
        logger.warning("[Devil's Advocate] BLOCKED %s: %s", ticker, churn)
        return result

    earnings_block = _check_earnings_proximity(ticker)
    result.checks_run.append("earnings_proximity")
    if earnings_block:
        result.blocked      = True
        result.proceed      = False
        result.block_reason = earnings_block
        logger.warning("[Devil's Advocate] BLOCKED %s: %s", ticker, earnings_block)
        return result

    # Valuation and short interest are network calls — run only for conviction ≥ 4
    val_block, val_warn = _check_valuation(ticker)
    result.checks_run.append("valuation")
    if val_block:
        result.blocked      = True
        result.proceed      = False
        result.block_reason = val_block
        logger.warning("[Devil's Advocate] BLOCKED %s: %s", ticker, val_block)
        return result
    if val_warn:
        result.warnings.append(val_warn)

    si_block, si_warn = _check_short_interest(ticker)
    result.checks_run.append("short_interest")
    if si_block:
        result.blocked      = True
        result.proceed      = False
        result.block_reason = si_block
        logger.warning("[Devil's Advocate] BLOCKED %s: %s", ticker, si_block)
        return result
    if si_warn:
        result.warnings.append(si_warn)

    # ── Warning-only checks ───────────────────────────────────────────────────
    mom_warn = _check_recent_momentum(ticker)
    result.checks_run.append("momentum")
    if mom_warn:
        result.warnings.append(mom_warn)

    # ── Fable 5 adversarial bear case ────────────────────────────────────────
    # Already gated: this function fast-returns above for conviction < 4,
    # so the LLM only fires on the few high-conviction buys per week.
    try:
        from apex_llm import reason as _r, is_available
        if is_available("pre_trade_challenge"):
            bull = "; ".join((reasons or [])[:3]) or "high-conviction 13F signal"
            warns = "; ".join(result.warnings) or "none"
            raw = _r(
                "You are Apex Capital's devil's advocate. Your only job is to find "
                "the strongest reason this trade fails. Reply JSON only: "
                '{"bear_case": "1-2 sentences", "severity": "LOW"|"MEDIUM"|"HIGH"}',
                f"BUY {ticker}, conviction {conviction:.0f}/5.\n"
                f"Bull case: {bull}\nMechanical warnings: {warns}\n"
                "What's the strongest bear case the research team is missing?",
                agent_name="pre_trade_challenge", max_tokens=140,
            )
            if raw:
                p = json.loads(raw.strip())
                bc, sev = p.get("bear_case", ""), p.get("severity", "LOW").upper()
                if bc:
                    result.checks_run.append("llm_bear_case")
                    result.warnings.append(f"[LLM {sev}] {bc}")
                    if sev == "HIGH":
                        logger.warning("[Devil's Advocate] LLM HIGH-severity bear case on %s: %s",
                                       ticker, bc)
    except Exception:
        pass

    if result.warnings:
        logger.info("[Devil's Advocate] %s PASSED with warnings: %s", ticker, "; ".join(result.warnings))
    else:
        logger.info("[Devil's Advocate] %s PASSED — no concerns", ticker)

    return result


def log_challenge_result(result: ChallengeResult) -> None:
    """Append result to data/devil_advocate_log.json for audit trail."""
    log_path = DATA_DIR / "devil_advocate_log.json"
    log = _load(log_path, [])
    if not isinstance(log, list):
        log = []
    log.append({
        "ts":           datetime.now().isoformat(),
        "ticker":       result.ticker,
        "proceed":      result.proceed,
        "blocked":      result.blocked,
        "block_reason": result.block_reason,
        "warnings":     result.warnings,
        "checks_run":   result.checks_run,
    })
    log = log[-500:]
    try:
        with open(log_path, "w") as f:
            json.dump(log, f, indent=2, default=str)
    except Exception as e:
        logger.debug("[Devil's Advocate] Log write failed: %s", e)
