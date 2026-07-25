#!/usr/bin/env python3
"""
quiz_master.py — Assessment & Retention Agent
==============================================
Designs and delivers quizzes after each lesson.
Tracks correct/incorrect responses, identifies weak spots,
updates the spaced repetition queue via quiz results.

Quiz format:
  5 questions per session, 30-second reading limit each
  Mix: 2 conceptual, 2 calculation, 1 case study
  Pass: >= 80% (4/5 correct)

Outputs:
  - data/latest_quiz_result.json: score + question breakdown
  - Email to student with results and explanations
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")
logger = logging.getLogger("QuizMaster")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

QUESTION_BANK = {
    "F1": [
        {"q": "Which financial statement shows profitability over a PERIOD of time?",
         "choices": ["A) Balance Sheet", "B) Income Statement", "C) Cash Flow Statement", "D) Equity Statement"],
         "answer": "B", "explanation": "Income Statement covers a period (quarter/year). Balance Sheet is a snapshot at a point in time."},
        {"q": "A company has Net Income of $100M and D&A of $20M. Operating cash flow is roughly:",
         "choices": ["A) $80M", "B) $100M", "C) $120M", "D) Not determinable from this info"],
         "answer": "C", "explanation": "OCF ≈ Net Income + D&A (when ignoring working capital changes). D&A is added back because it's non-cash."},
        {"q": "What does 'accrual accounting' mean?",
         "choices": ["A) Revenue recorded when cash is received", "B) Revenue recorded when earned, regardless of cash", "C) Expenses recorded when paid", "D) Assets valued at market price"],
         "answer": "B", "explanation": "Accrual: revenue recognized when earned. Cash accounting: recognized when cash received. Most public companies use accrual."},
        {"q": "Assets = Liabilities + ___",
         "choices": ["A) Revenue", "B) Net Income", "C) Shareholders' Equity", "D) EBITDA"],
         "answer": "C", "explanation": "The fundamental accounting equation. This ALWAYS holds true. Equity = what owners would receive if assets were liquidated and debts paid."},
        {"q": "If EBITDA = $50M and D&A = $10M, what is EBIT?",
         "choices": ["A) $40M", "B) $50M", "C) $60M", "D) $55M"],
         "answer": "A", "explanation": "EBIT = EBITDA - D&A = $50M - $10M = $40M. EBIT = Earnings Before Interest & Taxes."},
    ],
    "V1": [
        {"q": "In a DCF, what does the discount rate represent?",
         "choices": ["A) The company's revenue growth rate", "B) The required rate of return / cost of capital", "C) The inflation rate", "D) The risk-free rate"],
         "answer": "B", "explanation": "The discount rate (WACC) is the required return for investors — it accounts for the time value of money and risk."},
        {"q": "If WACC = 10% and terminal growth rate = 2%, the perpetuity growth factor is:",
         "choices": ["A) 8x", "B) 10x", "C) 12.5x", "D) Not calculable"],
         "answer": "C", "explanation": "1/(WACC-g) = 1/(10%-2%) = 1/8% = 12.5x. This multiplied by the final year FCF gives terminal value."},
        {"q": "Which of the following is FREE CASH FLOW (to the firm)?",
         "choices": ["A) Net Income + D&A", "B) EBIT(1-t) + D&A - CapEx - ΔNWC", "C) EBITDA - CapEx", "D) Revenue - COGS"],
         "answer": "B", "explanation": "FCFF = EBIT × (1-tax) + D&A - CapEx - Change in Working Capital. This is the unlevered free cash flow available to all capital providers."},
        {"q": "Enterprise Value minus net debt equals:",
         "choices": ["A) EBITDA", "B) Revenue", "C) Equity Value", "D) Book Value"],
         "answer": "C", "explanation": "EV = Equity Value + Net Debt (debt minus cash). So Equity Value = EV - Net Debt."},
        {"q": "In a typical DCF, what percentage of value usually comes from Terminal Value?",
         "choices": ["A) 20-30%", "B) 40-50%", "C) 60-80%", "D) 90-100%"],
         "answer": "C", "explanation": "For most companies, Terminal Value = 60-80% of total DCF value. This is why TV assumptions are critical — small changes drive huge value differences."},
    ],
    "IB1": [
        {"q": "What comes FIRST in a sell-side M&A process?",
         "choices": ["A) LOI signing", "B) Due diligence", "C) Preparation of CIM", "D) Regulatory filing"],
         "answer": "C", "explanation": "Preparation phase (CIM, teaser, target list) precedes everything. You can't market a deal without materials."},
        {"q": "A 'go-shop' provision allows:",
         "choices": ["A) The acquirer to raise financing post-signing", "B) The target to solicit competing bids after signing", "C) Employees to exercise stock options early", "D) The regulator to review the deal faster"],
         "answer": "B", "explanation": "Go-shop = target CAN actively look for a better deal after signing. Typically 25-50 days. Designed to protect against fiduciary duty claims."},
        {"q": "A deal has a 3% break-up fee on $20B deal value. If a competing bid emerges at $22B, the original buyer must pay ___ to walk away:",
         "choices": ["A) $200M", "B) $600M", "C) $2B", "D) $660M"],
         "answer": "B", "explanation": "$20B × 3% = $600M. This is the 'reverse termination fee' the buyer pays if they walk. $22B - $20B = $2B cost to raise bid, so buyer might pay $600M to walk."},
        {"q": "What does NDA stand for in M&A context?",
         "choices": ["A) Non-Dilutive Agreement", "B) Net Debt Adjustment", "C) Non-Disclosure Agreement", "D) New Deal Authorization"],
         "answer": "C", "explanation": "Non-Disclosure Agreement signed before receiving confidential information (CIM). Prevents buyer from using info to trade the stock or poach employees."},
        {"q": "In M&A, 'strategic buyer' refers to:",
         "choices": ["A) A private equity firm", "B) A hedge fund", "C) A company in the same or adjacent industry", "D) A sovereign wealth fund"],
         "answer": "C", "explanation": "Strategic buyer = operational company that can capture synergies. Financial buyer = PE firm (uses leverage, looks for financial returns). Strategics often pay more due to synergies."},
    ],
    "M5": [
        {"q": "The Sharpe Ratio equals:",
         "choices": ["A) (Return - Risk-Free Rate) / Standard Deviation", "B) Return / Beta", "C) Return / Volatility", "D) Alpha / Beta"],
         "answer": "A", "explanation": "Sharpe = (Rp - Rf) / σp. It measures excess return per unit of TOTAL risk (std dev). Apex Capital's ML model uses Sharpe-like target: return / volatility."},
        {"q": "If Beta = 1.5 and SPY returns 10%, CAPM predicts the stock returns:",
         "choices": ["A) 10%", "B) 13%", "C) 15%", "D) 7.5% + risk-free rate"],
         "answer": "D", "explanation": "CAPM: E(r) = Rf + β(Rm - Rf). If Rf=4%, β=1.5, Rm=10%: E(r) = 4% + 1.5×(10%-4%) = 4% + 9% = 13%. Choice D is the general formula."},
        {"q": "Portfolio diversification works because different assets have:",
         "choices": ["A) High positive correlations", "B) Zero returns", "C) Low or negative correlations", "D) High betas"],
         "answer": "C", "explanation": "Diversification reduces unsystematic risk when assets are NOT perfectly correlated. If ρ < 1, combining assets reduces total portfolio volatility."},
        {"q": "In CAPM, what risk CANNOT be diversified away?",
         "choices": ["A) Company-specific risk", "B) Industry risk", "C) Management risk", "D) Systematic (market) risk"],
         "answer": "D", "explanation": "Systematic risk (beta) affects ALL assets — recessions, interest rate changes, wars. Only idiosyncratic (company-specific) risk can be diversified away."},
        {"q": "Apex Capital's ML model achieves AUC of 0.58 for 20-day returns. What does this tell you?",
         "choices": ["A) The model is wrong 42% of the time", "B) The model beats random guessing by 8 percentage points", "C) 58% of predictions are correct", "D) The model's Sharpe ratio is 0.58"],
         "answer": "B", "explanation": "AUC of 0.50 = random. AUC 0.58 = model is right 8pp more often than chance. Not 'correct 58% of time' — AUC measures RANKING ability across all thresholds."},
    ],
    "M7": [
        {"q": "When must institutional investors file a 13F?",
         "choices": ["A) Every month", "B) Immediately after each trade", "C) Within 45 days after quarter end", "D) Annually"],
         "answer": "C", "explanation": "13F required within 45 days after calendar quarter end. This lag is WHY 13F-based strategies have edge — the signal is semi-stale but still predictive!"},
        {"q": "Which funds are REQUIRED to file 13F?",
         "choices": ["A) All hedge funds", "B) Any investor with >$100M in 13F securities", "C) Only mutual funds", "D) Any fund with >$1B AUM"],
         "answer": "B", "explanation": "$100M threshold in Section 13(f) securities (US equities, ETFs, options). This includes pension funds, mutual funds, hedge funds, family offices above the threshold."},
        {"q": "conviction_score in Apex's system measures:",
         "choices": ["A) The size of the position in dollars", "B) A composite of position size, duration, and institutional quality", "C) The number of institutions holding the stock", "D) The analyst's recommendation"],
         "answer": "B", "explanation": "Conviction score combines: position size as % of fund AUM, how long they've held (hold streak), fund's historical accuracy (hit rate). A small fund putting 10% of AUM in one stock = VERY high conviction."},
        {"q": "Why does conviction_velocity beat conviction_score in predicting returns? (from our experiments)",
         "choices": ["A) Velocity is easier to compute", "B) Rising conviction (adding to a position) is more predictive than static holding", "C) Velocity correlates with market returns", "D) Score has data quality issues"],
         "answer": "B", "explanation": "Static score: a fund HOLDING Apple at 5% tells you little. Rising score: a fund INCREASING Apple from 3% to 7% over 3 quarters = they're actively adding. That's the real signal."},
        {"q": "What is 'herding' in 13F context?",
         "choices": ["A) Multiple institutions buying the same stock simultaneously", "B) A fund copying another fund's portfolio", "C) Institutions selling due to fear", "D) Concentrated position in one sector"],
         "answer": "A", "explanation": "Herding = multiple funds adding to same stock in same quarter. Can amplify price movement but also signals broad institutional consensus. Apex's herding_flag captures this."},
    ],
}

DEFAULT_QUIZ = [
    {"q": "What is the fundamental accounting equation?",
     "choices": ["A) Revenue = Expenses + Profit", "B) Assets = Liabilities + Equity", "C) Cash = Income + D&A", "D) EV = Equity + Debt - Cash"],
     "answer": "B", "explanation": "Assets = Liabilities + Shareholders' Equity. Always. This is the foundation of double-entry bookkeeping."},
    {"q": "What does EBITDA stand for?",
     "choices": ["A) Earnings Before Interest Tax Depreciation Amortization",
                 "B) Earnings Before Income Tax Distributions and Advances",
                 "C) Enterprise Balance Including Total Debt Allowances",
                 "D) Equity Based Income Tax Dividend Amount"],
     "answer": "A", "explanation": "EBITDA is a proxy for operating cash generation — it strips out financing decisions (interest), tax jurisdiction, and non-cash charges (D&A)."},
]


def generate_quiz(module_id: str, lesson_content: dict) -> dict:
    """Generate a quiz for the given module."""
    questions = QUESTION_BANK.get(module_id, DEFAULT_QUIZ)
    # Select 5 questions (or all if fewer)
    selected = questions[:5]
    return {
        "module_id": module_id,
        "questions": selected,
        "num_questions": len(selected),
        "passing_score": 80,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "time_limit_seconds": 300,  # 5 min for 5 questions
    }


def evaluate_responses(quiz: dict, responses: list) -> dict:
    """
    Evaluate quiz responses.
    responses: list of answer choices like ["A", "B", "C", "D", "B"]
    """
    questions = quiz["questions"]
    if not responses:
        # No responses provided — return empty result (used when quiz is delivered via email)
        return {
            "score_pct": 0,
            "num_correct": 0,
            "num_questions": len(questions),
            "awaiting_responses": True,
            "quiz_id": quiz.get("module_id"),
        }
    
    results = []
    correct = 0
    for i, (q, resp) in enumerate(zip(questions, responses)):
        is_correct = (resp.upper() == q["answer"])
        if is_correct:
            correct += 1
        results.append({
            "question_num": i + 1,
            "question": q["q"],
            "your_answer": resp,
            "correct_answer": q["answer"],
            "is_correct": is_correct,
            "explanation": q["explanation"],
        })
    
    score_pct = round(correct / len(questions) * 100)
    return {
        "module_id": quiz["module_id"],
        "score_pct": score_pct,
        "num_correct": correct,
        "num_questions": len(questions),
        "passed": score_pct >= quiz["passing_score"],
        "results": results,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "message": f"{'✅ PASSED' if score_pct >= 80 else '❌ Need review'}: {score_pct}% ({correct}/{len(questions)})",
    }


def deliver_quiz_to_student(quiz: dict, lesson_content: dict) -> None:
    """Save quiz to file for student to answer, and email it."""
    quiz_path = DATA_DIR / "todays_quiz.json"
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(quiz, f, indent=2)
    os.replace(tmp, str(quiz_path))
    
    # Email the quiz (via email buffer)
    try:
        sys.path.insert(0, str(BASE_DIR))
        from email_buffer import buffer_email
        
        module_id = quiz["module_id"]
        questions_text = ""
        for i, q in enumerate(quiz["questions"], 1):
            questions_text += f"\n\nQ{i}: {q['q']}\n"
            for choice in q["choices"]:
                questions_text += f"  {choice}\n"
        
        subject = f"📚 Apex Academy — Quiz: {lesson_content.get('module_title', module_id)}"
        body = (f"Today's lesson: {lesson_content.get('module_title', module_id)}\n\n"
                f"QUIZ — Answer all 5 questions:\n{questions_text}\n\n"
                f"Reply with your answers (e.g., 'B, C, A, D, B') to update your progress.\n"
                f"Passing score: 80% (4/5)\n\n"
                f"Good luck, Jack! 🎯")
        buffer_email(subject, body, sender_name="Apex Academy Quiz Master")
        logger.info(f"  Quiz emailed to student: {module_id}")
    except Exception as e:
        logger.debug(f"  Email non-fatal: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", type=str, default="F1")
    parser.add_argument("--responses", type=str, help="Comma-separated answers: A,B,C,D,B")
    args = parser.parse_args()
    
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — QUIZ MASTER")
    logger.info("=" * 55)
    
    lesson_content = {}
    lesson_path = DATA_DIR / "todays_lesson_content.json"
    if lesson_path.exists():
        with open(lesson_path) as f:
            lesson_content = json.load(f)
    
    module_id = args.module or lesson_content.get("module_id", "F1")
    quiz = generate_quiz(module_id, lesson_content)
    
    if args.responses:
        responses = [r.strip().upper() for r in args.responses.split(",")]
        result = evaluate_responses(quiz, responses)
        logger.info(f"  {result['message']}")
        quiz_result_path = DATA_DIR / "latest_quiz_result.json"
        fd, tmp = str.__new__(str, ""), None
        import tempfile
        fd_val, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        with os.fdopen(fd_val, "w") as f:
            json.dump(result, f, indent=2)
        os.replace(tmp, str(quiz_result_path))
        logger.info(f"  Result saved: {result['score_pct']}%")
    else:
        deliver_quiz_to_student(quiz, lesson_content)
        logger.info(f"  Quiz delivered for module {module_id}")


if __name__ == "__main__":
    main()
