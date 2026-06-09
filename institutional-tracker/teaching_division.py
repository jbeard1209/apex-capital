# =============================================================================
# APEX CAPITAL — TEACHING DIVISION
# =============================================================================
#
# Five Harvard-caliber tutors built around Jack's personal learning schedule:
# 30 minutes per day, goal of investment-banking & finance fluency by fall.
#
#   TeachingCurriculumDirector  — plans lessons, tracks progress, adapts curriculum
#   FinanceFundamentalsTeacher  — P&L, balance sheet, cash flow, DCF, WACC, valuation
#   InvestmentBankingTeacher    — LBOs, M&A, deal structure, pitch books, capital markets
#   MarketsMicrostructureTeacher — equities, bonds, options, order types, market mechanics
#   PortfolioManagementTeacher  — asset allocation, factor models, risk-adjusted returns
#
# PHILOSOPHY
#   Agents revolve around the student (Jack), not a fixed syllabus.
#   If Jack struggles with a topic, the Director flags it and all teachers
#   emphasise it across their own domains until mastery is confirmed.
#   Quizzes run after every lesson — results gate the next session.
#
# DATA FILES
#   data/teaching_curriculum.json  — full 90-day syllabus + lesson schedule
#   data/teaching_progress.json    — Jack's per-topic mastery + quiz history
#   data/teaching_lesson_today.json — today's prepared lesson (rendered for the dashboard)
#
# Usage:
#   python3 teaching_division.py                  # generate today's lesson
#   python3 teaching_division.py --quiz            # run today's quiz
#   python3 teaching_division.py --report          # print progress report
#   python3 teaching_division.py --day N           # generate lesson for day N
# =============================================================================

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("teaching_division")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

CURRICULUM_FILE  = DATA_DIR / "teaching_curriculum.json"
PROGRESS_FILE    = DATA_DIR / "teaching_progress.json"
LESSON_FILE      = DATA_DIR / "teaching_lesson_today.json"

# =============================================================================
# 90-DAY CURRICULUM
# Loosely inspired by "Training the Street" and CFA Level 1 fundamentals.
# Week 1–3:  Finance Fundamentals
# Week 4–6:  Investment Banking Core
# Week 5–8:  Markets & Microstructure
# Week 7–10: Portfolio Management
# Week 9–13: Integration (deal cases, live model tie-ins, full simulations)
# =============================================================================

CURRICULUM = [
    # ── BLOCK 1: Finance Fundamentals (Days 1–21) ────────────────────────────
    {"day": 1,  "teacher": "FinanceFundamentals", "topic": "The Three Financial Statements",
     "description": "How the P&L, balance sheet, and cash flow statement connect. Build intuition for how a single transaction flows through all three.",
     "key_concepts": ["revenue recognition", "accrual vs. cash accounting", "retained earnings link"],
     "prereqs": []},
    {"day": 2,  "teacher": "FinanceFundamentals", "topic": "Reading a P&L Like a Banker",
     "description": "Revenue, COGS, gross margin, EBITDA, EBIT, EBT, net income. Why each line matters and what drives it.",
     "key_concepts": ["gross margin", "EBITDA", "operating leverage", "margin expansion"],
     "prereqs": ["Day 1"]},
    {"day": 3,  "teacher": "FinanceFundamentals", "topic": "The Balance Sheet Deep Dive",
     "description": "Assets, liabilities, equity. Working capital. Net debt. Enterprise value vs. equity value distinction.",
     "key_concepts": ["working capital", "net debt", "enterprise value", "book value"],
     "prereqs": ["Day 1"]},
    {"day": 4,  "teacher": "FinanceFundamentals", "topic": "Cash Flow Statement & Free Cash Flow",
     "description": "Operating, investing, financing sections. Why net income ≠ cash. Unlevered Free Cash Flow (UFCF) calculation.",
     "key_concepts": ["UFCF", "CapEx", "D&A add-back", "working capital changes"],
     "prereqs": ["Day 2", "Day 3"]},
    {"day": 5,  "teacher": "FinanceFundamentals", "topic": "Time Value of Money",
     "description": "Present value, future value, discount rates, annuities. The mathematical foundation of every valuation.",
     "key_concepts": ["NPV", "IRR", "discount factor", "perpetuity formula"],
     "prereqs": []},
    {"day": 6,  "teacher": "FinanceFundamentals", "topic": "WACC — Weighted Average Cost of Capital",
     "description": "Cost of equity (CAPM), cost of debt, capital structure weighting. Why WACC is the discount rate in a DCF.",
     "key_concepts": ["CAPM", "beta", "equity risk premium", "tax shield on debt"],
     "prereqs": ["Day 5"]},
    {"day": 7,  "teacher": "FinanceFundamentals", "topic": "DCF Valuation — Build it From Scratch",
     "description": "Step-by-step DCF: project FCF, calculate terminal value (Gordon Growth + exit multiple), discount back, bridge to equity value.",
     "key_concepts": ["terminal value", "Gordon Growth Model", "sensitivity table", "equity bridge"],
     "prereqs": ["Day 4", "Day 5", "Day 6"]},
    {"day": 8,  "teacher": "FinanceFundamentals", "topic": "Comparable Company Analysis (Comps)",
     "description": "EV/EBITDA, P/E, EV/Revenue. Building a comps table, scrubbing non-recurring items, deriving implied valuation range.",
     "key_concepts": ["NTM multiples", "LTM multiples", "normalisation", "trading comps vs. transaction comps"],
     "prereqs": ["Day 3", "Day 7"]},
    {"day": 9,  "teacher": "FinanceFundamentals", "topic": "Precedent Transaction Analysis",
     "description": "Same structure as comps but uses acquisition prices — includes a control premium. When to use each method.",
     "key_concepts": ["control premium", "synergies in multiples", "deal value vs. equity value"],
     "prereqs": ["Day 8"]},
    {"day": 10, "teacher": "FinanceFundamentals", "topic": "Accounting Adjustments Bankers Make",
     "description": "Stock-based compensation, one-time charges, restructuring costs, pension obligations — what to strip and why.",
     "key_concepts": ["adjusted EBITDA", "non-recurring items", "SBC treatment", "lease obligations"],
     "prereqs": ["Day 2", "Day 3"]},
    {"day": 11, "teacher": "FinanceFundamentals", "topic": "Debt & Credit Concepts",
     "description": "Senior secured, unsecured, subordinated debt. Covenants, leverage ratios, interest coverage. High-yield vs. investment-grade.",
     "key_concepts": ["leverage ratio", "DSCR", "covenants", "credit ratings"],
     "prereqs": ["Day 3", "Day 6"]},
    {"day": 12, "teacher": "FinanceFundamentals", "topic": "Returns Analysis — IRR & MOIC",
     "description": "How PE funds measure returns. Cash-on-cash vs. IRR. Why IRR timing matters. Building a simple returns bridge.",
     "key_concepts": ["MOIC", "IRR", "hold period", "value creation bridge"],
     "prereqs": ["Day 5", "Day 7"]},
    {"day": 13, "teacher": "FinanceFundamentals", "topic": "Accounting for M&A — Purchase Price Allocation",
     "description": "Goodwill creation, intangible step-ups, deferred tax liabilities. Why GAAP EPS often looks terrible post-acquisition.",
     "key_concepts": ["goodwill", "PPA", "intangible amortisation", "deferred tax"],
     "prereqs": ["Day 3", "Day 10"]},
    {"day": 14, "teacher": "FinanceFundamentals", "topic": "Finance Fundamentals Review & Integration Quiz",
     "description": "Comprehensive review session tying together Days 1–13. Integration case: read a 10-K and extract all key metrics.",
     "key_concepts": ["10-K navigation", "integrated model logic", "analyst checklist"],
     "prereqs": ["Day 1-13"], "is_review": True},

    # ── BLOCK 2: Investment Banking Core (Days 15–35) ────────────────────────
    {"day": 15, "teacher": "InvestmentBanking", "topic": "What Investment Banks Actually Do",
     "description": "Divisions: M&A advisory, ECM, DCM, restructuring, sales & trading. How a deal flows from pitch to close.",
     "key_concepts": ["bulge bracket", "boutique", "buy-side vs. sell-side", "tombstone"],
     "prereqs": []},
    {"day": 16, "teacher": "InvestmentBanking", "topic": "The M&A Process End-to-End",
     "description": "LOI, due diligence, purchase agreement, closing. Sell-side vs. buy-side mandates. Auction vs. negotiated deal.",
     "key_concepts": ["LOI", "exclusivity", "material adverse change", "representations & warranties"],
     "prereqs": ["Day 15"]},
    {"day": 17, "teacher": "InvestmentBanking", "topic": "Accretion / Dilution Analysis",
     "description": "Will the deal be accretive or dilutive to EPS? Building a merger model in your head (and on a page).",
     "key_concepts": ["EPS accretion/dilution", "pro-forma earnings", "share issuance", "synergies"],
     "prereqs": ["Day 2", "Day 8", "Day 13"]},
    {"day": 18, "teacher": "InvestmentBanking", "topic": "Leveraged Buyout (LBO) — The Concept",
     "description": "Why PE uses debt to amplify equity returns. The 4 levers of LBO value creation: leverage paydown, EBITDA growth, multiple expansion, synergies.",
     "key_concepts": ["LBO", "entry/exit multiple", "leverage paydown", "equity check"],
     "prereqs": ["Day 11", "Day 12"]},
    {"day": 19, "teacher": "InvestmentBanking", "topic": "Building an LBO Model",
     "description": "Sources & uses, debt schedule, operating model, exit assumptions, returns output. Walk through a full paper LBO.",
     "key_concepts": ["sources & uses", "debt waterfall", "PIK interest", "management rollover"],
     "prereqs": ["Day 18"]},
    {"day": 20, "teacher": "InvestmentBanking", "topic": "Equity Capital Markets — IPOs & Follow-ons",
     "description": "IPO process: S-1, roadshow, bookbuild, pricing, stabilisation. Follow-on offerings. Lockup expirations.",
     "key_concepts": ["S-1 filing", "bookbuilding", "greenshoe", "lockup period"],
     "prereqs": ["Day 15"]},
    {"day": 21, "teacher": "InvestmentBanking", "topic": "Debt Capital Markets",
     "description": "Investment-grade bonds, high-yield bonds, leveraged loans, revolving credit. Pricing, ratings, covenants.",
     "key_concepts": ["investment grade", "high yield", "leveraged loans", "credit spread"],
     "prereqs": ["Day 11", "Day 15"]},
    {"day": 22, "teacher": "InvestmentBanking", "topic": "Reading a Pitch Book",
     "description": "What bankers include in a CIM or management presentation. Situation overview, strategic alternatives, valuation football field.",
     "key_concepts": ["CIM", "football field", "strategic alternatives", "fairness opinion"],
     "prereqs": ["Day 7", "Day 8", "Day 9"]},
    {"day": 23, "teacher": "InvestmentBanking", "topic": "Restructuring & Special Situations",
     "description": "Chapter 11, distressed debt, 363 sales, pre-packaged bankruptcies. How value is distributed to creditors.",
     "key_concepts": ["Chapter 11", "absolute priority rule", "DIP financing", "363 sale"],
     "prereqs": ["Day 11", "Day 21"]},
    {"day": 24, "teacher": "InvestmentBanking", "topic": "IB Interview Questions — The Classics",
     "description": "Walk me through a DCF. How does a $100 write-down flow through the statements? Walk me through an LBO. Why is EBITDA a proxy for cash flow?",
     "key_concepts": ["3-statement interview Q", "goodwill impairment Q", "LBO return driver Q"],
     "prereqs": ["Day 7", "Day 10", "Day 19"], "is_review": True},

    # ── BLOCK 3: Markets & Microstructure (Days 25–42) ───────────────────────
    {"day": 25, "teacher": "MarketsMicrostructure", "topic": "How Stock Markets Actually Work",
     "description": "Exchanges, dark pools, market makers, limit order books. How a buy order gets filled from click to confirmation.",
     "key_concepts": ["limit order book", "bid-ask spread", "market maker", "dark pool"],
     "prereqs": []},
    {"day": 26, "teacher": "MarketsMicrostructure", "topic": "Equity Indices & Benchmarks",
     "description": "S&P 500, Nasdaq, Russell 2000 construction. Price-weighted vs. cap-weighted. Why benchmarks matter for fund managers.",
     "key_concepts": ["cap-weighted index", "float adjustment", "reconstitution", "benchmark hugging"],
     "prereqs": ["Day 25"]},
    {"day": 27, "teacher": "MarketsMicrostructure", "topic": "Fixed Income — Bonds 101",
     "description": "Coupon, par, yield, duration, convexity. Why bond prices move inversely to yields. The yield curve.",
     "key_concepts": ["duration", "yield to maturity", "yield curve", "credit spread"],
     "prereqs": ["Day 5"]},
    {"day": 28, "teacher": "MarketsMicrostructure", "topic": "Options — The Building Blocks",
     "description": "Calls, puts, strike, expiry, premium. Intrinsic vs. time value. Simple payoff diagrams.",
     "key_concepts": ["call option", "put option", "in-the-money", "time value"],
     "prereqs": ["Day 25"]},
    {"day": 29, "teacher": "MarketsMicrostructure", "topic": "Options Greeks — Delta, Gamma, Theta, Vega",
     "description": "What each Greek measures and how traders use them to manage exposure.",
     "key_concepts": ["delta hedging", "gamma scalping", "theta decay", "implied volatility"],
     "prereqs": ["Day 28"]},
    {"day": 30, "teacher": "MarketsMicrostructure", "topic": "Short Selling & Borrow Markets",
     "description": "Why and how funds short stocks. Locate, borrow cost, short squeeze, gamma squeeze.",
     "key_concepts": ["short interest", "borrow cost", "short squeeze", "days-to-cover"],
     "prereqs": ["Day 25"]},
    {"day": 31, "teacher": "MarketsMicrostructure", "topic": "Futures & Forwards",
     "description": "How futures contracts work. Roll cost. Using futures to hedge or gain macro exposure.",
     "key_concepts": ["futures roll", "basis", "contango", "backwardation"],
     "prereqs": ["Day 5", "Day 27"]},
    {"day": 32, "teacher": "MarketsMicrostructure", "topic": "Market Regimes & Volatility",
     "description": "Bull/bear/sideways markets. VIX as a fear gauge. How volatility affects trading strategies.",
     "key_concepts": ["VIX", "implied vs. realised vol", "regime detection", "vol surface"],
     "prereqs": ["Day 26", "Day 29"]},
    {"day": 33, "teacher": "MarketsMicrostructure", "topic": "How Apex Capital's Models Use Market Data",
     "description": "Bridge lesson: how RSI, momentum, sector breadth, and options flow appear in the firm's ML feature set.",
     "key_concepts": ["RSI", "momentum factor", "options flow signal", "sector breadth"],
     "prereqs": ["Day 29", "Day 30", "Day 32"], "is_apex_bridge": True},

    # ── BLOCK 4: Portfolio Management (Days 34–52) ──────────────────────────
    {"day": 34, "teacher": "PortfolioManagement", "topic": "Modern Portfolio Theory",
     "description": "Harry Markowitz, efficient frontier, diversification, covariance matrix. Why a low-return asset can improve a portfolio.",
     "key_concepts": ["efficient frontier", "covariance", "Sharpe ratio", "minimum variance"],
     "prereqs": ["Day 5"]},
    {"day": 35, "teacher": "PortfolioManagement", "topic": "CAPM & Factor Models",
     "description": "Capital Asset Pricing Model — systematic vs. idiosyncratic risk. Fama-French 3-factor model. Alpha generation.",
     "key_concepts": ["beta", "alpha", "systematic risk", "Fama-French factors"],
     "prereqs": ["Day 6", "Day 34"]},
    {"day": 36, "teacher": "PortfolioManagement", "topic": "Risk Metrics — Sharpe, Sortino, Max Drawdown",
     "description": "How to evaluate a portfolio's risk-adjusted performance. What the metrics tell you and what they hide.",
     "key_concepts": ["Sharpe ratio", "Sortino ratio", "maximum drawdown", "Calmar ratio"],
     "prereqs": ["Day 34", "Day 35"]},
    {"day": 37, "teacher": "PortfolioManagement", "topic": "Value at Risk (VaR) & CVaR",
     "description": "How Apex Capital's risk manager uses VaR. Parametric vs. historical vs. Monte Carlo. Why CVaR is better.",
     "key_concepts": ["VaR", "CVaR", "tail risk", "confidence interval"],
     "prereqs": ["Day 36"], "is_apex_bridge": True},
    {"day": 38, "teacher": "PortfolioManagement", "topic": "Asset Allocation — Strategic & Tactical",
     "description": "60/40 portfolio, risk parity, factor tilts. Rebalancing mechanics. How macro regimes should shift allocation.",
     "key_concepts": ["strategic asset allocation", "tactical asset allocation", "risk parity", "rebalancing"],
     "prereqs": ["Day 34", "Day 35"]},
    {"day": 39, "teacher": "PortfolioManagement", "topic": "Hedge Fund Strategies",
     "description": "Long/short equity, market neutral, event-driven, macro, quant. How each generates alpha differently.",
     "key_concepts": ["long/short equity", "market neutral", "event-driven", "quant strategies"],
     "prereqs": ["Day 35", "Day 38"]},
    {"day": 40, "teacher": "PortfolioManagement", "topic": "Position Sizing & Kelly Criterion",
     "description": "How to size positions given edge, odds, and bankroll. Kelly full vs. half-Kelly. Apex's dynamic position sizer.",
     "key_concepts": ["Kelly Criterion", "bet sizing", "edge", "ruin probability"],
     "prereqs": ["Day 36", "Day 37"], "is_apex_bridge": True},
    {"day": 41, "teacher": "PortfolioManagement", "topic": "Performance Attribution",
     "description": "Brinson-Hood-Beebower attribution. Sector selection vs. stock selection effect. How to explain returns.",
     "key_concepts": ["Brinson attribution", "selection effect", "allocation effect", "interaction effect"],
     "prereqs": ["Day 35", "Day 36"]},
    {"day": 42, "teacher": "PortfolioManagement", "topic": "Portfolio Management Review & Integration",
     "description": "Full review of Blocks 3 & 4. Live case: review Apex's current portfolio using tools from the course.",
     "key_concepts": ["integrated portfolio review", "live Apex case"],
     "prereqs": ["Day 34-41"], "is_review": True, "is_apex_bridge": True},

    # ── BLOCK 5: Integration & Advanced Topics (Days 43–60) ──────────────────
    {"day": 43, "teacher": "InvestmentBanking", "topic": "Full LBO Case Study — End to End",
     "description": "A complete paper LBO from scratch: entry, operating assumptions, debt schedule, exit, returns.",
     "key_concepts": ["paper LBO", "case study", "returns bridge"],
     "prereqs": ["Day 18", "Day 19", "Day 23"]},
    {"day": 44, "teacher": "FinanceFundamentals", "topic": "Reading Real 10-Ks & 10-Qs",
     "description": "Navigate an actual SEC filing. Find the key metrics quickly. Spot red flags in footnotes.",
     "key_concepts": ["10-K", "10-Q", "MD&A", "footnotes", "red flags"],
     "prereqs": ["Day 1-14"]},
    {"day": 45, "teacher": "MarketsMicrostructure", "topic": "Earnings Season & Catalysts",
     "description": "How markets react to earnings. EPS beats/misses, guidance, whisper numbers. Using options to trade earnings.",
     "key_concepts": ["earnings surprise", "guidance", "whisper number", "implied move"],
     "prereqs": ["Day 28", "Day 29", "Day 32"]},
    {"day": 46, "teacher": "PortfolioManagement", "topic": "Macro Factors & the Business Cycle",
     "description": "How the economy moves through expansion, peak, contraction, trough. Sector rotation playbook.",
     "key_concepts": ["business cycle", "sector rotation", "leading indicators", "yield curve inversion"],
     "prereqs": ["Day 27", "Day 38"]},
    {"day": 47, "teacher": "InvestmentBanking", "topic": "Tech Sector Valuation — Revenue Multiples & Rule of 40",
     "description": "Why SaaS companies trade on ARR multiples. Rule of 40. NRR, churn, CAC payback.",
     "key_concepts": ["EV/ARR", "Rule of 40", "NRR", "CAC payback", "LTV/CAC"],
     "prereqs": ["Day 7", "Day 8"]},
    {"day": 48, "teacher": "FinanceFundamentals", "topic": "Financial Modeling Best Practices",
     "description": "Excel/spreadsheet discipline. Hardcodes vs. formulas. Error-checking. Circular references in a DCF.",
     "key_concepts": ["model architecture", "circular reference", "error checking", "sensitivity"],
     "prereqs": ["Day 7", "Day 19"]},
    {"day": 49, "teacher": "MarketsMicrostructure", "topic": "Technical Analysis — What Works & What Doesn't",
     "description": "Support/resistance, moving averages, RSI, MACD. Academic evidence. How Apex uses TA as signals.",
     "key_concepts": ["moving average", "RSI", "MACD", "support/resistance"],
     "prereqs": ["Day 25", "Day 32"], "is_apex_bridge": True},
    {"day": 50, "teacher": "PortfolioManagement", "topic": "ESG & Alternative Data",
     "description": "How ESG ratings work. Alternative data: satellite imagery, credit card data, app downloads. Alpha decay.",
     "key_concepts": ["ESG screening", "alternative data", "data edge", "alpha decay"],
     "prereqs": ["Day 35", "Day 39"]},
    {"day": 51, "teacher": "InvestmentBanking", "topic": "SPACs, Spin-offs, Carve-outs",
     "description": "How each transaction type works, why companies use them, and how to value each.",
     "key_concepts": ["SPAC", "spin-off", "carve-out", "tracking stock"],
     "prereqs": ["Day 15", "Day 20"]},
    {"day": 52, "teacher": "PortfolioManagement", "topic": "Building Apex Capital's Full Investment Thesis",
     "description": "Capstone session: construct a complete investment thesis for one of Apex's current positions using every tool from the course.",
     "key_concepts": ["investment thesis", "valuation", "risk/reward", "catalyst"],
     "prereqs": ["Day 1-51"], "is_review": True, "is_apex_bridge": True},
]

# Fill remaining days 53–90 with advanced review and case studies
for _d in range(53, 91):
    _block = (_d - 53) % 4
    _teacher = ["FinanceFundamentals", "InvestmentBanking", "MarketsMicrostructure", "PortfolioManagement"][_block]
    CURRICULUM.append({
        "day": _d,
        "teacher": _teacher,
        "topic": f"Advanced Case Study & Review — Day {_d}",
        "description": "Deep-dive case study drawing on all prior material. Teacher selects the most relevant live example from current markets.",
        "key_concepts": ["case study", "live market application", "full-cycle review"],
        "prereqs": [f"Day 1-{_d-1}"],
    })


# =============================================================================
# QUIZ BANK (sample questions per topic key concept)
# =============================================================================

QUIZ_BANK = {
    "EBITDA": [
        {"q": "A company has revenue of $500M, COGS of $200M, SG&A of $100M, and D&A of $40M. What is EBITDA?",
         "a": "$240M  (Revenue $500 – COGS $200 – SG&A $100 = EBIT $200; EBIT $200 + D&A $40 = EBITDA $240)"},
        {"q": "Why do bankers use EBITDA rather than net income as the primary earnings metric in M&A?",
         "a": "EBITDA strips out capital structure (interest), taxes (jurisdiction-dependent), and non-cash charges (D&A), making it a cleaner proxy for operating cash generation and cross-company comparable."},
    ],
    "DCF": [
        {"q": "Walk me through a DCF in 4 sentences.",
         "a": "Project the company's unlevered free cash flows over a 5–10 year period. Calculate a terminal value (Gordon Growth or exit multiple). Discount all cash flows back to the present using WACC. Bridge from enterprise value to equity value by subtracting net debt."},
        {"q": "If WACC increases from 9% to 11%, what happens to your DCF valuation? Why?",
         "a": "Valuation decreases because future cash flows are discounted more heavily — a higher denominator produces a lower present value. Terminal value is particularly sensitive as it compounds the effect."},
    ],
    "LBO": [
        {"q": "Name the 4 main value creation levers in an LBO.",
         "a": "1) Debt paydown (deleveraging increases equity value); 2) EBITDA growth (organic or M&A); 3) Multiple expansion (selling at a higher EV/EBITDA than you paid); 4) Cash generation / dividend recaps."},
        {"q": "A PE firm buys a company for 8x EBITDA with 60% debt. After 5 years EBITDA doubles and debt is half paid down. Entry equity check was $400M. Estimate the exit equity value at 9x EBITDA.",
         "a": "Entry EV = 8x × EBITDA₀; if equity check = $400M at 40% equity, EV₀ = $1,000M → EBITDA₀ = $125M. Exit EBITDA = $250M. Exit EV = 9 × $250M = $2,250M. Remaining debt ≈ $300M. Exit equity ≈ $1,950M. MOIC ≈ 4.9x."},
    ],
    "Sharpe ratio": [
        {"q": "Portfolio A returns 12% with 15% vol. Portfolio B returns 9% with 8% vol. Risk-free rate is 3%. Which has a better Sharpe ratio?",
         "a": "A: (12-3)/15 = 0.60  |  B: (9-3)/8 = 0.75. Portfolio B is more efficient on a risk-adjusted basis despite lower raw returns."},
    ],
    "duration": [
        {"q": "A bond has a duration of 7 years. If yields rise 50 bps, approximately what happens to its price?",
         "a": "Price falls ≈ 7 × 0.50% = 3.5%. (Duration measures the percentage price change per 1% yield change, so 7 × 0.005 = 3.5% decline.)"},
    ],
    "Kelly Criterion": [
        {"q": "Your model has a 55% win rate and wins/losses are symmetrical (1:1). What does full Kelly say to bet?",
         "a": "f* = p − q = 0.55 − 0.45 = 0.10 → bet 10% of capital. Practitioners typically use half-Kelly (5%) to account for model uncertainty."},
    ],
}


# =============================================================================
# AGENT CLASSES
# =============================================================================

class TeachingCurriculumDirector:
    """
    Plans daily 30-minute sessions, tracks mastery, adapts the syllabus.
    If Jack struggles with a topic, all teachers are notified to reinforce it.
    """

    def __init__(self):
        self.progress = self._load_progress()
        self.today = date.today().isoformat()

    def _load_progress(self) -> dict:
        if PROGRESS_FILE.exists():
            return json.loads(PROGRESS_FILE.read_text())
        return {
            "student": "Jack Beard",
            "program_start": date.today().isoformat(),
            "current_day": 1,
            "completed_days": [],
            "mastery": {},          # {concept: score 0-100}
            "struggle_flags": [],   # concepts below 60 mastery
            "quiz_history": [],
            "streak_days": 0,
            "last_session": None,
        }

    def _save_progress(self):
        PROGRESS_FILE.write_text(json.dumps(self.progress, indent=2))

    def get_current_day(self) -> int:
        return self.progress.get("current_day", 1)

    def get_lesson_for_day(self, day: int) -> Optional[dict]:
        for lesson in CURRICULUM:
            if lesson["day"] == day:
                return lesson
        return None

    def get_struggle_topics(self) -> list:
        return [
            k for k, v in self.progress.get("mastery", {}).items()
            if v < 60
        ]

    def prepare_session(self, day: Optional[int] = None) -> dict:
        day = day or self.get_current_day()
        lesson = self.get_lesson_for_day(day)
        if not lesson:
            return {"error": f"No lesson found for day {day}"}

        struggles = self.get_struggle_topics()
        warmup = None
        if struggles:
            warmup = f"Quick warmup on '{struggles[0]}' — this is a topic we're reinforcing."

        session = {
            "generated_at": datetime.now().isoformat(),
            "session_day": day,
            "student": "Jack Beard",
            "teacher": lesson["teacher"],
            "topic": lesson["topic"],
            "description": lesson["description"],
            "key_concepts": lesson["key_concepts"],
            "is_review": lesson.get("is_review", False),
            "is_apex_bridge": lesson.get("is_apex_bridge", False),
            "warmup_topic": warmup,
            "reinforcement_flags": struggles,
            "session_plan": self._build_session_plan(lesson, struggles),
            "quiz": self._build_quiz(lesson),
            "director_notes": self._generate_director_notes(day, struggles),
        }

        LESSON_FILE.write_text(json.dumps(session, indent=2))
        logger.info(f"Session prepared — Day {day}: '{lesson['topic']}' (teacher: {lesson['teacher']})")
        return session

    def _build_session_plan(self, lesson: dict, struggles: list) -> list:
        plan = []

        if struggles:
            plan.append({
                "minute": "0–3",
                "segment": "Warmup / Reinforcement",
                "content": f"60-second recap of '{struggles[0]}' — what was the key takeaway? Jack answers from memory. Teacher provides immediate feedback.",
            })
            start = 3
        else:
            start = 0

        plan.append({
            "minute": f"{start}–{start+15}",
            "segment": "Core Teaching",
            "content": f"Teacher delivers '{lesson['topic']}'. Concepts covered: {', '.join(lesson['key_concepts'][:3])}. Uses concrete examples and analogies, not jargon.",
        })
        plan.append({
            "minute": f"{start+15}–{start+22}",
            "segment": "Worked Example",
            "content": "Teacher walks through a numerical example or real-world case study applying the concepts.",
        })
        plan.append({
            "minute": f"{start+22}–{start+27}",
            "segment": "Jack's Turn",
            "content": "Jack attempts to answer 1–2 application questions before seeing the answers. Teacher coaches rather than corrects.",
        })
        plan.append({
            "minute": f"{start+27}–30",
            "segment": "Quiz & Wrap-up",
            "content": "2–3 quiz questions. Score recorded. Director updates mastery map. Preview of next session.",
        })
        return plan

    def _build_quiz(self, lesson: dict) -> list:
        questions = []
        for concept in lesson.get("key_concepts", []):
            if concept in QUIZ_BANK:
                questions.extend(QUIZ_BANK[concept][:1])
        if not questions:
            questions = [{
                "q": f"In your own words, what is the most important takeaway from today's topic: '{lesson['topic']}'?",
                "a": "Open-ended — teacher evaluates based on key concepts being present in the answer.",
            }]
        return questions[:3]

    def _generate_director_notes(self, day: int, struggles: list) -> str:
        notes = []
        if struggles:
            notes.append(f"Struggling topics detected: {', '.join(struggles)}. All teachers have been notified to weave reinforcement into upcoming sessions.")
        streak = self.progress.get("streak_days", 0)
        if streak > 0:
            notes.append(f"Current streak: {streak} consecutive days. Momentum is building.")
        if day % 7 == 0:
            notes.append("End of week — consider a 10-minute review of all concepts from this week before starting today's session.")
        return "  |  ".join(notes) if notes else "On track. Keep the pace."

    def record_quiz_result(self, day: int, scores: dict):
        for concept, score in scores.items():
            self.progress["mastery"][concept] = score

        # Update struggle flags
        self.progress["struggle_flags"] = [
            k for k, v in self.progress["mastery"].items() if v < 60
        ]

        self.progress["quiz_history"].append({
            "day": day,
            "date": self.today,
            "scores": scores,
            "avg": sum(scores.values()) / len(scores) if scores else 0,
        })

        # Advance day if quiz passed (avg >= 65)
        avg = sum(scores.values()) / len(scores) if scores else 0
        if avg >= 65 and day == self.get_current_day():
            self.progress["current_day"] = day + 1
            completed = self.progress.setdefault("completed_days", [])
            if day not in completed:
                completed.append(day)

        # Streak
        last = self.progress.get("last_session")
        if last == (date.today() - timedelta(days=1)).isoformat():
            self.progress["streak_days"] = self.progress.get("streak_days", 0) + 1
        elif last != self.today:
            self.progress["streak_days"] = 1
        self.progress["last_session"] = self.today

        self._save_progress()
        logger.info(f"Quiz recorded for Day {day} — avg score: {avg:.0f}/100")

    def progress_report(self) -> str:
        p = self.progress
        current = p.get("current_day", 1)
        completed = len(p.get("completed_days", []))
        struggles = p.get("struggle_flags", [])
        streak = p.get("streak_days", 0)
        mastery = p.get("mastery", {})
        top_concepts = sorted(mastery.items(), key=lambda x: x[1], reverse=True)[:3]
        weak_concepts = sorted(mastery.items(), key=lambda x: x[1])[:3]

        lines = [
            f"=== JACK'S LEARNING PROGRESS REPORT ===",
            f"Program start : {p.get('program_start', 'N/A')}",
            f"Current day   : {current} / 90",
            f"Days completed: {completed}",
            f"Streak        : {streak} consecutive days",
            f"",
            f"STRONGEST TOPICS:",
            *[f"  {c}: {s}/100" for c, s in top_concepts],
            f"",
            f"NEEDS REINFORCEMENT:",
            *[f"  {c}: {s}/100 ← flagged for all teachers" for c, s in weak_concepts if s < 70],
            f"",
            f"DIRECTOR'S ASSESSMENT:",
        ]
        if completed == 0:
            lines.append("  Day 1 — the journey begins. No data yet.")
        elif completed < 7:
            lines.append(f"  Early stage ({completed} days). Fundamentals phase. Build the foundation carefully.")
        elif completed < 21:
            lines.append(f"  Good progress ({completed} days). Investment banking block underway.")
        elif completed < 42:
            lines.append(f"  Strong ({completed} days). Markets and portfolio management in view.")
        else:
            lines.append(f"  Advanced stage ({completed} days). Integration and case studies.")
        if struggles:
            lines.append(f"  Reinforcing: {', '.join(struggles)}")
        return "\n".join(lines)


class FinanceFundamentalsTeacher:
    """
    Teaches accounting, valuation, and financial modelling.
    Harvard-caliber: expects precision, rewards curiosity.
    """
    name = "Prof. Harrington"
    title = "Finance Fundamentals — Head of Valuation & Accounting"
    philosophy = (
        "Every number on a financial statement is a story. My job is to teach you to read those stories "
        "before other people figure out what they say. We will build models, not memorise formulas. "
        "We will question assumptions, not accept them. By the end, a 10-K will feel like a conversation."
    )

    def deliver_lesson(self, lesson: dict) -> str:
        return (
            f"[{self.name} — {self.title}]\n\n"
            f"Today's Topic: {lesson['topic']}\n\n"
            f"{lesson['description']}\n\n"
            f"Key Concepts We Will Master Today:\n" +
            "\n".join(f"  • {c}" for c in lesson["key_concepts"]) +
            f"\n\nProf. Harrington's Opening:\n"
            f"Let me start with a question: why does a highly profitable company sometimes run out of cash? "
            f"The answer is the foundation of everything we cover today. "
            f"Keep that question in mind as we work through the material — by the end of the session, "
            f"you will answer it with precision."
        )


class InvestmentBankingTeacher:
    """
    Teaches M&A, LBOs, capital markets, pitch books.
    Street-smart and direct — how a senior banker actually thinks.
    """
    name = "Prof. Callahan"
    title = "Investment Banking — Head of M&A & Capital Markets"
    philosophy = (
        "Bankers are paid to be right fast. My sessions are high-intensity by design. "
        "We think through deals the way a managing director does on a Sunday night: "
        "quickly, cleanly, with a number attached to every opinion. "
        "There are no stupid questions — only answers without supporting analysis."
    )

    def deliver_lesson(self, lesson: dict) -> str:
        return (
            f"[{self.name} — {self.title}]\n\n"
            f"Today's Topic: {lesson['topic']}\n\n"
            f"{lesson['description']}\n\n"
            f"Concepts We're Drilling:\n" +
            "\n".join(f"  • {c}" for c in lesson["key_concepts"]) +
            f"\n\nProf. Callahan's Opening:\n"
            f"This is the topic that separates analysts who get staffed on live deals from those who don't. "
            f"I will teach you the concept, show you the math, and then you will show me you can do it cold. "
            f"No formula sheet. No notes. That's how it works on the floor, and that's how we do it here."
        )


class MarketsMicrostructureTeacher:
    """
    Teaches equities, bonds, options, and how markets actually function.
    Practitioner-first: academic theory is a tool, not the destination.
    """
    name = "Prof. Chen"
    title = "Markets & Microstructure — Head of Trading & Derivatives"
    philosophy = (
        "Markets are conversations between buyers and sellers at every price level. "
        "Understanding the mechanics — not just the theory — is what lets you find edge. "
        "We will connect textbook concepts to live market behaviour. "
        "By the end, when you see a VIX spike or an options skew, you'll know exactly what it means."
    )

    def deliver_lesson(self, lesson: dict) -> str:
        return (
            f"[{self.name} — {self.title}]\n\n"
            f"Today's Topic: {lesson['topic']}\n\n"
            f"{lesson['description']}\n\n"
            f"Today's Key Mechanics:\n" +
            "\n".join(f"  • {c}" for c in lesson["key_concepts"]) +
            f"\n\nProf. Chen's Opening:\n"
            f"Before we start — check a live market right now. What's the S&P doing? Up or down? "
            f"By more or less than expected? Keep that in your head. Today's lesson will explain "
            f"exactly why markets moved the way they did in the last 24 hours."
        )


class PortfolioManagementTeacher:
    """
    Teaches portfolio construction, risk management, factor models.
    Quant-literate and systematic — ties everything back to Apex's live portfolio.
    """
    name = "Prof. Okafor"
    title = "Portfolio Management — Head of Risk & Quantitative Strategy"
    philosophy = (
        "Portfolio management is applied decision-making under uncertainty. "
        "Every tool we cover — Sharpe ratios, VaR, Kelly — exists to help you make better decisions "
        "when you don't know the future. We build systems, not hunches. "
        "And we always, always ask: what's the downside?"
    )

    def deliver_lesson(self, lesson: dict) -> str:
        return (
            f"[{self.name} — {self.title}]\n\n"
            f"Today's Topic: {lesson['topic']}\n\n"
            f"{lesson['description']}\n\n"
            f"Today's Risk & Portfolio Toolkit:\n" +
            "\n".join(f"  • {c}" for c in lesson["key_concepts"]) +
            f"\n\nProf. Okafor's Opening:\n"
            f"Here is the framework for today: every concept we cover, I want you to immediately ask "
            f"'how would I apply this to Apex Capital's current portfolio?' "
            f"Theory exists to be applied. By the end of this session, you will be able to evaluate "
            f"a real portfolio using today's tools."
        )


# =============================================================================
# ORCHESTRATOR
# =============================================================================

TEACHERS = {
    "FinanceFundamentals":   FinanceFundamentalsTeacher(),
    "InvestmentBanking":     InvestmentBankingTeacher(),
    "MarketsMicrostructure": MarketsMicrostructureTeacher(),
    "PortfolioManagement":   PortfolioManagementTeacher(),
}


def run_teaching_session(day: Optional[int] = None, quiz_mode: bool = False) -> dict:
    director = TeachingCurriculumDirector()
    day = day or director.get_current_day()
    session = director.prepare_session(day)

    if "error" in session:
        logger.error(session["error"])
        return session

    # Deliver lesson via the appropriate teacher
    teacher_key = session.get("teacher", "FinanceFundamentals")
    teacher = TEACHERS.get(teacher_key, TEACHERS["FinanceFundamentals"])
    lesson_text = teacher.deliver_lesson(session)
    session["lesson_text"] = lesson_text

    if quiz_mode:
        print("\n" + "="*60)
        print("QUIZ TIME — Day", day)
        print("="*60)
        quiz = session.get("quiz", [])
        scores = {}
        for i, q_item in enumerate(quiz, 1):
            print(f"\nQ{i}: {q_item['q']}")
            input("Press Enter to see the answer...")
            print(f"A:  {q_item['a']}")
            raw = input("Self-score 0–100 (how well did you know this?): ").strip()
            try:
                score = int(raw)
            except ValueError:
                score = 50
            # Associate score with a concept
            concept = session["key_concepts"][i-1] if i-1 < len(session["key_concepts"]) else f"concept_{i}"
            scores[concept] = score
        director.record_quiz_result(day, scores)
        print("\n" + director.progress_report())

    LESSON_FILE.write_text(json.dumps(session, indent=2))
    return session


def run_teaching_division_report() -> str:
    director = TeachingCurriculumDirector()
    return director.progress_report()


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Capital Teaching Division")
    parser.add_argument("--quiz",   action="store_true", help="Run interactive quiz after lesson")
    parser.add_argument("--report", action="store_true", help="Print progress report")
    parser.add_argument("--day",    type=int,            help="Generate lesson for specific day number")
    args = parser.parse_args()

    BASE_DIR.parent  # ensure path resolution works when run from project root

    if args.report:
        print(run_teaching_division_report())
    else:
        session = run_teaching_session(day=args.day, quiz_mode=args.quiz)
        if "lesson_text" in session:
            print("\n" + "="*60)
            print(session["lesson_text"])
            print("\n--- SESSION PLAN ---")
            for step in session.get("session_plan", []):
                print(f"  [{step['minute']} min] {step['segment']}: {step['content'][:100]}")
            print("\n--- QUIZ ---")
            for i, q in enumerate(session.get("quiz", []), 1):
                print(f"  Q{i}: {q['q']}")
            notes = session.get("director_notes", "")
            if notes:
                print(f"\nDirector's Notes: {notes}")
