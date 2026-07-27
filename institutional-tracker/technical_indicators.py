#!/usr/bin/env python3
"""
Shared technical indicators
===========================
One correct implementation of indicators that were previously copy-pasted (with a
bug) across research_team, rd_team_leader, rd_historical_analyst, review_agent and
energy_research_team. The duplicated RSI block computed `rs = gain / loss.replace(0,
nan)`, so any window with zero down-days produced RSI = NaN, which got serialized as
the invalid JSON token `NaN` and corrupted downstream files (data/rd_ml_history.json,
manager_decisions.json). Centralising the logic kills the bug and stops it recurring.
"""
from __future__ import annotations


def rsi_last(close, window: int = 14, default: float = 50.0) -> float:
    """Wilder-style RSI of the most recent bar, guaranteed FINITE.

    Convention (this is where the old code went wrong):
      • zero average loss  -> 100.0  (only gains in the window = max overbought)
      • zero average gain  -> 0.0    (only losses = max oversold)
      • insufficient/flat data -> `default` (neutral 50)
    Never returns NaN/inf, so callers can store the value in JSON safely.
    """
    try:
        import pandas as pd
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(window).mean()
        loss = (-delta.clip(upper=0)).rolling(window).mean()
        g = gain.iloc[-1]
        l = loss.iloc[-1]
        g = 0.0 if pd.isna(g) else float(g)
        l = 0.0 if pd.isna(l) else float(l)
        if g == 0 and l == 0:
            return float(default)
        if l == 0:
            return 100.0
        if g == 0:
            return 0.0
        return float(100 - 100 / (1 + g / l))
    except Exception:
        return float(default)
