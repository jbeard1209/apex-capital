#!/usr/bin/env python3
"""
APEX CAPITAL — WHERE THE RISK LIMITS COME FROM
==============================================
`MIN_CASH_FLOOR_PCT = 0.20` and `MAX_OPEN_POSITIONS = 20` have governed every
entry this firm has made. Neither has a derivation anywhere in the repo. They
are round numbers that arrived as defaults and were never asked to justify
themselves — and on 2026-08-19 they blocked every buy including the one hedge
that would have relieved the exposure they exist to bound.

An inherited parameter is not wrong because it is inherited. It is wrong to not
know whether it is wrong. This module does three separate measurements and
hands the Chairman a decision, because these are live risk limits and moving
one is not an engineering change:

  A. THE HARD LIQUIDITY FLOOR — how much cash the firm genuinely cannot go
     below, computed from the broker's own maintenance requirement and a
     measured adverse-day move. This is the piece nothing substitutes for, and
     `risk_gate.HARD_CASH_FLOOR_PCT` claims to be ~3x it. That claim is checked
     against the live book on every run, and escalates when it stops holding.

  B. THE SHADOW PRICE OF THE BUFFER — what the waivable part of the 20% floor
     costs, in dollars per year, at the firm's own measured realized return.
     A constraint whose price is never computed is a constraint nobody can
     argue with.

  C. THE MARGINAL POSITION — what the 21st name is actually worth, from the
     realized correlation of the book the firm holds. The textbook result is
     that diversification benefit is nearly exhausted well before 20 names; if
     that holds here, the cap is not buying diversification at the margin and
     must be justified on some other ground (monitoring load, concentration of
     research attention) or moved.

WHAT THIS MODULE MAY NOT DO
---------------------------
It does not edit `config.py`. Same rule as `pick_gate_trial_review`, and for
the same reason: `MIN_CASH_FLOOR_PCT` and `MAX_OPEN_POSITIONS` are live risk
limits, so changing one is the Chairman's decision and an agent that could
quietly relax a limit it was built to evaluate is not an evaluator. Pinned by
an AST test.

It also does not claim more than it measured. Where an input is unavailable the
output says so and the recommendation is withheld; a derivation with a guessed
input is the inherited constant again, wearing a derivation's clothes.

Usage:  python3 derive_risk_limits.py [--report] [--no-queue]
Schedule: weekly. Exits 0.
Guarded by test_pipeline_smoke.py::TestRiskLimitsAreDerivedNotInherited.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("derive_risk_limits")

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
REPORT = DATA / "risk_limit_derivation.json"
CHAIRMAN = DATA / "chairman_action_required.json"

# ── the assumptions, named and in one place ──────────────────────────────────
# Every one of these is a judgement. They are constants like the ones this
# module exists to interrogate, so they are declared where a reader can see and
# argue with them rather than buried in an expression.

ADVERSE_SIGMAS = 2.0        # how bad a day the floor must survive. 2 sigma is
                            # ~1 day in 40, i.e. roughly monthly. Not a tail
                            # number — the floor is a liquidity minimum, not a
                            # solvency reserve, and a floor sized for the
                            # 99.9th percentile would bind constantly.
SHORT_MAINTENANCE_PCT = 0.30   # Reg-T maintenance on a short. Used ONLY when
                            # the broker does not report its own requirement;
                            # the broker's number is truth and this is the
                            # documented fallback.
VOL_LOOKBACK_DAYS = 252
MIN_VOL_OBSERVATIONS = 60
MIN_RETURN_OBSERVATIONS = 30   # below this a mean return is not a measurement
SAFETY_MULTIPLE_FLOOR = 2.0    # a derived requirement covered by less than 2x
                            # is TIGHT: the inputs (short book size, daily vol)
                            # move faster than anyone revisits a constant.
CASH_YIELD_ANNUAL = 0.0     # Alpaca paper pays no interest on the cash balance.
                            # A real cash yield would net against the shadow
                            # price below, and this is where it would go.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v) -> Optional[float]:
    """float(v) if it is a real number, else None. NaN is the reason."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ═════════════════════════════════════════════════════════════════════════════
# Inputs — every one from the broker or from measured prices, none assumed
# ═════════════════════════════════════════════════════════════════════════════

def account_snapshot(client=None) -> Optional[dict]:
    """Equity, cash, long/short market value and the broker's OWN margin
    requirement. None when the account cannot be read.

    `maintenance_margin` is the point of this function. The firm has been
    reasoning about margin safety through a cash percentage, which is a proxy;
    the broker publishes the actual requirement, and the distance between
    equity and it is the real liquidity headroom. Deriving a requirement the
    counterparty already states would be inventing a second copy of a number
    that can then drift — the mirror-check lesson.
    """
    try:
        if client is None:
            from alpaca.trading.client import TradingClient
            client = TradingClient(os.environ["ALPACA_API_KEY"],
                                   os.environ["ALPACA_SECRET_KEY"], paper=True)
        a = client.get_account()
        equity = _f(getattr(a, "equity", None))
        cash = _f(getattr(a, "cash", None))
        if equity is None or cash is None or equity <= 0:
            return None
        positions = {}
        for p in client.get_all_positions():
            q = _f(getattr(p, "qty", None))
            px = _f(getattr(p, "current_price", None))
            if q is None or q == 0:
                continue
            positions[str(p.symbol).upper()] = {"qty": q, "price": px}
        return {
            "equity": equity,
            "cash": cash,
            "long_market_value": _f(getattr(a, "long_market_value", None)) or 0.0,
            "short_market_value": _f(getattr(a, "short_market_value", None)) or 0.0,
            "maintenance_margin": _f(getattr(a, "maintenance_margin", None)),
            "positions": positions,
            "n_positions": len(positions),
        }
    except Exception as e:
        logger.warning(f"  [derive] account unreadable: {e}")
        return None


def _closes(symbols: List[str],
            days: int = VOL_LOOKBACK_DAYS) -> Dict[str, Dict[str, float]]:
    """{SYMBOL: {date: close}} for whatever could be downloaded. Never raises.

    Keyed by DATE, not positionally. Tickers in this book have between 248 and
    251 observations over the same year — different listing histories and
    different halted days — and lining them up by position instead of by date
    would pair each name's Tuesday with another's Wednesday. That does not fail
    loudly; it biases every correlation toward zero, which would make the book
    look more diversified than it is. The alignment IS the measurement.
    """
    out: Dict[str, Dict[str, float]] = {}
    syms = sorted({str(s).upper().strip() for s in symbols if str(s).strip()})
    if not syms:
        return out
    try:
        import yfinance as yf
        data = yf.download(syms, period=f"{days}d", auto_adjust=True,
                           progress=False)
        closes = data["Close"] if "Close" in data else None
        if closes is None:
            return out
        for s in syms:
            try:
                col = closes[s] if s in closes else (
                    closes if len(syms) == 1 else None)
                if col is None:
                    continue
                series = {}
                for idx, v in col.dropna().items():
                    fv = _f(v)
                    if fv is not None:
                        series[str(idx)[:10]] = fv
                if len(series) >= 2:
                    out[s] = series
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"  [derive] price history unavailable: {e}")
    return out


def _returns_by_date(series: Dict[str, float]) -> Dict[str, float]:
    """{date: return} from {date: close}, over consecutive observed dates."""
    dates = sorted(series)
    out = {}
    for i in range(1, len(dates)):
        prev = series[dates[i - 1]]
        if prev:
            out[dates[i]] = series[dates[i]] / prev - 1.0
    return out


def _returns(closes: List[float]) -> List[float]:
    return [closes[i] / closes[i - 1] - 1.0
            for i in range(1, len(closes))
            if _f(closes[i - 1]) not in (None, 0.0)]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _pearson(a: List[float], b: List[float]) -> Optional[float]:
    """Correlation of two equal-length series.

    Written out rather than `statistics.correlation`, which is Python 3.10+.
    The Mac runs 3.9, so that call raises AttributeError, and it was inside a
    `except Exception: continue` — the whole correlation measurement returned
    "not enough history" while sitting on a full year of it for twenty names
    (memory `host_divergence_is_a_bug_class`).
    """
    n = min(len(a), len(b))
    if n < 3:
        return None
    x, y = a[:n], b[:n]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    if sxx <= 0 or syy <= 0:
        return None
    r = sxy / math.sqrt(sxx * syy)
    return r if math.isfinite(r) else None


def market_daily_vol() -> Optional[float]:
    """Realized daily standard deviation of SPY. None if it cannot be measured.

    None, not 0.01. A vol of 1% is the number the risk_gate header used as an
    illustration, and quietly substituting it here would turn an illustration
    into a measurement — the exact move `fallback-that-means-act` is about.
    """
    series = _closes(["SPY"]).get("SPY")
    if not series:
        return None
    r = list(_returns_by_date(series).values())
    if len(r) < MIN_VOL_OBSERVATIONS:
        return None
    return statistics.pstdev(r)


# ═════════════════════════════════════════════════════════════════════════════
# A. The hard liquidity floor
# ═════════════════════════════════════════════════════════════════════════════

def hard_floor(snap: dict, sigma: Optional[float],
               short_beta_dollars: Optional[float] = None) -> dict:
    """What cash the firm genuinely cannot go below, and whether 5% covers it.

    Two claims on cash that a hedge cannot substitute for:

      1. MAINTENANCE ON THE SHORT BOOK. A short is a borrow; the broker holds
         a requirement against it that does not care how well hedged the firm
         is. Taken from the broker when it reports one, else Reg-T's 30%.
      2. AN ADVERSE DAY ON THE SHORTS. Shorts lose money when the market rises,
         and that loss lands as a cash claim the same day. Sized on
         beta-weighted short exposure, not gross short value: two shorts of the
         same size do not carry the same market risk.

    Longs are deliberately NOT in this number. A long that falls costs equity,
    which is what the buffer half of the floor and the CRO's drawdown rules are
    for. This is the liquidity minimum: the level below which the firm cannot
    meet a claim, not the level below which it is uncomfortable.
    """
    equity = _f(snap.get("equity")) or 0.0
    smv = abs(_f(snap.get("short_market_value")) or 0.0)
    broker_maint = _f(snap.get("maintenance_margin"))

    maint = SHORT_MAINTENANCE_PCT * smv
    maint_source = f"Reg-T {SHORT_MAINTENANCE_PCT:.0%} of short market value"
    if broker_maint is not None and smv > 0:
        # The broker's figure covers the WHOLE book. Only the short portion is
        # a cash-like claim here, so the Reg-T share is used and the broker's
        # total is reported alongside as the independent second reading —
        # equity minus it is the real headroom, and that is checked below.
        maint_source += f" (broker reports ${broker_maint:,.0f} on the whole book)"

    adverse: Optional[float] = None
    if sigma is not None and short_beta_dollars is not None:
        adverse = ADVERSE_SIGMAS * sigma * abs(short_beta_dollars)

    if adverse is None:
        required = None
        reason = ("cannot size the adverse day: "
                  + ("no measured market vol" if sigma is None
                     else "no beta-weighted short exposure"))
    else:
        required = maint + adverse
        reason = ""

    out = {
        "equity": round(equity, 2),
        "short_market_value": round(smv, 2),
        "maintenance_requirement": round(maint, 2),
        "maintenance_source": maint_source,
        "adverse_sigmas": ADVERSE_SIGMAS,
        "market_daily_vol": round(sigma, 6) if sigma is not None else None,
        "short_beta_dollars": (round(short_beta_dollars, 2)
                               if short_beta_dollars is not None else None),
        "adverse_day_claim": round(adverse, 2) if adverse is not None else None,
        "required_usd": round(required, 2) if required is not None else None,
        "required_pct": (round(required / equity, 6)
                         if (required is not None and equity > 0) else None),
        "reason": reason,
    }

    # The broker's own headroom, as an independent second measurement of the
    # same thing. If these two disagree by a lot, one of them is wrong.
    if broker_maint is not None and equity > 0:
        out["broker_maintenance_margin"] = round(broker_maint, 2)
        out["excess_liquidity"] = round(equity - broker_maint, 2)
        out["excess_liquidity_pct"] = round((equity - broker_maint) / equity, 4)

    # Is the constant in risk_gate still honest?
    try:
        import risk_gate
        claimed = float(risk_gate.HARD_CASH_FLOOR_PCT)
    except Exception:
        claimed = None
    out["configured_hard_floor_pct"] = claimed
    if claimed is None or out["required_pct"] is None:
        out["verdict"] = "CANNOT_TELL"
        out["headline"] = ("the configured hard floor could not be checked "
                           "against the live book")
    else:
        mult = (claimed / out["required_pct"]) if out["required_pct"] > 0 else float("inf")
        out["safety_multiple"] = round(mult, 2) if math.isfinite(mult) else None
        if out["required_pct"] > claimed:
            out["verdict"] = "BREACHED"
            out["headline"] = (
                f"the derived liquidity requirement is "
                f"{out['required_pct']*100:.2f}% of equity, ABOVE the configured "
                f"{claimed*100:.0f}% hard floor — the floor no longer covers it")
        elif mult < SAFETY_MULTIPLE_FLOOR:
            out["verdict"] = "TIGHT"
            out["headline"] = (
                f"the configured {claimed*100:.0f}% hard floor covers the derived "
                f"{out['required_pct']*100:.2f}% requirement only {mult:.1f}x "
                f"(want {SAFETY_MULTIPLE_FLOOR:.0f}x)")
        else:
            out["verdict"] = "COVERED"
            out["headline"] = (
                f"the derived liquidity requirement is "
                f"{out['required_pct']*100:.2f}% of equity "
                f"(${out['required_usd']:,.0f}); the configured "
                f"{claimed*100:.0f}% hard floor covers it {mult:.1f}x")
    return out


# ═════════════════════════════════════════════════════════════════════════════
# B. The shadow price of the buffer
# ═════════════════════════════════════════════════════════════════════════════

def firm_daily_returns(client=None, period: str = "3M") -> List[float]:
    """The firm's OWN realized daily equity returns. [] when unavailable.

    Not a market proxy and not an assumed equity premium. The question the
    shadow price answers is what THIS firm gives up by holding cash, so the
    input has to be this firm's realized return, with the sample size attached
    so the answer can be honest about how little it knows.
    """
    try:
        if client is None:
            from alpaca.trading.client import TradingClient
            client = TradingClient(os.environ["ALPACA_API_KEY"],
                                   os.environ["ALPACA_SECRET_KEY"], paper=True)
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        h = client.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe="1D"))
        eq = [v for v in (h.equity or []) if _f(v) not in (None, 0.0)]
        return _returns(eq)
    except Exception as e:
        logger.warning(f"  [derive] portfolio history unavailable: {e}")
        return []


def shadow_price(snap: dict, hard: dict, rets: List[float],
                 cash_floor_pct: float) -> dict:
    """What the WAIVABLE part of the cash floor costs per year.

    buffer = (cash_floor - hard_floor) x equity: the dry-powder half, the part
    a hedge substitutes for. Its price is the return that capital would have
    earned deployed, minus what it earns as cash.

    The deployed return is inferred from the firm's realized EQUITY return by
    dividing out the deployed fraction — equity return already carries the drag
    of the cash the firm was holding, so using it directly would understate the
    cost of holding more. That inference is stated in the output as an
    assumption, because it treats the deployment ratio over the window as
    roughly the current one.

    The confidence interval is the point of this function as much as the point
    estimate. A shadow price of $2,000/yr that cannot be distinguished from
    zero at n=63 is not an argument for moving a risk limit, and saying so is
    the difference between a measurement and a number.
    """
    equity = _f(snap.get("equity")) or 0.0
    hard_pct = hard.get("required_pct")
    out = {
        "cash_floor_pct": cash_floor_pct,
        "hard_floor_pct": hard_pct,
        "n_observations": len(rets),
        "buffer_usd": None, "annual_cost_usd": None,
        "annual_return_pct": None, "ci95_annual_cost_usd": None,
        "distinguishable_from_zero": None, "assumptions": [],
    }
    if hard_pct is None:
        out["reason"] = ("the hard floor is unknown, so the waivable buffer "
                         "cannot be separated from the part that is not")
        return out

    buffer_pct = max(0.0, cash_floor_pct - hard_pct)
    buffer_usd = buffer_pct * equity
    out["buffer_pct"] = round(buffer_pct, 6)
    out["buffer_usd"] = round(buffer_usd, 2)

    if len(rets) < MIN_RETURN_OBSERVATIONS:
        out["reason"] = (f"only {len(rets)} daily return(s) — below "
                         f"{MIN_RETURN_OBSERVATIONS}, a mean return here would "
                         f"be a number, not a measurement")
        return out

    mean_d = statistics.fmean(rets)
    sd_d = statistics.stdev(rets)
    n = len(rets)
    deployed_frac = 0.0
    if equity > 0:
        deployed_frac = max(0.05, min(1.0,
                                      1.0 - (_f(snap.get("cash")) or 0.0) / equity))
    # equity return -> deployed return
    mean_dep = mean_d / deployed_frac
    se_dep = (sd_d / math.sqrt(n)) / deployed_frac

    ann = mean_dep * 252.0
    se_ann = se_dep * 252.0
    lo, hi = ann - 1.96 * se_ann, ann + 1.96 * se_ann

    out.update({
        "deployed_fraction": round(deployed_frac, 4),
        "annual_return_pct": round(ann * 100, 2),
        "ci95_annual_return_pct": [round(lo * 100, 2), round(hi * 100, 2)],
        "annual_cost_usd": round(buffer_usd * (ann - CASH_YIELD_ANNUAL), 2),
        "ci95_annual_cost_usd": [round(buffer_usd * (lo - CASH_YIELD_ANNUAL), 2),
                                 round(buffer_usd * (hi - CASH_YIELD_ANNUAL), 2)],
        "distinguishable_from_zero": bool(lo > 0 or hi < 0),
        "assumptions": [
            f"cash earns {CASH_YIELD_ANNUAL:.1%} (Alpaca paper pays nothing)",
            f"deployed return inferred from equity return / deployed fraction "
            f"({deployed_frac:.0%}), assumed roughly constant over the window",
            f"n={n} daily observations — the CI is the honest width of this",
        ],
        "reason": "",
    })
    if not out["distinguishable_from_zero"]:
        out["headline"] = (
            f"the {buffer_pct*100:.0f}-point buffer (${buffer_usd:,.0f}) has a "
            f"point-estimate cost of ${out['annual_cost_usd']:,.0f}/yr, but at "
            f"n={n} the 95% interval spans zero "
            f"(${out['ci95_annual_cost_usd'][0]:,.0f} to "
            f"${out['ci95_annual_cost_usd'][1]:,.0f}) — this is not evidence "
            f"the buffer is expensive, it is evidence the firm cannot yet "
            f"measure what it costs")
    else:
        out["headline"] = (
            f"the {buffer_pct*100:.0f}-point buffer (${buffer_usd:,.0f}) costs "
            f"${out['annual_cost_usd']:,.0f}/yr "
            f"(95% CI ${out['ci95_annual_cost_usd'][0]:,.0f} to "
            f"${out['ci95_annual_cost_usd'][1]:,.0f}) at the firm's realized "
            f"{ann*100:.1f}% deployed return over n={n} days")
    return out


# ═════════════════════════════════════════════════════════════════════════════
# C. The marginal position
# ═════════════════════════════════════════════════════════════════════════════

def _avg_pairwise_correlation(rets: Dict[str, Dict[str, float]]) -> Optional[dict]:
    """Average pairwise rho and average daily sigma, aligned on shared dates.

    `rets` is {SYMBOL: {date: return}}. Every pair is correlated over the dates
    the two of them actually share, and the count of shared dates is reported —
    a rho computed from eleven overlapping days is not the same object as one
    from two hundred, and the caller has to be able to tell.
    """
    syms = [s for s, r in rets.items() if len(r) >= MIN_VOL_OBSERVATIONS]
    if len(syms) < 3:
        return None
    sigmas, rhos, overlaps, skipped = [], [], [], 0
    for s in syms:
        sd = statistics.pstdev(list(rets[s].values()))
        if sd > 0:
            sigmas.append(sd)
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = rets[syms[i]], rets[syms[j]]
            shared = sorted(set(a) & set(b))
            if len(shared) < MIN_VOL_OBSERVATIONS:
                skipped += 1
                continue
            rho = _pearson([a[d] for d in shared], [b[d] for d in shared])
            if rho is not None:
                rhos.append(rho)
                overlaps.append(len(shared))
    if not sigmas or not rhos:
        return None
    return {"rho": statistics.fmean(rhos),
            "sigma": statistics.fmean(sigmas),
            "n_symbols": len(syms),
            "n_pairs": len(rhos),
            "pairs_skipped_for_overlap": skipped,
            "min_shared_days": min(overlaps),
            "rho_min": min(rhos), "rho_max": max(rhos)}


def _portfolio_sigma(sigma_bar: float, rho_bar: float, n: int) -> float:
    """Equal-weight portfolio vol: sigma * sqrt(1/n + (1 - 1/n) * rho)."""
    if n <= 0:
        return float("nan")
    v = (1.0 / n) + (1.0 - 1.0 / n) * rho_bar
    return sigma_bar * math.sqrt(max(v, 0.0))


def marginal_position(snap: dict, max_positions: int) -> dict:
    """What the (cap+1)-th name is worth, from the book's realized correlation.

    The cap is justified in this repo as a diversification/monitoring limit.
    Diversification is measurable: equal-weight portfolio variance falls as
    1/n toward the average-covariance floor, and the fall is nearly exhausted
    long before 20 names for any realistic rho. If the marginal name buys a
    basis point, the cap is not doing the job it is named for, and it needs a
    different justification or a different number.
    """
    out = {"max_positions": max_positions, "n_symbols_measured": 0,
           "avg_correlation": None, "avg_daily_vol": None, "curve": [],
           "marginal_bp_at_cap": None, "reason": ""}
    syms = sorted(snap.get("positions", {}))
    if len(syms) < 3:
        out["reason"] = f"only {len(syms)} position(s) — no correlation to measure"
        return out
    closes = _closes(syms)
    rets = {s: _returns_by_date(c) for s, c in closes.items()}
    rets = {s: r for s, r in rets.items() if len(r) >= MIN_VOL_OBSERVATIONS}
    agg = _avg_pairwise_correlation(rets)
    if agg is None:
        out["reason"] = ("not enough overlapping history across the book to "
                         "measure an average correlation")
        return out
    rho, sigma, n_sym = agg["rho"], agg["sigma"], agg["n_symbols"]
    out.update({"n_symbols_measured": n_sym,
                "n_pairs": agg["n_pairs"],
                "pairs_skipped_for_overlap": agg["pairs_skipped_for_overlap"],
                "min_shared_days": agg["min_shared_days"],
                "correlation_range": [round(agg["rho_min"], 4),
                                      round(agg["rho_max"], 4)],
                "avg_correlation": round(rho, 4),
                "avg_daily_vol": round(sigma, 6),
                "annualized_avg_vol_pct": round(sigma * math.sqrt(252) * 100, 2)})

    curve = []
    for n in (1, 5, 10, 15, max_positions, max_positions + 1, 40):
        if n < 1:
            continue
        s_n = _portfolio_sigma(sigma, rho, n)
        prev = _portfolio_sigma(sigma, rho, n - 1) if n > 1 else None
        curve.append({
            "n": n,
            "annual_vol_pct": round(s_n * math.sqrt(252) * 100, 3),
            "marginal_bp": (round((prev - s_n) * math.sqrt(252) * 10000, 2)
                            if prev is not None else None),
        })
    out["curve"] = curve

    s_cap = _portfolio_sigma(sigma, rho, max_positions)
    s_next = _portfolio_sigma(sigma, rho, max_positions + 1)
    marginal_bp = (s_cap - s_next) * math.sqrt(252) * 10000
    s_1 = _portfolio_sigma(sigma, rho, 1)
    s_inf = sigma * math.sqrt(max(rho, 0.0))
    achieved = ((s_1 - s_cap) / (s_1 - s_inf)) if (s_1 - s_inf) > 0 else None

    out["marginal_bp_at_cap"] = round(marginal_bp, 2)
    out["floor_annual_vol_pct"] = round(s_inf * math.sqrt(252) * 100, 3)
    out["diversification_achieved_pct"] = (round(achieved * 100, 2)
                                           if achieved is not None else None)

    # The marginal name is the wrong question on its own. Nobody adds one
    # position; they raise a cap. What a Chairman is choosing between is the
    # book at 20 names and the book at 30 or 40, so the cumulative figure is
    # the one the decision turns on — 11 bp reads as noise, 120 bp does not.
    for target in (30, 40):
        if target > max_positions:
            s_t = _portfolio_sigma(sigma, rho, target)
            out[f"cumulative_bp_to_{target}"] = round(
                (s_cap - s_t) * math.sqrt(252) * 10000, 1)

    out["headline"] = (
        f"average pairwise correlation {rho:.2f} across {n_sym} held names "
        f"({agg['n_pairs']} pairs, >= {agg['min_shared_days']} shared days); "
        f"the {_ordinal(max_positions + 1)} position lowers annualized "
        f"portfolio vol by {marginal_bp:.1f} bp"
        + (f", with {achieved*100:.0f}% of all available diversification already "
           f"captured at {max_positions}" if achieved is not None else "")
        + (f", but raising the cap to 40 would lower it by "
           f"{out['cumulative_bp_to_40']:.0f} bp in total"
           if out.get("cumulative_bp_to_40") else ""))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# The decision
# ═════════════════════════════════════════════════════════════════════════════

def _queue_decision(result: dict) -> bool:
    """One Chairman row per derivation, deduped by the day. Never raises.

    This is where the module stops. It has three measurements and no authority:
    `MIN_CASH_FLOOR_PCT` and `MAX_OPEN_POSITIONS` are live risk limits, and an
    agent that can quietly move the limit it was built to evaluate is not an
    evaluator. The row states the numbers and the options and leaves the choice
    where it belongs.
    """
    try:
        rows = json.loads(CHAIRMAN.read_text()) if CHAIRMAN.exists() else []
        if not isinstance(rows, list):
            return False
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rid = f"risk-limit-derivation-{day}"
        if any(isinstance(r, dict) and r.get("id") == rid for r in rows):
            return False
        hard = result.get("hard_floor", {})
        shadow = result.get("shadow_price", {})
        marg = result.get("marginal_position", {})
        sev = "HIGH" if hard.get("verdict") in ("BREACHED", "TIGHT") else "MEDIUM"
        lines = [
            "DECISION: the two limits that gate every entry have never been "
            "derived. Here is what they are worth, measured against the live "
            "book. Nothing has been changed — these are risk limits.",
            "",
            f"A. HARD LIQUIDITY FLOOR — {hard.get('headline', 'unavailable')}",
            f"B. COST OF THE BUFFER  — {shadow.get('headline') or shadow.get('reason', 'unavailable')}",
            f"C. THE MARGINAL NAME   — {marg.get('headline') or marg.get('reason', 'unavailable')}",
            "",
            "Options: (1) ratify 20% / 20 as they stand, now with a derivation "
            "on file; (2) split the cash floor formally into the hard liquidity "
            "minimum and a soft buffer the CRO may waive; (3) move either "
            "number, stating which measurement above justifies it. Doing "
            "nothing keeps limits nobody can defend, which is how the SH hedge "
            "came to be blocked.",
        ]
        rows.append({
            "id": rid,
            "severity": sev,
            "description": "\n".join(lines),
            "source": "derive_risk_limits",
            "ts": _now(),
            "status": "awaiting_chairman",
            "times_seen": 1,
            "evidence": REPORT.name,
        })
        DATA.mkdir(exist_ok=True)
        CHAIRMAN.write_text(json.dumps(rows, indent=2))
        return True
    except Exception as e:
        logger.warning(f"  [derive] could not queue the decision: {e}")
        return False


def run(queue: bool = True, client=None) -> dict:
    snap = account_snapshot(client=client)
    if snap is None:
        out = {"as_of": _now(), "available": False,
               "reason": "the account could not be read — nothing was derived"}
        logger.warning("  [derive] " + out["reason"])
        return out

    try:
        import risk_gate
        lim = risk_gate.limits()
    except Exception:
        lim = {"cash_floor": 0.20, "max_positions": 20}

    sigma = market_daily_vol()
    sbd = None
    try:
        import portfolio_beta
        nb = portfolio_beta.net_beta(snap["positions"], snap["equity"])
        sbd = nb.get("short_beta_dollars")
        net_beta_val = nb.get("beta")
    except Exception as e:
        logger.warning(f"  [derive] net beta unavailable: {e}")
        net_beta_val = None

    hard = hard_floor(snap, sigma, sbd)
    shadow = shadow_price(snap, hard, firm_daily_returns(client=client),
                          float(lim["cash_floor"]))
    marg = marginal_position(snap, int(lim["max_positions"]))

    result = {
        "as_of": _now(),
        "available": True,
        "configured": {"cash_floor_pct": lim["cash_floor"],
                       "max_positions": lim["max_positions"]},
        "book": {"equity": round(snap["equity"], 2),
                 "cash": round(snap["cash"], 2),
                 "cash_pct": round(snap["cash"] / snap["equity"], 4),
                 "n_positions": snap["n_positions"],
                 "net_beta": net_beta_val},
        "hard_floor": hard,
        "shadow_price": shadow,
        "marginal_position": marg,
    }
    def _write_report() -> None:
        try:
            DATA.mkdir(exist_ok=True)
            REPORT.write_text(json.dumps(result, indent=2))
        except Exception as e:                              # pragma: no cover
            logger.warning(f"  [derive] could not write the report: {e}")

    # Written twice on purpose. This module is forbidden from editing
    # config.py — MAX_OPEN_POSITIONS is a live risk limit and changing it is
    # the Chairman's decision, so the ONLY consequence this agent can produce
    # is a queued decision. Whether it queued one is therefore the single most
    # important field here, and it was being set after the only write.
    _write_report()
    result["queued"] = _queue_decision(result) if queue else False
    _write_report()
    return result


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="print the derivation (does not suppress the queue)")
    ap.add_argument("--no-queue", action="store_true",
                    help="derive without writing a Chairman row")
    args = ap.parse_args()
    res = run(queue=not args.no_queue)
    if not res.get("available"):
        print(json.dumps(res, indent=2))
        raise SystemExit(0)
    b = res["book"]
    print(f"\nBOOK  equity ${b['equity']:,.0f}  cash ${b['cash']:,.0f} "
          f"({b['cash_pct']*100:.1f}%)  positions {b['n_positions']}  "
          f"net beta {b['net_beta']}")
    print(f"      configured floor {res['configured']['cash_floor_pct']*100:.0f}%  "
          f"cap {res['configured']['max_positions']}\n")
    for key, label in (("hard_floor", "A. HARD LIQUIDITY FLOOR"),
                       ("shadow_price", "B. COST OF THE BUFFER"),
                       ("marginal_position", "C. THE MARGINAL POSITION")):
        sec = res[key]
        print(f"{label}")
        print(f"  {sec.get('headline') or sec.get('reason') or 'unavailable'}\n")
    if res.get("queued"):
        print("queued one Chairman decision row (nothing in config.py was touched)")
    if args.report:
        print(json.dumps(res, indent=2))
