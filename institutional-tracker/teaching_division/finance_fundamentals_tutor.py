#!/usr/bin/env python3
"""
finance_fundamentals_tutor.py — Finance Foundations Specialist
===============================================================
Covers: accounting, financial statements, corporate finance, valuation basics.
Teaching style: build everything from first principles.
"""

import json, logging, os, sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")
logger = logging.getLogger("FFTutor")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

LESSONS = {
    "F1": {
        "title": "The Three Financial Statements",
        "hook": "Every company Apex Capital analyzes — from GS to LLY to AVGO — tells its story through three documents. You'll read them in under 10 minutes once you understand how they connect.",
        "core_concept": """
THE THREE FINANCIAL STATEMENTS — How They Connect
==================================================
Think of a business as a MOVIE: 
  • Balance Sheet = the PHOTO (snapshot at one moment)
  • Income Statement = the STORYLINE (what happened over time)
  • Cash Flow Statement = the MONEY TRAIL (where cash actually went)

1. INCOME STATEMENT (aka P&L)
   Covers a PERIOD of time (Q1 2024, FY2024)
   
   Revenue (top line)
   - COGS (Cost of Goods Sold)
   = Gross Profit [Gross Margin = GP/Revenue]
   
   - Operating Expenses (SG&A, R&D)
   = EBIT (Operating Income)
   
   - Interest Expense
   ± Other Income/Expense
   = EBT (Earnings Before Tax)
   
   - Income Taxes
   = Net Income (bottom line) [Net Margin = NI/Revenue]
   
   ÷ Shares Outstanding
   = EPS (Earnings Per Share)

2. BALANCE SHEET
   Snapshot at a POINT in time (as of Dec 31, 2024)
   
   ASSETS = LIABILITIES + EQUITY (always!)
   
   Assets (what company owns/controls):
     Current: Cash, Accounts Receivable, Inventory
     Long-term: PP&E, Goodwill, Intangibles
   
   Liabilities (what company owes):
     Current: Accounts Payable, Short-term debt
     Long-term: Long-term debt, Deferred tax
   
   Equity (what shareholders own):
     Common Stock, Retained Earnings, Treasury Stock

3. CASH FLOW STATEMENT
   Reconciles Net Income → Actual Cash Generated
   
   Operating Activities:
     Net Income
     + D&A (non-cash, add back)
     ± Changes in Working Capital
     = Cash from Operations (CFO) ← most important
   
   Investing Activities:
     - CapEx (buying equipment)
     ± Acquisitions/divestitures
   
   Financing Activities:
     + Debt issued
     - Debt repaid
     - Dividends paid
     ± Stock buybacks/issuances

HOW THEY LINK:
  Net Income flows to → Retained Earnings on Balance Sheet
  Cash from CFS = Change in Cash on Balance Sheet
  If you earn $100M but collect nothing → AR up on BS, NI up on IS, CFO low on CFS
""",
        "deep_dive": """
THE MOST IMPORTANT LINE: Free Cash Flow
========================================
Why Net Income can be misleading:
  Company A: NI = $100M, CFO = $50M (collecting cash, half stuck in receivables)
  Company B: NI = $80M, CFO = $120M (collecting MORE than it earns — shrinking AR)
  
  Company B is actually the BETTER business despite lower NI!

Free Cash Flow (FCF) = Cash from Operations - CapEx
  This is the cash a business actually GENERATES for owners.
  
  High-quality businesses: FCF > Net Income (D&A > CapEx)
  Capital-intensive businesses: FCF < Net Income (CapEx > D&A)
  
Examples from our portfolio:
  LLY (pharma): High R&D CapEx → FCF often below NI during growth
  GS (financials): CapEx minimal → FCF ≈ NI
  AVGO (semiconductors): CapEx heavy but generates massive FCF at scale
""",
        "real_deal": """
READING GS's Q3 2024 IN 2 MINUTES:
===================================
Revenue: $12.7B (trading + IB + asset management fees)
Net Income: $2.99B — Net Margin: 23.5% ← strong for financials
EPS: $8.40 (consensus was $6.89 — big beat!)

Balance Sheet:
  Total Assets: $1.6T (mostly financial assets — loans, securities)
  Total Debt: $350B (investment banks are highly leveraged)
  Equity: $116B
  Book Value per Share: $331 — stock at $499 = 1.5x book

Key: Banks trade on P/Book, not P/E.
  GS at 1.5x P/Book vs historical 1.2x = premium valuation
  This tells you market expects above-average ROE to continue.
  Our Apex model flagged GS based on institutional accumulation.
  If you'd read the balance sheet, you'd have understood WHY.
""",
        "quiz_preview": "Be ready to: (1) list the three statements and what time period each covers, (2) write the balance sheet equation, (3) calculate Free Cash Flow from given inputs, (4) explain why CFO > NI can signal quality"
    },
    "F6": {
        "title": "Time Value of Money & NPV",
        "hook": "Every valuation model — DCF, LBO, options — is built on TVM. This is the foundation of everything in finance. When Apex discounts future cash flows, this is the math it uses.",
        "core_concept": """
TIME VALUE OF MONEY — The Foundation of All Finance
====================================================
Core principle: $100 today > $100 tomorrow.
Why? Because money today can be invested and earn returns.

FUTURE VALUE (FV):
  FV = PV × (1 + r)^n
  
  Example: You invest $1,000 at 8% for 5 years:
  FV = $1,000 × (1.08)^5 = $1,000 × 1.469 = $1,469

PRESENT VALUE (PV):
  PV = FV / (1 + r)^n
  
  Example: What is $1,469 in 5 years worth TODAY at 8%?
  PV = $1,469 / (1.08)^5 = $1,000 ✓

NET PRESENT VALUE (NPV):
  NPV = Σ [CFₜ / (1 + r)^t] - Initial Investment
  
  NPV > 0: Project creates value (INVEST)
  NPV < 0: Project destroys value (PASS)
  NPV = 0: Break-even (indifferent)

EXAMPLE — Should Apex Capital invest $500K in infrastructure?
  Year 0: -$500,000 (investment)
  Year 1: +$150,000 return
  Year 2: +$200,000 return
  Year 3: +$250,000 return
  Discount rate: 10%
  
  PV(Y1) = $150K / 1.10 = $136,364
  PV(Y2) = $200K / 1.21 = $165,289
  PV(Y3) = $250K / 1.331 = $187,820
  Total PV = $489,473
  
  NPV = $489,473 - $500,000 = -$10,527
  
  Answer: DON'T invest! Returns don't cover the cost of capital.

ANNUITY SHORTCUT (equal payments every period):
  PV(Annuity) = CF × [1 - (1+r)^-n] / r
  PV(Perpetuity) = CF / r (payments forever)
""",
        "deep_dive": """
INTERNAL RATE OF RETURN (IRR)
==============================
IRR = the discount rate that makes NPV = 0

Interpretation:
  IRR > cost of capital → invest (project earns above required return)
  IRR < cost of capital → pass
  IRR = cost of capital → break-even

IRR decision rule for Apex's investments:
  If we require 15% return on new tech investments,
  and a project's IRR = 22% → invest
  If IRR = 10% → pass

LIMITATIONS OF IRR (critical for IB interviews):
  1. Multiple IRRs if cash flows change sign multiple times
  2. IRR ASSUMES reinvestment at the IRR rate (often unrealistic)
  3. For mutually exclusive projects, NPV is superior
  4. Can favor short-duration projects over long-term value creators
  
  Use Modified IRR (MIRR) to fix the reinvestment assumption.
""",
        "real_deal": """
TVM IN APEX'S ML MODEL:
========================
When our model uses 'risk_adjusted_20d' target (idea 28, AUC 0.5804):
  risk_adjusted_20d = outcome_20d / volatility_20d
  
  This is literally Time-Value-of-Risk adjusted return:
  Just as PV = FV / (1+r), our label = Return / Risk
  
  Higher risk requires higher return to be 'worth it' at same PV.
  Our Sharpe-based target is TVM applied to risk instead of time.
  
  Warren Buffett's rule: 'It's better to get a 10% return with 
  certainty than a 20% return with 50% chance of loss.'
  TVM explains WHY in mathematical terms.
""",
        "quiz_preview": "Be ready to: (1) calculate FV of $5K at 7% for 3 years, (2) find NPV of a project given cash flows and discount rate, (3) explain when to use IRR vs NPV, (4) define perpetuity PV formula"
    }
}

DEFAULT_LESSON_CONTENT = {
    "hook": "Finance fundamentals are the language of every deal, every portfolio, every decision.",
    "core_concept": "This module is under development. Session coming soon.",
    "deep_dive": "Advanced content to follow.",
    "real_deal": "Real-world application TBD.",
    "quiz_preview": "Quiz coming after content delivery."
}


def deliver_lesson(lesson_brief: dict) -> dict:
    module_id = lesson_brief.get("module_id", "F1")
    lesson_data = LESSONS.get(module_id, DEFAULT_LESSON_CONTENT)
    lesson_content = {
        "module_id": module_id,
        "module_title": lesson_brief.get("module_title", module_id),
        "specialist": "Finance Fundamentals Tutor",
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
        lesson_brief = {"module_id": "F1", "module_title": "The Three Financial Statements",
                       "student_name": "Jack", "session_type": "new"}
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — FINANCE FUNDAMENTALS TUTOR")
    logger.info(f"  Module: {lesson_brief.get('module_id')}")
    logger.info("=" * 55)
    deliver_lesson(lesson_brief)

if __name__ == "__main__":
    main()
