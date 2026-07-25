"""
portfolio_risk_dashboard.py -- Daily pre-market risk email for all Apex Capital lanes.
Crontab: 0 7 * * * cd /home/jackbeard/institutional-tracker && python3 portfolio_risk_dashboard.py >> logs/portfolio_risk_dashboard.log 2>&1
"""

import datetime
import smtplib
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import yfinance as yf
except ImportError:
    yf = None

sys.path.insert(0, os.path.dirname(__file__))
from portfolio_tracker import PortfolioTracker
import config

# ------------------------------------------------------------------
# Lane config
# ------------------------------------------------------------------
LANES = {
    "institutional_tracker": {"capital": config.STARTING_CAPITAL,         "label": "Institutional",  "drawdown_thresh": -0.10},
    "ml_shortterm":          {"capital": config.ML_SHORTTERM_CAPITAL,      "label": "ML Short-term",  "drawdown_thresh": -0.10},
    "ml_longterm":           {"capital": config.ML_LONGTERM_CAPITAL,       "label": "ML Long-term",   "drawdown_thresh": -0.10},
    "first_eagle":           {"capital": config.FIRST_EAGLE_LANE_CAPITAL,  "label": "First Eagle",    "drawdown_thresh": -0.15},
}

CONC_HIGH_THRESH   = 0.20   # >20% single ticker in lane = HIGH
CONC_MEDIUM_THRESH = 0.60   # top-3 >60% = MEDIUM
SECTOR_THRESH      = 0.40   # >40% any sector = flag


# ------------------------------------------------------------------
# Market regime
# ------------------------------------------------------------------

def get_market_regime():
    """Return dict with SPY price, DMAs, VIX, and regime string."""
    result = {
        "spy_price": None, "dma50": None, "dma200": None,
        "vix": None, "regime": "UNKNOWN",
        "spy_wtd": None,
    }
    if yf is None:
        return result
    try:
        spy = yf.download("SPY", period="1y", auto_adjust=True, progress=False)
        if not spy.empty:
            closes = spy["Close"].dropna()
            result["spy_price"] = float(closes.iloc[-1])
            if len(closes) >= 50:
                result["dma50"]  = float(closes.iloc[-50:].mean())
            if len(closes) >= 200:
                result["dma200"] = float(closes.iloc[-200:].mean())
    except Exception:
        pass
    try:
        vix_data = yf.download("^VIX", period="5d", auto_adjust=True, progress=False)
        if not vix_data.empty:
            result["vix"] = float(vix_data["Close"].dropna().iloc[-1])
    except Exception:
        pass

    vix = result["vix"]
    if vix is not None:
        if vix < 15:
            result["regime"] = "CALM"
        elif vix < 20:
            result["regime"] = "NORMAL"
        elif vix <= 30:
            result["regime"] = "ELEVATED"
        else:
            result["regime"] = "CRISIS"
    return result


# ------------------------------------------------------------------
# Risk flag helpers
# ------------------------------------------------------------------

def check_concentration(lane_key, positions, lane_capital):
    """Return list of (level, ticker, reason) flags for concentration."""
    flags = []
    if not positions:
        return flags
    tickers = [p["ticker"] for p in positions]
    prices  = {}
    if yf is not None:
        try:
            data = yf.download(tickers, period="2d", auto_adjust=True, progress=False)
            if not data.empty:
                if len(tickers) == 1:
                    prices[tickers[0]] = float(data["Close"].dropna().iloc[-1])
                else:
                    for t in tickers:
                        try:
                            prices[t] = float(data["Close"][t].dropna().iloc[-1])
                        except Exception:
                            pass
        except Exception:
            pass

    pos_values = []
    for p in positions:
        price = prices.get(p["ticker"])
        value = (price * p["shares"]) if price is not None else p["capital_deployed"]
        pos_values.append((p["ticker"], value))

    total = sum(v for _, v in pos_values) or lane_capital
    sorted_vals = sorted(pos_values, key=lambda x: x[1], reverse=True)

    for ticker, value in sorted_vals:
        pct = value / total
        if pct > CONC_HIGH_THRESH:
            flags.append(("HIGH", ticker,
                          "{} is {:.1f}% of {} lane (>20% limit)".format(ticker, pct * 100, lane_key)))

    if len(sorted_vals) >= 3:
        top3_pct = sum(v for _, v in sorted_vals[:3]) / total
        if top3_pct > CONC_MEDIUM_THRESH:
            top3_tickers = ", ".join(t for t, _ in sorted_vals[:3])
            flags.append(("MEDIUM", top3_tickers,
                          "Top 3 ({}) = {:.1f}% of {} lane (>60%)".format(top3_tickers, top3_pct * 100, lane_key)))
    return flags


def check_sector_exposure(positions):
    """Return list of (level, sector, reason) flags for sector concentration."""
    flags = []
    if not positions or yf is None:
        return flags

    sector_values = {}
    total_value   = 0.0
    for pos in positions:
        try:
            info   = yf.Ticker(pos["ticker"]).info
            sector = info.get("sector", "Unknown")
        except Exception:
            sector = "Unknown"
        val = pos["capital_deployed"]
        sector_values[sector] = sector_values.get(sector, 0.0) + val
        total_value += val

    if total_value == 0:
        return flags
    for sector, val in sector_values.items():
        pct = val / total_value
        if pct > SECTOR_THRESH:
            flags.append(("MEDIUM", sector,
                          "{} sector = {:.1f}% of portfolio (>40%)".format(sector, pct * 100)))
    return flags


def check_drawdowns(positions, drawdown_thresh, lane_label):
    """Return list of (level, ticker, reason) for positions beyond drawdown threshold."""
    flags   = []
    watches = []
    if not positions or yf is None:
        return flags, watches

    tickers = [p["ticker"] for p in positions]
    prices  = {}
    try:
        data = yf.download(tickers, period="2d", auto_adjust=True, progress=False)
        if not data.empty:
            if len(tickers) == 1:
                prices[tickers[0]] = float(data["Close"].dropna().iloc[-1])
            else:
                for t in tickers:
                    try:
                        prices[t] = float(data["Close"][t].dropna().iloc[-1])
                    except Exception:
                        pass
    except Exception:
        pass

    for pos in positions:
        price = prices.get(pos["ticker"])
        if price is None:
            continue
        pct_chg = (price - pos["entry_price"]) / pos["entry_price"]
        if pct_chg < drawdown_thresh:
            flags.append(("HIGH", pos["ticker"],
                          "[{}] {} down {:.1f}% from entry (threshold {:.0f}%)".format(
                              lane_label, pos["ticker"], pct_chg * 100, drawdown_thresh * 100)))
        elif pct_chg < (drawdown_thresh * 0.6):
            watches.append((pos["ticker"], pct_chg * 100,
                            "Approaching drawdown threshold"))
        else:
            # Still show anything >3% move either way as something to watch
            if abs(pct_chg) > 0.03:
                watches.append((pos["ticker"], pct_chg * 100, lane_label))
    return flags, watches


# ------------------------------------------------------------------
# Lane P&L block
# ------------------------------------------------------------------

def lane_pnl_block(lane_key, label, capital, positions, pnl_summary):
    lane_data = pnl_summary.get("by_lane", {}).get(lane_key, {})
    deployed  = lane_data.get("deployed", 0.0)
    pnl       = lane_data.get("total_pnl", 0.0)
    # estimate current value from deployed + unrealized
    cur_value = deployed + lane_data.get("unrealized", 0.0)
    pct       = (pnl / deployed * 100) if deployed else 0.0
    sign      = "+" if pnl >= 0 else ""
    return "[{:<14}]  Deployed: ${:>10,.0f}  Value: ${:>10,.0f}  P&L: ${}{:,.0f} ({}{:.1f}%)".format(
        label, deployed, cur_value, sign, pnl, sign, pct)


# ------------------------------------------------------------------
# Email builder
# ------------------------------------------------------------------

def build_email(date_str, market, pnl_summary, all_flags, watches, lane_pnl_lines, has_positions):
    lines = []
    lines.append("APEX CAPITAL -- DAILY RISK DASHBOARD")
    lines.append("{} | Pre-Market".format(date_str))
    lines.append("")
    lines.append("MARKET REGIME")
    spy   = market["spy_price"]
    d50   = market["dma50"]
    d200  = market["dma200"]
    vix   = market["vix"]
    regime = market["regime"]

    spy_str  = "${:.2f}".format(spy)   if spy  is not None else "N/A"
    d50_str  = "${:.2f}".format(d50)   if d50  is not None else "N/A"
    d200_str = "${:.2f}".format(d200)  if d200 is not None else "N/A"
    vix_str  = "{:.1f}".format(vix)    if vix  is not None else "N/A"

    lines.append("SPY: {} | 50DMA: {} | 200DMA: {}".format(spy_str, d50_str, d200_str))
    lines.append("VIX: {} -- {}".format(vix_str, regime))
    lines.append("")

    if not has_positions:
        lines.append("PORTFOLIO SUMMARY")
        lines.append("No positions currently tracked.")
        lines.append("")
        lines.append("RISK FLAGS")
        lines.append("No positions -- no risk flags.")
        return "\n".join(lines)

    total_deployed = pnl_summary.get("total_deployed", 0.0)
    total_unreal   = pnl_summary.get("total_unrealized", 0.0)
    total_real     = pnl_summary.get("total_realized", 0.0)
    total_value    = total_deployed + total_unreal
    total_pnl      = total_real + total_unreal
    pct_overall    = (total_pnl / total_deployed * 100) if total_deployed else 0.0
    sign           = "+" if total_pnl >= 0 else ""

    lines.append("PORTFOLIO SUMMARY")
    lines.append("Total Deployed:    ${:>12,.0f}".format(total_deployed))
    lines.append("Est. Current Value:${:>12,.0f}".format(total_value))
    lines.append("Overall P&L:       ${}{:>11,.0f} ({}{:.1f}%)".format(sign, total_pnl, sign, pct_overall))
    lines.append("")

    lines.append("LANE BREAKDOWN")
    for line in lane_pnl_lines:
        lines.append(line)
    lines.append("")

    lines.append("RISK FLAGS")
    if all_flags:
        for level, ticker, reason in all_flags:
            lines.append("[{:<6}] {} -- {}".format(level, ticker, reason))
    else:
        lines.append("None")
    lines.append("")

    if watches:
        lines.append("POSITIONS TO WATCH")
        for ticker, pct, note in watches:
            sign2 = "+" if pct >= 0 else ""
            lines.append("{}: {}{:.1f}% from entry -- {}".format(ticker, sign2, pct, note))

    return "\n".join(lines)


# ------------------------------------------------------------------
# Send email
# ------------------------------------------------------------------

def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"]    = config.EMAIL_SENDER
    msg["To"]      = config.EMAIL_RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "us-ascii"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_SENDER, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string())
        print("Email sent: {}".format(subject))
    except Exception as e:
        print("Email send error: {}".format(e))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    today     = datetime.date.today()
    date_str  = today.strftime("%Y-%m-%d")
    print("[portfolio_risk_dashboard] Running {}".format(date_str))

    tracker = PortfolioTracker()
    pnl_summary = tracker.get_pnl_summary()
    all_positions = tracker.get_current_positions()
    has_positions = len(all_positions) > 0

    # Market regime
    market = get_market_regime()
    print("  SPY: {}  VIX: {}  Regime: {}".format(
        market["spy_price"], market["vix"], market["regime"]))

    all_flags = []
    watches   = []
    lane_pnl_lines = []

    for lane_key, lane_cfg in LANES.items():
        label    = lane_cfg["label"]
        capital  = lane_cfg["capital"]
        thresh   = lane_cfg["drawdown_thresh"]
        positions = tracker.get_current_positions(lane=lane_key)

        # Concentration flags
        conc_flags = check_concentration(lane_key, positions, capital)
        all_flags.extend(conc_flags)

        # Drawdown flags
        dd_flags, dd_watches = check_drawdowns(positions, thresh, label)
        all_flags.extend(dd_flags)
        watches.extend(dd_watches)

        # Lane P&L line
        lane_pnl_lines.append(lane_pnl_block(lane_key, label, capital, positions, pnl_summary))

    # Sector exposure across all positions
    sector_flags = check_sector_exposure(all_positions)
    all_flags.extend(sector_flags)

    # Sort: HIGH first
    all_flags.sort(key=lambda x: (0 if x[0] == "HIGH" else 1, x[1]))

    body = build_email(date_str, market, pnl_summary, all_flags, watches, lane_pnl_lines, has_positions)

    subject = "Apex Capital -- Daily Risk Dashboard | {}".format(date_str)
    print(body)
    send_email(subject, body)


if __name__ == "__main__":
    main()
