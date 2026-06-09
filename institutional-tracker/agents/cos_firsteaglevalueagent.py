# Chairman-designated Lane 4 — First Eagle Fundamental Value
# Specialization: Margin of Safety + Hard Money Hedge (Jean-Marie Eveillard style)
# Tier: 1  |  ETF: GLD  |  Lane: first_eagle  |  Capital: ,000
# Created: 2026-05-16
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research_team import SectorResearchAgent, ENERGY_BLOCK

class FirstEagleValueAgent(SectorResearchAgent):
    """
    Applies the First Eagle Investment Management philosophy — founded by Jean-Marie Eveillard —
    to identify high-quality businesses trading at a meaningful discount to intrinsic value,
    while maintaining a permanent hard-money hedge via gold miners.

    Core tenets: absolute return (not relative), margin of safety (30-40%+ discount to IV),
    balance sheet strength, durable competitive advantages, and a 15% allocation to gold
    miners as permanent portfolio insurance regardless of macro environment.

    Chairman directive 2026-04-27: All agents must be experts in their field,
    having studied all important books and case studies in their domain.
    This agent's knowledge base is embedded below and informs every analysis.
    """

    # ── Domain expertise (Chairman directive: agents must be experts) ────────
    KNOWLEDGE_BASE = {
    "frameworks": [
        "Margin of safety: only buy at 30-40%+ discount to conservatively estimated intrinsic value — Klarman/Graham foundational principle",
        "Eveillard quality-at-a-discount: durable competitive advantages + strong balance sheet + depressed price = First Eagle entry signal",
        "Hard money hedge theory: 15% permanent gold/gold-miner allocation as currency debasement and tail-risk insurance — non-negotiable regardless of macro view",
        "Absolute return vs. relative return: never benchmarked against an index; goal is preservation then growth of real purchasing power",
        "FCF yield as intrinsic value proxy: FCF yield > 8% on a quality business with low leverage = deeply undervalued on absolute basis"
    ],
    "seminal_texts": [
        "First Eagle Global Fund Annual Reports (Eveillard era) — foundational philosophy on absolute return, gold, and quality-at-a-discount",
        "You Can Be a Stock Market Genius — Joel Greenblatt (special situations and deep value identification)",
        "The Most Important Thing — Howard Marks (second-level thinking, risk asymmetry, cycle awareness)",
        "Margin of Safety — Seth Klarman (permanent capital loss avoidance; illiquid and misunderstood asset pricing)",
        "Security Analysis — Graham & Dodd (balance sheet analysis, earnings power value, asset-based valuation)"
    ],
    "case_studies": [
        "First Eagle buying gold in 2001 when everyone hated it — 600% return over the following decade; validated hard-money hedge thesis",
        "Eveillard holding 20%+ cash in 1999 dot-com bubble and underperforming badly — was right 18 months later; absolute return discipline rewarded",
        "NVR homebuilder: asset-light model with no land ownership, ROIC>30% for 20 consecutive years — textbook First Eagle quality business",
        "Berkshire buying See's Candies: pricing power + capital-light business model + durable brand = compounding intrinsic value — First Eagle style quality"
    ],
    "edge": "Buys quality businesses with durable competitive advantages only when priced at a meaningful discount to intrinsic value. Gold miners as permanent insurance. Never benchmarked — absolute return only. Asymmetric thinking: evaluate downside first, upside second."
}

    # ── Conviction scoring thresholds (First Eagle criteria) ─────────────────
    # 5 stars: FCF yield > 8% AND P/B < 1.5 AND net debt/EBITDA < 1.5 AND ROIC > 15%
    #          OR gold miner with strong balance sheet in rising gold environment
    # 4 stars: FCF yield > 6% AND P/B < 2.0 AND net debt/EBITDA < 2.5 AND ROIC > 10%
    # 3 stars: FCF yield > 4% AND quality metrics mostly met
    # 2 stars: FCF yield > 2% AND meets quality bar — surface for Manager review
    #   (Chairman directive 2026-06-09: widen scope; surface 2-star as WATCH
    #    so Manager can decide, not pre-filtered by research agent)
    CONVICTION_THRESHOLDS = {
        5: {"fcf_yield_min": 0.08, "pb_max": 1.5,  "nd_ebitda_max": 1.5, "roic_min": 0.15},
        4: {"fcf_yield_min": 0.06, "pb_max": 2.0,  "nd_ebitda_max": 2.5, "roic_min": 0.10},
        3: {"fcf_yield_min": 0.04, "pb_max": 3.0,  "nd_ebitda_max": 3.5, "roic_min": 0.07},
        2: {"fcf_yield_min": 0.02, "pb_max": 4.0,  "nd_ebitda_max": 5.0, "roic_min": 0.05},
    }
    MIN_SUBMISSION_CONVICTION = 2   # was effectively 3; lowered so Manager sees more candidates

    _RAW_WATCHLIST = [
        # Quality value — durable competitive advantages, strong FCF, low leverage
        "BRK-B",   # Berkshire: unmatched balance sheet + diversified earnings power
        "MKL",     # Markel: Berkshire-in-miniature, specialty insurance + investment portfolio
        "NVR",     # NVR: asset-light homebuilder, ROIC>30%, no land risk
        "FICO",    # Fair Isaac: monopoly credit scoring, extreme pricing power, capital-light
        "WTW",     # Willis Towers Watson: advisory moat, recurring revenue, depressed vs peers
        "FNF",     # Fidelity National Financial: title insurance leader, cyclical discount
        "LKQ",     # LKQ: auto parts distribution moat, fragmented market consolidator
        "BRKR",    # Bruker: analytical instruments, niche scientific moat
        "RLI",     # RLI Corp: specialty insurance, 25-yr combined ratio < 100, pristine underwriting
        "CBOE",    # CBOE: exchange monopoly on VIX/options, durable market structure moat
        "MSCI",    # MSCI: index licensing monopoly, 95%+ retention, capital-light recurring revenue
        "MCO",     # Moody's: ratings duopoly, pricing power, financial infrastructure
        "SPGI",    # S&P Global: data duopoly, ROIC>30%, recurring revenue
        "ICE",     # Intercontinental Exchange: exchange infrastructure moat
        "CME",     # CME Group: derivatives exchange monopoly, FCF machine
        "NDAQ",    # Nasdaq: exchange + analytics, recurring revenue transformation
        "FDS",     # FactSet: financial data subscription moat, low churn
        "VRSK",    # Verisk Analytics: data analytics moat, insurance/energy verticals
        "MORN",    # Morningstar: independent ratings and data, durable moat
        "CSGP",    # CoStar Group: commercial real estate data monopoly
        "ANSS",    # Ansys: simulation software monopoly, aerospace/auto/semi
        "PTC",     # PTC Inc: industrial IoT software, niche moat
        "CDNS",    # Cadence Design: EDA software duopoly with SNPS
        "SNPS",    # Synopsys: EDA software duopoly with CDNS
        "MANH",    # Manhattan Associates: supply chain software, high switching cost
        "SAIC",    # Science Applications: government IT services, recurring contracts
        "LDOS",    # Leidos: defense IT, recurring government revenue
        "CACI",    # CACI International: government IT, predictable cash flow
        "BAH",     # Booz Allen Hamilton: government consulting, top-secret clearances as moat
        # Gold/hard money — permanent portfolio insurance (target 15% allocation)
        "NEM",     # Newmont: largest gold miner, tier-1 assets, strong balance sheet
        "AEM",     # Agnico Eagle: premium operator, low-cost mines, disciplined capital allocation
        "WPM",     # Wheaton Precious Metals: royalty model, no mining risk, highest margins
        "GDX",     # VanEck Gold Miners ETF — diversified gold miner exposure
        "GDXJ",    # VanEck Junior Gold Miners ETF — higher beta gold exposure
        "FNV",     # Franco-Nevada: gold royalty/streaming, fortress balance sheet, zero debt
        # International value ADRs — out-of-favor quality internationally
        "ALIZY",   # Allianz: European insurance giant, deep discount to intrinsic value
        "LNSTY",   # Linde: industrial gas oligopoly, long-term contracts, pricing power
    ]
    DEFAULT_WATCHLIST = [t for t in _RAW_WATCHLIST if t not in ENERGY_BLOCK]
    DEFAULT_SECTOR = "First-Eagle-Value"
    DEFAULT_ETF    = "GLD"
    DEFAULT_THESIS = (
        "First Eagle absolute return philosophy — Eveillard style. Buy quality businesses with "
        "durable competitive advantages only at a 30-40%+ discount to conservatively estimated "
        "intrinsic value. Maintain 15% permanent allocation to gold miners (NEM, AEM, WPM, GDX) "
        "as hard-money insurance. Never benchmark against an index. Evaluate downside first, "
        "upside second. FCF yield > 6% on a quality franchise with net debt/EBITDA < 2.5x is the "
        "minimum entry hurdle. Hold for years, not weeks. Cash is a position when nothing is cheap."
    )

    # Every reasoning string must include: intrinsic value estimate, margin of safety %,
    # balance sheet quality assessment, and why the position fits 'first, do no harm'.
    REASONING_TEMPLATE = (
        "IV estimate: {iv_estimate} | Margin of safety: {mos_pct}% below IV | "
        "Balance sheet: {bs_quality} | FCF yield: {fcf_yield}% | "
        "First-do-no-harm: {downside_case} | Upside: {upside_case}"
    )

    def __init__(self, agent_id: str = "firsteaglevalueagent_chairman"):
        super().__init__({
            "agent_id":           agent_id,
            "sector_name":        self.DEFAULT_SECTOR,
            "sector_etf":         self.DEFAULT_ETF,
            "description":        (
                "Applies First Eagle Investment Management philosophy — absolute return, margin of safety, "
                "hard money hedge — to identify quality businesses trading at a meaningful discount to "
                "intrinsic value. Gold miners maintained as permanent 15% portfolio insurance."
            ),
            "watchlist":          self.DEFAULT_WATCHLIST,
            "investment_horizon": "long",
            "leader_rationale":   self.DEFAULT_THESIS,
            "lane":               "first_eagle",
            "lane_capital_usd":   5000.00,
            "tier":               1,
        })
