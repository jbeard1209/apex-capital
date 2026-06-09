"""
Value Strategy — Quantitative Analysis Team (Comparison Strategy)
Low P/E, low P/B quantitative value screen.
"""

from typing import Dict, List, Optional
import pandas as pd
import numpy as np

from src.strategies.base_strategy import BaseStrategy
from config import STRATEGY_CONFIG, BACKTEST_CONFIG


class ValueStrategy(BaseStrategy):
    """
    Value Strategy (comparison baseline).

    Rules:
    - Screen for low P/E (< 20) and low P/B (< 3)
    - Buy stocks in bottom quintile of valuation
    - Hold 6 months, rebalance quarterly
    - Equal weight

    Note: In backtesting, we approximate value using price/book proxy
    via price relative to its 52-week high (price at a discount).
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = config or STRATEGY_CONFIG["value"]
        super().__init__("Value (Low P/E, P/B)", cfg)
        self.holding_period = cfg.get("holding_period_days", 180)
        self.max_position_pct = BACKTEST_CONFIG["max_position_size_pct"]

    def generate_signals(
        self, prices: pd.DataFrame, **kwargs
    ) -> pd.DataFrame:
        """
        Value proxy: buy stocks that are furthest below their 52-week high
        (on-sale relative to recent peak) with positive recent trend.
        """
        signals = pd.DataFrame(0, index=prices.index, columns=prices.columns)
        rebalance_freq = 63  # Quarterly rebalance (approx 63 trading days)

        for i in range(252, len(prices)):
            if i % rebalance_freq != 0:
                continue

            current = prices.iloc[i]
            high_52w = prices.iloc[i - 252 : i].max()

            # Discount to 52-week high (value proxy)
            discount = (current - high_52w) / high_52w
            discount = discount.dropna()

            if discount.empty:
                continue

            # Also require positive 3-month trend (avoid value traps)
            trend_3m = prices.iloc[i] / prices.iloc[max(0, i - 63)] - 1
            trend_filter = trend_3m > -0.15  # Not down more than 15% in 3 months

            # Select stocks with deepest discount but positive trend filter
            value_candidates = discount[trend_filter]
            if value_candidates.empty:
                continue

            threshold = value_candidates.quantile(0.25)  # Bottom quartile (most discounted)
            buy_tickers = value_candidates[value_candidates <= threshold].index.tolist()

            # Hold for rebalance period
            for j in range(i, min(i + rebalance_freq, len(prices))):
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
