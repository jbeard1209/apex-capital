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

    # Every reading in this pod derives from these three. If the context does not
    # carry them, the agent has no opinion — see the note on run().
    REQUIRED_SPREAD_KEYS = ("hy_spread_bps", "ig_spread_bps")

    def __init__(self, spread_context: dict = None):
        self.spreads = spread_context or {}
        # fi_credit_monitor publishes the regime under `spread_regime`. Reading
        # `regime` alone meant self.regime was the literal string "NORMAL" on
        # every run since 2026-06-09 — including through fi_pipeline.py, which
        # DOES pass live context. A WIDE or DISTRESSED credit market has never
        # once reached these agents, so `if regime in ("WIDE","DISTRESSED")`
        # in seven subclasses is dead code that has never evaluated true.
        self.regime = (self.spreads.get("spread_regime")
                       or self.spreads.get("regime")
                       or "UNKNOWN")
        # Not "did somebody pass a dict?" but "does it carry the numbers?".
        self.has_live_spreads = all(
            self.spreads.get(k) is not None for k in self.REQUIRED_SPREAD_KEYS
        )

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
        # ── No live spreads => no picks. Not a softer opinion: NO opinion. ──
        # Every subclass scores with `self.spreads.get("hy_spread_bps", 350)`.
        # A missing feed therefore does not fail — it silently becomes "HY at
        # 350bps, IG at 120bps", which scores NORMAL, which scores conviction 4,
        # which BUYs. Every ticker. Unconditionally. The whole watchlist.
        #
        # That is exactly what happened: research_team.py constructs these
        # agents with no spread_context, and on 2026-08-14 the manager received
        # 118 fixed-income BUYs whose reasoning strings all read "HY spread=350bps
        # IG spread=120bps" — the defaults, verbatim, on every pick, six times a
        # day. Live spreads that hour were 315/158. The pod was not analysing the
        # credit market; it was reciting two literals from this file.
        #
        # Same family as memory `nan-is-not-none` and `presence-is-not-currency`:
        # a plausible fallback makes an absent input indistinguishable from a
        # measured one, and confident output is the failure mode. A default is
        # only safe when being wrong about it is cheap. Here it is a buy order.
        if not self.has_live_spreads:
            logger.warning(
                f"  [{self.__class__.__name__}] no live spread context "
                f"({', '.join(self.REQUIRED_SPREAD_KEYS)} absent) — 0 picks from "
                f"{len(self.WATCHLIST)} candidates. Refusing to score off defaults."
            )
            return {
                "agent":           self.__class__.__name__,
                "segment":         self.SEGMENT,
                "thesis":          self.THESIS,
                "regime":          self.regime,
                "recommendations": [],
                "tickers_scanned": 0,
                "abstained":       True,
                "abstain_reason":  ("no live credit-spread context — scoring off "
                                    "hardcoded defaults would fabricate a signal"),
            }

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
            "abstained":       False,
        }
