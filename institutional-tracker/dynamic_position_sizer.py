#!/usr/bin/env python3
"""
APEX CAPITAL — DYNAMIC POSITION SIZER
=======================================
Converts ML probability scores (ml_score from predictor.py) into
recommended position sizes using a half-Kelly criterion.

Why Kelly?
----------
The Kelly criterion (f* = (p*b - q) / b) maximises long-run geometric
growth of capital by accounting for both the probability of winning and
the expected payoff ratio. We use *half-Kelly* (f = f*/2) as a safety
margin against model mis-calibration — academically this is the standard
approach for strategies with uncertain edge estimates.

Guardrails enforced:
  - Hard cap at MAX_POSITION_SIZE_PCT from config.py (7%)
  - Portfolio-level MIN_CASH_FLOOR_PCT enforced (20% cash reserved)
  - Regime-adaptive max from config.py REGIME_MAP
  - Positions with ml_score < 0.5 → size 0 (no negative-edge trades)
  - Total new buys limited to available deployment headroom

Input
-----
  data/predictive_signals.json   — signals from predictor.py (have ml_score)
  data/positions.json            — current portfolio positions
  data/institution_performance.json — win-rate calibration (used for b estimate)
  data/portfolio_summary.json    — total portfolio value (from performance_ledger)

Output
------
  data/position_sizes.json       — recommended sizes with Kelly rationale
  STDOUT + logs/position_sizer.log

Run
---
  python3 dynamic_position_sizer.py
  python3 dynamic_position_sizer.py --regime HIGH  # override regime
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Paths & imports
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

SIGNALS_PATH      = DATA_DIR / "predictive_signals.json"
FALLBACK_SIGNALS  = DATA_DIR / "signals.json"
POSITIONS_PATH    = DATA_DIR / "positions.json"
INST_PERF_PATH    = DATA_DIR / "institution_performance.json"
PORTFOLIO_PATH    = DATA_DIR / "portfolio_summary.json"
OUTPUT_PATH       = DATA_DIR / "position_sizes.json"
DB_PATH           = DATA_DIR / "historical_13f.db"

sys.path.insert(0, str(BASE_DIR))

try:
    from config import (
        MAX_POSITION_SIZE_PCT, MIN_CASH_FLOOR_PCT, MAX_OPEN_POSITIONS,
        STOP_LOSS_PCT, TAKE_PROFIT_PCT, REGIME_MAP,
    )
except ImportError:
    MAX_POSITION_SIZE_PCT = 0.07
    MIN_CASH_FLOOR_PCT    = 0.20
    MAX_OPEN_POSITIONS    = 15
    STOP_LOSS_PCT         = 0.07
    TAKE_PROFIT_PCT       = 0.20
    REGIME_MAP: dict      = {}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "position_sizer.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("dynamic_position_sizer")

# ---------------------------------------------------------------------------
# Historical payoff estimation (fallback when no institutional data)
# ---------------------------------------------------------------------------
# Estimated from a broad basket of institutional 13F picks (academic literature):
#   Winning picks avg +8% in 20d, losing picks avg -5% in 20d.
#   b = 8/5 = 1.6
DEFAULT_WIN_PAYOUT  = 0.08    # 8% average gain on winning 20d picks
DEFAULT_LOSS_PAYOUT = 0.05    # 5% average loss on losing 20d picks
KELLY_FRACTION      = 0.5     # half-Kelly


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> object:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def _load_signals() -> list[dict]:
    """Load signals from predictive_signals.json (preferred) or signals.json fallback."""
    data = _load_json(SIGNALS_PATH)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("signals", [])

    data = _load_json(FALLBACK_SIGNALS)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("signals", [])
    return []


def _load_portfolio_value() -> float:
    """Return current portfolio value in USD. Falls back to starting capital."""
    data = _load_json(PORTFOLIO_PATH)
    if isinstance(data, dict):
        return float(data.get("portfolio_value", data.get("total_value", 100_000.0)))
    return 100_000.0


def _load_current_positions() -> dict[str, float]:
    """Return {ticker: weight_pct} for currently held positions."""
    data = _load_json(POSITIONS_PATH)
    if not isinstance(data, list):
        return {}
    result: dict[str, float] = {}
    for pos in data:
        if isinstance(pos, dict):
            ticker = pos.get("symbol", pos.get("ticker", ""))
            weight = float(pos.get("market_value_pct", pos.get("weight", 0.0)))
            if ticker:
                result[ticker] = weight
    return result


def _load_payoff_estimates() -> tuple[float, float]:
    """
    Load win/loss payout estimates from institution_performance.json if available.
    Falls back to defaults from academic literature.
    """
    data = _load_json(INST_PERF_PATH)
    if not isinstance(data, dict):
        return DEFAULT_WIN_PAYOUT, DEFAULT_LOSS_PAYOUT

    overall = data.get("overall", {})
    n = overall.get("n", 0)
    if n < 30:
        return DEFAULT_WIN_PAYOUT, DEFAULT_LOSS_PAYOUT

    # We don't have win/loss mean separated, so approximate from Sharpe-like ratio
    mean_ret = overall.get("mean_ret_20d", DEFAULT_WIN_PAYOUT * 0.8)
    std_ret  = overall.get("std_ret_20d",  DEFAULT_LOSS_PAYOUT * 1.5)
    win_rate = overall.get("win_rate",     0.5)

    if win_rate > 0 and (1 - win_rate) > 0:
        # Approximate: mean_ret = p * win_payout - q * loss_payout
        # Treat win_payout ≈ std_ret * 1.2 (positive tail), loss_payout ≈ std_ret * 0.8
        win_payout  = max(abs(mean_ret / win_rate), DEFAULT_WIN_PAYOUT * 0.5)
        loss_payout = max(abs((mean_ret - win_rate * win_payout) / -(1 - win_rate)), DEFAULT_LOSS_PAYOUT * 0.5)
        return float(win_payout), float(loss_payout)

    return DEFAULT_WIN_PAYOUT, DEFAULT_LOSS_PAYOUT


def _get_regime_cap(regime: Optional[str]) -> float:
    """Return regime-specific max position cap."""
    if not regime or not REGIME_MAP:
        return MAX_POSITION_SIZE_PCT
    regime_cfg = REGIME_MAP.get(regime.upper(), {})
    return float(regime_cfg.get("max_position_pct", MAX_POSITION_SIZE_PCT))


# ---------------------------------------------------------------------------
# Half-Kelly sizing
# ---------------------------------------------------------------------------

def half_kelly(
    p: float,
    win_payout: float,
    loss_payout: float,
    cap: float,
) -> float:
    """
    Half-Kelly position size as fraction of portfolio.

    f* = (p*b - q) / b, where:
      p = probability of win (ml_score)
      q = 1 - p
      b = win_payout / loss_payout (payout ratio)

    Half-Kelly: f = f* * 0.5
    Capped at 'cap'.
    """
    if p <= 0 or p >= 1:
        return 0.0
    q = 1.0 - p
    b = win_payout / loss_payout if loss_payout > 0 else 1.0
    f_star = (p * b - q) / b
    f      = f_star * KELLY_FRACTION

    if f <= 0:
        return 0.0

    return min(f, cap)


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def run(regime: Optional[str] = None) -> list[dict]:
    log.info("=" * 70)
    log.info("Dynamic Position Sizer starting — %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 70)

    # ------------------------------------------------------------------
    # 1. Load inputs
    # ------------------------------------------------------------------
    signals   = _load_signals()
    portfolio = _load_portfolio_value()
    positions = _load_current_positions()
    win_pay, loss_pay = _load_payoff_estimates()

    regime_cap = _get_regime_cap(regime)

    log.info(
        "Portfolio: $%.0f | Open positions: %d | Regime cap: %.0f%% | "
        "Payoffs: win=%.1f%% loss=%.1f%% | Regime: %s",
        portfolio, len(positions), regime_cap * 100,
        win_pay * 100, loss_pay * 100,
        regime or "unknown",
    )

    if not signals:
        log.warning("No signals found in %s or %s — nothing to size.", SIGNALS_PATH, FALLBACK_SIGNALS)
        return []

    # ------------------------------------------------------------------
    # 2. Portfolio constraints
    # ------------------------------------------------------------------
    current_deployed_pct = sum(positions.values())
    cash_pct             = 1.0 - current_deployed_pct
    deployable_pct       = max(0.0, cash_pct - MIN_CASH_FLOOR_PCT)
    open_slots           = max(0, MAX_OPEN_POSITIONS - len(positions))

    log.info(
        "Deployed: %.1f%% | Cash: %.1f%% | Deployable: %.1f%% | Open slots: %d",
        current_deployed_pct * 100, cash_pct * 100, deployable_pct * 100, open_slots,
    )

    if open_slots == 0:
        log.info("No open position slots — all sizing will be 0.")

    # ------------------------------------------------------------------
    # 3. Compute Kelly size for each signal
    # ------------------------------------------------------------------
    results: list[dict] = []

    for sig in signals:
        if not isinstance(sig, dict):
            continue

        ticker   = sig.get("ticker", "")
        ml_score = float(sig.get("ml_score", 0.5))
        conv     = float(sig.get("ml_enhanced_conviction", sig.get("conviction_score", 0.0)))
        inst     = sig.get("institution", "unknown")

        # Already held — show current weight, no new buy
        if ticker in positions:
            results.append({
                "ticker":           ticker,
                "action":           "HOLD",
                "ml_score":         round(ml_score, 4),
                "conviction":       round(conv, 2),
                "current_weight":   round(positions[ticker], 4),
                "kelly_size":       None,
                "dollar_size":      None,
                "rationale":        "Already in portfolio",
                "institution":      inst,
            })
            continue

        # Low-edge signal — skip
        if ml_score < 0.50:
            results.append({
                "ticker":      ticker,
                "action":      "SKIP",
                "ml_score":    round(ml_score, 4),
                "conviction":  round(conv, 2),
                "kelly_size":  0.0,
                "dollar_size": 0.0,
                "rationale":   f"ml_score={ml_score:.3f} < 0.50 (no positive edge)",
                "institution": inst,
            })
            continue

        # Compute raw Kelly size
        kelly_raw = half_kelly(ml_score, win_pay, loss_pay, cap=regime_cap)

        # Apply deployment constraint — can't exceed remaining deployable
        kelly_capped = min(kelly_raw, deployable_pct, regime_cap)

        # No slots → size 0
        if open_slots == 0:
            kelly_capped = 0.0

        dollar_size = portfolio * kelly_capped

        # Rationale string
        b = win_pay / loss_pay if loss_pay > 0 else 1.0
        f_star = (ml_score * b - (1 - ml_score)) / b
        rationale = (
            f"p={ml_score:.3f} b={b:.2f} → f*={f_star:.3f} × {KELLY_FRACTION} = "
            f"{f_star * KELLY_FRACTION:.3f}, capped at {kelly_capped:.3f}"
        )

        action = "BUY" if kelly_capped > 0 else "SKIP"

        results.append({
            "ticker":       ticker,
            "action":       action,
            "ml_score":     round(ml_score, 4),
            "conviction":   round(conv, 2),
            "kelly_raw":    round(kelly_raw, 4),
            "kelly_size":   round(kelly_capped, 4),
            "dollar_size":  round(dollar_size, 2),
            "pct_portfolio": round(kelly_capped * 100, 2),
            "rationale":    rationale,
            "institution":  inst,
        })

        if kelly_capped > 0:
            deployable_pct -= kelly_capped
            open_slots     -= 1

        log.info(
            "  %-8s  ml_score=%.3f  kelly=%.1f%%  $%.0f  [%s]  %s",
            ticker, ml_score, kelly_capped * 100, dollar_size,
            action, inst,
        )

    # ------------------------------------------------------------------
    # 4. Sort: BUY first (descending kelly), then HOLD, then SKIP
    # ------------------------------------------------------------------
    order = {"BUY": 0, "HOLD": 1, "SKIP": 2}
    results.sort(key=lambda x: (order.get(x["action"], 9), -(x.get("kelly_size") or 0)))

    # ------------------------------------------------------------------
    # 5. Summary
    # ------------------------------------------------------------------
    buys   = [r for r in results if r["action"] == "BUY"]
    skips  = [r for r in results if r["action"] == "SKIP"]
    holds  = [r for r in results if r["action"] == "HOLD"]

    total_new_deployment = sum(r["kelly_size"] for r in buys)

    log.info("")
    log.info("Summary:")
    log.info("  BUY signals:   %d  (%.1f%% of portfolio  =  $%.0f)",
             len(buys), total_new_deployment * 100, total_new_deployment * portfolio)
    log.info("  HOLD positions:%d", len(holds))
    log.info("  SKIP signals:  %d", len(skips))

    # ── Fable 5 sizing sanity check — gated to runs with BUY signals ──────
    llm_sizing_note = None
    if buys:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("position_sizer"):
                buy_txt = "; ".join(
                    f"{b['ticker']} {b['pct_portfolio']:.1f}% (p={b['ml_score']:.2f})"
                    for b in buys[:5])
                llm_sizing_note = _r(
                    "You are a risk-sizing reviewer for a half-Kelly position sizer.",
                    f"Regime {regime or '?'} (cap {regime_cap:.0%}) | "
                    f"Total new deployment {total_new_deployment:.1%} of portfolio\n"
                    f"Buys: {buy_txt}\n"
                    "2 sentences: are these sizes prudent given the regime, and is any "
                    "single position oversized relative to its model confidence?",
                    agent_name="position_sizer", max_tokens=110,
                )
                if llm_sizing_note:
                    log.info("LLM sizing review: %s", llm_sizing_note.strip()[:100])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 6. Save output
    # ------------------------------------------------------------------
    output = {
        **({"llm_sizing_note": llm_sizing_note.strip()} if llm_sizing_note else {}),
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "portfolio_value":    portfolio,
        "regime":             regime,
        "win_payout_est":     round(win_pay, 5),
        "loss_payout_est":    round(loss_pay, 5),
        "kelly_fraction":     KELLY_FRACTION,
        "regime_cap":         regime_cap,
        "total_new_buy_pct":  round(total_new_deployment, 4),
        "total_new_buy_usd":  round(total_new_deployment * portfolio, 2),
        "signals":            results,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
    log.info("Position sizes written → %s", OUTPUT_PATH)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apex Capital — Dynamic Position Sizer")
    p.add_argument(
        "--regime", choices=["GREEN", "ELEVATED", "HIGH", "CRITICAL", "UNKNOWN"],
        help="Override market regime (default: reads from latest CRO state)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Try to load regime from CRO state file if not passed
    regime = args.regime
    if not regime:
        cro_path = DATA_DIR / "cro_state.json"
        cro_data = _load_json(cro_path) if cro_path.exists() else None
        if isinstance(cro_data, dict):
            regime = cro_data.get("risk_level", cro_data.get("regime"))

    run(regime=regime)
