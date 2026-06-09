"""
Base Strategy Interface — Quantitative Analysis Team
All strategies implement this interface for backtesting compatibility.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.
    Strategies must implement generate_signals() and get_positions().
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.signals_history: List[dict] = []

    @abstractmethod
    def generate_signals(
        self, prices: pd.DataFrame, **kwargs
    ) -> pd.DataFrame:
        """
        Generate buy/sell signals.

        Args:
            prices: DataFrame of adjusted close prices (columns = tickers)

        Returns:
            DataFrame with same index as prices, columns = tickers,
            values = signal (-1: sell, 0: hold, 1: buy)
        """
        pass

    @abstractmethod
    def get_positions(
        self, signals: pd.DataFrame, current_capital: float
    ) -> Dict[str, float]:
        """
        Convert signals to position sizes (in dollars).

        Returns:
            Dict: {ticker: dollar_amount}
        """
        pass

    def get_name(self) -> str:
        return self.name

    def get_config(self) -> dict:
        return self.config

    def compute_momentum_score(
        self, prices: pd.Series, lookback: int = 252, skip_recent: int = 21
    ) -> float:
        """
        12-1 month momentum: returns over lookback period, skipping most
        recent month to avoid short-term reversal effect.
        """
        if len(prices) < lookback + skip_recent:
            return 0.0
        past = prices.iloc[-(lookback + skip_recent)]
        recent = prices.iloc[-skip_recent]
        return (recent - past) / past if past > 0 else 0.0

    def equal_weight_positions(
        self,
        tickers: List[str],
        total_capital: float,
        max_position_pct: float = 0.10,
    ) -> Dict[str, float]:
        """Equally weight positions up to max_position_pct each."""
        if not tickers:
            return {}
        per_position = min(total_capital / len(tickers), total_capital * max_position_pct)
        return {t: per_position for t in tickers}
