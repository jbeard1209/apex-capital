#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE MOAT ANALYZER
==========================================
Economic moat assessment using EDGAR XBRL fundamentals.

Four dimensions:
  a) Gross margin stability (mean + std over 5-10yr)
  b) ROIC vs WACC spread (ROIC each year vs 9% WACC estimate)
  c) Revenue growth consistency (YoY growth mean + std)
  d) FCF margin (avg FCF / revenue over available years)

Score 0-100:
  70+ : WIDE moat
  40-69: NARROW moat
  <40 : NONE

Usage:
  from fe_moat_analyzer import analyze_moat
  result = analyze_moat("AAPL")
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

WACC_ESTIMATE = 0.09  # fixed 9% WACC as specified


def _safe(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _mean(vals: list) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _std(vals: list) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    if len(clean) < 2:
        return None
    m = sum(clean) / len(clean)
    variance = sum((x - m) ** 2 for x in clean) / (len(clean) - 1)
    return variance ** 0.5


def _gross_margin_series(gross_profit: list, revenue: list) -> list[Optional[float]]:
    margins = []
    for gp, rev in zip(gross_profit, revenue):
        if gp is not None and rev is not None and rev > 0:
            margins.append(gp / rev)
        else:
            margins.append(None)
    return margins


def _roic_series(net_income: list, total_assets: list, current_liabilities: list) -> list[Optional[float]]:
    roics = []
    for ni, assets, cl in zip(net_income, total_assets, current_liabilities):
        if ni is None or assets is None:
            roics.append(None)
            continue
        cl_v = cl if cl is not None else 0.0
        inv_cap = assets - cl_v
        if inv_cap > 0:
            roics.append(ni / inv_cap)
        else:
            roics.append(None)
    return roics


def _revenue_growth_series(revenue: list) -> list[Optional[float]]:
    growths = []
    for i in range(1, len(revenue)):
        prev = revenue[i - 1]
        curr = revenue[i]
        if prev is not None and curr is not None and prev > 0:
            growths.append((curr / prev) - 1.0)
        else:
            growths.append(None)
    return growths


def _fcf_margin_series(operating_cf: list, capex: list, revenue: list) -> list[Optional[float]]:
    margins = []
    for ocf, cap, rev in zip(operating_cf, capex, revenue):
        if ocf is None or rev is None or rev <= 0:
            margins.append(None)
            continue
        cap_v = cap if cap is not None else 0.0
        fcf = ocf - cap_v
        margins.append(fcf / rev)
    return margins


def _score_gross_margin(gm_mean: Optional[float], gm_std: Optional[float]) -> int:
    """Score gross margin stability. High mean + low std = moat."""
    if gm_mean is None:
        return 5
    # Base from mean level
    if gm_mean >= 0.50:
        base = 25
    elif gm_mean >= 0.35:
        base = 18
    elif gm_mean >= 0.20:
        base = 10
    else:
        base = 4
    # Penalty for high std (instability)
    if gm_std is not None:
        if gm_std < 0.03:
            pass  # no penalty
        elif gm_std < 0.07:
            base -= 4
        else:
            base -= 10
    return max(0, min(25, base))


def _score_roic_spread(roic_vals: list) -> tuple[int, float]:
    """Score ROIC vs WACC spread."""
    spreads = [r - WACC_ESTIMATE for r in roic_vals if r is not None]
    if not spreads:
        return 5, 0.0
    avg_spread = sum(spreads) / len(spreads)
    if avg_spread >= 0.15:
        return 25, avg_spread
    elif avg_spread >= 0.08:
        return 18, avg_spread
    elif avg_spread >= 0.03:
        return 12, avg_spread
    elif avg_spread >= 0.0:
        return 6, avg_spread
    else:
        return 0, avg_spread


def _score_revenue_consistency(rev_growth_mean: Optional[float], rev_growth_std: Optional[float]) -> int:
    """Score revenue growth consistency."""
    if rev_growth_mean is None:
        return 5
    if rev_growth_mean >= 0.10:
        base = 25
    elif rev_growth_mean >= 0.05:
        base = 18
    elif rev_growth_mean >= 0.02:
        base = 12
    elif rev_growth_mean >= 0.0:
        base = 6
    else:
        base = 0
    if rev_growth_std is not None:
        if rev_growth_std < 0.05:
            pass
        elif rev_growth_std < 0.12:
            base -= 4
        else:
            base -= 10
    return max(0, min(25, base))


def _score_fcf_margin(fcf_margin_avg: Optional[float]) -> int:
    """Score FCF margin."""
    if fcf_margin_avg is None:
        return 5
    if fcf_margin_avg >= 0.20:
        return 25
    elif fcf_margin_avg >= 0.12:
        return 18
    elif fcf_margin_avg >= 0.06:
        return 12
    elif fcf_margin_avg >= 0.0:
        return 6
    else:
        return 0


def analyze_moat(ticker: str) -> dict:
    """
    Analyze economic moat strength using EDGAR XBRL data.

    Returns:
      {
        ticker, score, moat_width,
        gross_margin_avg, gross_margin_std,
        roic_spread_avg,
        revenue_growth_mean, revenue_growth_std,
        fcf_margin_avg,
        notes
      }
    """
    base = {
        "ticker":               ticker,
        "score":                0,
        "moat_width":           "NONE",
        "gross_margin_avg":     None,
        "gross_margin_std":     None,
        "roic_spread_avg":      None,
        "revenue_growth_mean":  None,
        "revenue_growth_std":   None,
        "fcf_margin_avg":       None,
        "notes":                [],
        "data_source":          "EDGAR_XBRL",
    }

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        f = get_edgar_fundamentals(ticker)
        if not f:
            base["notes"].append("No EDGAR fundamentals available")
            return base
    except Exception as e:
        logger.warning(f"[Moat] EDGAR fetch failed for {ticker}: {e}")
        base["notes"].append(f"EDGAR error: {e}")
        return base

    try:
        revenue         = f.get("revenue", [])
        gross_profit    = f.get("gross_profit", [])
        net_income      = f.get("net_income", [])
        total_assets    = f.get("total_assets", [])
        curr_liab       = f.get("current_liabilities", [])
        operating_cf    = f.get("operating_cf", [])
        capex           = f.get("capex", [])

        # ── a) Gross margin stability ─────────────────────────────────────────
        gm_series = _gross_margin_series(gross_profit, revenue)
        gm_mean   = _mean(gm_series)
        gm_std    = _std(gm_series)

        # ── b) ROIC vs WACC spread ────────────────────────────────────────────
        roic_series = _roic_series(net_income, total_assets, curr_liab)
        roic_score, roic_spread_avg = _score_roic_spread(roic_series)

        # ── c) Revenue growth consistency ─────────────────────────────────────
        rev_growth = _revenue_growth_series(revenue)
        rev_g_mean = _mean(rev_growth)
        rev_g_std  = _std(rev_growth)

        # ── d) FCF margin ──────────────────────────────────────────────────────
        fcf_margins  = _fcf_margin_series(operating_cf, capex, revenue)
        fcf_margin_avg = _mean(fcf_margins)

        # ── Scoring ───────────────────────────────────────────────────────────
        gm_score  = _score_gross_margin(gm_mean, gm_std)
        rev_score = _score_revenue_consistency(rev_g_mean, rev_g_std)
        fcf_score = _score_fcf_margin(fcf_margin_avg)

        total_score = gm_score + roic_score + rev_score + fcf_score
        total_score = max(0, min(100, total_score))

        if total_score >= 70:
            moat_width = "WIDE"
        elif total_score >= 40:
            moat_width = "NARROW"
        else:
            moat_width = "NONE"

        # Build notes
        notes = []
        n_years = len([v for v in revenue if v is not None])
        notes.append(f"Analysis based on {n_years} years of EDGAR data")
        if gm_mean is not None:
            notes.append(f"Gross margin avg {gm_mean*100:.1f}% (std {(gm_std or 0)*100:.1f}%)")
        if roic_spread_avg is not None:
            notes.append(
                f"ROIC-WACC spread avg {roic_spread_avg*100:.1f}% "
                f"(WACC={WACC_ESTIMATE*100:.0f}%)"
            )
        if rev_g_mean is not None:
            notes.append(
                f"Revenue growth avg {rev_g_mean*100:.1f}% "
                f"(std {(rev_g_std or 0)*100:.1f}%)"
            )
        if fcf_margin_avg is not None:
            notes.append(f"FCF margin avg {fcf_margin_avg*100:.1f}%")

        logger.info(
            f"[Moat] {ticker}: score={total_score} | {moat_width} | "
            f"gm={round(gm_mean or 0,3)} | roic_spread={round(roic_spread_avg,3)} | "
            f"fcf_margin={round(fcf_margin_avg or 0,3)}"
        )

        return {
            "ticker":               ticker,
            "score":                total_score,
            "moat_width":           moat_width,
            "gross_margin_avg":     round(gm_mean, 4) if gm_mean is not None else None,
            "gross_margin_std":     round(gm_std, 4) if gm_std is not None else None,
            "roic_spread_avg":      round(roic_spread_avg, 4),
            "revenue_growth_mean":  round(rev_g_mean, 4) if rev_g_mean is not None else None,
            "revenue_growth_std":   round(rev_g_std, 4) if rev_g_std is not None else None,
            "fcf_margin_avg":       round(fcf_margin_avg, 4) if fcf_margin_avg is not None else None,
            "score_breakdown": {
                "gross_margin_score":    gm_score,
                "roic_vs_wacc_score":    roic_score,
                "revenue_growth_score":  rev_score,
                "fcf_margin_score":      fcf_score,
            },
            "notes":       notes,
            "data_source": "EDGAR_XBRL",
        }

    except Exception as e:
        logger.warning(f"[Moat] analyze_moat({ticker}) failed: {e}")
        base["notes"].append(f"Analysis error: {e}")
        return base


if __name__ == "__main__":
    import sys
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL", "MSFT", "NEM"]
    for tk in tickers:
        r = analyze_moat(tk)
        print(f"\n{tk}:")
        print(json.dumps(r, indent=2))
