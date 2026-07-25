#!/usr/bin/env python3
"""
ib_tutor.py — Investment Banking Specialist Tutor
===================================================
Covers: M&A, DCF/LBO/Comps, deal structures, pitchbooks, capital markets.
Teaching philosophy: training-the-street style — concepts taught with
real numbers, worked examples, and deal case studies.

Every lesson has:
  1. HOOK (2 min): Why this matters to YOU right now
  2. CORE CONCEPT (15 min): The main theory with a worked example
  3. DEEP DIVE (8 min): A nuance or edge case most people miss
  4. REAL DEAL (3 min): How this played out in an actual transaction
  5. SETUP FOR QUIZ (2 min): Telegraphs what to remember

Output: saves to data/todays_lesson_content.json (read by student + quiz_master)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")
logger = logging.getLogger("IBTutor")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

LESSONS = {
    "IB1": {
        "title": "M&A Process: Buy-Side vs Sell-Side",
        "hook": "When Boeing (BA) — a stock you shorted — reports bad earnings, an activist investor might push for a sale. You'd need to know exactly how that process works to trade it intelligently.",
        "core_concept": """
THE M&A PROCESS — SELL-SIDE (Company selling itself)
=====================================================
Phase 1: PREPARATION (4-8 weeks)
  • Board hires investment bank as financial advisor
  • Management prepares Confidential Information Memorandum (CIM)
  • Banker builds a 'teaser' (anonymous 1-pager) and target list

Phase 2: MARKETING — FIRST ROUND (3-4 weeks)
  • Teasers sent to potential buyers (strategic + financial)
  • Interested buyers sign Non-Disclosure Agreement (NDA)
  • CIM distributed; management presentations scheduled
  • Buyers submit non-binding Indications of Interest (IOIs)

Phase 3: DUE DILIGENCE + SECOND ROUND (4-6 weeks)
  • Banker selects shortlist of serious buyers
  • Virtual Data Room (VDR) opened with detailed financials
  • Management Q&A sessions
  • Buyers submit binding Letters of Intent (LOIs)

Phase 4: FINAL NEGOTIATIONS (2-4 weeks)  
  • Board selects winning bidder ('Final Definitive Agreement')
  • Price, reps & warranties, break-up fees negotiated
  • Board fairness opinion from second bank

Phase 5: REGULATORY + CLOSING (3-12 months)
  • HSR antitrust filing (for large deals)
  • Shareholder vote (if acquirer is public)
  • CFIUS review (if foreign buyer)
  • Closing!

BUY-SIDE PROCESS (Company acquiring):
  • Strategic rationale deck → board approval
  • Identify target → approach management or buy in market
  • Sign NDA → receive CIM
  • Internal model (DCF + synergies) → submit IOI/LOI
  • Due diligence → negotiate definitive agreement
""",
        "deep_dive": """
WHAT MOST PEOPLE MISS: The 'go-shop' provision
================================================
After signing a definitive agreement, some deals include a GO-SHOP period
(usually 25-50 days) where the target CAN solicit competing bids.

Why does this matter to you as an investor?
  • Go-shop = opportunity for a higher bid to emerge
  • Stock price often trades BELOW deal price during go-shop (risk premium)
  • If a competing bid comes in 10% higher, the original buyer either 
    pays the 'break-up fee' (~3-4% of deal value) or raises their bid

Break-up fee math:
  Deal value: $10B
  Break-up fee: 3% = $300M
  If competing bid at $11B: original buyer must raise by $1B OR pay $300M
  → Often better to pay the $300M (you get released from a bad deal)
""",
        "real_deal": """
REAL DEAL: Microsoft / Activision (2022-2023)
=============================================
Deal: Microsoft acquired Activision for $95/share ($68.7B)
Process took 21 MONTHS due to FTC antitrust challenge.

Key moments:
  • Announced Jan 2022 at $95/share (79% premium to prior close)
  • FTC sued to block in Dec 2022
  • UK CMA initially blocked in April 2023
  • Microsoft restructured deal (divested cloud streaming rights to Ubisoft)
  • Deal closed Oct 2023

Trading insight: After FTC sued, ATVI traded at $75-80 (below $95 deal price)
→ MASSIVE arb opportunity if you believed deal would close (it did!)
""",
        "quiz_preview": "Be ready to: (1) sequence the 5 phases of a sell-side process, (2) define go-shop provision, (3) calculate a break-up fee, (4) explain why merger arb discount exists"
    },

    "IB3": {
        "title": "Accretion/Dilution Analysis",
        "hook": "When you see a BUY_NEW signal for Broadcom (AVGO) in Apex's system, AVGO's history of M&A is part of why institutions love it. Today you'll understand exactly how they make those acquisitions work financially.",
        "core_concept": """
ACCRETION/DILUTION: Does an acquisition create or destroy value for the acquirer's shareholders?

SETUP:
  Acquirer issues NEW shares to pay for target → existing shareholders own LESS of company
  But: if target adds earnings FASTER than dilution → EPS goes UP (accretive!)

THE MATH:
  Accretion/Dilution = Change in EPS after acquisition

  New EPS = (Acquirer Net Income + Target Net Income + Synergies - Cost of Financing) /
             (Acquirer Shares + New Shares Issued)

EXAMPLE — Acme Corp buys Widget Inc:
  Acme: EPS = $5.00, shares = 100M, P/E = 20x → stock price $100
  Widget: Net income = $50M, being acquired for $1B (20x NI)

  Acme pays with stock at $100/share → issues 10M new shares

  Post-deal:
    Combined NI = 100M × $5 + $50M = $550M
    New shares = 100M + 10M = 110M
    New EPS = $550M / 110M = $5.00 → NEUTRAL (break-even)

  If Acme's P/E (20x) > Widget's acquisition multiple (20x NI = 20x P/E) → neutral
  If Acme's P/E > acquisition P/E → ACCRETIVE
  If Acme's P/E < acquisition P/E → DILUTIVE

THE RULE:
  P/E_acquirer > P/E_target → accretive (in a stock deal)
  P/E_acquirer < P/E_target → dilutive
  High-P/E acquirers can use stock as 'currency' to buy cheaper companies
""",
        "deep_dive": """
SYNERGIES vs 'PURCHASE PRICE PREMIUM'
=====================================
Deals are ALMOST ALWAYS dilutive at the acquisition price BEFORE synergies.
The question is: do synergies make it accretive?

Revenue synergies (hard to guarantee):
  • Cross-selling, new markets, brand extension
  • Investors discount these 50%+ (show me the money)

Cost synergies (more credible):
  • Headcount reduction ($)
  • Facilities consolidation ($)
  • Procurement leverage (materials, software) ($)
  
Rule of thumb: Acquirers typically pay a 30-40% premium.
To earn this back, they need synergies worth ~20-25% of target's value.

AVGO's playbook: Buy mature tech companies with high margins, cut 50% of 
costs, raise margins from 30% → 60%, use the cash to do next deal.
This is why AVGO compounds. Study it.
""",
        "real_deal": """
AVGO / VMware (2023) — $61B acquisition
========================================
  AVGO paid $61B for VMware (= 12x revenue, 22x EBITDA)
  VMware NI: ~$1.8B, AVGO NI: ~$11B

  Was it accretive? By itself, barely. But:
  • AVGO target: $8.5B in cost synergies within 3 years
  • Headcount cut: ~5,000 people (VMware had 37,000)
  • Software pricing power: raised VMware customer contracts 3-4x
  • Result: VMware EBITDA expected to go from $4B → $8.5B

  Post-acquisition, AVGO stock rallied 25% in 6 months.
  The market believed the synergy story. And the 13F signals agreed.
""",
        "quiz_preview": "Be ready to: (1) calculate EPS accretion/dilution with given numbers, (2) explain the P/E rule for stock deals, (3) distinguish revenue vs cost synergies, (4) describe AVGO's acquisition strategy"
    },

    "V1": {
        "title": "Intrinsic Value & DCF Walkthrough",
        "hook": "Apex Capital's fe_intrinsic_value.py runs a DCF on every stock in our watchlist. You're already using DCF outputs — today you'll understand exactly how they're built.",
        "core_concept": """
DCF — DISCOUNTED CASH FLOW: The Foundation of All Valuation
=============================================================
Core idea: A dollar today is worth more than a dollar tomorrow.
A business is worth the sum of all future cash flows, discounted to today.

Value = CF₁/(1+r)¹ + CF₂/(1+r)² + ... + CF_n/(1+r)^n + Terminal Value/(1+r)^n

STEP 1: PROJECT FREE CASH FLOW (FCF)
  FCF = EBIT × (1 - Tax Rate) + D&A - CapEx - ΔNWC

  Where:
  EBIT = Earnings before interest and taxes (from income statement)
  D&A = Depreciation & Amortization (non-cash, add back)
  CapEx = Capital expenditures (cash out for equipment/property)
  ΔNWC = Change in net working capital (cash tied up in operations)

  Project 5-10 years using revenue growth and margin assumptions.

STEP 2: CHOOSE A DISCOUNT RATE (WACC)
  WACC = Cost of Equity × (E/V) + Cost of Debt × (D/V) × (1 - Tax Rate)
  Typical range: 7-12% for investment-grade companies

STEP 3: CALCULATE TERMINAL VALUE
  Method 1 — Gordon Growth: TV = FCF_final × (1+g) / (WACC - g)
    g = long-term growth rate (usually 2-3%, GDP-like)
  Method 2 — Exit Multiple: TV = EBITDA_final × EV/EBITDA_multiple

STEP 4: DISCOUNT EVERYTHING BACK
  Sum all PV(FCF) + PV(Terminal Value) = Enterprise Value
  Enterprise Value - Net Debt = Equity Value
  Equity Value / Shares Outstanding = Intrinsic Value per Share

WORKED EXAMPLE — LLY (Eli Lilly, simplified):
  Revenue 2024: $45B, growing 20% YoY for 3 years, then 10%, then 5%
  EBIT margin: 35%, Tax: 21%
  D&A: $1.5B, CapEx: $2B, ΔNWC: $0.5B
  WACC: 8%, Terminal multiple: 20x EBITDA

  Year 1 FCF: $45B × 1.20 × 0.35 × 0.79 - ($2B - $1.5B + $0.5B) = $14.8B
  ... [full model shows intrinsic value ~$900-1100/share vs $880 current]
""",
        "deep_dive": """
THE #1 MISTAKE IN DCF: Terminal Value Dominance
================================================
In most DCFs, 60-80% of the value comes from Terminal Value (not the projection period).
This means your growth rate assumption in year 10+ drives almost everything.

Test: Change g from 2% to 3% in a LLY DCF with WACC=8%:
  TV = FCF × (1+g) / (WACC - g)
  At g=2%: TV = $15B × 1.02 / 0.06 = $255B
  At g=3%: TV = $15B × 1.03 / 0.05 = $309B

$54B difference from changing ONE assumption by 1%.

The lesson: DCF is a framework, not a calculator. The output is only as good as
your growth assumptions. This is why PE firms prefer exit multiple method —
it anchors to what the market actually pays.
""",
        "real_deal": "LLY's 2024 valuation: At $880/share and 65x P/E, the market is pricing in Mounjaro/Zepbound becoming $50B+ revenue products by 2030. Our DCF in fe_intrinsic_value.py captures this via a 25% revenue CAGR assumption. The 'margin of safety' is thin — which is why our ML model still gives LLY high conviction based on institutional accumulation rather than pure valuation.",
        "quiz_preview": "Be ready to: (1) write the FCF formula from memory, (2) explain why terminal value matters so much, (3) describe WACC components, (4) calculate intrinsic value from a simple 3-year DCF"
    }
}

# Default lesson for modules not yet written
DEFAULT_LESSON = {
    "hook": "This is a core concept every IB analyst must master cold.",
    "core_concept": "Lesson content being developed — check back tomorrow.",
    "deep_dive": "Advanced nuances will be covered in the next session.",
    "real_deal": "Case study TBD.",
    "quiz_preview": "Quiz coming after lesson completion."
}


def deliver_lesson(lesson_brief: dict) -> dict:
    """Deliver the lesson for the given module."""
    module_id = lesson_brief.get("module_id", "IB1")
    lesson_data = LESSONS.get(module_id, DEFAULT_LESSON)
    
    lesson_content = {
        "module_id": module_id,
        "module_title": lesson_brief.get("module_title", module_id),
        "specialist": "Investment Banking Tutor",
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
        "key_terms": lesson_data.get("key_terms", []),
        "quiz_module": module_id,
    }
    
    # Save for student and quiz master
    output_path = BASE_DIR / "data" / "todays_lesson_content.json"
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(lesson_content, f, indent=2)
    os.replace(tmp, str(output_path))
    
    logger.info(f"  ✅ Lesson delivered: {module_id} — {lesson_content['module_title']}")
    return lesson_content


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lesson", type=str, help="Path to lesson brief JSON")
    args = parser.parse_args()
    
    if args.lesson:
        with open(args.lesson) as f:
            lesson_brief = json.load(f)
    else:
        lesson_brief = {"module_id": "IB1", "module_title": "M&A Process", 
                       "student_name": "Jack", "session_type": "new"}
    
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — INVESTMENT BANKING TUTOR")
    logger.info(f"  Module: {lesson_brief.get('module_id')}")
    logger.info("=" * 55)
    
    deliver_lesson(lesson_brief)


if __name__ == "__main__":
    main()
