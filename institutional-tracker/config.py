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
MAX_POSITION_SIZE_PCT  = 0.07         # Max 7% of portfolio per position (board directive: reduced from 12% — concentration risk)
MIN_INSTITUTIONS_SIGNAL = 2           # Minimum institutions buying same stock to trigger buy
STOP_LOSS_PCT          = 0.07         # 7% stop loss per position
TAKE_PROFIT_PCT        = 0.20         # 20% take profit per position
MAX_OPEN_POSITIONS     = 15           # Max simultaneous open positions
HOLD_DAYS_MAX          = 60           # Max days to hold a position before forced exit

# --- CAPITAL DEPLOYMENT POLICY ---
# Board directive: regime-adaptive deployment targets (approved 2026-05-06).
# In CRITICAL/HIGH regimes the firm must not be fully deployed — cash is a position.
MIN_DEPLOYMENT_PCT          = 0.30    # Alert if less than 30% deployed (was 50%)
TARGET_DEPLOYMENT_PCT       = 0.70    # Target in GREEN/NEUTRAL regime
TARGET_DEPLOYMENT_CRITICAL  = 0.20   # Target in CRITICAL regime — preserve capital
TARGET_DEPLOYMENT_HIGH      = 0.40   # Target in HIGH regime
TARGET_DEPLOYMENT_ELEVATED  = 0.55   # Target in ELEVATED regime

# --- REGIME-ADAPTIVE STRATEGY (board-approved 2026-05-06) ---
# CRO risk level → strategy mandate shift.
# The R&D Team Leader reads REGIME_MANDATE at assignment time to shift sector
# scoring weights. Manager Agent reads REGIME_MAX_POSITION to enforce per-position limit.
REGIME_MANDATES = {
    "GREEN":    {"mandate": "MOMENTUM",   "max_position_pct": 0.07, "deploy_target": TARGET_DEPLOYMENT_PCT},
    "ELEVATED": {"mandate": "MOMENTUM",   "max_position_pct": 0.06, "deploy_target": 0.55},
    "HIGH":     {"mandate": "DEFENSIVE",  "max_position_pct": 0.05, "deploy_target": 0.40},
    "CRITICAL": {"mandate": "PRESERVE",   "max_position_pct": 0.04, "deploy_target": 0.20},
    "UNKNOWN":  {"mandate": "MOMENTUM",   "max_position_pct": 0.07, "deploy_target": TARGET_DEPLOYMENT_PCT},
}

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
HEDGE_POSITION_PCT     = 0.08    # 8% of portfolio into SH (board directive: reduced from 20% — oversized for $100k portfolio)
MAX_SHORT_POSITIONS    = 3       # Max individual momentum shorts at once
SHORT_POSITION_SIZE_PCT = 0.05   # 5% of portfolio per momentum short

# Momentum short candidates — stocks in our universe most likely to underperform
# Selected for: losing market share, below 50MA + 200MA, earnings misses
# Expanded (CIT Think Tank proposal 2026-06-09): wider short book for HIGH/CRITICAL regimes
SHORT_CANDIDATES = [
    "INTC",   # Intel — catastrophic AI chip market share loss to NVDA/AMD
    "DIS",    # Disney — streaming losses, theme park softness
    "BA",     # Boeing — production crises, safety scandals
    "PFE",    # Pfizer — post-COVID revenue cliff, pipeline disappointments
    "CSCO",   # Cisco — hardware commoditization, slowing enterprise spend
    # Extended short book — activated in HIGH/CRITICAL regime only
    "MO",     # Altria — long-run secular decline, FDA pressure
    "INTC",   # Intel (already above) — AI era loser
    "VFC",    # VF Corp — brand portfolio restructuring, dividend cut
    "KHC",    # Kraft Heinz — CPG secular decline, private label pressure
]
SHORT_CANDIDATES = list(dict.fromkeys(SHORT_CANDIDATES))  # deduplicate

# Short book policy (CIT Think Tank 2026-06-09):
# In CRO HIGH regime: up to MAX_SHORT_POSITIONS single-name shorts
# In CRO CRITICAL regime: up to MAX_SHORT_POSITIONS + 2 single-name shorts
# In CRO MEDIUM or below: only SH ETF hedge (no single-name shorts)
SHORT_BOOK_ENABLED          = True
SHORT_BOOK_CRO_MIN          = "HIGH"   # minimum CRO level to activate short book
SHORT_BOOK_CRITICAL_BONUS   = 2        # extra short slots in CRITICAL vs HIGH

# --- CRON HEARTBEAT (healthchecks.io) ---
# Ping this URL at the end of each successful main.py run.
# If the scheduler dies silently, healthchecks.io sends an alert to Jack.
# Set up free at https://healthchecks.io — paste your ping URL here.
# Set to "" or None to disable.
HEALTHCHECK_PING_URL = ""   # e.g. "https://hc-ping.com/your-uuid-here"

# --- STRUCTURAL GUARDRAILS ---
# Conservative hard limits that agents cannot override. Give agents room to
# operate normally but prevent runaway losses at the portfolio level.
DAILY_DRAWDOWN_HALT_PCT  = 0.02   # Halt new buys if portfolio drops 2% in one day
HWM_DRAWDOWN_HALT_PCT    = 0.12   # Halt new buys if 12% below portfolio all-time peak
MIN_CASH_FLOOR_PCT       = 0.20   # Always keep at least 20% of portfolio in cash

# --- BACKTEST SETTINGS ---
BACKTEST_START_DATE = "2010-01-01"
BACKTEST_END_DATE   = "2024-12-31"

# --- REPORT SETTINGS ---
PAPER_TRADING_DAYS = 30
DAILY_REPORT_HOUR  = 16   # 4:00 PM ET — after market close
DAILY_REPORT_MIN   = 15   # 4:15 PM ET

# --- ONE-DIGEST MAIL POLICY (2026-07-21) ---
# Install the SMTP guard so EVERY script that sends mail (even ones with their
# own smtplib connection, even VM-only ones) routes non-urgent mail into the
# single daily digest. This is the real chokepoint; see mail_guard.py header.
try:
    import mail_guard as _mail_guard
    _mail_guard.install()
except Exception as _mge:
    import logging as _lg
    _lg.getLogger("config").warning(f"mail_guard not installed: {_mge}")
