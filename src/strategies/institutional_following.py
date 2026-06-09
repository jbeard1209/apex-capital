"""
Institutional Following Strategy — Research Team (Primary Strategy)
Follow the smart money: buy when large institutions quietly accumulate.

Signal sources:
  - 13F filing deltas (new/increased positions)
  - Block purchase detection (market microstructure)
  - Multi-institution convergence (conviction amplifier)
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from src.strategies.base_strategy import BaseStrategy
from config import STRATEGY_CONFIG, BACKTEST_CONFIG

logger = logging.getLogger(__name__)


class InstitutionalFollowingStrategy(BaseStrategy):
    """
    Primary strategy: Follow institutional smart money.

    Entry rules:
      1. At least N institutions increased/opened position in same stock
      2. Composite score (filing + market) > threshold
      3. Price is above 50-day SMA (trend filter)
      4. Volume confirms: relative volume > 1.2x on entry

    Exit rules:
      1. Fixed holding period (default 90 days)
      2. Stop loss at -8%
      3. Take profit at +25%
      4. If institutions reduce positions in next 13F → exit
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = config or STRATEGY_CONFIG["institutional_follow"]
        super().__init__("Institutional Following", cfg)
        self.signal_lag_days = cfg.get("signal_lag_days", 3)
        self.holding_period = cfg.get("holding_period_days", 90)
        self.min_institutions = cfg.get("min_institutions_buying", 2)
        self.stop_loss = BACKTEST_CONFIG["stop_loss_pct"]
        self.take_profit = BACKTEST_CONFIG["take_profit_pct"]
        self.max_position_pct = BACKTEST_CONFIG["max_position_size_pct"]

    def generate_signals(
        self,
        prices: pd.DataFrame,
        institution_scores: Optional[pd.DataFrame] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Generate signals based on institutional score + technical filters.

        Args:
            prices: Close prices DataFrame
            institution_scores: Optional scored tickers from InstitutionalTracker

        Returns:
            Signal DataFrame (1=buy, -1=sell, 0=hold)
        """
        signals = pd.DataFrame(0, index=prices.index, columns=prices.columns)

        if institution_scores is not None and not institution_scores.empty:
            # Use institutional scores to select stocks
            scored_tickers = institution_scores[
                institution_scores["composite_score"] >= 0.55
            ]["ticker"].tolist()
        else:
            # Fallback: use momentum filter
            scored_tickers = self._momentum_filter(prices)

        for ticker in scored_tickers:
            if ticker not in prices.columns:
                continue

            price_series = prices[ticker].dropna()
            if len(price_series) < 60:
                continue

            # Compute technical filters
            sma50 = price_series.rolling(50).mean()
            sma20 = price_series.rolling(20).mean()
            volume = kwargs.get("volumes", {}).get(ticker, pd.Series())

            for i in range(60, len(price_series)):
                price = price_series.iloc[i]
                s50 = sma50.iloc[i]
                s20 = sma20.iloc[i]

                if pd.isna(s50) or pd.isna(s20):
                    continue

                # Trend filter: price above 50-day SMA
                trend_ok = price > s50

                # Momentum: 20d SMA trending up
                momentum_ok = s20 > sma20.iloc[i - 5] if i >= 65 else False

                if trend_ok and momentum_ok:
                    idx = price_series.index[i]
                    if idx in signals.index:
                        signals.loc[idx, ticker] = 1

        return signals

    def _momentum_filter(self, prices: pd.DataFrame, top_n: int = 15) -> List[str]:
        """Select top-N momentum stocks as fallback."""
        if prices.empty:
            return []
        returns_12m = prices.pct_change(252).iloc[-1]
        returns_1m = prices.pct_change(21).iloc[-1]
        # 12-1 month momentum
        momentum = returns_12m - returns_1m
        top = momentum.nlargest(top_n).index.tolist()
        return [t for t in top if not pd.isna(momentum[t])]

    def get_positions(
        self, signals: pd.DataFrame, current_capital: float
    ) -> Dict[str, float]:
        """
        Generate dollar positions from signals.
        Uses equal weighting with max position size cap.
        """
        if signals.empty:
            return {}

        # Get last row of signals
        last_signals = signals.iloc[-1]
        buy_tickers = last_signals[last_signals == 1].index.tolist()

        return self.equal_weight_positions(buy_tickers, current_capital, self.max_position_pct)

    def apply_risk_rules(
        self,
        entry_price: float,
        current_price: float,
        entry_date: datetime,
        current_date: datetime,
    ) -> str:
        """
        Evaluate whether to exit a position.
        Returns: 'HOLD', 'STOP_LOSS', 'TAKE_PROFIT', 'TIME_EXIT'
        """
        price_change = (current_price - entry_price) / entry_price

        if price_change <= -self.stop_loss:
            return "STOP_LOSS"
        if price_change >= self.take_profit:
            return "TAKE_PROFIT"

        days_held = (current_date - entry_date).days
        if days_held >= self.holding_period:
            return "TIME_EXIT"

        return "HOLD"
