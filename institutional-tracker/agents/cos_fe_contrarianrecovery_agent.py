# Chairman-designated Lane 4 — First Eagle R&D Research Agent
# Specialization: Quality Businesses in Temporarily Hated Sectors
# Tier: 1  |  ETF: VTV  |  Lane: first_eagle
# Created: 2026-05-16
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research_team import SectorResearchAgent, ENERGY_BLOCK

class FEContrarianRecoveryAgent(SectorResearchAgent):
    """
    Identifies quality businesses in temporarily hated sectors where franchise
    remains intact and balance sheet can survive the downturn — the most reliable
    contrarian alpha source in First Eagle's toolkit.

    Eveillard's patience: great businesses go through bad periods. If the franchise
    is intact and balance sheet survives, the market will re-rate. Buy maximum fear,
    sell maximum complacency. Time is on the side of the patient contrarian.

    Chairman directive 2026-04-27: All agents must be experts in their field,
    having studied all important books and case studies in their domain.
    This agent's knowledge base is embedded below and informs every analysis.
    """

    KNOWLEDGE_BASE = {
        "frameworks": [
            "Franchise survival test: can the business survive 3 more years of current adversity without dilutive equity raise? If yes, consider entry",
            "Sector hate cycle: when 90%+ of analysts downgrade and sell-side coverage drops, sector is near maximum pessimism = contrarian entry zone",
            "Balance sheet stress test: net debt/EBITDA < 3x gives runway; > 5x with no clear deleveraging path = value trap risk, not contrarian value",
            "Thesis intact check: is the competitive moat still there? Temporary demand weakness vs. structural franchise erosion — crucial distinction",
            "Sentiment reversal trigger: what specific catalyst converts from hate to re-rating? Earnings beat, guidance raise, management change, or simple passage of time",
        ],
        "seminal_texts": [
            "Contrarian Investment Strategies — David Dreman (value premium from hated sectors, analyst consensus failure, contrarian outperformance)",
            "The Most Important Thing — Howard Marks (second-level thinking, cycles, avoiding the consensus, buying discomfort)",
            "Patient Capital — Victoria Ivashina & Josh Lerner (why patient, contrarian capital earns premium vs. consensus momentum)",
            "Security Analysis — Graham & Dodd (normalized earnings vs. cyclical trough, franchise value in downcycles)",
            "Margin of Safety — Seth Klarman (buying out-of-favor, avoiding value traps, franchise vs. commodity business distinction)",
        ],
        "case_studies": [
            "Philip Morris / Altria MO: hated tobacco sector for 30 years, franchise never impaired, dividend reinvested = 15%+ CAGR for decades",
            "CVS Health 2022-2024: pharmacy + benefits management franchise under pressure from reimbursement headwinds — thesis intact, balance sheet survives",
            "Pfizer PFE post-COVID cliff: patent expires + pipeline doubt created 60% decline — R&D franchise and cash generation intact underneath",
            "Intel INTC 2022-2024: foundry pivot doubted by market, stock halved — manufacturing franchise is real, turnaround timeline uncertain but outcome binary",
        ],
        "edge": "Sector hate + franchise intact + balance sheet survivability = First Eagle contrarian entry. Maximum patience required — contrarian thesis often takes 18-36 months to be recognized by market. Never invest in a hated sector with a broken balance sheet — that is a value trap.",
    }

    _RAW_WATCHLIST = [
        "CVS",   # CVS Health: pharmacy + benefits franchise under reimbursement pressure — thesis intact
        "WBA",   # Walgreens Boots Alliance: pharmacy network in structural reform — franchise value question
        "PFE",   # Pfizer: post-COVID revenue cliff, pipeline doubt — R&D franchise intact, cheap on normalized basis
        "INTC",  # Intel: foundry pivot, market share loss to AMD/NVDA — manufacturing franchise real but timeline uncertain
        "MPW",   # Medical Properties Trust: healthcare REIT under tenant stress — real estate portfolio vs. tenant risk
        "VFC",   # VF Corporation: brand portfolio (Vans, North Face) under operational restructuring
        "KHC",   # Kraft Heinz: CPG portfolio restructuring, dividend cut history — brand portfolio question
        "MO",    # Altria: hated tobacco, franchise never impaired, 8%+ dividend yield, oral nicotine growth
        "BTI",   # British American Tobacco: global tobacco + vape portfolio, UK-listed discount
        "PARA",  # Paramount Global: streaming losses + linear TV decline — content library vs. debt load
        # ── Expanded contrarian universe (2026-05-23) ─────────────────────────
        "DLTR",  # Dollar Tree: off multi-year lows post-Family Dollar struggles — franchise intact, fixing execution
        "DG",    # Dollar General: beaten down 65%+ from highs on margin pressure — convenience franchise durable
        "BAX",   # Baxter International: post-Hillrom spin-off restructuring discount, healthcare infrastructure
        "HII",   # Huntington Ingalls: defense shipbuilding duopoly, Navy capex cycle, hated on cost overruns
        "SWBI",  # Smith & Wesson Brands: hated sector (firearms), steady FCF, low valuation
        "BALY",  # Bally's Corporation: casino + igaming, post-acquisition integration discount, real asset base
        "WBD",   # Warner Bros Discovery: content library + streaming at deep discount to SOTP, restructuring
        "XRAY",  # Dentsply Sirona: dental equipment, revenue miss cycle, franchise intact in dental duopoly
        "RRR",   # Red Rock Resorts: Las Vegas locals casino, hated gaming sector, best-in-class margins
        "ENR",   # Energizer Holdings: hated consumer staples, high debt but durable battery franchise + pricing power
    ]
    DEFAULT_WATCHLIST = [t for t in _RAW_WATCHLIST if t not in ENERGY_BLOCK]
    DEFAULT_SECTOR = "FE-ContrarianRecovery"
    DEFAULT_ETF    = "VTV"
    DEFAULT_THESIS = (
        "Eveillard's patience — great businesses go through bad periods. If franchise is intact and "
        "balance sheet survives, the market will re-rate. Sector hate + franchise intact + balance sheet "
        "survivability = First Eagle contrarian entry. Buy maximum fear, sell maximum complacency. "
        "Never invest in hated sector with broken balance sheet — that is a value trap, not value."
    )

    def __init__(self, agent_id: str = "fe_contrarianrecovery_chairman"):
        super().__init__({
            "agent_id":           agent_id,
            "sector_name":        self.DEFAULT_SECTOR,
            "sector_etf":         self.DEFAULT_ETF,
            "description":        (
                "Identifies quality businesses in temporarily hated sectors where franchise remains intact "
                "and balance sheet can survive the downturn. Eveillard contrarian patience applied — "
                "maximum pessimism in sector + franchise survival = First Eagle entry signal."
            ),
            "watchlist":          self.DEFAULT_WATCHLIST,
            "investment_horizon": "long",
            "leader_rationale":   self.DEFAULT_THESIS,
            "lane":               "first_eagle",
            "tier":               1,
        })
    def run(self) -> dict:
        """
        Override run() to attach web research, earnings analysis, and investment thesis
        to each pick after the base evaluation. Wraps in try/except -- zero impact on failure.
        """
        report = super().run()

        try:
            from fe_web_research import get_company_research
            from fe_earnings_analyzer import analyze_earnings_track_record
            from fe_thesis_builder import build_investment_thesis
        except ImportError:
            return report

        enriched = []
        for pick in report.get("recommendations", []):
            ticker = pick.get("ticker", "")
            if not ticker:
                enriched.append(pick)
                continue
            try:
                iv_result = {
                    "current_price":    pick.get("current_price"),
                    "conservative_iv":  pick.get("conservative_iv"),
                    "margin_of_safety": pick.get("margin_of_safety"),
                    "dcf_iv":           pick.get("dcf_iv"),
                    "epv_iv":           pick.get("epv_iv"),
                    "fcf_yield":        pick.get("fcf_yield"),
                    "roic":             pick.get("roic"),
                }
                web_research = get_company_research(ticker)
                earnings     = analyze_earnings_track_record(ticker)
                thesis       = build_investment_thesis(ticker, iv_result, web_research, earnings)
                pick["web_research"]      = web_research
                pick["earnings_analysis"] = earnings
                pick["investment_thesis"] = thesis
            except Exception as e:
                pick["web_research"] = {"error": str(e)}
            enriched.append(pick)

        report["recommendations"] = enriched
        return report
