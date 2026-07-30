"""
performance_tracker.py -- Weekly performance vs benchmarks.
Crontab: 0 17 * * 5 cd /home/jackbeard/institutional-tracker && python3 performance_tracker.py >> logs/performance_tracker.log 2>&1
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
def _lane_capital(name: str) -> float:
    """Per-lane notional, or STARTING_CAPITAL if the Chairman hasn't split it yet.

    2026-07-30: ML_SHORTTERM_CAPITAL / ML_LONGTERM_CAPITAL / FIRST_EAGLE_LANE_CAPITAL
    are referenced here but have never existed in config.py, so this module raised
    AttributeError at IMPORT time — it has never run once, despite the crontab line
    in the docstring above (which is also not installed on either host).

    Note what the "capital" field actually does today: NOTHING reads it. Every return
    below is computed from position values, not from lane notional. So the fallback
    cannot misstate a number — but a silent getattr default would let a lane sit
    permanently unfunded-on-paper without anyone noticing, which is exactly how this
    stayed broken. Fallbacks are recorded and printed in the report instead.
    """
    val = getattr(config, name, None)
    if val is None:
        LANE_CAPITAL_FALLBACKS.append(name)
        return config.STARTING_CAPITAL
    return val


LANE_CAPITAL_FALLBACKS: list = []

LANES = {
    "institutional_tracker": {
        "label":     "Institutional",
        "capital":   config.STARTING_CAPITAL,
        "benchmark": "SPY",
        "bench_label": "SPY",
    },
    "ml_shortterm": {
        "label":     "ML Short-term",
        "capital":   _lane_capital("ML_SHORTTERM_CAPITAL"),
        "benchmark": "QQQ",
        "bench_label": "QQQ",
    },
    "ml_longterm": {
        "label":     "ML Long-term",
        "capital":   _lane_capital("ML_LONGTERM_CAPITAL"),
        "benchmark": "QQQ",
        "bench_label": "QQQ",
    },
    "first_eagle": {
        "label":     "First Eagle",
        "capital":   _lane_capital("FIRST_EAGLE_LANE_CAPITAL"),
        "benchmark": "BRK-B",
        "bench_label": "BRK-B",
    },
}


# ------------------------------------------------------------------
# Price helpers
# ------------------------------------------------------------------

def last_friday():
    """Return the date of the most recent Friday before today (not today even if Friday)."""
    today = datetime.date.today()
    # weekday(): Mon=0 ... Fri=4 ... Sun=6
    days_back = (today.weekday() - 4) % 7
    if days_back == 0:
        days_back = 7
    return today - datetime.timedelta(days=days_back)


def get_price_on_or_before(ticker, target_date):
    """Return the closing price on or just before target_date."""
    if yf is None:
        return None
    try:
        start = (target_date - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        end   = (target_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        data  = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
        if data.empty:
            return None
        return float(data["Close"].dropna().iloc[-1])
    except Exception:
        return None


def get_current_price(ticker):
    if yf is None:
        return None
    try:
        data = yf.download(ticker, period="2d", auto_adjust=True, progress=False)
        if data.empty:
            return None
        return float(data["Close"].dropna().iloc[-1])
    except Exception:
        return None


def benchmark_wtd(ticker):
    """Return WTD return of benchmark as a fraction (e.g. 0.012 = +1.2%)."""
    fri_price = get_price_on_or_before(ticker, last_friday())
    cur_price = get_current_price(ticker)
    if fri_price is None or cur_price is None or fri_price == 0:
        return None
    return (cur_price - fri_price) / fri_price


# ------------------------------------------------------------------
# Portfolio WTD per lane
# ------------------------------------------------------------------

def lane_wtd(lane_key, positions):
    """
    Compute WTD return for a lane.
    Use entry_price vs current_price for positions entered this week,
    and current_price vs last-Friday close for older positions.
    Returns (wtd_pct, best_ticker, best_pct, worst_ticker, worst_pct).
    """
    if not positions or yf is None:
        return None, None, None, None, None

    fri = last_friday()
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

    total_start_value = 0.0
    total_end_value   = 0.0
    ticker_returns    = []

    for pos in positions:
        cur_price = prices.get(pos["ticker"])
        if cur_price is None:
            continue

        # Determine start-of-week basis price
        entry_date = datetime.date.fromisoformat(pos["entry_date"])
        if entry_date > fri:
            # Position opened this week -- use entry price as base
            start_price = pos["entry_price"]
        else:
            # Use last Friday close as base
            start_price = get_price_on_or_before(pos["ticker"], fri)
            if start_price is None:
                start_price = pos["entry_price"]

        if start_price == 0:
            continue

        end_val   = cur_price * pos["shares"]
        start_val = start_price * pos["shares"]
        total_start_value += start_val
        total_end_value   += end_val
        ret = (cur_price - start_price) / start_price
        ticker_returns.append((pos["ticker"], ret))

    if total_start_value == 0:
        return None, None, None, None, None

    wtd = (total_end_value - total_start_value) / total_start_value

    best  = max(ticker_returns, key=lambda x: x[1]) if ticker_returns else (None, None)
    worst = min(ticker_returns, key=lambda x: x[1]) if ticker_returns else (None, None)

    return wtd, best[0], best[1], worst[0], worst[1]


# ------------------------------------------------------------------
# Email builder
# ------------------------------------------------------------------

def format_pct(val):
    if val is None:
        return "N/A"
    sign = "+" if val >= 0 else ""
    return "{}{:.1f}%".format(sign, val * 100)


def build_email(date_str, lane_results, overall_wtd, overall_bench_wtd, pnl_summary, best_mover, worst_mover):
    lines = []
    lines.append("APEX CAPITAL -- WEEKLY PERFORMANCE REPORT")
    lines.append("Week ending {}".format(date_str))
    lines.append("")
    lines.append("LANE PERFORMANCE (WTD)")

    for lane_key, res in lane_results.items():
        label      = res["label"]
        bench_lbl  = res["bench_label"]
        lane_wtd_v = res["wtd"]
        bench_wtd_v = res["bench_wtd"]

        lane_str  = format_pct(lane_wtd_v)
        bench_str = format_pct(bench_wtd_v)

        if lane_wtd_v is not None and bench_wtd_v is not None:
            diff = lane_wtd_v - bench_wtd_v
            direction = "OUTPERFORMING" if diff >= 0 else "UNDERPERFORMING"
            diff_str = "({} by {})".format(direction, format_pct(abs(diff)))
        else:
            diff_str = "(benchmark unavailable)"

        lines.append("{:<16} {} vs {} {} -- {}".format(
            label + ":", lane_str, bench_lbl, bench_str, diff_str))

    lines.append("")
    lines.append("TOTAL PORTFOLIO: {} vs SPY {}".format(
        format_pct(overall_wtd), format_pct(overall_bench_wtd)))
    lines.append("")

    lines.append("TOP MOVERS THIS WEEK")
    if best_mover and best_mover[0]:
        lines.append("Best:  {} {} ({})".format(
            best_mover[0], format_pct(best_mover[1]), best_mover[2]))
    else:
        lines.append("Best:  N/A")
    if worst_mover and worst_mover[0]:
        lines.append("Worst: {} {} ({})".format(
            worst_mover[0], format_pct(worst_mover[1]), worst_mover[2]))
    else:
        lines.append("Worst: N/A")
    lines.append("")

    lines.append("SINCE INCEPTION")
    total_deployed = pnl_summary.get("total_deployed", 0.0)
    total_pnl      = pnl_summary.get("total_pnl", 0.0)
    total_value    = total_deployed + pnl_summary.get("total_unrealized", 0.0) + pnl_summary.get("total_realized", 0.0)
    total_ret      = (total_pnl / total_deployed * 100) if total_deployed else 0.0
    sign           = "+" if total_pnl >= 0 else ""
    lines.append("Total Deployed: ${:,.0f}  |  Current Value: ${:,.0f}  |  Total Return: {}{:.1f}%".format(
        total_deployed, total_value, sign, total_ret))

    if LANE_CAPITAL_FALLBACKS:
        lines.append("")
        lines.append("CONFIG GAP -- these lane capitals are undefined in config.py and are")
        lines.append("defaulting to STARTING_CAPITAL: {}".format(", ".join(sorted(set(LANE_CAPITAL_FALLBACKS)))))

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
    today    = datetime.date.today()
    date_str = today.strftime("%Y-%m-%d")
    print("[performance_tracker] Running {}".format(date_str))

    tracker     = PortfolioTracker()
    pnl_summary = tracker.get_pnl_summary()

    lane_results = {}
    all_movers   = []  # (ticker, pct, lane_label)

    for lane_key, lane_cfg in LANES.items():
        positions   = tracker.get_current_positions(lane=lane_key)
        wtd, best_t, best_p, worst_t, worst_p = lane_wtd(lane_key, positions)
        bench_wtd_v = benchmark_wtd(lane_cfg["benchmark"])

        lane_results[lane_key] = {
            "label":      lane_cfg["label"],
            "bench_label": lane_cfg["bench_label"],
            "wtd":        wtd,
            "bench_wtd":  bench_wtd_v,
            "best":       (best_t, best_p),
            "worst":      (worst_t, worst_p),
        }
        if best_t and best_p is not None:
            all_movers.append((best_t, best_p, lane_cfg["label"]))
        if worst_t and worst_p is not None and (worst_t, worst_p, lane_cfg["label"]) not in all_movers:
            all_movers.append((worst_t, worst_p, lane_cfg["label"]))

    # Overall WTD: every open position treated as one pool, weighted by position
    # value (NOT by lane capital — the old comment here claimed that, but no lane
    # weighting has ever been applied). This used to call lane_wtd() twice with
    # identical arguments, doubling the yfinance downloads for an identical result.
    all_positions = tracker.get_current_positions()
    if all_positions:
        overall_wtd_val, ob, obp, ow, owp = lane_wtd("__all__", all_positions)
    else:
        overall_wtd_val, ob, obp, ow, owp = (None, None, None, None, None)

    overall_bench_wtd = benchmark_wtd("SPY")

    # Best/worst movers overall
    best_mover  = max(all_movers, key=lambda x: x[1]) if all_movers else (None, None, None)
    worst_mover = min(all_movers, key=lambda x: x[1]) if all_movers else (None, None, None)

    body = build_email(date_str, lane_results, overall_wtd_val, overall_bench_wtd,
                       pnl_summary, best_mover, worst_mover)

    subject = "Apex Capital -- Weekly Performance Report | Week ending {}".format(date_str)
    print(body)
    send_email(subject, body)


if __name__ == "__main__":
    main()
