# Chairman-designated Lane 4 — First Eagle R&D Research Agent
# Specialization: Quality Small/Mid-Cap Businesses Below Analyst Radar
# Tier: 1  |  ETF: IWN  |  Lane: first_eagle
# Created: 2026-05-16
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research_team import SectorResearchAgent, ENERGY_BLOCK

class FESmallMidCapValueAgent(SectorResearchAgent):
    """
    Identifies quality small/mid-cap businesses below analyst radar where information
    advantage from deep fundamental research generates asymmetric returns.

    Neglected stock effect: smaller companies with no analyst coverage trade at larger
    discount to intrinsic value due to information vacuum. First Eagle's fundamental
    research fills this gap and earns a premium unavailable in large-cap.

    Chairman directive 2026-04-27: All agents must be experts in their field,
    having studied all important books and case studies in their domain.
    This agent's knowledge base is embedded below and informs every analysis.
    """

    KNOWLEDGE_BASE = {
        "frameworks": [
            "Neglected stock effect: 0-2 analyst coverage + market cap < $2B = information vacuum creates persistent discount to intrinsic value",
            "Small-cap quality filter: ROIC > 12% + net debt/EBITDA < 2x + FCF positive for 5yr in small-cap = exceptional business, likely undiscovered",
            "Analyst neglect premium: stocks with zero buy-side coverage outperform stocks with 10+ analysts by 3-5% annually on risk-adjusted basis",
            "Liquidity discount opportunity: institutional funds too large to buy small-cap creates structural seller-to-buyer imbalance in favor of individual investors",
            "Community bank value: local banks trading below 1x tangible book + NPA ratio < 1% = deep value with real estate asset floor",
        ],
        "seminal_texts": [
            "One Up On Wall Street — Peter Lynch (find great small companies before Wall Street does, 10-bagger thesis)",
            "Small Stocks, Big Profits — Gerald Perritt (small-cap outperformance, neglected company premium)",
            "What Works on Wall Street — James O'Shaughnessy (value factor works best in small-cap universe)",
            "Beating the Street — Peter Lynch (Fidelity Magellan process, finding overlooked companies)",
            "Security Analysis — Graham & Dodd (asset-based valuation applies even more powerfully to small-cap companies)",
        ],
        "case_studies": [
            "Capital Southwest CSWC: internally managed BDC, ROIC compounder, management owns 3%+ of shares, no analyst coverage for first decade",
            "Investors Title ITIC: small-cap title insurance, 30yr track record, family-controlled, consistently profitable through cycles",
            "RLI Corp in its early years: small specialty insurer, zero analyst coverage, 25yr combined ratio < 100 — discovered only after 10x from IPO",
            "FRP Holdings FRPH: industrial REIT + quarry in Florida, family-controlled, sub-$500M market cap, real estate and aggregate value hidden in balance sheet",
        ],
        "edge": "Information edge in neglected small-cap universe where fundamental research is not commoditized. Liquidity discount is a feature, not a bug — patient capital earns the premium. Filter: ROIC > 12% + 0-2 analyst coverage + FCF > 0 for 5yr + net debt/EBITDA < 2x.",
    }

    # Chairman directive 2026-06-09: widen scope beyond community banks
    # to all neglected small/mid-cap sectors; surface 2-star as WATCH
    MIN_SUBMISSION_CONVICTION = 2

    _RAW_WATCHLIST = [
        # ── Community Banks (original core) ──────────────────────────────────
        "CSWC",  # Capital Southwest: internally managed BDC, owner-aligned, disciplined credit
        "ITIC",  # Investors Title: small-cap title insurance, family-controlled, 30yr track record
        "RLI",   # RLI Corp: specialty insurance, 25yr combined ratio < 100, neglected gem
        "FRPH",  # FRP Holdings: industrial REIT + quarry, family-controlled, hidden asset value
        "LADR",  # Ladder Capital: commercial real estate lender + REIT, below book value entry
        "TCBK",  # TriCo Bancshares: California community bank, low NPA, trading near tangible book
        "NBTB",  # NBT Bancorp: Northeast community bank, pristine credit quality, 1x tangible book
        "PFIS",  # Peoples Financial Services: Pennsylvania community bank, conservative lending
        "ESSA",  # ESSA Bancorp: Pennsylvania thrift, below tangible book, improving efficiency
        "NFBK",  # Northfield Bancorp: NYC metro thrift, below tangible book, strong capital ratios
        "HMNF",  # HMN Financial: Minnesota thrift, below tangible book, conservative loan book
        "BSVN",  # Bank7 Corp: Oklahoma/Texas community bank, disciplined credit, cheap on P/B
        "NWFL",  # Norwood Financial: Northeast community bank, below 1x tangible book
        "CHMG",  # Chemung Financial: Upstate NY community bank, pristine credit, trading at book
        "BMRC",  # Bank of Marin: Bay Area community bank, conservative underwriting, near tangible book
        "HWBK",  # Hawthorn Bancorp: Midwest community bank, franchise value unrecognized
        "BWFG",  # Bankwell Financial: CT commercial bank, cheap P/E, conservative balance sheet
        "GSBC",  # Great Southern Bancorp: Midwest regional, disciplined credit, below peers on P/B
        "TRMK",  # Trustmark Corp: Mississippi/Alabama franchise bank, 1x tangible book, durable dividend
        "FBIZ",  # First Business Financial: Wisconsin business bank, niche commercial lending, low P/B
        # ── Expanded: Neglected Small/Mid-Cap Non-Banks ───────────────────────
        # Specialty Insurance
        "ERIE",  # Erie Indemnity: mutual insurer, premium pricing power, neglected gem
        "KMPR",  # Kemper: specialty P&C, restructuring discount vs. franchise
        "MAIN",  # Main Street Capital: BDC, best-in-class management, below-peer P/B
        "GBDC",  # Golub Capital BDC: quality middle-market lender, consistent dividend
        # Small-cap industrial value
        "HTLF",  # Heartland Financial: Midwest bank, disciplined M&A acquirer
        "FFIN",  # First Financial Bankshares: Texas community bank, ROIC > 15% consistently
        "CATY",  # Cathay General Bancorp: Asian-American community bank, LA/NYC franchise
        "HAFC",  # Hanmi Financial: Korean-American community bank, L.A. focus
        "HOPE",  # Hope Bancorp: Korean-American bank, trading below tangible book
        # Neglected industrials / services
        "WDFC",  # WD-40 Company: single-product global franchise, pricing power, ignored
        "MGRC",  # McGrath RentCorp: modular building + electronics rental, recurring cash
        "UFPI",  # UFP Industries: treated wood products, niche construction supply
        "PGTI",  # PGT Innovations: impact windows, Florida dominance, recurring replacement
        "PATK",  # Patrick Industries: RV and marine components, cyclical but dominant
        "REVG",  # REV Group: specialty vehicles (fire trucks, ambulances), government contracts
        "HBT",   # Heartland BancCorp: Illinois community bank, below-book opportunity
        "SMBC",  # Southern Missouri Bancorp: conservative Midwest bank
        "UVSP",  # Univest Financial: Pennsylvania community bank, franchise value
        # Neglected healthcare
        "GMED",  # Globus Medical: spine implants + robotics, niche surgical moat
        "PNTG",  # Pennant Group: home health + hospice, low-float, neglected
        "ADUS",  # Addus HomeCare: personal care services, government-reimbursed, neglected
        "ENSG",  # Ensign Group: skilled nursing + senior living, consistent acquirer
        "LHCG",  # LHC Group: home health, government reimbursement moat
        # Neglected consumer/retail
        "PRGO",  # Perrigo: OTC pharma, restructuring discount vs. global OTC franchise
        "COTY",  # Coty: fragrance + cosmetics, post-restructuring discount vs. brand value
        "USNA",  # USANA Health Sciences: nutrition MLM, depressed vs. Asian franchise value
        "SMPL",  # Simply Good Foods: Quest + Atkins brands, clean balance sheet, growing
    ]
    DEFAULT_WATCHLIST = [t for t in _RAW_WATCHLIST if t not in ENERGY_BLOCK]
    DEFAULT_SECTOR = "FE-SmallMidCapValue"
    DEFAULT_ETF    = "IWN"
    DEFAULT_THESIS = (
        "Neglected stock effect — smaller companies with no analyst coverage trade at larger discount to IV. "
        "Information advantage from deep fundamental research fills the vacuum. "
        "Filter: ROIC > 12% + 0-2 analyst coverage + FCF positive 5yr + net debt/EBITDA < 2x. "
        "Liquidity discount is a feature — patient capital earns the illiquidity premium."
    )

    def __init__(self, agent_id: str = "fe_smallmidcapvalue_chairman"):
        super().__init__({
            "agent_id":           agent_id,
            "sector_name":        self.DEFAULT_SECTOR,
            "sector_etf":         self.DEFAULT_ETF,
            "description":        (
                "Identifies quality small/mid-cap businesses below analyst radar where information advantage "
                "from deep fundamental research generates returns unavailable in large-cap. Neglected stock "
                "effect + liquidity discount = First Eagle margin-of-safety opportunity."
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
