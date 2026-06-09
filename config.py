"""
Central configuration for the Institutional Block Tracker system.
Research Team / Development Team joint ownership.
"""

# ─── Organizational Metadata ─────────────────────────────────────────────────
ORG = {
    "company": "Apex Capital Intelligence",
    "chairman": "Jack Beard",
    "president": "Claude (AI President)",
    "teams": [
        "Development",
        "Research",
        "Quantitative Analysis",
        "Design",
        "Data Engineering",
        "Risk Management",
    ],
}

# ─── Target Institutions (CIK numbers from SEC EDGAR) ────────────────────────
# These are the "smart money" firms we track.
INSTITUTIONS = {
    "Citadel Advisors":       "1423298",
    "Renaissance Technologies": "1037389",
    "Two Sigma Investments":  "1450144",
    "Bridgewater Associates": "1350694",
    "Millennium Management":  "1273931",
    "D.E. Shaw":              "1009207",
    "Point72 Asset Management": "1841201",
    "AQR Capital Management": "1167557",
}

# ─── Block Detection Parameters (Research Team) ──────────────────────────────
BLOCK_CONFIG = {
    # Minimum shares to qualify as a block trade
    "min_block_shares": 10_000,
    # Minimum USD value to qualify as a block trade
    "min_block_value_usd": 200_000,
    # Volume ratio — must exceed X * avg_daily_volume to flag as significant
    "volume_spike_multiplier": 2.0,
    # Rolling window (days) for average volume calculation
    "avg_volume_window": 20,
    # Consecutive days of accumulation to confirm a pattern
    "accumulation_days": 5,
    # Price impact threshold: flag if large volume occurs with minimal price move
    # (signature of institutional stealth buying)
    "stealth_buy_max_price_change_pct": 0.5,
}

# ─── 13F Filing Parameters ────────────────────────────────────────────────────
SEC_CONFIG = {
    "base_url": "https://data.sec.gov",
    "submissions_url": "https://data.sec.gov/submissions/CIK{cik}.json",
    "user_agent": "Apex Capital Intelligence research@apexcapital.ai",
    # Minimum position change (shares) to flag as new/increased block
    "min_position_change": 50_000,
    # Minimum NEW position size to flag
    "min_new_position": 100_000,
    # Filing types to pull
    "target_filings": ["13F-HR", "13F-HR/A"],
}

# ─── Backtesting Parameters (Quant Team) ─────────────────────────────────────
BACKTEST_CONFIG = {
    "start_date": "2018-01-01",
    "end_date": "2024-12-31",
    "initial_capital": 1_000_000,      # $1M starting capital
    "commission_per_trade": 0.001,     # 0.1% per trade (slippage + commission)
    "max_position_size_pct": 0.10,     # Max 10% of portfolio in one position
    "stop_loss_pct": 0.08,             # 8% stop loss
    "take_profit_pct": 0.25,           # 25% take profit
    "rebalance_frequency": "weekly",   # How often to rebalance
    "benchmark": "SPY",                # Benchmark for alpha calculation
}

# ─── Strategy Parameters ─────────────────────────────────────────────────────
STRATEGY_CONFIG = {
    "institutional_follow": {
        "enabled": True,
        "signal_lag_days": 3,          # Days after 13F filing to enter
        "holding_period_days": 90,     # Days to hold the position
        "min_institutions_buying": 2,  # Must have 2+ institutions buying
        "weight": 0.5,                 # Portfolio weight for this strategy
    },
    "momentum": {
        "enabled": True,
        "lookback_days": 252,          # 1-year momentum
        "holding_period_days": 21,
        "top_percentile": 0.2,         # Buy top 20% momentum stocks
        "weight": 0.2,
    },
    "mean_reversion": {
        "enabled": True,
        "lookback_days": 20,
        "z_score_entry": -2.0,         # Enter when Z-score < -2
        "z_score_exit": 0.0,           # Exit at mean
        "weight": 0.15,
    },
    "value": {
        "enabled": True,
        "pe_max": 20,                  # Max P/E ratio
        "pb_max": 3,                   # Max P/B ratio
        "holding_period_days": 180,
        "weight": 0.15,
    },
}

# ─── Risk Management Parameters (Risk Team) ──────────────────────────────────
RISK_CONFIG = {
    "max_portfolio_drawdown_pct": 0.20,    # Halt trading if DD > 20%
    "max_sector_exposure_pct": 0.30,       # Max 30% in one sector
    "max_single_stock_pct": 0.10,          # Max 10% in one stock
    "var_confidence": 0.95,                # 95% VaR
    "var_lookback_days": 252,
    "position_sizing_method": "kelly",     # 'kelly', 'equal_weight', 'risk_parity'
    "kelly_fraction": 0.25,                # Use 25% Kelly (conservative)
}

# ─── Reporting (Design Team) ──────────────────────────────────────────────────
REPORT_CONFIG = {
    "output_dir": "reports",
    "chart_style": "dark_background",
    "color_primary": "#00FF88",
    "color_secondary": "#FF4444",
    "color_neutral": "#AAAAAA",
}

# ─── Universe of Stocks to Monitor ───────────────────────────────────────────
# Default watchlist — institutions tend to hold large-cap US equities
STOCK_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "MA", "LLY", "AVGO", "CVX",
    "HD", "MRK", "PEP", "ABBV", "KO", "PFE", "COST", "TMO", "DHR", "MCD",
    "CSCO", "ACN", "WFC", "ABT", "DIS", "BAC", "ADBE", "NEE", "CRM", "PM",
    "TXN", "NFLX", "QCOM", "VZ", "RTX", "INTC", "AMD", "AMGN", "HON", "GE",
    "CAT", "SBUX", "GS", "MS", "BLK", "SPGI", "AXP", "PLD", "C", "USB",
]
