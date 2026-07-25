#!/usr/bin/env python3
"""
investment_tutor.py — Markets & Investing Specialist Tutor
===========================================================
Covers: equities, fixed income, derivatives, portfolio theory,
macro, institutional investing. Deep expertise in quantitative
approaches — mirrors what you're already doing at Apex.
"""

import json, logging, os, sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")
logger = logging.getLogger("InvestmentTutor")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

LESSONS = {
    "M5": {
        "title": "Portfolio Theory: CAPM, Beta, Sharpe Ratio",
        "hook": "Apex Capital's ML model uses Sharpe ratio as its training target (idea 28, AUC 0.5804). You built the system — now understand the theory behind why Sharpe works as a signal.",
        "core_concept": """
MODERN PORTFOLIO THEORY (MPT) — Markowitz, 1952
================================================
KEY INSIGHT: What matters isn't a single asset's risk — it's HOW that asset
correlates with everything else in the portfolio.

Expected Portfolio Return:
  E(Rp) = Σ wᵢ × E(Rᵢ)   [weighted average of returns]

Portfolio Variance (2-asset):
  σ²p = w₁²σ₁² + w₂²σ₂² + 2w₁w₂ρ₁₂σ₁σ₂
  
  When ρ₁₂ = +1: NO diversification benefit
  When ρ₁₂ = 0:  Partial diversification
  When ρ₁₂ = -1: MAXIMUM diversification (variance goes to zero)

THE EFFICIENT FRONTIER:
  For every level of expected return, there's a MINIMUM variance portfolio.
  The set of these portfolios = Efficient Frontier.
  Rational investors always pick a portfolio ON the frontier.

CAPM — Capital Asset Pricing Model:
  E(Rᵢ) = Rf + βᵢ × (Rm - Rf)
  
  Rf = Risk-free rate (~4.5% now, based on 10Y Treasury)
  βᵢ = Asset's sensitivity to market returns
     = Cov(Rᵢ, Rm) / Var(Rm)
  Rm - Rf = Equity Risk Premium (historically ~5-7%)
  
  Beta interpretation:
    β = 1.0: Moves exactly with market
    β = 1.5: 50% more volatile than market (tech, growth)
    β = 0.5: Half market volatility (utilities, staples)
    β < 0:   Moves OPPOSITE to market (gold, VIX ETFs)

THE SHARPE RATIO:
  S = (Rp - Rf) / σp
  
  Measures: return per unit of TOTAL risk
  Good: > 1.0 (excellent: > 2.0)
  
  WHY APEX USES IT: Sharpe normalizes by volatility — a stock with 50% return
  but 60% volatility (S=0.83) is WORSE than a stock with 20% return and 15%
  volatility (S=1.33). Sharpe-based ML target finds QUALITY winners, not just
  high-return stocks that happened to be in bull markets.
  
  Treynor Ratio (similar but uses beta instead of σ):
  T = (Rp - Rf) / β  ← preferred when portfolio is part of larger portfolio
""",
        "deep_dive": """
WHAT CAPM GETS WRONG — and what replaces it
============================================
CAPM failures (well-documented):
  1. Beta alone doesn't explain all returns
  2. Low-beta stocks consistently OUTPERFORM CAPM prediction (low-vol anomaly)
  3. Size effect: small caps beat large caps risk-adjusted
  4. Value effect: cheap stocks beat expensive ones

FAMA-FRENCH 3-FACTOR MODEL (1993):
  E(Ri) = Rf + β_MKT × (Rm - Rf) + β_SMB × SMB + β_HML × HML
  
  SMB = Small Minus Big (size factor)
  HML = High Minus Low (value factor, book-to-market)
  
FAMA-FRENCH 5-FACTOR (2015): adds Profitability + Investment factors

MODERN FACTOR INVESTING (what Apex does):
  Institutional investors use DOZENS of factors:
  - Momentum (12-1 month return)
  - Quality (margins, ROE)  
  - Low volatility
  - Institutional ownership (conviction score!)
  - Earnings revision
  
  Apex's 13F analysis IS factor investing — we're capturing the
  'institutional momentum' factor before it's fully priced in.
""",
        "real_deal": """
REAL WORLD: Apex's SH position
================================
When Canary went ORANGE (market stress), Apex bought SH:
  SH = ProShares Short S&P 500 ETF
  Beta ≈ -1.0 (moves OPPOSITE to SPY)
  
  By adding SH when SPY/QQQ were falling, Apex:
  1. Reduced portfolio beta (from ~0.7 to ~0.4)
  2. Used SH as 'portfolio insurance'
  
  The Canary agent tracks: VIX (fear index), SPY momentum, sector breadth.
  These are all CAPM/factor signals — you were using MPT without knowing it!
""",
        "quiz_preview": "Be ready to: (1) write CAPM formula, (2) calculate portfolio variance for 2 assets, (3) explain Sharpe vs Treynor, (4) name 3 CAPM anomalies"
    },
    "M7": {
        "title": "Institutional Investing: 13F Filings & Hedge Funds",
        "hook": "You built Apex Capital. Today you'll understand WHY 13F signals work — and what separates the $10B funds that move markets from the ones that just follow.",
        "core_concept": """
INSTITUTIONAL LANDSCAPE — Who Files 13F?
=========================================
Anyone managing >$100M in US equity securities must file SEC Form 13F
quarterly within 45 days after quarter end.

Who this covers:
  • Hedge funds (Tiger Global, Point72, Millennium)
  • Mutual funds (Fidelity, Vanguard, BlackRock)
  • Pension funds (CalPERS, Norway SWF)
  • Family offices (Bezos Expeditions, etc.)
  • Insurance companies

What they report:
  • All long US equity positions >10K shares or $200K value
  • Puts and calls (separately)
  NOT reported: shorts, bonds, derivatives (hedge info)

CONVICTION SCORING FRAMEWORK:
================================
High conviction signals (Apex's feature set):

  1. POSITION SIZE (largest alpha signal)
     Large position = large bet = high conviction
     Sizing = position value / total AUM

  2. HOLD STREAK (patience signal)
     Held 4+ consecutive quarters = committed, not trading

  3. NEW POSITION (attention signal)
     Quarter 1 of new position = research just done, timing is fresh
     
  4. LARGE INCREASE (conviction BUILDING signal)  
     Adding >50% = still buying, not distributing

  5. INSTITUTION HIT RATE (quality signal)
     Manager history: % of prior picks that outperformed
     
  6. CONVICTION VELOCITY (our new signal)
     QoQ change in composite conviction score
     Rising = actively building → buy signal
     Falling = distributing → warning signal

HEDGE FUND STRATEGIES (who you're reading the filings of):
  • Long/Short Equity (most 13F filers)
  • Global Macro (Bridgewater, Tudor)
  • Quantitative (Two Sigma, Renaissance — don't show in 13F!)
  • Activist (Icahn, Elliott — 13D/G filings more relevant)
  • Multi-Strategy (Citadel, Millennium)
""",
        "deep_dive": """
THE 13F ALPHA PUZZLE — Why does the edge still exist?
======================================================
If 13F filings are PUBLIC, why do they predict returns?

Answer: INFORMATION DECAY ASYMMETRY

  Filing lag = 45 days after quarter end
  Quarter is 90 days long
  Average trade age = 45 + 45 = ~90 days old by disclosure date

  BUT — high-conviction long-term holders (3+ year positions) are 
  STILL ACCUMULATING even after disclosure. The 13F reveals the TIP
  of the iceberg for multi-year thesis positions.

CONVICTION TRAJECTORY EFFECT:
  Q1: Fund buys 1M shares (filing shows: small new position)
  Q2: Fund buys 2M shares (filing shows: doubled, conviction growing)
  Q3: Fund buys 3.5M shares (filing shows: major conviction builder)
  
  By Q3 filing, the stock may have already moved. But:
  → The trajectory signal says: this fund is NOT done buying
  → Thesis intact: expect continued accumulation
  → Short squeeze risk: if heavily shorted + fund keeps buying

  This is the 13F alpha. Not 'what did they buy?' but 
  'are they still building this position?'
""",
        "real_deal": """
CASE STUDY: Warren Buffett / Apple (AAPL)
==========================================
2016: Berkshire disclosed small Apple position (~9M shares)
2017: Increased to 165M shares — 13F showed massive buildup
2018: Increased to 252M shares — still building!
2022: ~900M shares, ~46% of Berkshire portfolio

If you ran Apex in 2017 and saw:
  conviction_score: 5★ (10% of Berkshire AUM)
  hold_streak: 4 quarters and growing
  conviction_velocity: STRONGLY POSITIVE (doubling each quarter)

→ Apex's model would have given AAPL a 1.5× multiplier on conviction
→ Position sizing would have allocated maximum capital
→ AAPL returned 600%+ from 2017-2022

This is exactly what institutional conviction tracking captures.
""",
        "quiz_preview": "Be ready to: (1) list 3 types of 13F filers, (2) explain why 13F edge persists despite public data, (3) describe conviction_velocity signal, (4) calculate filing timeline (when does Q1 data reach public)"
    }
}

DEFAULT_LESSON_CONTENT = {
    "hook": "Markets are the arena where everything you learn gets tested.",
    "core_concept": "This module's content is being developed. Check back next session.",
    "deep_dive": "Advanced analysis to follow.",
    "real_deal": "Case study in preparation.",
    "quiz_preview": "Quiz to follow."
}


def deliver_lesson(lesson_brief: dict) -> dict:
    module_id = lesson_brief.get("module_id", "M5")
    lesson_data = LESSONS.get(module_id, DEFAULT_LESSON_CONTENT)
    lesson_content = {
        "module_id": module_id,
        "module_title": lesson_brief.get("module_title", module_id),
        "specialist": "Investment & Markets Tutor",
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "student_name": lesson_brief.get("student_name", "Student"),
        "session_type": lesson_brief.get("session_type", "new"),
        "portfolio_hook": lesson_brief.get("portfolio_hook", ""),
        "sections": {
            "hook": lesson_data.get("hook", ""),
            "core_concept": lesson_data.get("core_concept", ""),
            "deep_dive": lesson_data.get("deep_dive", ""),
            "real_deal": lesson_data.get("real_deal", ""),
            "quiz_preview": lesson_data.get("quiz_preview", ""),
        },
        "quiz_module": module_id,
    }
    output_path = DATA_DIR / "todays_lesson_content.json"
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(lesson_content, f, indent=2)
    os.replace(tmp, str(output_path))
    logger.info(f"  Lesson delivered: {module_id}")
    return lesson_content


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lesson", type=str)
    args = parser.parse_args()
    lesson_brief = {}
    if args.lesson:
        with open(args.lesson) as f:
            lesson_brief = json.load(f)
    else:
        lesson_brief = {"module_id": "M7", "module_title": "Institutional Investing",
                       "student_name": "Jack", "session_type": "new"}
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — INVESTMENT & MARKETS TUTOR")
    logger.info(f"  Module: {lesson_brief.get('module_id')}")
    logger.info("=" * 55)
    deliver_lesson(lesson_brief)

if __name__ == "__main__":
    main()
