# Auto-generated FI agent — Credit Spread Monitor
# Specialization: Identifies ETF and single-name credit spread opportunities
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FICreditSpreadAgent(FIBaseAgent):
    """
    Monitors credit spreads across IG and HY markets.
    Buys spread compression opportunities — widened spreads relative to fundamentals.
    Covers: credit ETFs, CDS proxies, and broad credit index vehicles.

    Widens scope when regime is TIGHT (to find any value) and in WIDE/DISTRESSED
    (to find the highest-quality entry points in widened-spread environment).
    """

    SEGMENT = "credit_spread"
    THESIS  = (
        "Credit spread compression: buy credit when spreads are wide relative to "
        "historical norms and issuer fundamentals are intact. ETF vehicles provide "
        "diversified exposure with daily liquidity. Single-name high-conviction "
        "when spread/quality mismatch is severe."
    )

    # Broad credit universe — ETFs first (most liquid), then single-name proxies
    WATCHLIST = [
        # ── Investment Grade Credit ETFs ──────────────────────────────────────
        "LQD",   # iShares IG Corporate Bond — flagship IG vehicle, deep liquidity
        "VCIT",  # Vanguard Intermediate-Term Corporate — lower cost, similar exposure
        "IGIB",  # iShares IG 5-10yr — intermediate duration, spread-sensitive
        "VCSH",  # Vanguard Short-Term Corporate — minimal duration risk, pure spread play
        "SPSB",  # SPDR Short-Term Corporate — sub-3yr, near-cash credit spread
        "USIG",  # iShares Broad USD IG Corporate — widest IG universe

        # ── High Yield Credit ETFs ────────────────────────────────────────────
        "HYG",   # iShares HY Corporate Bond — most liquid HY vehicle
        "JNK",   # SPDR HY Bond — alternative HY vehicle, slightly different composition
        "USHY",  # iShares Broad HY — expanded HY universe including CCC
        "HYDB",  # iShares HY ex-Energy — removes volatile energy component
        "BKLN",  # Invesco Senior Loan — floating rate, spreads over SOFR, minimal duration
        "SRLN",  # SPDR Senior Loan — alternative leveraged loan vehicle
        "FALN",  # iShares Fallen Angels — recently downgraded IG→HY, historically outperform
        "ANGL",  # VanEck Fallen Angels — same fallen angel thesis, slightly different index

        # ── Short Duration / Floating Rate ───────────────────────────────────
        "FLOT",  # iShares Floating Rate Bond — SOFR + spread, hedged duration
        "FLRN",  # SPDR Floating Rate — alternative floating rate vehicle
        "ICSH",  # iShares Ultra Short-Term Bond — near-cash, 3-6mo spread

        # ── Single-name credit proxies (when spread/fundamental mismatch) ────
        "T",     # AT&T: IG-rated, historically wide spread vs. peers, large dividend
        "VZ",    # Verizon: IG, telecom capex debt load creates spread premium vs. quality
        "MO",    # Altria: technically IG, tobacco discount baked into spread
        "WBA",   # Walgreens: recently downgraded to junk, spread extreme vs. franchise value
        "MPW",   # Medical Properties Trust: REIT under stress, spread wide vs. asset value
        "RHI",   # Robert Half: cyclical staffing, IG quality, trades at slight spread premium
        "NWL",   # Newell Brands: IG-adjacent, brand portfolio vs. leverage concern
    ]

    def _score_ticker(self, ticker: str) -> dict:
        hy_spread = self.spreads.get("hy_spread_bps", 350)
        ig_spread = self.spreads.get("ig_spread_bps", 120)
        regime    = self.regime

        # Conviction calibrated to spread wideness — wide spreads = high conviction
        if regime in ("WIDE", "DISTRESSED") or hy_spread > 500:
            conviction = 5
        elif regime == "NORMAL" or (350 <= hy_spread <= 500):
            conviction = 4
        elif hy_spread < 300 and ig_spread < 100:
            conviction = 2   # Tight spreads — still surface but low conviction
        else:
            conviction = 3

        # Prefer ETFs in tight markets (diversification hedge); single-names in wide
        is_etf = ticker in ("LQD","VCIT","IGIB","VCSH","SPSB","USIG","HYG","JNK","USHY",
                             "HYDB","BKLN","SRLN","FALN","ANGL","FLOT","FLRN","ICSH")
        if regime == "TIGHT" and not is_etf:
            conviction = max(1, conviction - 1)

        segment = "investment_grade" if ticker in ("LQD","VCIT","IGIB","VCSH","SPSB",
                                                    "USIG","FLOT","FLRN","ICSH","T","VZ") else "high_yield"
        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      "BUY" if conviction >= 3 else "WATCH",
            "reasoning":   (
                f"{ticker}: {segment} credit spread opportunity | regime={regime} | "
                f"HY spread={hy_spread}bps IG spread={ig_spread}bps | "
                f"thesis=spread compression play"
            ),
            "credit_tier": segment.replace("_", " ").upper(),
            "segment":     self.SEGMENT,
        }
