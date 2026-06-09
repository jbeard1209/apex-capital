# Auto-generated FI agent — Distressed Credit
# Specialization: distressed/stressed credits, restructuring plays, turnarounds
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FIDistressedAgent(FIBaseAgent):
    """
    Distressed credit specialist. Buys when spreads imply an excessive default
    probability relative to the actual recovery/turnaround potential.
    Focuses on companies with genuine asset coverage and operational path forward.

    Wide scope: covers all major distressed/stressed sectors. Surfaces WATCH picks
    during normal markets so the list is ready when distress arrives.
    """

    SEGMENT = "distressed"
    THESIS  = (
        "Distressed: buy when spread implies extreme default probability but "
        "asset coverage ratio > 1x and management has a credible restructuring plan. "
        "Asymmetric: downside limited by asset value, upside from recovery. "
        "Never buy distressed for yield — buy for recovery. Avoid pure equity-like "
        "CCC unless post-restructuring emergence candidate."
    )

    WATCHLIST = [
        # ── Distressed ETFs ───────────────────────────────────────────────────
        "USHY",  # iShares Broad HY — includes CCC tier, proxy for distressed universe
        "HYEM",  # VanEck EM HY Bond — EM distressed credit, wider spreads
        "FALN",  # iShares Fallen Angels — recent downgrades, sometimes near-distressed
        "SJNK",  # SPDR Short-Term HY — short-maturity distressed, lower duration risk

        # ── Stressed/Distressed single names ─────────────────────────────────
        "WBA",   # Walgreens Boots: HY since downgrade, pharmacy network asset coverage
        "DISH",  # DISH/EchoStar: spectrum value ($30-40B) vs. $20B+ debt, wide spread
        "ALK",   # Alaska Airlines B+: carrier recovery, near-distressed spread vs. ops
        "SAVE",  # Spirit Airlines: near-bankruptcy proxy, ultra-wide spread
        "PARA",  # Paramount: streaming losses, $15B content library vs. $14B debt
        "WBD",   # Warner Bros Discovery: content + network assets vs. debt load
        "VIAC",  # See PARA
        "BALL",  # Ball Corporation BB: packaging, FCF positive, spread vs. leverage
        "ODP",   # ODP Corporation B+: office products, secular decline but FCF positive
        "BIG",   # Big Lots B: discount retail restructuring, real estate asset coverage
        "BBWI",  # Bath & Body Works BB: high leverage vs. strong brand FCF
        "RGS",   # Regis Corp: salon franchise, below-book spread, restructuring
        "CURO",  # CURO Group: consumer finance, stressed spread vs. loan book
        "OMF",   # OneMain Financial BB: consumer finance, high spread vs. genuine cash
        "SLM",   # Sallie Mae BB: student lending, spread vs. government-backed portfolio
        "NAVI",  # Navient BB-: student loan servicer, regulatory discount + spread
        "CACC",  # Credit Acceptance BB: auto lending, spread vs. secured loan book
        "CASL",  # Castle Peak Holding: real estate distressed proxy
        "MPW",   # Medical Properties Trust BB-: REIT under tenant stress, RE asset cover
        "OHI",   # Omega Healthcare B+: skilled nursing, government payment risk spread
        "SBRA",  # Sabra Health Care BB-: REIT under operator stress, asset coverage

        # ── Energy distressed ─────────────────────────────────────────────────
        "TELL",  # Tellurian: LNG distressed, project finance spread
        "CRC",   # California Resources B+: California oil, regulatory risk premium
        "CIVI",  # Civitas Resources BB: Permian, commodity-linked spread
        "MTDR",  # Matador Resources BB: Permian E&P, quality ops + HY spread

        # ── Retail distressed ─────────────────────────────────────────────────
        "BBBY",  # Bed Bath & Beyond proxy — successor or restructured entity
        "TUEM",  # Tuesday Morning: post-bankruptcy emergence candidate proxy
    ]

    def _score_ticker(self, ticker: str) -> dict:
        hy_spread = self.spreads.get("hy_spread_bps", 350)
        regime    = self.regime

        # Distressed opportunities grow when HY spreads blow out
        if regime == "DISTRESSED" or hy_spread > 700:
            conviction = 5
        elif regime == "WIDE" or hy_spread > 500:
            conviction = 4
        elif hy_spread > 400:
            conviction = 3
        else:
            conviction = 2   # Surface for monitoring — distress can arrive suddenly

        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      "BUY" if conviction >= 4 else ("WATCH" if conviction >= 2 else "HOLD"),
            "reasoning":   (
                f"{ticker}: distressed/stressed | regime={regime} | "
                f"HY spread={hy_spread}bps | "
                f"thesis=asset coverage > default implied probability"
            ),
            "credit_tier": "DISTRESSED",
            "segment":     self.SEGMENT,
        }
