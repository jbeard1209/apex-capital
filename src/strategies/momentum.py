"""
Momentum Strategy — Quantitative Analysis Team (Comparison Strategy)
Classic 12-1 month cross-sectional momentum with monthly rebalance.
Fama-French / Jegadeesh-Titman momentum factor.
"""

from typing import Dict, List, Optional
import pandas as pd
import numpy as np

from src.strategies.base_strategy import BaseStrategy
from config import STRATEGY_CONFIG, BACKTEST_CONFIG


class MomentumStrategy(BaseStrategy):
    """
    Momentum Strategy (comparison baseline).

    Rules:
    - Monthly rebalance
    - Rank stocks by 12-1 month return (skip last month)
    - Buy top quintile (top 20%)
    - Equal weight positions
    - Hold 1 month, then rebalance
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = config or STRATEGY_CONFIG["momentum"]
        super().__init__("Momentum (12-1M)", cfg)
        self.lookback = cfg.get("lookback_days", 252)
        self.holding_period = cfg.get("holding_period_days", 21)
        self.top_percentile = cfg.get("top_percentile", 0.2)
        self.max_position_pct = BACKTEST_CONFIG["max_position_size_pct"]

    def generate_signals(
        self, prices: pd.DataFrame, **kwargs
    ) -> pd.DataFrame:
        """
        Generate monthly momentum signals.
        Buy top quintile momentum stocks, sell bottom quintile.
        """
        signals = pd.DataFrame(0, index=prices.index, columns=prices.columns)
        skip_recent = 21  # Skip last month (reversal effect)

        for i in range(self.lookback + skip_recent, len(prices)):
            if i % self.holding_period != 0:  # Only rebalance monthly
                continue

            # 12-1 month momentum
            past_price = prices.iloc[i - self.lookback - skip_recent]
            recent_price = prices.iloc[i - skip_recent]
            current_price = prices.iloc[i]

            momentum = (recent_price - past_price) / past_price.replace(0, np.nan)
            momentum = momentum.dropna()

            if momentum.empty:
                continue

            # Select top quintile
            threshold = momentum.quantile(1 - self.top_percentile)
            buy_tickers = momentum[momentum >= threshold].index.tolist()

            # Generate buy signals for holding period
            for j in range(i, min(i + self.holding_period, len(prices))):
                idx = prices.index[j]
                for ticker in buy_tickers:
                    if ticker in signals.columns:
                        signals.loc[idx, ticker] = 1

        return signals

    def get_positions(
        self, signals: pd.DataFrame, current_capital: float
    ) -> Dict[str, float]:
        if signals.empty:
            return {}
        last = signals.iloc[-1]
        buys = last[last == 1].index.tolist()
        return self.equal_weight_positions(buys, current_capital, self.max_position_pct)
