# =============================================================================
# INSTITUTIONAL TRACKER — CONFIGURATION
# =============================================================================
# Credentials are loaded from .env (via python-dotenv).
# .env is gitignored — never hardcode secrets here.
#
# To set up:
#   pip install python-dotenv
#   cp .env.example .env   (or create .env manually)
#   Fill in your credentials in .env
#
# The hardcoded fallbacks below are kept only so the system still runs if
# python-dotenv is not installed.  They should be removed in production.
# =============================================================================

import os as _os

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=_os.path.join(_os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    pass   # python-dotenv not installed — fall back to hardcoded values below

# --- ALPACA PAPER TRADING ---
ALPACA_API_KEY    = _os.getenv("ALPACA_API_KEY",    "PKMJMECIKK235BQTNKGCCSKEL3")
ALPACA_SECRET_KEY = _os.getenv("ALPACA_SECRET_KEY", "7uxURY6amjpDaQkVcoD2tXrcMsrpUHntjut95QwHZbR8")
ALPACA_BASE_URL   = _os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")

# --- EMAIL (Gmail App Password) ---
EMAIL_SENDER       = _os.getenv("EMAIL_SENDER",       "jack.beard07@gmail.com")
EMAIL_RECIPIENT    = _os.getenv("EMAIL_RECIPIENT",    "jack.beard07@gmail.com")
GMAIL_APP_PASSWORD = _os.getenv("GMAIL_APP_PASSWORD", "vaje mgnh jhtm kknf")

# --- TRADING PARAMETERS ---
STARTING_CAPITAL       = 100000.00    # USD — paper trading starting balance
MAX_POSITION_SIZE_PCT  = 0.12         # Max 12% of portfolio per position (up from 10%)
MIN_INSTITUTIONS_SIGNAL = 2           # Minimum institutions buying same stock to trigger buy
STOP_LOSS_PCT          = 0.07         # 7% stop loss per position
TAKE_PROFIT_PCT        = 0.20         # 20% take profit per position
MAX_OPEN_POSITIONS     = 15           # Max simultaneous open positions (up from 8)
HOLD_DAYS_MAX          = 60           # Max days to hold a position before forced exit

# --- CAPITAL DEPLOYMENT POLICY ---
# Board directive: target keeping at least 50% of portfolio deployed at all times.
# The PDO and board meeting will issue DEPLOY_MORE signals when below this threshold.
MIN_DEPLOYMENT_PCT     = 0.50         # Alert if less than 50% of portfolio is invested
TARGET_DEPLOYMENT_PCT  = 0.75         # Target 75% deployment in normal market conditions

# --- TRACKED INSTITUTIONS (SEC CIK numbers) ---
TRACKED_INSTITUTIONS = {
    "Citadel":              "0001423053",
    "Renaissance Tech":     "0001037389",
    "Two Sigma":            "0001649339",
    "Millennium Mgmt":      "0001273087",
    "Point72":              "0001603466",
    "D.E. Shaw":            "0001009207",
    "AQR Capital":          "0001167557",
    "Bridgewater":          "0001350694",
}

# --- UNIVERSE OF STOCKS TO MONITOR ---
# Large-cap stocks most likely to appear in institutional 13F filings
STOCK_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK.B",
    "JPM",  "V",    "JNJ",   "XOM",  "UNH",  "MA",   "HD",   "PG",
    "BAC",  "AVGO", "MRK",   "CVX",  "LLY",  "ABBV", "COST", "PEP",
    "KO",   "TMO",  "CSCO",  "MCD",  "WMT",  "DIS",  "CRM",  "ADBE",
    "NFLX", "AMD",  "INTC",  "QCOM", "TXN",  "HON",  "UPS",  "CAT",
    "GS",   "MS",   "BLK",   "SPGI", "CB",   "MMC",  "AXP",  "C",
]

# --- LONG-SHORT STRATEGY SETTINGS ---
HEDGE_ETF              = "SH"    # ProShares Short S&P500 — bought when SPY < 200MA
HEDGE_POSITION_PCT     = 0.20    # 20% of portfolio into SH during bear regime
MAX_SHORT_POSITIONS    = 3       # Max individual momentum shorts at once
SHORT_POSITION_SIZE_PCT = 0.05   # 5% of portfolio per momentum short

# Momentum short candidates — stocks in our universe most likely to underperform
# Selected for: losing market share, below 50MA + 200MA, earnings misses
SHORT_CANDIDATES = [
    "INTC",   # Intel — catastrophic AI chip market share loss to NVDA/AMD
    "DIS",    # Disney — streaming losses, theme park softness
    "BA",     # Boeing — production crises, safety scandals
    "PFE",    # Pfizer — post-COVID revenue cliff, pipeline disappointments
    "CSCO",   # Cisco — hardware commoditization, slowing enterprise spend
]

# --- BACKTEST SETTINGS ---
BACKTEST_START_DATE = "2010-01-01"
BACKTEST_END_DATE   = "2024-12-31"

# --- REPORT SETTINGS ---
PAPER_TRADING_DAYS = 30
DAILY_REPORT_HOUR  = 16   # 4:00 PM ET — after market close
DAILY_REPORT_MIN   = 15   # 4:15 PM ET
