# Auto-generated FI agent — Investment Grade
# Specialization: IG corporate bonds — quality credit at spread premium
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FIInvestmentGradeAgent(FIBaseAgent):
    """
    Focuses on investment-grade corporate credit with widened spreads.
    Prefers ETFs for core allocation; adds single-name when a specific
    issuer offers exceptional spread/quality mismatch.

    Wide scope: scans 40+ vehicles across the IG universe to maximize
    the probability of finding a viable entry point in any rate regime.
    """

    SEGMENT = "investment_grade"
    THESIS  = (
        "IG credit: buy quality when the market is pricing in excess risk. "
        "BBB-rated bonds near historic wide spread levels offer equity-like "
        "returns without equity-level drawdown risk. Duration-matched ETFs "
        "provide liquid IG exposure. Step up to single-name when specific "
        "issuer spread/fundamentals diverge significantly."
    )

    WATCHLIST = [
        # ── Core IG ETFs ──────────────────────────────────────────────────────
        "LQD",   # iShares IG Corporate — flagship, $30B+ AUM, tightest bid/ask
        "VCIT",  # Vanguard Intermediate Corporate — ultra-low 0.04% ER
        "IGIB",  # iShares IG 5-10yr — intermediate duration sweet spot
        "QLTA",  # iShares Aaa-A Rated Corporate — highest quality IG only
        "GBIL",  # Goldman Sachs Treasury — AAA near-cash, rate-hedged allocation
        "MBB",   # iShares MBS — agency mortgage-backed, government guarantee + spread
        "VMBS",  # Vanguard MBS — ultra-low cost mortgage exposure
        "GNMA",  # iShares GNMA Bond — Ginnie Mae, explicit government guarantee
        "SCHI",  # Schwab Intermediate IG — low-cost intermediate IG exposure
        "SPIB",  # SPDR Portfolio IG Corp — broad intermediate IG

        # ── Short Duration IG ─────────────────────────────────────────────────
        "IGSB",  # iShares Short-Term IG Corp — 1-5yr, minimal rate sensitivity
        "VCSH",  # Vanguard Short-Term Corp — lowest ER short IG
        "CSJ",   # iShares 1-3yr Credit — ultra-short, credit spread with minimal duration
        "SLQD",  # iShares 0-5yr IG Corp — shortest IG duration, floating-rate friendly

        # ── Long Duration IG ──────────────────────────────────────────────────
        "IGLB",  # iShares Long-Term IG Corp — 10yr+, high duration, high spread sensitivity
        "VCLT",  # Vanguard Long-Term Corp — long duration IG, cheap cost
        "SPLB",  # SPDR Long-Term Corporate — 30yr IG, maximum spread sensitivity

        # ── Sector-Specific IG ────────────────────────────────────────────────
        "PFFD",  # Global X Preferred — preferred stock/hybrid IG-adjacent
        "PGX",   # Invesco Preferred — alternative preferred vehicle
        "FLCO",  # Franklin Liberty IG Corp — active IG management, spread-selective

        # ── Single-name IG with spread premium (BBB-rated stress) ────────────
        "T",     # AT&T BBB: $130B+ debt load creates spread premium vs. actual IG quality
        "VZ",    # Verizon BBB: telecom capex debt premium vs. recurring revenue quality
        "F",     # Ford Motor BBB-: auto cycle discount, genuine IG cash generation
        "GM",    # GM BBB-: auto industry cycle, net cash positive since restructuring
        "BA",    # Boeing BBB-: aerospace duopoly, production ramp discount vs. franchise
        "WFC",   # Wells Fargo A: regulatory overhang discount vs. best-in-class deposit franchise
        "BAC",   # Bank of America A: large bank premium vs. pure IG quality
        "JPM",   # JPMorgan A+: highest quality bank IG, tight vs. sector
        "GS",    # Goldman A: broker-dealer premium creates spread vs. bank IG
        "C",     # Citigroup BBB+: international restructuring premium, genuine IG
        "MS",    # Morgan Stanley A: broker-dealer, spread vs. pure bank IG
        "MO",    # Altria BBB: tobacco discount vs. cash generation quality
        "PM",    # Philip Morris A-: international tobacco, lower US legal risk than MO
        "CVS",   # CVS Health BBB: pharmacy/PBM integration debt premium
        "HCA",   # HCA Healthcare BB+: near-IG hospital operator, strong FCF
        "NEM",   # Newmont BBB: gold miner IG, commodity spread premium
    ]

    def _score_ticker(self, ticker: str) -> dict:
        ig_spread = self.spreads.get("ig_spread_bps", 120)
        regime    = self.regime
        tsy_10y   = self.spreads.get("treasury_10y_yield", 4.5)

        # Wide IG spreads = strong conviction
        if ig_spread > 160:
            conviction = 5
        elif ig_spread > 130:
            conviction = 4
        elif ig_spread > 100:
            conviction = 3
        else:
            conviction = 2   # Still surface — spreads might widen from here

        is_etf = len(ticker) <= 4 and ticker in (
            "LQD","VCIT","IGIB","QLTA","GBIL","MBB","VMBS","GNMA","SCHI","SPIB",
            "IGSB","VCSH","CSJ","SLQD","IGLB","VCLT","SPLB","PFFD","PGX","FLCO"
        )
        if is_etf:
            conviction = min(conviction + 1, 5)   # ETFs get +1 for liquidity

        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      "BUY" if conviction >= 3 else "WATCH",
            "reasoning":   (
                f"{ticker}: IG credit | regime={regime} | "
                f"IG spread={ig_spread}bps | 10yr={tsy_10y}% | "
                f"thesis=quality credit at spread premium"
            ),
            "credit_tier": "INVESTMENT GRADE",
            "segment":     self.SEGMENT,
        }
