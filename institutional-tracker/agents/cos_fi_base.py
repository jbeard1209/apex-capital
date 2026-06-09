# =============================================================================
# APEX CAPITAL — FI BASE AGENT
# Shared base class for all Fixed Income research agents.
# Each agent receives spread context from fi_pipeline.py and returns
# recommendations in a format compatible with fi_data_layer scoring.
# =============================================================================

import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


class FIBaseAgent:
    """
    Base class for Fixed Income research agents.

    Subclasses define:
        WATCHLIST  — list of tickers/ETFs to scan
        SEGMENT    — "investment_grade" | "high_yield" | "distressed" | "preferred" | "convertible" | "duration"
        THESIS     — one-line investment thesis string
        MIN_CONVICTION — minimum conviction to surface a pick (default 2 — widened per Chairman directive)

    run() → {"segment": str, "recommendations": [...], "agent": str}

    Each recommendation dict:
        ticker, conviction (1–5), action, reasoning, segment, credit_tier
    """

    WATCHLIST     = []
    SEGMENT       = "fixed_income"
    THESIS        = ""
    MIN_CONVICTION = 2   # Chairman directive: surface picks at 2+ so Manager can review

    def __init__(self, spread_context: dict = None):
        self.spreads = spread_context or {}
        self.regime  = self.spreads.get("regime", "NORMAL")

    def _score_ticker(self, ticker: str) -> dict:
        """
        Light scoring pass using spread context.
        Returns {"conviction": int, "reasoning": str, "credit_tier": str, "action": str}
        Subclasses can override for segment-specific logic.
        """
        hy_spread  = self.spreads.get("hy_spread_bps", 350)
        ig_spread  = self.spreads.get("ig_spread_bps", 120)
        regime     = self.regime

        # Base conviction: looser credit conditions = higher conviction
        if regime in ("WIDE", "DISTRESSED"):
            base = 4
        elif regime == "NORMAL":
            base = 3
        else:   # TIGHT
            base = 2

        # Segment-specific adjustment
        conviction  = base
        action      = "BUY"
        credit_tier = "IG"
        reasoning   = f"{ticker}: {self.THESIS} | regime={regime} | spread_context={regime}"

        return {
            "ticker":      ticker,
            "conviction":  conviction,
            "action":      action,
            "reasoning":   reasoning,
            "credit_tier": credit_tier,
            "segment":     self.SEGMENT,
        }

    def run(self) -> dict:
        picks = []
        for ticker in self.WATCHLIST:
            try:
                result = self._score_ticker(ticker)
                if result.get("conviction", 0) >= self.MIN_CONVICTION:
                    picks.append(result)
            except Exception as e:
                logger.debug(f"  [{self.__class__.__name__}] {ticker} scoring failed: {e}")

        picks.sort(key=lambda p: p.get("conviction", 0), reverse=True)
        logger.info(f"  [{self.__class__.__name__}] {len(picks)} picks surfaced from {len(self.WATCHLIST)} candidates")

        return {
            "agent":           self.__class__.__name__,
            "segment":         self.SEGMENT,
            "thesis":          self.THESIS,
            "regime":          self.regime,
            "recommendations": picks,
            "tickers_scanned": len(self.WATCHLIST),
        }
