# Auto-generated FI agent — Convertible Bonds
# Specialization: convertible securities — equity upside + debt floor
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FIConvertibleAgent(FIBaseAgent):
    """
    Convertible bond specialist. Convertibles offer a debt floor (bond value)
    with equity upside (conversion option). Best when volatility is high and
    issuer credit is sound. Wide scope covers the full convert universe.
    """

    SEGMENT = "convertible"
    THESIS  = (
        "Convertibles: asymmetric — bond floor limits downside while conversion "
        "option captures equity upside. Busted converts (trading near bond value "
        "with little equity optionality) offer pure credit opportunity. "
        "In-the-money converts offer equity exposure with downside protection. "
        "Volatility expansion increases option value — buy before vol spikes."
    )

    WATCHLIST = [
        # ── Convertible ETFs ──────────────────────────────────────────────────
        "CWB",   # SPDR Barclays Convertible — broadest convertible ETF, benchmark vehicle
        "ICVT",  # iShares Convertible Bond — alternative broad convert vehicle
        "FCVT",  # First Trust SSI Strategic Convertible — active management
        "CVRT",  # Calamos Convertible — actively managed, quality-screened converts

        # ── Large-cap converts (stable credit, option value) ──────────────────
        "TSLA",  # Tesla converts: high vol issuer, wide converts outstanding
        "NVDA",  # NVIDIA converts: strongest equity momentum, clean credit
        "AMZN",  # Amazon converts: IG credit, massive equity optionality
        "NFLX",  # Netflix converts: streaming cash flow improving, equity upside
        "UBER",  # Uber converts: improving FCF, meaningful converts outstanding
        "LYFT",  # Lyft converts: ride-share #2, converts near busted territory
        "ABNB",  # Airbnb converts: asset-light, strong FCF, travel recovery
        "RBLX",  # Roblox converts: high-growth gaming, pure option value
        "SNOW",  # Snowflake converts: cloud growth, low credit risk, high vol
        "DDOG",  # Datadog converts: observability SaaS, strong fundamentals

        # ── Mid-cap converts (spread + option value) ──────────────────────────
        "EXPE",  # Expedia converts: travel recovery + OTA moat
        "TRIP",  # TripAdvisor converts: busted territory, pure credit play
        "OPEN",  # Opendoor converts: iBuying, real estate exposure
        "Z",     # Zillow converts: real estate platform, high vol issuer
        "SPCE",  # Virgin Galactic converts: speculative, deep OTM option value
        "LAZR",  # Luminar Tech converts: lidar, speculative option play
        "AFRM",  # Affirm converts: BNPL, credit risk + equity upside
        "UPST",  # Upstart converts: AI lending, busted convert near bond floor
        "SOFI",  # SoFi converts: fintech bank, near-IG credit quality + option

        # ── Healthcare/Biotech converts (high vol, defined credit risk) ───────
        "JAZZ",  # Jazz Pharma converts: specialty pharma, dividend + converts
        "NKTR",  # Nektar converts: biotech option value
        "CRNC",  # Cerence converts: AI software, distressed convert
        "NTNX",  # Nutanix converts: cloud infrastructure, improving credit

        # ── Real Estate converts ───────────────────────────────────────────────
        "INVH",  # Invitation Homes converts: SFR REIT, stable credit
        "AMH",   # American Homes 4 Rent converts: SFR REIT, similar to INVH
    ]

    def _score_ticker(self, ticker: str) -> dict:
        hy_spread = self.spreads.get("hy_spread_bps", 350)
        regime    = self.regime
        ig_spread = self.spreads.get("ig_spread_bps", 120)

        is_etf = ticker in ("CWB","ICVT","FCVT","CVRT")
        # Converts are attractive in: wide credit (credit floor valuable) OR high vol
        if hy_spread > 450:
            conviction = 5 if is_etf else 4
        elif hy_spread > 350:
            conviction = 4 if is_etf else 3
        else:
            conviction = 3 if is_etf else 2

        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      "BUY" if conviction >= 3 else "WATCH",
            "reasoning":   (
                f"{ticker}: convertible bond | regime={regime} | "
                f"HY spread={hy_spread}bps | "
                f"thesis=bond floor + equity option asymmetry"
            ),
            "credit_tier": "CONVERTIBLE",
            "segment":     self.SEGMENT,
        }
