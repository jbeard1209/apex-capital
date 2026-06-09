# Auto-generated FI agent — Preferred Securities
# Specialization: preferred stock, hybrid securities, baby bonds
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FIPreferredAgent(FIBaseAgent):
    """
    Preferred securities specialist. Preferreds sit between debt and equity —
    dividend priority over common, junior to debt. Attractive when yield spread
    vs. treasuries compensates for subordination risk.

    Baby bonds (exchange-listed term notes) treated as preferreds here.
    Wide scope: 40+ vehicles to maximize yield-hunting opportunities.
    """

    SEGMENT = "preferred"
    THESIS  = (
        "Preferreds: senior to common equity, pay fixed/floating dividend, "
        "often callable at par. Attractive when: (a) yield spread vs. 10yr treasury "
        "exceeds 250bps on investment-grade issuer, or (b) discount to liquidation "
        "preference > 10% on financially sound issuer. "
        "Bank preferreds offer deep expertise diversification."
    )

    WATCHLIST = [
        # ── Preferred ETFs ────────────────────────────────────────────────────
        "PFF",   # iShares Preferred & Income — largest preferred ETF, benchmark
        "PGX",   # Invesco Preferred — alternative, slightly different composition
        "PFFD",  # Global X US Preferred — less bank concentration than PFF
        "PFXF",  # VanEck Preferred Securities ex-Financials — removes bank risk
        "PSK",   # SPDR Wells Fargo Preferred — institutional-quality preferred index
        "IPFF",  # iShares International Preferred — global preferred exposure
        "FLRN",  # SPDR Floating Rate Treasury (also here for rate-reset comparison)

        # ── Bank / Insurance Preferreds ───────────────────────────────────────
        "BAC-PL", # Bank of America preferred — IG quality, 6%+ yield
        "WFC-PL", # Wells Fargo preferred — IG quality, above-par yield
        "JPM-PD", # JPMorgan preferred — highest quality bank preferred
        "GS-PA",  # Goldman Sachs preferred — broker-dealer risk premium
        "MS-PE",  # Morgan Stanley preferred — investment bank preferred
        "C-PJ",   # Citigroup preferred — international bank premium
        "USB-PP", # US Bancorp preferred — regional + IG quality
        "RF-PB",  # Regions Financial preferred — regional bank preferred
        "KEY-PI", # KeyCorp preferred — regional bank, yield opportunity
        "MET-A",  # MetLife preferred — life insurer, durable dividend
        "PRU-B",  # Prudential preferred — diversified insurer preferred
        "AFL-A",  # Aflac preferred — specialty insurer, quality preferred

        # ── REIT Preferreds ───────────────────────────────────────────────────
        "PSA-PL", # Public Storage preferred — storage REIT, A-rated
        "SPG-PJ", # Simon Property Group preferred — best mall REIT, IG quality
        "VNO-PM", # Vornado preferred — NYC office REIT, wide spread
        "SLG-PI", # SL Green preferred — NYC office, wide spread vs. IG quality
        "BXP-PB", # Boston Properties preferred — gateway office REIT
        "NHI-NA", # National Health Investors preferred — healthcare REIT
        "MPW-B",  # Medical Properties Trust preferred — wide spread opportunity
        "EPR-E",  # EPR Properties preferred — experiential REIT preferred
        "GOOD-A", # Gladstone Commercial preferred — net lease REIT preferred
        "STAG-C", # STAG Industrial preferred — industrial REIT preferred

        # ── Utility / Energy Preferreds ───────────────────────────────────────
        "NEE-PR", # NextEra Energy preferred — best utility, IG quality
        "DUK-PA", # Duke Energy preferred — regulated utility preferred
        "SO-PA",  # Southern Company preferred — regulated utility
        "D-PA",   # Dominion Energy preferred — utility preferred
        "AEP-PA", # American Electric Power preferred — utility preferred
        "ET-PC",  # Energy Transfer preferred — midstream MLP preferred, high yield
        "EPD-AC", # Enterprise Products preferred — IG midstream preferred
        "MPLX-PA",# MPLX preferred — refining/midstream preferred

        # ── Baby Bonds (exchange-listed term notes) ───────────────────────────
        "OXLCM",  # Oxford Lane Capital notes — BDC baby bond, credit exposure
        "GLADL",  # Gladstone Investment notes — IG-adjacent BDC note
        "GAIN-B", # Gladstone Acquisition notes — BDC term note
    ]

    def _score_ticker(self, ticker: str) -> dict:
        tsy_10y  = self.spreads.get("treasury_10y_yield", 4.5)
        regime   = self.regime

        # Preferred yield spread vs. 10yr — target 250bps+
        # When 10yr is high, preferreds need to yield 6.75%+ to be attractive
        # Most preferreds yield 5.5-7.5% depending on quality/subordination
        estimated_yield_spread = 300   # approximate — real scoring done by fi_data_layer

        if estimated_yield_spread > 300:
            conviction = 4
        elif estimated_yield_spread > 250:
            conviction = 3
        else:
            conviction = 2

        is_etf = ticker in ("PFF","PGX","PFFD","PFXF","PSK","IPFF","FLRN")
        if is_etf:
            conviction = min(conviction + 1, 5)

        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      "BUY" if conviction >= 3 else "WATCH",
            "reasoning":   (
                f"{ticker}: preferred/hybrid security | regime={regime} | "
                f"10yr={tsy_10y}% | "
                f"thesis=yield spread vs. treasury + subordination premium"
            ),
            "credit_tier": "PREFERRED",
            "segment":     self.SEGMENT,
        }
