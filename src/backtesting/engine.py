"""
Backtesting Engine — Quantitative Analysis Team
Event-driven backtester with full performance metrics.

Features:
- Realistic commission/slippage modeling
- Stop-loss and take-profit enforcement
- Portfolio-level drawdown monitoring
- Risk-adjusted return metrics
- Walk-forward validation support
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.strategies.base_strategy import BaseStrategy
from config import BACKTEST_CONFIG

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: Optional[str]
    entry_price: float
    exit_price: Optional[float]
    shares: float
    direction: int    # 1 = long, -1 = short
    exit_reason: str  # 'STOP_LOSS', 'TAKE_PROFIT', 'TIME_EXIT', 'SIGNAL', 'OPEN'
    pnl: float = 0.0
    pnl_pct: float = 0.0
    commission: float = 0.0

    def close(self, exit_date: str, exit_price: float, reason: str, commission: float):
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.exit_reason = reason
        self.commission = commission
        self.pnl = (exit_price - self.entry_price) * self.shares * self.direction - commission
        self.pnl_pct = (exit_price - self.entry_price) / self.entry_price * self.direction


@dataclass
class BacktestResult:
    strategy_name: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    win_rate_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    total_trades: int
    trades: List[Trade]
    equity_curve: pd.Series
    daily_returns: pd.Series
    benchmark_return_pct: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0

    def summary(self) -> dict:
        return {
            "Strategy": self.strategy_name,
            "Period": f"{self.start_date} → {self.end_date}",
            "Initial Capital": f"${self.initial_capital:,.0f}",
            "Final Capital": f"${self.final_capital:,.0f}",
            "Total Return": f"{self.total_return_pct:+.2f}%",
            "Ann. Return": f"{self.annualized_return_pct:+.2f}%",
            "Sharpe Ratio": f"{self.sharpe_ratio:.3f}",
            "Sortino Ratio": f"{self.sortino_ratio:.3f}",
            "Calmar Ratio": f"{self.calmar_ratio:.3f}",
            "Max Drawdown": f"{self.max_drawdown_pct:.2f}%",
            "Max DD Duration": f"{self.max_drawdown_duration_days} days",
            "Win Rate": f"{self.win_rate_pct:.1f}%",
            "Avg Win": f"{self.avg_win_pct:+.2f}%",
            "Avg Loss": f"{self.avg_loss_pct:+.2f}%",
            "Profit Factor": f"{self.profit_factor:.2f}",
            "Total Trades": self.total_trades,
            "Benchmark Return": f"{self.benchmark_return_pct:+.2f}%",
            "Alpha (vs Bench)": f"{self.alpha:+.2f}%",
            "Beta": f"{self.beta:.3f}",
        }


class BacktestEngine:
    """
    Portfolio-level backtesting engine.

    Architecture:
    - Processes one day at a time
    - Manages open positions with stop-loss/take-profit
    - Tracks portfolio equity curve
    - Computes full performance analytics
    """

    def __init__(
        self,
        initial_capital: float = None,
        commission: float = None,
        max_position_pct: float = None,
        stop_loss_pct: float = None,
        take_profit_pct: float = None,
        benchmark: str = None,
    ):
        self.initial_capital = initial_capital or BACKTEST_CONFIG["initial_capital"]
        self.commission = commission or BACKTEST_CONFIG["commission_per_trade"]
        self.max_position_pct = max_position_pct or BACKTEST_CONFIG["max_position_size_pct"]
        self.stop_loss_pct = stop_loss_pct or BACKTEST_CONFIG["stop_loss_pct"]
        self.take_profit_pct = take_profit_pct or BACKTEST_CONFIG["take_profit_pct"]
        self.benchmark = benchmark or BACKTEST_CONFIG["benchmark"]

    def run(
        self,
        strategy: BaseStrategy,
        prices: pd.DataFrame,
        signals: Optional[pd.DataFrame] = None,
        benchmark_prices: Optional[pd.Series] = None,
    ) -> BacktestResult:
        """
        Run backtest for a strategy.

        Args:
            strategy: Strategy instance
            prices: OHLCV or close prices DataFrame
            signals: Pre-computed signals (optional — if None, will call strategy)
            benchmark_prices: Benchmark price series for alpha/beta calculation

        Returns:
            BacktestResult with full metrics
        """
        logger.info(f"Running backtest: {strategy.name}")

        if signals is None:
            signals = strategy.generate_signals(prices)

        capital = self.initial_capital
        portfolio_value = capital
        open_positions: Dict[str, Trade] = {}
        closed_trades: List[Trade] = []
        equity_curve = []
        daily_returns = []

        dates = prices.index

        for day_idx, date in enumerate(dates):
            date_str = str(date)[:10]

            # ── Check open positions for exits ────────────────────────────
            for ticker in list(open_positions.keys()):
                if ticker not in prices.columns:
                    continue
                trade = open_positions[ticker]
                current_price = prices.loc[date, ticker]
                if pd.isna(current_price):
                    continue

                price_change = (current_price - trade.entry_price) / trade.entry_price

                exit_reason = None
                if price_change <= -self.stop_loss_pct:
                    exit_reason = "STOP_LOSS"
                elif price_change >= self.take_profit_pct:
                    exit_reason = "TAKE_PROFIT"
                elif (
                    ticker in signals.columns
                    and date in signals.index
                    and signals.loc[date, ticker] == -1
                ):
                    exit_reason = "SIGNAL"

                if exit_reason:
                    comm = trade.shares * current_price * self.commission
                    trade.close(date_str, current_price, exit_reason, comm)
                    capital += trade.shares * current_price - comm
                    closed_trades.append(trade)
                    del open_positions[ticker]

            # ── Enter new positions ────────────────────────────────────────
            if date in signals.index:
                day_signals = signals.loc[date]
                buy_tickers = day_signals[day_signals == 1].index.tolist()

                for ticker in buy_tickers:
                    if ticker in open_positions:
                        continue  # Already in position
                    if ticker not in prices.columns:
                        continue

                    price = prices.loc[date, ticker]
                    if pd.isna(price) or price <= 0:
                        continue

                    # Position sizing
                    max_dollars = portfolio_value * self.max_position_pct
                    dollars = min(max_dollars, capital * 0.95)
                    if dollars < 1000:
                        continue

                    shares = dollars / price
                    comm = shares * price * self.commission
                    cost = shares * price + comm

                    if cost > capital:
                        continue

                    capital -= cost
                    open_positions[ticker] = Trade(
                        ticker=ticker,
                        entry_date=date_str,
                        exit_date=None,
                        entry_price=price,
                        exit_price=None,
                        shares=shares,
                        direction=1,
                        exit_reason="OPEN",
                        commission=comm,
                    )

            # ── Compute portfolio value ────────────────────────────────────
            open_value = sum(
                open_positions[t].shares * prices.loc[date, t]
                for t in open_positions
                if t in prices.columns and not pd.isna(prices.loc[date, t])
            )
            portfolio_value = capital + open_value
            equity_curve.append(portfolio_value)
            if len(equity_curve) > 1:
                daily_returns.append(
                    (portfolio_value - equity_curve[-2]) / equity_curve[-2]
                )
            else:
                daily_returns.append(0.0)

        # ── Close all remaining open positions at last price ───────────────
        last_date = str(dates[-1])[:10]
        for ticker, trade in open_positions.items():
            if ticker in prices.columns:
                last_price = prices[ticker].iloc[-1]
                if not pd.isna(last_price):
                    comm = trade.shares * last_price * self.commission
                    trade.close(last_date, last_price, "TIME_EXIT", comm)
                    closed_trades.append(trade)

        # ── Build results ──────────────────────────────────────────────────
        equity = pd.Series(equity_curve, index=dates[:len(equity_curve)])
        returns = pd.Series(daily_returns, index=dates[:len(daily_returns)])

        result = self._compute_metrics(
            strategy_name=strategy.name,
            start_date=str(dates[0])[:10],
            end_date=str(dates[-1])[:10],
            equity_curve=equity,
            daily_returns=returns,
            trades=closed_trades,
            benchmark_prices=benchmark_prices,
        )

        logger.info(
            f"Backtest complete: {strategy.name} | "
            f"Return={result.total_return_pct:+.2f}% | "
            f"Sharpe={result.sharpe_ratio:.3f} | "
            f"MaxDD={result.max_drawdown_pct:.2f}%"
        )

        return result

    def _compute_metrics(
        self,
        strategy_name: str,
        start_date: str,
        end_date: str,
        equity_curve: pd.Series,
        daily_returns: pd.Series,
        trades: List[Trade],
        benchmark_prices: Optional[pd.Series] = None,
    ) -> BacktestResult:
        """Compute all performance metrics from equity curve and trades."""

        initial = self.initial_capital
        final = float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else initial

        total_return = (final - initial) / initial * 100
        n_years = len(equity_curve) / TRADING_DAYS_PER_YEAR
        ann_return = ((final / initial) ** (1 / max(n_years, 0.01)) - 1) * 100

        # Risk metrics
        clean_returns = daily_returns.replace([np.inf, -np.inf], 0).fillna(0)
        rf = 0.05 / TRADING_DAYS_PER_YEAR  # Daily risk-free rate (~5% annual)

        excess = clean_returns - rf
        sharpe = (
            (excess.mean() / excess.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
            if excess.std() > 0 else 0.0
        )

        downside = clean_returns[clean_returns < 0]
        sortino = (
            (excess.mean() / downside.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
            if len(downside) > 0 and downside.std() > 0 else 0.0
        )

        # Drawdown
        rolling_max = equity_curve.cummax()
        drawdown = (equity_curve - rolling_max) / rolling_max * 100
        max_dd = float(drawdown.min())

        # Drawdown duration
        in_dd = drawdown < 0
        dd_duration = 0
        max_dd_duration = 0
        for v in in_dd:
            if v:
                dd_duration += 1
                max_dd_duration = max(max_dd_duration, dd_duration)
            else:
                dd_duration = 0

        calmar = ann_return / abs(max_dd) if max_dd != 0 else 0.0

        # Trade metrics
        won = [t for t in trades if t.pnl > 0]
        lost = [t for t in trades if t.pnl <= 0]
        win_rate = len(won) / len(trades) * 100 if trades else 0.0
        avg_win = np.mean([t.pnl_pct for t in won]) * 100 if won else 0.0
        avg_loss = np.mean([t.pnl_pct for t in lost]) * 100 if lost else 0.0
        total_wins = sum(t.pnl for t in won)
        total_losses = abs(sum(t.pnl for t in lost))
        profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

        # Benchmark comparison
        bench_return = 0.0
        alpha = 0.0
        beta = 0.0
        if benchmark_prices is not None and len(benchmark_prices) > 1:
            bench_return = (
                benchmark_prices.iloc[-1] / benchmark_prices.iloc[0] - 1
            ) * 100
            bench_daily = benchmark_prices.pct_change().fillna(0)
            # Align indices
            aligned = pd.concat(
                [clean_returns, bench_daily], axis=1, join="inner"
            ).dropna()
            if len(aligned) > 10:
                cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])
                beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else 0.0
                alpha = ann_return - (beta * bench_return)

        return BacktestResult(
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial,
            final_capital=final,
            total_return_pct=total_return,
            annualized_return_pct=ann_return,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_dd,
            max_drawdown_duration_days=max_dd_duration,
            win_rate_pct=win_rate,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            profit_factor=profit_factor,
            total_trades=len(trades),
            trades=trades,
            equity_curve=equity_curve,
            daily_returns=clean_returns,
            benchmark_return_pct=bench_return,
            alpha=alpha,
            beta=beta,
        )

    def run_walk_forward(
        self,
        strategy: BaseStrategy,
        prices: pd.DataFrame,
        train_window: int = 504,   # 2 years
        test_window: int = 126,    # 6 months
        benchmark_prices: Optional[pd.Series] = None,
    ) -> List[BacktestResult]:
        """
        Walk-forward validation: train on in-sample, test on out-of-sample.
        Prevents overfitting — critical for any real deployment.
        """
        results = []
        start = 0

        while start + train_window + test_window <= len(prices):
            train_prices = prices.iloc[start : start + train_window]
            test_prices = prices.iloc[
                start + train_window : start + train_window + test_window
            ]

            # Generate signals on train data, apply to test data
            train_signals = strategy.generate_signals(train_prices)

            # Extend signals to test period using the last signal state
            test_signals = pd.DataFrame(
                0, index=test_prices.index, columns=test_prices.columns
            )

            if not train_signals.empty:
                last_signal = train_signals.iloc[-1]
                for col in test_signals.columns:
                    if col in last_signal.index:
                        test_signals[col] = last_signal[col]

            bench_slice = None
            if benchmark_prices is not None:
                bench_slice = benchmark_prices.loc[
                    benchmark_prices.index.isin(test_prices.index)
                ]

            result = self.run(strategy, test_prices, test_signals, bench_slice)
            result.start_date = str(test_prices.index[0])[:10]
            result.end_date = str(test_prices.index[-1])[:10]
            results.append(result)

            start += test_window

        return results
