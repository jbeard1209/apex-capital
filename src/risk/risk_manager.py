"""
Risk Manager — Risk Management Team
Monitors and enforces portfolio risk limits.
Capital preservation is the first priority.
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

import numpy as np
import pandas as pd

from config import RISK_CONFIG

logger = logging.getLogger(__name__)


class RiskBreach:
    def __init__(self, breach_type: str, severity: str, message: str, action: str):
        self.breach_type = breach_type
        self.severity = severity      # 'CRITICAL', 'WARNING', 'INFO'
        self.message = message
        self.action = action          # 'HALT_TRADING', 'REDUCE_EXPOSURE', 'ALERT'
        self.timestamp = datetime.now().isoformat()


class RiskManager:
    """
    Real-time risk monitoring engine.
    All trades must pass risk checks before execution.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or RISK_CONFIG
        self.breaches: List[RiskBreach] = []
        self.trading_halted = False

    def check_portfolio_drawdown(
        self,
        equity_curve: pd.Series,
        current_value: float,
    ) -> Optional[RiskBreach]:
        """Halt trading if portfolio drawdown exceeds limit."""
        if equity_curve.empty:
            return None

        peak = equity_curve.max()
        drawdown = (current_value - peak) / peak

        limit = self.config["max_portfolio_drawdown_pct"]
        if abs(drawdown) >= limit:
            breach = RiskBreach(
                breach_type="MAX_DRAWDOWN",
                severity="CRITICAL",
                message=f"Portfolio drawdown {drawdown:.1%} exceeds limit {limit:.0%}",
                action="HALT_TRADING",
            )
            self.trading_halted = True
            self.breaches.append(breach)
            logger.critical(f"RISK BREACH: {breach.message}")
            return breach
        return None

    def check_position_size(
        self,
        ticker: str,
        position_value: float,
        portfolio_value: float,
    ) -> Optional[RiskBreach]:
        """Enforce single-position concentration limit."""
        pct = position_value / portfolio_value
        limit = self.config["max_single_stock_pct"]

        if pct > limit:
            breach = RiskBreach(
                breach_type="POSITION_CONCENTRATION",
                severity="WARNING",
                message=f"{ticker} position {pct:.1%} exceeds limit {limit:.0%}",
                action="REDUCE_EXPOSURE",
            )
            self.breaches.append(breach)
            logger.warning(f"RISK WARNING: {breach.message}")
            return breach
        return None

    def check_sector_exposure(
        self,
        sector_exposures: Dict[str, float],
        portfolio_value: float,
    ) -> List[RiskBreach]:
        """Check sector concentration limits."""
        breaches = []
        limit = self.config["max_sector_exposure_pct"]

        for sector, exposure in sector_exposures.items():
            pct = exposure / portfolio_value
            if pct > limit:
                breach = RiskBreach(
                    breach_type="SECTOR_CONCENTRATION",
                    severity="WARNING",
                    message=f"Sector '{sector}' exposure {pct:.1%} exceeds limit {limit:.0%}",
                    action="REDUCE_EXPOSURE",
                )
                breaches.append(breach)
                self.breaches.append(breach)
                logger.warning(f"RISK WARNING: {breach.message}")

        return breaches

    def compute_var(
        self,
        daily_returns: pd.Series,
        portfolio_value: float,
        confidence: float = None,
    ) -> dict:
        """
        Compute Value at Risk (VaR) and Conditional VaR (CVaR).
        Historical simulation method.
        """
        conf = confidence or self.config["var_confidence"]
        lookback = self.config["var_lookback_days"]

        returns = daily_returns.dropna().tail(lookback)
        if len(returns) < 10:
            return {"var": 0, "cvar": 0, "confidence": conf}

        var_pct = np.percentile(returns, (1 - conf) * 100)
        cvar_pct = returns[returns <= var_pct].mean()

        return {
            "var_pct": var_pct,
            "var_dollar": var_pct * portfolio_value,
            "cvar_pct": cvar_pct,
            "cvar_dollar": cvar_pct * portfolio_value,
            "confidence": conf,
            "1_day_var_interpretation": (
                f"With {conf:.0%} confidence, max 1-day loss = "
                f"${abs(var_pct * portfolio_value):,.0f} ({abs(var_pct):.1%})"
            ),
        }

    def kelly_position_size(
        self,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
        portfolio_value: float,
        kelly_fraction: float = None,
    ) -> float:
        """
        Compute Kelly Criterion position size.
        Uses fractional Kelly (more conservative) for real-world application.

        Kelly formula: f* = (p * b - q) / b
        where: p = win probability, q = 1-p, b = win/loss ratio
        """
        f = kelly_fraction or self.config["kelly_fraction"]

        if avg_loss_pct == 0:
            return portfolio_value * self.config["max_single_stock_pct"]

        b = abs(avg_win_pct / avg_loss_pct)  # Win/loss ratio
        p = win_rate / 100
        q = 1 - p

        kelly = (p * b - q) / b
        fractional_kelly = kelly * f

        # Cap at max position size
        max_size = portfolio_value * self.config["max_single_stock_pct"]
        position_size = min(
            portfolio_value * max(0, fractional_kelly),
            max_size,
        )

        return position_size

    def get_risk_report(
        self,
        portfolio_value: float,
        positions: Dict[str, float],
        daily_returns: pd.Series,
    ) -> dict:
        """Generate a risk status report."""
        var_data = self.compute_var(daily_returns, portfolio_value)

        sector_exposures: Dict[str, float] = {}  # Would populate from fundamentals data

        return {
            "timestamp": datetime.now().isoformat(),
            "portfolio_value": portfolio_value,
            "trading_halted": self.trading_halted,
            "active_breaches": len([b for b in self.breaches if b.severity == "CRITICAL"]),
            "var_95": var_data,
            "position_count": len(positions),
            "largest_position": max(positions.values()) if positions else 0,
            "largest_position_pct": (
                max(positions.values()) / portfolio_value if positions and portfolio_value > 0 else 0
            ),
            "recent_breaches": [
                {"type": b.breach_type, "severity": b.severity, "msg": b.message}
                for b in self.breaches[-5:]
            ],
        }

    def is_trade_allowed(self) -> bool:
        """Quick check: is trading currently allowed?"""
        return not self.trading_halted

    def reset_halt(self):
        """Manual override to resume trading (requires Risk Officer approval)."""
        logger.warning("Trading halt manually reset by Risk Officer")
        self.trading_halted = False
