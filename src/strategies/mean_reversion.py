"""
Mean Reversion Strategy — Quantitative Analysis Team (Comparison Strategy)
Buy oversold stocks, sell at mean reversion. Z-score based.
"""

from typing import Dict, List, Optional
import pandas as pd
import numpy as np

from src.strategies.base_strategy import BaseStrategy
from config import STRATEGY_CONFIG, BACKTEST_CONFIG


class MeanReversionStrategy(BaseStrategy):
    """
    Mean Reversion Strategy (comparison baseline).

    Rules:
    - Compute rolling Z-score of returns for each stock
    - Buy when Z-score < -2.0 (oversold)
    - Exit when Z-score returns to 0.0
    - Stop loss at -5%
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = config or STRATEGY_CONFIG["mean_reversion"]
        super().__init__("Mean Reversion (Z-Score)", cfg)
        self.lookback = cfg.get("lookback_days", 20)
        self.z_entry = cfg.get("z_score_entry", -2.0)
        self.z_exit = cfg.get("z_score_exit", 0.0)
        self.max_position_pct = BACKTEST_CONFIG["max_position_size_pct"]

    def generate_signals(
        self, prices: pd.DataFrame, **kwargs
    ) -> pd.DataFrame:
        """Generate mean reversion signals based on Z-score."""
        signals = pd.DataFrame(0, index=prices.index, columns=prices.columns)

        for ticker in prices.columns:
            series = prices[ticker].dropna()
            if len(series) < self.lookback + 5:
                continue

            returns = series.pct_change()
            roll_mean = returns.rolling(self.lookback).mean()
            roll_std = returns.rolling(self.lookback).std()
            z_score = (returns - roll_mean) / roll_std.replace(0, np.nan)

            in_position = False
            for i in range(self.lookback, len(z_score)):
                z = z_score.iloc[i]
                idx = z_score.index[i]
                if pd.isna(z):
                    continue

                if not in_position and z <= self.z_entry:
                    # Enter long (oversold)
                    if ticker in signals.columns and idx in signals.index:
                        signals.loc[idx, ticker] = 1
                    in_position = True
                elif in_position and z >= self.z_exit:
                    # Exit at mean
                    if ticker in signals.columns and idx in signals.index:
                        signals.loc[idx, ticker] = -1
                    in_position = False
                elif in_position:
                    # Hold
                    if ticker in signals.columns and idx in signals.index:
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
