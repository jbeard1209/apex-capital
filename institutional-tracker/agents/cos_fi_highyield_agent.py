# Auto-generated FI agent — High Yield
# Specialization: HY/leveraged credit — spread compression plays
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FIHighYieldAgent(FIBaseAgent):
    """
    High-yield credit specialist. Buys credit risk when HY spreads compensate
    for default risk. Focuses on B/BB quality — avoids pure CCC unless spreads
    are truly distressed (>800bps) and business is operationally viable.

    Wide scope: 45+ vehicles to maximize opportunity surface across the HY universe.
    Surfaces WATCH picks even in tight environments — these are candidates for when
    spreads widen, not necessarily immediate buys.
    """

    SEGMENT = "high_yield"
    THESIS  = (
        "HY credit: spreads over 400bps on quality B/BB issuers compensate for "
        "default risk with meaningful risk-adjusted return. Fallen angels "
        "(IG→HY downgrades) historically outperform — stigma creates artificial "
        "spread premium vs. credit quality. ETF vehicles first; single-name "
        "when issuer/sector spread is anomalously wide vs. fundamentals."
    )

    WATCHLIST = [
        # ── HY ETFs (core) ────────────────────────────────────────────────────
        "HYG",   # iShares HY Corporate — most liquid HY ETF, benchmark vehicle
        "JNK",   # SPDR HY Bond — alternative HY, slightly different composition
        "USHY",  # iShares Broad HY — includes CCC tier, widest HY universe
        "HYDB",  # iShares HY ex-Energy — removes volatile energy, cleaner spread signal
        "SHYG",  # iShares Short Maturity HY — 0-5yr HY, minimizes duration extension
        "HYLB",  # Xtrackers HY — alternative issuer, different methodology
        "HYGV",  # iShares HY Value — screens for fundamental quality within HY
        "FALN",  # iShares Fallen Angels — recently downgraded, historically best HY subset
        "ANGL",  # VanEck Fallen Angels — alternative fallen angel vehicle

        # ── Leveraged Loans ───────────────────────────────────────────────────
        "BKLN",  # Invesco Senior Loan — floating rate, SOFR + spread, no duration risk
        "SRLN",  # SPDR Senior Loan — alternative leveraged loan vehicle
        "FTSL",  # First Trust Senior Loan — active loan selection

        # ── Sector HY ETFs ────────────────────────────────────────────────────
        "GHYB",  # Goldman Sachs HY — actively managed HY, sector-selective
        "PHB",   # Invesco Fundamental HY — quality-screened HY subset

        # ── Single-name HY (BB/B rated, operationally viable) ────────────────
        "WBA",   # Walgreens B+: pharmacy network, HY since 2024 downgrade, spread extreme
        "PARA",  # Paramount BB-: streaming losses create spread vs. content library asset
        "DISH",  # DISH Network B: spectrum asset vs. debt load, spread wide vs. spectrum value
        "AMC",   # AMC Networks BB-: legacy media, cash generative despite decline
        "UAL",   # United Airlines BB: travel recovery, spread vs. IG-quality cash generation
        "DAL",   # Delta Air Lines BB+: near-IG quality airline, spread compression candidate
        "CCL",   # Carnival B+: cruise recovery trade, spread normalizing post-COVID
        "NCLH",  # Norwegian Cruise B: higher leverage than CCL, wider spread
        "MGM",   # MGM Resorts BB: Las Vegas + digital, spread vs. recurring casino EBITDA
        "WYNN",  # Wynn Resorts BB-: Macau recovery + Las Vegas, spread vs. luxury franchise
        "SIX",   # Six Flags BB-: theme park operator, FCF positive, spread discount
        "RCL",   # Royal Caribbean BB+: cruise sector recovery, near-IG spread
        "CZR",   # Caesars Entertainment B+: gaming REIT + operations, spread vs. asset value
        "F",     # Ford Motor BB+: auto cycle spread, genuine cash generator
        "HBI",   # Hanesbrands BB: consumer staples under margin pressure, durable brand
        "KHC",   # Kraft Heinz BBB-: investment grade but watches closely for HY transition
        "ARCC",  # Ares Capital: BDC, credit-risk vehicle with HY-like yield
        "GBDC",  # Golub Capital BDC: quality middle-market lender, consistent dividend
        "MAIN",  # Main Street Capital: BDC with IG credit, best-in-class management
        "HTGC",  # Hercules Capital: tech-focused BDC, venture lending spread
        "TPVG",  # TriplePoint Venture Growth: VC-stage BDC, widest spread BDC subsector
        "SLRC",  # SLR Investment Corp: secured lending focus, lower risk BDC
    ]

    def _score_ticker(self, ticker: str) -> dict:
        hy_spread = self.spreads.get("hy_spread_bps", 350)
        regime    = self.regime

        if hy_spread > 600:
            conviction = 5
        elif hy_spread > 450:
            conviction = 4
        elif hy_spread > 350:
            conviction = 3
        elif hy_spread > 280:
            conviction = 2
        else:
            conviction = 2   # Still surface — tight HY is still wider than IG

        is_etf = ticker in ("HYG","JNK","USHY","HYDB","SHYG","HYLB","HYGV","FALN","ANGL",
                             "BKLN","SRLN","FTSL","GHYB","PHB")
        if is_etf:
            conviction = min(conviction + 1, 5)

        # Fallen angels get +1 — historically best HY subset
        if ticker in ("FALN", "ANGL", "WBA", "PARA"):
            conviction = min(conviction + 1, 5)

        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      "BUY" if conviction >= 3 else "WATCH",
            "reasoning":   (
                f"{ticker}: high-yield credit | regime={regime} | "
                f"HY spread={hy_spread}bps | "
                f"thesis=spread compression, carry capture"
            ),
            "credit_tier": "HIGH YIELD",
            "segment":     self.SEGMENT,
        }
