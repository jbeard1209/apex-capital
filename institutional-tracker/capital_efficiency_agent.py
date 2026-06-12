# =============================================================================
# APEX CAPITAL — PORTFOLIO DEPLOYMENT OFFICER (PDO)
# =============================================================================
# The Portfolio Deployment Officer monitors whether available capital is being
# put to work at an optimal level given:
#   - Current portfolio value and uninvested cash
#   - Market regime (bull / bear / neutral — SPY vs 200MA)
#   - Market volatility (VIX proxy from recent SPY daily returns)
#   - Number of open positions vs maximum allowed
#   - Quality of current investment opportunities (from energy research + review)
#
# Reports:
#   DEPLOY_MORE   — significant uninvested cash with favourable market conditions
#   NEAR_OPTIMAL  — deployment within acceptable range; minor adjustments only
#   REDUCE_EXPOSURE — overextended; raise cash for risk management
#   HOLD_CASH     — poor market conditions; cash is the correct position
#
# Output: data/capital_efficiency_report.json
# The Manager Agent reads this to calibrate approval thresholds.
#
# Usage:
#   python3 capital_efficiency_agent.py            # analyse + print + save
#   python3 capital_efficiency_agent.py --quiet    # save only
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

from config import (
    MAX_OPEN_POSITIONS, MAX_POSITION_SIZE_PCT,
    STARTING_CAPITAL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("capital_efficiency_agent")

OUTPUT_PATH = "data/capital_efficiency_report.json"

# Deployment thresholds
OPTIMAL_DEPLOY_MIN_PCT  = 0.55   # below this is under-deployed
OPTIMAL_DEPLOY_MAX_PCT  = 0.90   # above this risks over-exposure
BEAR_DEPLOY_TARGET_PCT  = 0.35   # target deployment in bear regime
NEUTRAL_DEPLOY_TARGET   = 0.55   # target in neutral regime
BULL_DEPLOY_TARGET      = 0.75   # target in bull regime


# =============================================================================
# MARKET CONDITIONS
# =============================================================================

def assess_market_conditions() -> dict:
    """
    Determine market regime and volatility from SPY data.
    Returns a structured conditions dict.
    """
    try:
        spy = yf.download("SPY", period="300d", progress=False, auto_adjust=True)
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)

        close   = spy["Close"]
        price   = float(close.iloc[-1])
        ma50    = float(close.iloc[-50:].mean())  if len(close) >= 50  else float(close.mean())
        ma200   = float(close.iloc[-200:].mean()) if len(close) >= 200 else ma50

        # Regime
        if price >= ma50 and price >= ma200:
            regime = "bull"
        elif price < ma50 and price < ma200:
            regime = "bear"
        else:
            regime = "neutral"

        pct_vs_200ma = (price - ma200) / ma200 * 100

        # Volatility proxy — 20-day realised vol annualised
        returns   = close.pct_change().dropna()
        vol_20d   = float(returns.iloc[-20:].std() * (252 ** 0.5) * 100) if len(returns) >= 20 else 20.0
        vix_proxy = round(vol_20d, 1)

        # Momentum
        ret_1m  = float((close.iloc[-1] / close.iloc[-21]  - 1) * 100) if len(close) >= 21  else 0.0
        ret_3m  = float((close.iloc[-1] / close.iloc[-63]  - 1) * 100) if len(close) >= 63  else 0.0
        ret_ytd_idx = max(0, len(close) - int(datetime.now().timetuple().tm_yday * 252 / 365))
        ret_ytd = float((close.iloc[-1] / close.iloc[ret_ytd_idx] - 1) * 100)

        # Breadth signal — is SPY making new highs?
        high_52w = float(close.iloc[-252:].max()) if len(close) >= 252 else float(close.max())
        near_high = price >= high_52w * 0.95

        return {
            "spy_price":       round(price,       2),
            "spy_ma50":        round(ma50,         2),
            "spy_ma200":       round(ma200,        2),
            "regime":          regime,
            "pct_vs_200ma":    round(pct_vs_200ma, 2),
            "vix_proxy":       vix_proxy,
            "volatility_state": "low" if vix_proxy < 15 else "elevated" if vix_proxy < 25 else "high",
            "ret_1m_pct":      round(ret_1m,  2),
            "ret_3m_pct":      round(ret_3m,  2),
            "ret_ytd_pct":     round(ret_ytd, 2),
            "near_52w_high":   near_high,
        }

    except Exception as e:
        logger.warning(f"Market conditions fetch failed: {e}")
        return {
            "spy_price": None, "spy_ma50": None, "spy_ma200": None,
            "regime": "unknown", "pct_vs_200ma": 0.0,
            "vix_proxy": 20.0, "volatility_state": "elevated",
            "ret_1m_pct": 0.0, "ret_3m_pct": 0.0, "ret_ytd_pct": 0.0,
            "near_52w_high": False,
        }


# =============================================================================
# OPPORTUNITY QUALITY CHECK
# =============================================================================

def count_available_opportunities() -> int:
    """
    Count how many high-conviction picks are waiting from the research team.
    Used to calibrate whether cash is idle due to bad conditions vs. no ideas.
    """
    count = 0
    for path in ("data/energy_research_report.json", "data/review_recommendations.json"):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            recs = data.get("recommendations", [])
            count += sum(
                1 for r in recs
                if r.get("action") in ("BUY_NEW", "ADD") and r.get("conviction", 0) >= 3
            )
        except Exception:
            pass
    return count


# =============================================================================
# CORE ANALYSIS
# =============================================================================

def _load_cro_bulletin() -> dict:
    """
    Load the CRO's public risk bulletin.
    The PDO reads this before generating its recommendation so that
    capital-deployment signals are always risk-aware.
    Board mandate (Special Session): PDO signal ceiling is capped by CRO risk level.
    """
    path = "data/risk_bulletin.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


# CRO → PDO signal ceiling map (board-approved)
_CRO_PDO_CEILING = {
    "CRITICAL": "HOLD_CASH",     # CRO CRITICAL → PDO can output at most HOLD_CASH
    "HIGH":     "NEAR_OPTIMAL",  # CRO HIGH     → PDO can output at most NEAR_OPTIMAL
    "ELEVATED": "DEPLOY_MORE",   # CRO ELEVATED → PDO unrestricted but logs advisory
    "MODERATE": "DEPLOY_MORE",   # CRO MODERATE → no restriction
    "LOW":      "DEPLOY_MORE",   # CRO LOW      → no restriction
    "UNKNOWN":  "DEPLOY_MORE",   # no bulletin  → no restriction (fail-open for ops)
}

_SIGNAL_RANK = {
    "DEPLOY_MORE":      3,
    "NEAR_OPTIMAL":     2,
    "REDUCE_EXPOSURE":  1,
    "HOLD_CASH":        0,
}


def run_analysis(account: Optional[dict] = None) -> dict:
    """
    Full capital efficiency analysis.
    If account is not provided, fetches directly from Alpaca.

    Board mandate (Special Session): CRO risk bulletin is loaded at the start
    of every analysis. If CRO risk level constrains the PDO signal, the original
    market-derived signal is preserved in reasoning["uncapped_signal"] so the
    Manager Agent can see both the constrained and unconstrained recommendation.
    """
    if account is None:
        from alpaca_trader import AlpacaTrader
        trader  = AlpacaTrader()
        account = trader.get_account()
        positions_df = trader.get_positions()
        n_positions  = len(positions_df) if not positions_df.empty else 0
        account["n_positions"] = n_positions

    portfolio_value = account["portfolio_value"]
    cash            = account["cash"]
    buying_power    = account["buying_power"]
    n_positions     = account.get("n_positions", 0)
    invested        = portfolio_value - cash

    # ── Load CRO bulletin FIRST — board mandate ────────────────────────────
    cro_bulletin  = _load_cro_bulletin()
    cro_risk      = cro_bulletin.get("risk_level", "UNKNOWN")
    cro_directives = cro_bulletin.get("directives", [])
    cro_generated  = cro_bulletin.get("generated_at", "unknown time")
    pdo_ceiling    = _CRO_PDO_CEILING.get(cro_risk, "DEPLOY_MORE")

    if cro_risk not in ("UNKNOWN",):
        logger.info(
            f"  [PDO] CRO bulletin loaded: risk={cro_risk} | "
            f"PDO ceiling={pdo_ceiling} | generated={cro_generated}"
        )

    # Ratios
    deploy_ratio    = invested / portfolio_value if portfolio_value > 0 else 0
    cash_ratio      = cash     / portfolio_value if portfolio_value > 0 else 1
    capacity_used   = n_positions / MAX_OPEN_POSITIONS

    # Market conditions
    conditions      = assess_market_conditions()
    regime          = conditions["regime"]
    vix_proxy       = conditions["vix_proxy"]

    # Target deployment for current regime
    if regime == "bull":
        target_pct = BULL_DEPLOY_TARGET
    elif regime == "bear":
        target_pct = BEAR_DEPLOY_TARGET
    else:
        target_pct = NEUTRAL_DEPLOY_TARGET

    # Adjust for high volatility
    if vix_proxy > 25:
        target_pct = max(BEAR_DEPLOY_TARGET, target_pct - 0.15)
    elif vix_proxy > 18:
        target_pct = max(BEAR_DEPLOY_TARGET, target_pct - 0.05)

    # Apply CRO risk-level deployment target compression
    if cro_risk == "HIGH":
        target_pct = min(target_pct, NEUTRAL_DEPLOY_TARGET)   # cap at 55%
    elif cro_risk == "CRITICAL":
        target_pct = min(target_pct, BEAR_DEPLOY_TARGET)      # cap at 35%

    target_invested  = portfolio_value * target_pct
    delta_to_target  = target_invested - invested   # +ve = need to deploy more
    available_slots  = MAX_OPEN_POSITIONS - n_positions
    opportunities    = count_available_opportunities()

    # ── Recommendation ─────────────────────────────────────────────────────
    reasoning: list[str] = []

    if regime == "bear" and vix_proxy > 20:
        recommendation = "HOLD_CASH"
        reasoning.append(
            f"Bear market regime (SPY {conditions['pct_vs_200ma']:+.1f}% vs 200MA) with "
            f"elevated volatility (VIX proxy {vix_proxy:.0f}%). Holding cash is the "
            f"risk-appropriate position. Target deployment: {target_pct*100:.0f}%."
        )

    elif deploy_ratio > OPTIMAL_DEPLOY_MAX_PCT:
        recommendation = "REDUCE_EXPOSURE"
        reduce_by = invested - (portfolio_value * OPTIMAL_DEPLOY_MAX_PCT)
        reasoning.append(
            f"Portfolio {deploy_ratio*100:.1f}% deployed — above {OPTIMAL_DEPLOY_MAX_PCT*100:.0f}% "
            f"safe maximum. Recommend reducing exposure by ~${reduce_by:,.0f} to rebuild "
            f"cash buffer for opportunistic re-entry or downside protection."
        )

    elif deploy_ratio < OPTIMAL_DEPLOY_MIN_PCT and opportunities == 0:
        recommendation = "NEAR_OPTIMAL"
        reasoning.append(
            f"Portfolio only {deploy_ratio*100:.1f}% deployed, but no high-conviction "
            f"opportunities are currently identified by the research team. Cash is correctly "
            f"idle — do not force deployment into low-quality ideas."
        )

    elif deploy_ratio < target_pct - 0.10 and opportunities > 0:
        recommendation = "DEPLOY_MORE"
        reasoning.append(
            f"Portfolio {deploy_ratio*100:.1f}% deployed vs {target_pct*100:.0f}% target "
            f"for current {regime} regime. ${delta_to_target:,.0f} of uninvested capital "
            f"available. {opportunities} high-conviction opportunity(s) identified across "
            f"the research team. {available_slots} position slot(s) open."
        )

    elif abs(deploy_ratio - target_pct) <= 0.10:
        recommendation = "NEAR_OPTIMAL"
        reasoning.append(
            f"Portfolio {deploy_ratio*100:.1f}% deployed — within ±10% of {target_pct*100:.0f}% "
            f"target for {regime} regime. No significant rebalancing required."
        )

    else:
        recommendation = "NEAR_OPTIMAL"
        reasoning.append(
            f"Portfolio {deploy_ratio*100:.1f}% deployed. Market regime: {regime}. "
            f"Target: {target_pct*100:.0f}%. Minor drift from target — monitor but no "
            f"immediate action required."
        )

    # ── CRO CEILING ENFORCEMENT — board mandate ────────────────────────────
    # Apply the CRO risk-level cap to the market-derived recommendation.
    # The uncapped signal is preserved in reasoning so the Manager Agent and
    # Chairman can see what PDO would have said without the risk constraint.
    uncapped_signal = recommendation
    cro_constrained = False

    if _SIGNAL_RANK.get(recommendation, 2) > _SIGNAL_RANK.get(pdo_ceiling, 2):
        cro_constrained  = True
        recommendation   = pdo_ceiling
        reasoning.append(
            f"[CRO CEILING APPLIED] Market metrics alone would signal {uncapped_signal}, "
            f"but CRO risk level ({cro_risk}) caps PDO output at {pdo_ceiling}. "
            f"The deployment opportunity is real — {opportunities} high-conviction pick(s) "
            f"exist — but the risk environment does not support aggressive deployment. "
            + (f"CRO directive: {cro_directives[0]}" if cro_directives else "")
        )
        logger.warning(
            f"  [PDO] Signal capped by CRO: {uncapped_signal} → {recommendation} "
            f"(CRO risk={cro_risk})"
        )
    elif cro_risk == "ELEVATED" and recommendation == "DEPLOY_MORE":
        reasoning.append(
            f"[CRO ADVISORY — ELEVATED] Deployment recommended but CRO flags elevated risk. "
            f"Proceed with tightened position sizing and high-conviction filter (4★+ only). "
            + (f"CRO: {cro_directives[0]}" if cro_directives else "")
        )

    # Manager directives — concrete instructions for the Manager Agent
    manager_directives: list[str] = []

    if recommendation == "DEPLOY_MORE":
        per_position = portfolio_value * MAX_POSITION_SIZE_PCT
        max_new_positions = min(available_slots, max(1, int(delta_to_target / per_position)))
        manager_directives.append(
            f"Approve up to {max_new_positions} new position(s) at standard sizing "
            f"(${per_position:,.0f} each) to close the deployment gap."
        )
        manager_directives.append(
            f"Prioritise high-conviction (4★+) picks from the Energy Research Team "
            f"and long-term horizon recommendations."
        )
        if buying_power < per_position:
            manager_directives.append(
                f"WARNING: Buying power (${buying_power:,.0f}) is below one full position "
                f"size (${per_position:,.0f}). Consider partial sizing or waiting for "
                f"an existing position to close."
            )

    elif recommendation == "REDUCE_EXPOSURE":
        reduce_by = invested - (portfolio_value * OPTIMAL_DEPLOY_MAX_PCT)
        manager_directives.append(
            f"Trim or close weakest-performing position(s) to free up ~${reduce_by:,.0f}."
        )
        manager_directives.append(
            "Prioritise exiting positions with negative momentum or thesis deterioration."
        )

    elif recommendation == "HOLD_CASH":
        manager_directives.append(
            "Reject all new BUY_NEW and ADD recommendations until regime improves."
        )
        manager_directives.append(
            "Enforce stop-loss rules aggressively to reduce risk further."
        )

    # ── Fable 5 deployment sanity check (one call/run, signal-changes only) ─
    llm_view = None
    try:
        from apex_llm import reason as _r, is_available
        if is_available("capital_efficiency") and (cro_constrained or recommendation != "NEAR_OPTIMAL"):
            raw = _r(
                "You are Apex Capital's Portfolio Deployment Officer reviewing a capital signal.",
                f"Signal: {recommendation}{' (CRO-capped from ' + uncapped_signal + ')' if cro_constrained else ''} | "
                f"Deployed {deploy_ratio*100:.0f}% | Cash ${cash:,.0f} | "
                f"CRO {cro_risk} | {opportunities} high-conviction picks available\n"
                "2 sentences: is this signal right, and what's the single biggest "
                "risk of following it?",
                agent_name="capital_efficiency", max_tokens=110,
            )
            if raw:
                llm_view = raw.strip()
                reasoning.append(f"[LLM PDO] {llm_view}")
    except Exception:
        pass

    result = {
        "generated_at":        datetime.now().isoformat(),
        "agent":               "Portfolio Deployment Officer",
        "account": {
            "portfolio_value": portfolio_value,
            "cash":            cash,
            "buying_power":    buying_power,
            "invested":        round(invested, 2),
            "n_positions":     n_positions,
        },
        "deployment": {
            "current_pct":          round(deploy_ratio * 100, 1),
            "cash_pct":             round(cash_ratio   * 100, 1),
            "target_pct":           round(target_pct   * 100, 1),
            "delta_to_target_usd":  round(delta_to_target, 2),
            "capacity_used_pct":    round(capacity_used   * 100, 1),
            "available_slots":      available_slots,
            "opportunities_found":  opportunities,
        },
        "market_conditions":   conditions,
        "recommendation":      recommendation,
        "uncapped_signal":     uncapped_signal,          # what PDO would say without CRO constraint
        "cro_risk_at_time":    cro_risk,                 # CRO risk level that influenced this signal
        "cro_constrained":     cro_constrained,          # True if CRO ceiling was applied
        "reasoning":           reasoning,
        "manager_directives":  manager_directives,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2, default=str)

    return result


# =============================================================================
# PRINT REPORT
# =============================================================================

def print_report(report: dict) -> None:
    acc   = report["account"]
    dep   = report["deployment"]
    mkt   = report["market_conditions"]
    rec   = report["recommendation"]

    rec_colors = {
        "DEPLOY_MORE":      "▲ DEPLOY MORE",
        "NEAR_OPTIMAL":     "✓ NEAR OPTIMAL",
        "REDUCE_EXPOSURE":  "▼ REDUCE EXPOSURE",
        "HOLD_CASH":        "⚑ HOLD CASH",
    }

    print(f"\n{'═'*64}")
    print("  APEX CAPITAL — PORTFOLIO DEPLOYMENT OFFICER")
    print(f"  {report['generated_at']}")
    print(f"{'═'*64}")
    print(f"\n  ACCOUNT")
    print(f"  Portfolio Value  : ${acc['portfolio_value']:>12,.2f}")
    print(f"  Invested         : ${acc['invested']:>12,.2f}  ({dep['current_pct']:.1f}%)")
    print(f"  Cash / Buying Pw : ${acc['cash']:>12,.2f}  ({dep['cash_pct']:.1f}%)")
    print(f"  Open Positions   : {acc['n_positions']} / {MAX_OPEN_POSITIONS}  "
          f"({dep['capacity_used_pct']:.0f}% capacity)")

    print(f"\n  MARKET CONDITIONS")
    print(f"  SPY Regime       : {mkt['regime'].upper()}  "
          f"({mkt['pct_vs_200ma']:+.1f}% vs 200MA)")
    print(f"  Volatility (20d) : {mkt['vix_proxy']:.1f}  [{mkt['volatility_state']}]")
    print(f"  SPY 1M / 3M      : {mkt['ret_1m_pct']:+.1f}% / {mkt['ret_3m_pct']:+.1f}%")

    print(f"\n  DEPLOYMENT TARGET : {dep['target_pct']:.0f}%  "
          f"(delta: ${dep['delta_to_target_usd']:+,.0f})")
    print(f"  Opportunities    : {dep['opportunities_found']} high-conviction pick(s) available")

    print(f"\n  RECOMMENDATION: {rec_colors.get(rec, rec)}")
    for line in report["reasoning"]:
        print(f"    {line}")

    if report["manager_directives"]:
        print(f"\n  DIRECTIVES TO MANAGER AGENT:")
        for d in report["manager_directives"]:
            print(f"    → {d}")

    print(f"{'═'*64}\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

def run_pdo(account: Optional[dict] = None, quiet: bool = False) -> dict:
    logger.info("Running Portfolio Deployment Officer analysis...")
    report = run_analysis(account)
    logger.info(
        f"  Recommendation: {report['recommendation']} | "
        f"Deployed: {report['deployment']['current_pct']:.1f}% | "
        f"Target: {report['deployment']['target_pct']:.1f}%"
    )
    if not quiet:
        print_report(report)
    return report


if __name__ == "__main__":
    quiet_flag = "--quiet" in sys.argv
    run_pdo(quiet=quiet_flag)
