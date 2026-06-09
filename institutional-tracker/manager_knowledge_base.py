# =============================================================================
# APEX CAPITAL — MANAGER AGENT KNOWLEDGE BASE
# =============================================================================
# This module encodes the accumulated knowledge the Manager Agent draws on
# when evaluating every investment decision. Sources:
#
#   HARVARD BUSINESS SCHOOL
#   ─────────────────────────────────────────────────────────────────────
#   • Porter's Five Forces (competitive intensity framework)
#   • BCG Growth-Share Matrix (portfolio positioning)
#   • Disruptive Innovation — Clayton Christensen
#   • Blue Ocean Strategy — Kim & Mauborgne
#   • Competitive Advantage & Value Chain — Michael Porter
#   • Agency Theory & Capital Allocation — Jensen & Meckling
#   • HBS Case Method: structured problem decomposition
#
#   WARREN BUFFETT — BERKSHIRE HATHAWAY ANNUAL LETTERS (1965–2024)
#   ─────────────────────────────────────────────────────────────────────
#   • Economic moat (wide, narrow, none)
#   • Circle of competence — only invest in what you understand
#   • Mr. Market — price is what you pay, value is what you get
#   • Margin of safety — always demand a discount to intrinsic value
#   • Compounding — time is the friend of wonderful businesses
#   • Owner mentality — think like a business owner, not a stock trader
#   • Permanent capital loss is the only real risk
#   • Fear when others are greedy; greedy when others are fearful
#   • Wonderful company at fair price > fair company at wonderful price
#   • Management quality matters as much as the business model
#   • Float and capital allocation are the engines of Berkshire
#
#   BENJAMIN GRAHAM — THE INTELLIGENT INVESTOR, SECURITY ANALYSIS
#   ─────────────────────────────────────────────────────────────────────
#   • Margin of safety: buy at significant discount to intrinsic value
#   • Mr. Market: treat price fluctuations as opportunities, not signals
#   • Defensive investor: diversification, quality, consistent earnings
#   • Aggressive investor: deep value, special situations, workouts
#   • Net-net valuation: assets vs. market cap
#
#   CHARLIE MUNGER — POOR CHARLIE'S ALMANACK
#   ─────────────────────────────────────────────────────────────────────
#   • Latticework of mental models: use multiple lenses simultaneously
#   • Inversion: always ask "what could go wrong?"
#   • Lollapalooza effect: multiple factors reinforcing same conclusion
#   • Avoid psychological biases: anchoring, confirmation, availability
#   • Sit on your ass investing: fewer, better decisions beat constant activity
#   • Understand incentives: show me the incentive, I'll show you the outcome
#
#   PHILIP FISHER — COMMON STOCKS AND UNCOMMON PROFITS
#   ─────────────────────────────────────────────────────────────────────
#   • Scuttlebutt: research beyond financial statements (competitors, customers)
#   • 15-point checklist: growth potential, R&D, sales organisation, margins
#   • Hold great companies through adversity
#   • Concentrated portfolio of truly understood businesses
#
#   HOWARD MARKS — THE MOST IMPORTANT THING
#   ─────────────────────────────────────────────────────────────────────
#   • Second-level thinking: what does everyone else think, and are they wrong?
#   • Risk is not volatility — it is the probability of permanent loss
#   • Market cycles: understanding where we are in the cycle
#   • Knowing what you don't know is more powerful than what you do know
#   • Defensive investing: if you avoid the losers, the winners take care of themselves
#
#   NASSIM TALEB — THE BLACK SWAN, ANTIFRAGILE
#   ─────────────────────────────────────────────────────────────────────
#   • Fat tails: extreme events are more common than normal distributions assume
#   • Antifragility: structure portfolio to gain from volatility, not just survive it
#   • Barbell strategy: safe core + asymmetric upside bets
#   • Avoid ruin: never risk the whole portfolio on a single outcome
#
#   UNIVERSITY OF CHICAGO — ECONOMICS & FINANCE
#   ─────────────────────────────────────────────────────────────────────
#   Eugene Fama: Efficient Market Hypothesis (semi-strong form)
#     → Prices reflect all publicly available information
#     → Active alpha requires skill + structural edge, not luck
#   Fama-French Factor Model (3-factor, then 5-factor):
#     → Market risk premium (beta)
#     → Size premium (small cap outperforms)
#     → Value premium (high book-to-market outperforms)
#     → Profitability (robust > weak)
#     → Investment (conservative > aggressive)
#   Momentum factor (Carhart 4-factor): 12-1 month momentum predicts returns
#   Richard Thaler: Behavioral Economics
#     → Mental accounting, loss aversion, status quo bias
#     → Markets are NOT always rational — exploit mispricings
#   Robert Shiller: Irrational Exuberance
#     → CAPE ratio: cyclically adjusted P/E as valuation gauge
#     → Narrative economics: stories drive markets as much as fundamentals
#   Milton Friedman: Monetarism
#     → Inflation is always and everywhere a monetary phenomenon
#     → Interest rate environment is the cost of capital and discount rate
#   George Soros: Reflexivity
#     → Market participants' beliefs affect the fundamentals they are measuring
#     → Self-reinforcing trends exist until they reverse suddenly
#
#   KEY ECONOMICS BOOKS
#   ─────────────────────────────────────────────────────────────────────
#   • "Against the Gods" — Peter Bernstein (history of risk)
#   • "Misbehaving" — Richard Thaler (behavioural biases in markets)
#   • "Animal Spirits" — Akerlof & Shiller (confidence and the economy)
#   • "The Alchemy of Finance" — George Soros (reflexivity theory)
#   • "A Random Walk Down Wall Street" — Burton Malkiel
#   • "Stocks for the Long Run" — Jeremy Siegel (equities over time)
#   • "Capital in the 21st Century" — Thomas Piketty (inequality + returns)
#   • "The Wealth of Nations" — Adam Smith (invisible hand, specialisation)
#
# =============================================================================

from typing import Optional


# =============================================================================
# MOAT CLASSIFICATION
# =============================================================================

# Sectors with structurally wide economic moats (Buffett / Porter)
WIDE_MOAT_SECTORS = {
    "Technology", "Semiconductors", "Cloud & SaaS", "Cybersecurity",
    "Financials",   # network effects in payments (V, MA)
    "Healthcare",   # regulatory barriers, IP
    "Biotech",      # patent protection
    "Defense & Aerospace",  # sole-source government contracts
}

# Characteristics that indicate a moat at the ticker level
MOAT_INDICATORS = {
    "network_effect":       ["META", "GOOGL", "MSFT", "NFLX", "UBER", "ABNB"],
    "switching_cost":       ["ORCL", "CRM", "NOW", "ADBE", "INTU", "MSFT", "SAP"],
    "cost_advantage":       ["WMT", "COST", "AMZN", "DELL"],
    "intangible_assets":    ["LLY", "ABBV", "MRK", "UNH", "V", "MA", "BLK"],
    "efficient_scale":      ["AMT", "PLD", "EQIX", "AWK"],
}

# Fama-French factor proxies — tickers with known factor tilts
FF_VALUE_TICKERS     = {"BRK-B", "JPM", "BAC", "WFC", "C", "GS"}
FF_MOMENTUM_LEADERS  = {"NVDA", "META", "MSFT", "AVGO", "GE", "ETN"}
FF_QUALITY_HIGH      = {"V", "MA", "MSFT", "GOOGL", "AAPL", "LLY", "UNH"}


# =============================================================================
# BUFFETT PRINCIPLES — Applied as filters
# =============================================================================

BUFFETT_PRINCIPLES = [
    "Invest only within your circle of competence.",
    "Price is what you pay; value is what you get.",
    "The stock market is a device for transferring money from the impatient to the patient.",
    "Rule No. 1: Never lose money. Rule No. 2: Never forget Rule No. 1.",
    "Only buy something you'd be perfectly happy to hold for 10 years.",
    "Wide moat + excellent management + fair price = ideal long-term investment.",
    "Be fearful when others are greedy; greedy when others are fearful.",
    "Time is the friend of the wonderful business, the enemy of the mediocre.",
    "Diversification is protection against ignorance; makes little sense for the informed.",
    "The most important quality for an investor is temperament, not intellect.",
    "Never invest in a business you cannot understand.",
    "A company's intrinsic value is the discounted present value of all future cash flows.",
    "Float and capital allocation discipline are the engines of compounding.",
    "Avoid businesses with structurally deteriorating economics.",
    "Management candour and honesty are non-negotiable criteria.",
]

GRAHAM_PRINCIPLES = [
    "Always demand a margin of safety — buy at a discount to intrinsic value.",
    "Treat Mr. Market as your servant, not your master.",
    "Distinguish between investment and speculation at the outset.",
    "An investment operation is one that, upon thorough analysis, promises safety of principal and an adequate return.",
    "The defensive investor seeks safety and freedom from bother; the enterprising investor seeks superior results.",
    "Net asset value is the floor, not the ceiling, of a stock's worth.",
    "Earnings stability over 10 years matters more than one great year.",
    "Avoid companies with excessive debt relative to tangible assets.",
]

MUNGER_MENTAL_MODELS = [
    "Inversion: Before buying, ask what would have to be true for this to be a terrible investment.",
    "Lollapalooza: Multiple independent factors pointing in the same direction → high conviction.",
    "Sit on your ass investing: Activity is the enemy of returns for most investors.",
    "Incentive analysis: Understand what management is paid to do.",
    "Avoid the top-of-mind error: The vivid story is not the same as the true story.",
    "First principles: Strip away the narrative; what do the numbers actually say?",
    "Opportunity cost: Every investment decision is also a decision NOT to invest in alternatives.",
    "Avoid confirmation bias: Actively seek evidence that contradicts your thesis.",
    "Circle of competence must be real, not imagined.",
    "Failure to understand accounting is a fatal investor flaw.",
]

MARKS_PRINCIPLES = [
    "Second-level thinking: The consensus view is already in the price. What do YOU know that others don't?",
    "Risk is not volatility — it is the probability of permanent loss of capital.",
    "Understand where we are in the market cycle: early, middle, or late?",
    "The best investments are made when you are doing something others are afraid to do.",
    "Defensive investing: avoiding losers is more reliable than picking winners.",
    "Never forget the six-foot man who drowned crossing a stream five feet deep on average.",
    "Asymmetry of outcomes: limit downside, allow upside to run.",
]

TALEB_PRINCIPLES = [
    "Never risk ruin. Avoid bets that can wipe out the portfolio.",
    "Barbell: a safe, predictable core + small, asymmetric speculative positions.",
    "Fat tails: the market will produce extreme events more often than models predict.",
    "Antifragility: structure positions so volatility can be turned to advantage.",
    "Skin in the game: favour management with significant personal equity ownership.",
    "Fragility heuristic: if a company's model depends on perfect conditions, it is fragile.",
]

UCHICAGO_PRINCIPLES = [
    "Fama EMH: you need a structural or informational edge to generate alpha consistently.",
    "Fama-French: size + value + profitability + momentum factors explain most cross-sectional returns.",
    "Momentum (Carhart): stocks that have outperformed over 12-1 months tend to continue — but reverse sharply.",
    "Thaler: Markets misprice assets due to behavioural biases — exploit loss aversion and anchoring.",
    "Shiller CAPE: when the market-wide CAPE is above 30, expected 10-year returns compress significantly.",
    "Friedman: Inflation expectations and the real interest rate are the primary discount rate determinants.",
    "Soros reflexivity: prices move in self-reinforcing cycles; know which phase you are in.",
    "Fama on active management: most active managers underperform after fees — only structural edges survive.",
    "Thaler on mental accounting: investors sell winners too early and hold losers too long.",
]


# =============================================================================
# HARVARD FRAMEWORKS
# =============================================================================

PORTER_FIVE_FORCES_CHECKS = {
    "threat_of_new_entrants": (
        "Are there high barriers to entry? (capital, regulation, IP, network effects) "
        "Low barriers = margin compression risk."
    ),
    "supplier_power": (
        "Can suppliers raise prices? Fabless semis depend on TSMC/Samsung — "
        "concentration risk. Retailers with private label reduce supplier power."
    ),
    "buyer_power": (
        "Are customers fragmented or concentrated? B2B SaaS with Fortune 500 clients "
        "face churn risk from large buyers."
    ),
    "threat_of_substitutes": (
        "Can the product/service be replaced? Cloud replacing on-prem; "
        "EV displacing ICE; streaming displacing cable."
    ),
    "competitive_rivalry": (
        "How intense is the competition? Commodity industries compete on price; "
        "differentiated businesses compete on value."
    ),
}

HBS_DECISION_FRAMEWORK = [
    "Define the decision clearly: what exactly is being decided and why.",
    "Identify all relevant information: financials, competitive position, management quality.",
    "Generate alternatives: what are the options beyond buy/hold/sell?",
    "Evaluate trade-offs: return potential vs. risk, short-term vs. long-term.",
    "Consider second-order effects: what happens if everyone is correct about this?",
    "Apply the decision and set review criteria: under what conditions would you reverse?",
    "Document the reasoning: decisions made with clear rationale improve over time.",
]

BCG_MATRIX = {
    "star":         "High growth, high market share → invest and grow",
    "cash_cow":     "Low growth, high market share → harvest for free cash flow",
    "question_mark":"High growth, low market share → assess if it can become a star",
    "dog":          "Low growth, low market share → divest or ignore",
}


# =============================================================================
# KNOWLEDGE-ENHANCED EVALUATION
# =============================================================================

def get_moat_assessment(ticker: str, sector: str) -> dict:
    """
    Assess the economic moat of a ticker using Buffett/Porter frameworks.
    Returns a dict with moat_type, moat_sources, and moat_score (0–3).
    """
    moat_sources = []
    moat_score   = 0

    # Sector-level moat
    if sector in WIDE_MOAT_SECTORS:
        moat_score  += 1
        moat_sources.append(f"{sector} sector has structural competitive barriers")

    # Ticker-level moat
    for moat_type, tickers in MOAT_INDICATORS.items():
        if ticker in tickers:
            moat_score  += 1
            moat_sources.append(moat_type.replace("_", " ").title())

    moat_score = min(3, moat_score)
    moat_type  = {3: "wide", 2: "moderate", 1: "narrow", 0: "none"}.get(moat_score, "none")

    return {
        "moat_type":    moat_type,
        "moat_score":   moat_score,
        "moat_sources": moat_sources,
    }


def get_factor_alignment(ticker: str, momentum_trend: str, rsi: float) -> dict:
    """
    Check Fama-French / Carhart factor alignment for a ticker.
    Returns factor_score (0–3) and factors_present list.
    """
    factors     = []
    factor_score = 0

    # Momentum factor (Carhart)
    if momentum_trend == "bullish" and 45 <= rsi <= 70:
        factor_score += 1
        factors.append("Momentum factor ✓ (uptrend, RSI 45–70)")
    elif rsi > 72:
        factors.append("Momentum factor ⚠ (overbought — near reversal risk)")

    # Quality / profitability factor (Fama-French)
    if ticker in FF_QUALITY_HIGH:
        factor_score += 1
        factors.append("Quality factor ✓ (high-ROIC business)")

    # Value factor
    if ticker in FF_VALUE_TICKERS:
        factor_score += 1
        factors.append("Value factor ✓ (book-to-market tilt)")

    return {
        "factor_score":    factor_score,
        "factors_present": factors,
    }


def apply_munger_inversion(ticker: str, action: str, pnl_pct: float,
                            hold_days: int, momentum_trend: str) -> list[str]:
    """
    Apply Munger's inversion: what must be true for this to be the WRONG decision?
    Returns a list of risk flags.
    """
    risks = []

    if action in ("BUY_NEW", "ADD"):
        if momentum_trend == "bearish":
            risks.append(
                "Munger inversion: buying into a bearish trend requires a contrarian thesis — "
                "what is the specific catalyst for reversal?"
            )
        if momentum_trend == "bullish" and pnl_pct == 0:
            risks.append(
                "Munger inversion: buying at market price in a bullish trend — "
                "ensure you are not chasing the crowd (Buffett: be fearful when others are greedy)."
            )

    if action == "HOLD" and pnl_pct < -5:
        risks.append(
            "Munger inversion: holding a losing position — is this conviction or denial? "
            "Graham: distinguish between temporary price decline and permanent impairment."
        )

    if action == "SELL" and pnl_pct > 10:
        risks.append(
            "Munger: selling a winner early is the most common investor mistake. "
            "Buffett: time is the friend of the wonderful business. Confirm thesis is broken before selling."
        )

    return risks


def apply_marks_cycle_check(regime: str, rsi: float, ret_3m: float) -> dict:
    """
    Howard Marks cycle assessment: are we early, mid, or late in the move?
    """
    if regime == "bull" and rsi > 70 and ret_3m > 20:
        cycle_phase   = "late"
        cycle_note    = (
            "Marks warning: strong bull run + overbought RSI + high 3M return suggests "
            "late-cycle conditions. 'The time to be most cautious is when returns have been best.'"
        )
    elif regime == "bull" and 50 <= rsi <= 65 and 5 <= ret_3m <= 20:
        cycle_phase   = "mid"
        cycle_note    = (
            "Mid-cycle bull: momentum intact, not overbought. "
            "Marks: 'The most profitable investments are made in the middle of the cycle.'"
        )
    elif regime == "neutral":
        cycle_phase   = "transition"
        cycle_note    = (
            "Market at inflection point. Marks: demand extra margin of safety at cycle transitions."
        )
    else:
        cycle_phase   = "early_or_bear"
        cycle_note    = (
            "Marks: early cycle or bear phase offers the best entry prices "
            "but requires the courage to buy when others are fearful."
        )

    return {"cycle_phase": cycle_phase, "cycle_note": cycle_note}


def taleb_ruin_check(portfolio_value: float, position_cost: float,
                     n_positions: int, max_positions: int) -> Optional[str]:
    """
    Taleb: never risk ruin. Flag if single position would be dangerously large.
    """
    if portfolio_value <= 0:
        return None
    pct = position_cost / portfolio_value * 100
    if pct > 20:
        return (
            f"Taleb ruin check: this position would be {pct:.1f}% of portfolio. "
            f"Barbell principle: no single position should threaten the portfolio's survival."
        )
    if n_positions < 3 and pct > 15:
        return (
            f"Taleb concentration warning: {n_positions} positions, new one at {pct:.1f}%. "
            f"Antifragility requires spreading exposure across uncorrelated assets."
        )
    return None


# =============================================================================
# MASTER EVALUATION FUNCTION
# =============================================================================

def knowledge_enhanced_rationale(
    ticker:           str,
    sector:           str,
    action:           str,
    pnl_pct:          float,
    hold_days:        int,
    momentum_trend:   str,
    rsi:              float,
    ret_3m_pct:       float,
    regime:           str,
    portfolio_value:  float,
    position_cost:    float = 0.0,
    n_positions:      int   = 0,
    max_positions:    int   = 8,
) -> dict:
    """
    Apply all knowledge frameworks to a single decision.
    Returns a structured enhancement dict that the Manager Agent adds to its rationale.
    """
    moat      = get_moat_assessment(ticker, sector)
    factors   = get_factor_alignment(ticker, momentum_trend, rsi)
    inversions= apply_munger_inversion(ticker, action, pnl_pct, hold_days, momentum_trend)
    cycle     = apply_marks_cycle_check(regime, rsi, ret_3m_pct)
    ruin_flag = taleb_ruin_check(portfolio_value, position_cost, n_positions, max_positions)

    # Composite knowledge score: moat + factors (used to adjust conviction)
    knowledge_score = moat["moat_score"] + factors["factor_score"]
    knowledge_boost = (
        "STRENGTHEN" if knowledge_score >= 4
        else "NEUTRAL"  if knowledge_score >= 2
        else "WEAKEN"
    )

    # Select the most relevant principle for this specific decision
    if action in ("BUY_NEW", "ADD"):
        guiding_principle = (
            "Buffett: 'Only buy something you would be perfectly happy to hold if the market "
            "shut down for ten years.' | Graham: Always demand a margin of safety."
        )
    elif action == "SELL":
        guiding_principle = (
            "Munger: 'It is not supposed to be easy. Anyone who finds it easy is stupid.' "
            "| Marks: Confirm whether this is permanent impairment or temporary price movement."
        )
    else:
        guiding_principle = (
            "Buffett: 'The stock market is a device for transferring money from the impatient "
            "to the patient.' Patience is the primary edge of the long-term investor."
        )

    return {
        "moat":              moat,
        "factors":           factors,
        "munger_inversions": inversions,
        "cycle":             cycle,
        "ruin_flag":         ruin_flag,
        "knowledge_score":   knowledge_score,
        "knowledge_boost":   knowledge_boost,
        "guiding_principle": guiding_principle,
    }


def format_knowledge_rationale(ke: dict) -> list[str]:
    """
    Format the knowledge enhancement as human-readable rationale lines
    for inclusion in the Manager Agent's decision output.
    """
    lines = []

    moat = ke["moat"]
    if moat["moat_score"] > 0:
        lines.append(
            f"Economic Moat [{moat['moat_type'].upper()}]: "
            + ", ".join(moat["moat_sources"])
        )

    for f in ke["factors"]["factors_present"]:
        lines.append(f"Factor: {f}")

    lines.append(f"Market Cycle: {ke['cycle']['cycle_phase'].upper()} — {ke['cycle']['cycle_note']}")

    for inv in ke["munger_inversions"]:
        lines.append(f"Risk (Munger Inversion): {inv}")

    if ke["ruin_flag"]:
        lines.append(f"Capital Safety (Taleb): {ke['ruin_flag']}")

    lines.append(f"Guiding Principle: {ke['guiding_principle']}")

    return lines
