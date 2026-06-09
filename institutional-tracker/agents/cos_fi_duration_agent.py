# Auto-generated FI agent — Duration Management
# Specialization: rate sensitivity, yield curve plays, duration positioning
# Lane: fixed_income | Tier: 1
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cos_fi_base import FIBaseAgent


class FIDurationAgent(FIBaseAgent):
    """
    Duration specialist — manages interest rate sensitivity positioning.
    Buys long-duration when rates are at cycle highs and rate cuts are expected.
    Buys short-duration (or inverse duration) when rates are rising.
    Also covers TIPS for inflation-linked real yield opportunities.

    Wide scope: 35+ vehicles across the duration curve and TIPS universe.
    """

    SEGMENT = "duration"
    THESIS  = (
        "Duration management: buy long bonds when 10yr yield is elevated vs. "
        "cycle norms and Fed pivot is approaching. Rate sensitivity = convexity "
        "return when rates fall. Short duration / TIPS in rising rate environment. "
        "Real yields on TIPS attractive when nominal yields > expected inflation."
    )

    WATCHLIST = [
        # ── Broad Treasury Duration ───────────────────────────────────────────
        "TLT",   # iShares 20yr+ Treasury — highest duration, max rate sensitivity
        "EDV",   # Vanguard Extended Duration — zero-coupon Treasuries, ultra-high duration
        "VGLT",  # Vanguard Long-Term Treasury — cheaper TLT alternative
        "SPTL",  # SPDR Long-Term Treasury — another long-duration vehicle
        "IEF",   # iShares 7-10yr Treasury — intermediate duration, core positioning
        "VGIT",  # Vanguard Intermediate-Term Treasury — same duration, lower cost
        "SPTI",  # SPDR Intermediate Treasury — broad intermediate exposure
        "SHY",   # iShares 1-3yr Treasury — minimal duration, near-cash government
        "BIL",   # SPDR 1-3 Month T-Bill — essentially cash-equivalent, rate floor
        "TBIL",  # US Treasury 3-Month T-Bill — near-zero duration, max safety
        "USFR",  # WisdomTree Floating Rate Treasury — no duration risk, rate floor

        # ── TIPS / Inflation Protection ───────────────────────────────────────
        "TIP",   # iShares TIPS Bond — core inflation protection vehicle
        "SCHP",  # Schwab TIPS — lowest cost TIPS exposure
        "VTIP",  # Vanguard Short-Term TIPS — 0-5yr, minimal nominal rate risk
        "STIP",  # iShares 0-5yr TIPS — alternative short TIPS vehicle
        "LTPZ",  # PIMCO Long-Term TIPS — long-duration real yield positioning
        "TIPX",  # SPDR TIPS — alternative broad TIPS vehicle

        # ── Yield Curve Trades ────────────────────────────────────────────────
        "FLAT",  # iPath Yield Curve Flatten — profits from yield curve flattening
        "STPP",  # iPath Yield Curve Steepen — profits from yield curve steepening
        "IVOL",  # Quadratic Rate Volatility — long volatility + TIPS, unique exposure

        # ── Mortgage Duration ─────────────────────────────────────────────────
        "MBB",   # iShares MBS — agency MBS, negative convexity but government support
        "VMBS",  # Vanguard MBS — alternative agency MBS
        "GNMA",  # iShares GNMA — Ginnie Mae explicit government guarantee
        "CMBS",  # iShares CMBS — commercial MBS, higher spread + shorter duration

        # ── International Rate Exposure ───────────────────────────────────────
        "BWX",   # SPDR International Treasury — ex-US sovereign, currency diversification
        "IGOV",  # iShares International Treasury — developed market sovereign bonds
        "EMB",   # iShares USD EM Bond — emerging market sovereign, USD-denominated
        "PCY",   # Invesco EM Sovereign Debt — higher-spread EM sovereign alternative
        "LEMB",  # iShares EM Local Currency Bond — currency + rate dual play

        # ── Rate Inverse (hedges) ─────────────────────────────────────────────
        "TBF",   # ProShares Short 20yr Treasury — inverse long bond, rising rate hedge
        "TBT",   # ProShares UltraShort 20yr — 2x inverse long bond
        "RATES", # Global X Interest Rate Hedge — explicit rate-rise hedge

        # ── Munis ─────────────────────────────────────────────────────────────
        "MUB",   # iShares National Muni Bond — tax-exempt IG muni, tax-equivalent yield
        "VTEB",  # Vanguard Tax-Exempt Bond — lowest-cost muni vehicle
        "HYD",   # VanEck HY Muni — muni credit spread, tax-exempt HY
        "SHYD",  # VanEck Short HY Muni — short duration muni credit
    ]

    def _score_ticker(self, ticker: str) -> dict:
        tsy_10y  = self.spreads.get("treasury_10y_yield", 4.5)
        regime   = self.regime

        # Long duration attractive when 10yr is high (>4.5% = near cycle high)
        long_dur = ticker in ("TLT","EDV","VGLT","SPTL","LTPZ","TIP")
        short_dur = ticker in ("SHY","BIL","TBIL","USFR","VTIP","STIP")
        inverse  = ticker in ("TBF","TBT","RATES")

        if tsy_10y >= 4.5:
            conviction = 4 if long_dur else (2 if short_dur else 3)
        elif 3.5 <= tsy_10y < 4.5:
            conviction = 3
        else:
            conviction = 2 if long_dur else 4

        if inverse:
            conviction = 5 - conviction   # inverse: high conviction when rates rising

        return {
            "ticker":      ticker,
            "conviction":  max(2, conviction),
            "action":      "BUY" if conviction >= 3 else "WATCH",
            "reasoning":   (
                f"{ticker}: duration play | 10yr={tsy_10y}% | regime={regime} | "
                f"long_dur={long_dur} | thesis=rate positioning"
            ),
            "credit_tier": "GOVERNMENT" if ticker in ("TLT","EDV","IEF","SHY","BIL","TIP") else "MIXED",
            "segment":     self.SEGMENT,
        }
