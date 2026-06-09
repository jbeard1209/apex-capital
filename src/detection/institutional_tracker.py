"""
Institutional Tracker — Research Team
Combines 13F filing signals with market microstructure signals
to build high-conviction institutional accumulation alerts.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from src.detection.block_detector import BlockDetector, BlockPurchaseSignal
from src.data.market_data import MarketDataProvider
from config import BLOCK_CONFIG, STRATEGY_CONFIG, STOCK_UNIVERSE

logger = logging.getLogger(__name__)


class InstitutionalSignal:
    """
    A composite signal combining 13F data + market microstructure.
    High-conviction institutional accumulation alert.
    """

    def __init__(
        self,
        ticker: str,
        institutions_buying: List[str],
        filing_signal_strength: float,   # Based on # institutions + size of increase
        market_signal_strength: float,    # Based on block detection
        composite_score: float,
        entry_price: float,
        signal_date: str,
        holding_period_days: int = 90,
    ):
        self.ticker = ticker
        self.institutions_buying = institutions_buying
        self.filing_signal_strength = filing_signal_strength
        self.market_signal_strength = market_signal_strength
        self.composite_score = composite_score
        self.entry_price = entry_price
        self.signal_date = signal_date
        self.holding_period_days = holding_period_days

    def expected_exit_date(self) -> str:
        d = datetime.strptime(self.signal_date[:10], "%Y-%m-%d")
        return (d + timedelta(days=self.holding_period_days)).strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "institutions_buying": self.institutions_buying,
            "institution_count": len(self.institutions_buying),
            "filing_signal": round(self.filing_signal_strength, 4),
            "market_signal": round(self.market_signal_strength, 4),
            "composite_score": round(self.composite_score, 4),
            "entry_price": round(self.entry_price, 2),
            "signal_date": self.signal_date,
            "expected_exit": self.expected_exit_date(),
        }

    def __repr__(self):
        return (
            f"InstitutionalSignal({self.ticker} | "
            f"Score={self.composite_score:.2f} | "
            f"Buyers={len(self.institutions_buying)} institutions | "
            f"Entry=${self.entry_price:.2f})"
        )


class InstitutionalTracker:
    """
    Main tracking engine.

    Workflow:
    1. Load 13F filing deltas (which institutions bought what)
    2. Score each ticker using block detection on price/volume
    3. Combine signals into conviction-ranked opportunity list
    4. Apply minimum institution count filter
    5. Output ranked buy signals
    """

    def __init__(
        self,
        market_data: MarketDataProvider,
        block_detector: Optional[BlockDetector] = None,
    ):
        self.market = market_data
        self.detector = block_detector or BlockDetector(**BLOCK_CONFIG)
        self.min_institutions = STRATEGY_CONFIG["institutional_follow"][
            "min_institutions_buying"
        ]

    def generate_signals(
        self,
        filing_delta: pd.DataFrame,
        lookback_days: int = 60,
        end_date: Optional[str] = None,
    ) -> List[InstitutionalSignal]:
        """
        Generate buy signals from 13F filing deltas + market data.

        Args:
            filing_delta: Output of SECFetcher.get_all_institutions_signals()
            lookback_days: Days of price history to analyze
            end_date: Analysis end date (defaults to today)

        Returns:
            List of InstitutionalSignal objects, sorted by composite_score
        """
        if filing_delta.empty:
            logger.warning("No filing delta provided — running in market-signal-only mode")
            return self._market_only_signals(lookback_days, end_date)

        end = end_date or datetime.now().strftime("%Y-%m-%d")
        start = (
            datetime.strptime(end, "%Y-%m-%d") - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        signals = []

        # Group by ticker to find multi-institution buys
        ticker_col = "ticker" if "ticker" in filing_delta.columns else "cusip"
        if ticker_col not in filing_delta.columns:
            return []

        # Filter for buying only
        buying = filing_delta[
            filing_delta["change_type"].isin(["NEW_POSITION", "INCREASED"])
        ]

        grouped = buying.groupby(ticker_col)

        for ticker, group in grouped:
            if not ticker or ticker == "nan" or len(ticker) < 1:
                continue

            institutions_buying = group["institution"].tolist()
            institution_count = len(institutions_buying)

            # Filter: need minimum institution count
            if institution_count < self.min_institutions:
                continue

            # File signal strength: more institutions + larger positions = stronger
            avg_delta_pct = group["shares_delta_pct"].mean() if "shares_delta_pct" in group else 0
            filing_strength = min(
                1.0,
                (institution_count / 5) * 0.5  # Up to 0.5 from institution count
                + min(avg_delta_pct / 100, 0.5),  # Up to 0.5 from position size
            )

            # Market signal from block detection
            df = self.market.compute_rolling_metrics(ticker, start, end)
            if df.empty:
                market_strength = 0.3  # No market data — use default
                current_price = 0.0
            else:
                score = self.detector.score_ticker(ticker, df)
                market_strength = score["composite_score"]
                current_price = float(df["Close"].iloc[-1]) if not df.empty else 0.0

            # Composite score
            composite = (filing_strength * 0.6) + (market_strength * 0.4)

            signals.append(InstitutionalSignal(
                ticker=ticker,
                institutions_buying=institutions_buying,
                filing_signal_strength=filing_strength,
                market_signal_strength=market_strength,
                composite_score=composite,
                entry_price=current_price,
                signal_date=end,
                holding_period_days=STRATEGY_CONFIG["institutional_follow"][
                    "holding_period_days"
                ],
            ))

        # Sort by composite score descending
        signals.sort(key=lambda s: s.composite_score, reverse=True)
        logger.info(f"Generated {len(signals)} institutional signals")
        return signals

    def _market_only_signals(
        self, lookback_days: int, end_date: Optional[str]
    ) -> List[InstitutionalSignal]:
        """
        Fallback: run block detection on the full stock universe
        when no 13F data is available.
        """
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        start = (
            datetime.strptime(end, "%Y-%m-%d") - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        signals = []
        logger.info(f"Running market-only signal scan on {len(STOCK_UNIVERSE)} stocks...")

        for ticker in STOCK_UNIVERSE:
            df = self.market.compute_rolling_metrics(ticker, start, end)
            if df.empty:
                continue

            score = self.detector.score_ticker(ticker, df)
            if score["composite_score"] < 0.55:
                continue

            current_price = float(df["Close"].iloc[-1])
            signals.append(InstitutionalSignal(
                ticker=ticker,
                institutions_buying=["Market Signal Only"],
                filing_signal_strength=0.0,
                market_signal_strength=score["composite_score"],
                composite_score=score["composite_score"] * 0.7,  # Discount: no filing confirmation
                entry_price=current_price,
                signal_date=end,
            ))

        signals.sort(key=lambda s: s.composite_score, reverse=True)
        return signals[:20]  # Return top 20

    def rank_signals(
        self, signals: List[InstitutionalSignal], top_n: int = 10
    ) -> pd.DataFrame:
        """Convert signal list to a ranked DataFrame for reporting."""
        if not signals:
            return pd.DataFrame()

        rows = [s.to_dict() for s in signals[:top_n]]
        df = pd.DataFrame(rows)

        # Add ranking
        df.insert(0, "rank", range(1, len(df) + 1))

        # Add signal tier
        def tier(score):
            if score >= 0.75:
                return "A  (High Conviction)"
            elif score >= 0.55:
                return "B  (Medium Conviction)"
            else:
                return "C  (Speculative)"

        df["tier"] = df["composite_score"].apply(tier)
        return df

    def simulate_live_scan(
        self,
        tickers: Optional[List[str]] = None,
        days_back: int = 30,
    ) -> pd.DataFrame:
        """
        Simulate a live scan without 13F data.
        Scans the provided tickers (or full universe) for block patterns.
        Returns scored DataFrame.
        """
        universe = tickers or STOCK_UNIVERSE[:30]  # Limit for demo
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days_back + 30)).strftime("%Y-%m-%d")

        results = []
        for ticker in universe:
            df = self.market.compute_rolling_metrics(ticker, start, end)
            if df.empty:
                continue
            score = self.detector.score_ticker(ticker, df)
            if score["composite_score"] > 0:
                current_price = float(df["Close"].iloc[-1])
                results.append({
                    "ticker": ticker,
                    "composite_score": score["composite_score"],
                    "signal_count": score["signal_count"],
                    "price": current_price,
                })

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results).sort_values(
            "composite_score", ascending=False
        )
        result_df["rank"] = range(1, len(result_df) + 1)
        return result_df
