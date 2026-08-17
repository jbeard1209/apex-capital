# =============================================================================
# APEX CAPITAL — REVIEW AGENT
# =============================================================================
# Fetches all current Alpaca positions and recent trade history, performs a
# multi-factor analysis on each position, and produces a structured set of
# recommendations for the Manager Agent to act on.
#
# Recommendation actions:
#   SELL   — exit fully (stop-loss hit, take-profit hit, max hold breached,
#             thesis broken, or momentum reversal confirmed)
#   TRIM   — reduce position (overweight vs risk limits, partial profit-taking)
#   HOLD   — keep as-is (within all parameters, thesis intact)
#   ADD    — increase or initiate (strong momentum, underweight vs conviction)
#   WATCH  — keep but flag for close monitoring (borderline metrics)
#
# Usage:
#   python3 review_agent.py            # print report + save to file
#   python3 review_agent.py --quiet    # save to file only
# =============================================================================

import json
import logging
import sys
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import yfinance as yf

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, HOLD_DAYS_MAX,
    MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS,
    SHORT_CANDIDATES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("review_agent")

REVIEW_OUTPUT_PATH = "data/review_recommendations.json"

# Maximum allowed exposure to any single GICS sector as % of portfolio value.
# Positions in the heaviest sector are flagged TRIM when this is breached.
SECTOR_CONCENTRATION_LIMIT_PCT = 30.0


# ---------------------------------------------------------------------------
# Sector helpers
# ---------------------------------------------------------------------------

def _get_sector(ticker: str) -> str:
    """
    Return GICS sector for *ticker* using yfinance `.info`.
    Falls back to 'Unknown' on any error or missing data.
    Results are NOT cached — callers should batch-call only for open positions
    (typically < 20 tickers, so API overhead is negligible).
    """
    try:
        info = yf.Ticker(ticker).fast_info  # fast_info is lighter than .info
        # fast_info doesn't have sector; fall back to full .info
        full = yf.Ticker(ticker).info
        return full.get("sector") or "Unknown"
    except Exception:
        return "Unknown"


def check_sector_concentration(
    recommendations: list[dict],
    portfolio_value: float,
) -> list[dict]:
    """
    Detect sector over-concentration and upgrade the largest HOLD positions
    in any breaching sector to TRIM.

    Rules:
      - Only open long/short positions (not BUY_NEW) contribute to sector weight.
      - If a sector's total market value > SECTOR_CONCENTRATION_LIMIT_PCT of
        portfolio, every HOLD in that sector is flagged TRIM (priority 2).
      - ADD positions in the offending sector are downgraded to HOLD.
      - Existing SELL / TRIM recommendations are left unchanged.
    """
    if not recommendations or portfolio_value <= 0:
        return recommendations

    # Build sector exposure — skip BUY_NEW (not yet held)
    ticker_sector: dict[str, str] = {}
    sector_value:  dict[str, float] = {}

    for rec in recommendations:
        if rec.get("action") == "BUY_NEW":
            continue
        ticker    = rec["ticker"]
        mkt_val   = (rec.get("weight_pct", 0) or 0) / 100.0 * portfolio_value
        sector    = _get_sector(ticker)
        ticker_sector[ticker] = sector
        sector_value[sector]  = sector_value.get(sector, 0.0) + mkt_val

    # Identify breaching sectors (exclude 'Unknown' — can't reliably enforce)
    overweight: dict[str, float] = {
        s: v / portfolio_value * 100
        for s, v in sector_value.items()
        if s != "Unknown" and v / portfolio_value * 100 > SECTOR_CONCENTRATION_LIMIT_PCT
    }

    if not overweight:
        return recommendations

    # Log for transparency
    for sector, pct in overweight.items():
        logger.warning(
            f"  ⚠️  SECTOR CONCENTRATION: {sector} at {pct:.1f}% "
            f"(limit: {SECTOR_CONCENTRATION_LIMIT_PCT:.0f}%) — flagging for trim"
        )

    # Upgrade / downgrade affected positions
    updated = []
    for rec in recommendations:
        ticker = rec["ticker"]
        sector = ticker_sector.get(ticker)
        if sector and sector in overweight:
            sector_pct = overweight[sector]
            concentration_reason = (
                f"Sector concentration: {sector} at {sector_pct:.1f}% of portfolio "
                f"(limit: {SECTOR_CONCENTRATION_LIMIT_PCT:.0f}%) — reduce exposure"
            )
            rec = dict(rec)  # shallow copy — don't mutate original
            if rec["action"] == "HOLD":
                rec["action"]   = "TRIM"
                rec["priority"] = min(rec.get("priority", 3), 2)
                rec["reasons"]  = [concentration_reason] + rec.get("reasons", [])
            elif rec["action"] == "ADD":
                rec["action"]   = "HOLD"
                rec["priority"] = 3
                rec["reasons"]  = [
                    f"ADD blocked — {concentration_reason}"
                ] + rec.get("reasons", [])
        updated.append(rec)

    return updated


# ---------------------------------------------------------------------------
# Momentum helpers
# ---------------------------------------------------------------------------

def _fetch_price_history(ticker: str, period: str = "250d") -> Optional[pd.DataFrame]:
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df if len(df) > 10 else None
    except Exception as e:
        logger.warning(f"Price fetch failed for {ticker}: {e}")
        return None


def _momentum_score(df: pd.DataFrame) -> dict:
    """
    Returns a dict with:
      - trend: 'bullish' | 'bearish' | 'neutral'
      - above_50ma: bool
      - above_200ma: bool
      - pct_from_50ma: float
      - pct_from_200ma: float
      - rsi_14: float (approximate)
    """
    if df is None or len(df) < 20:
        return {"trend": "unknown", "above_50ma": None, "above_200ma": None,
                "pct_from_50ma": None, "pct_from_200ma": None, "rsi_14": None}

    close = df["Close"]
    price = float(close.iloc[-1])
    ma50  = float(close.iloc[-50:].mean()) if len(close) >= 50  else float(close.mean())
    ma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else ma50

    pct_50  = (price - ma50)  / ma50  * 100
    pct_200 = (price - ma200) / ma200 * 100

    # Approximate RSI-14 (shared, NaN-safe)
    from technical_indicators import rsi_last
    rsi = rsi_last(close)

    if price > ma50 and price > ma200:
        trend = "bullish"
    elif price < ma50 and price < ma200:
        trend = "bearish"
    else:
        trend = "neutral"

    return {
        "trend":        trend,
        "above_50ma":   price >= ma50,
        "above_200ma":  price >= ma200,
        "pct_from_50ma":  round(pct_50,  2),
        "pct_from_200ma": round(pct_200, 2),
        "rsi_14":         round(rsi,     1),
    }


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def _hold_str(hold_days: Optional[int]) -> str:
    """Render a holding period, keeping "unknown" distinguishable from "0 days".

    `f"{None}d"` prints "Noned"; worse, printing 0 would let a ledger gap read
    as a position opened today. Anything a human or an LLM reads has to be able
    to tell a measurement from the absence of one.
    """
    return "?d (entry date unknown)" if hold_days is None else f"{hold_days}d"


def analyze_position(pos: dict, portfolio_value: float, entry_dates: dict) -> dict:
    """
    Full analysis of a single open position.
    Returns a recommendation dict.
    """
    ticker      = pos["ticker"]
    qty         = pos["qty"]
    side        = pos.get("side", "long")
    pnl_pct     = pos["unrealized_pct"]
    market_val  = pos["market_value"]
    avg_entry   = pos["avg_entry"]
    cur_price   = pos["current_price"]

    # An unknown entry date is UNKNOWN, not "yesterday". This used to default to
    # `now - 1 day`, which made every unledgered position read as one day old on
    # every run, forever — so the max-hold exit below could never fire on it and
    # nothing ever said why. 13 of the account's positions were in that state
    # (memory `fallback-that-means-act`: the constant a degraded read substitutes
    # is itself a decision, and "1 day old" decides HOLD). None now, and the
    # time-based rule abstains out loud instead of silently passing.
    entry_dt  = entry_dates.get(ticker)
    hold_days = ((datetime.now() - entry_dt).days
                 if isinstance(entry_dt, datetime) else None)

    weight_pct = (market_val / portfolio_value * 100) if portfolio_value > 0 else 0

    # Momentum
    df   = _fetch_price_history(ticker)
    mom  = _momentum_score(df)

    reasons  : list[str] = []
    action   : str       = "HOLD"
    priority : int       = 3   # 1=urgent, 2=high, 3=normal, 4=low

    is_short = (qty < 0) or (side == "short")

    # ── Rule: stop-loss ────────────────────────────────────────────────────
    if not is_short and pnl_pct <= -(STOP_LOSS_PCT * 100):
        action   = "SELL"
        priority = 1
        reasons.append(f"Stop-loss breached: {pnl_pct:.1f}% (limit: -{STOP_LOSS_PCT*100:.0f}%)")

    elif is_short and pnl_pct <= -(10):   # 10% stop on shorts
        action   = "SELL"
        priority = 1
        reasons.append(f"Short stop-loss breached: {pnl_pct:.1f}% unrealized loss")

    # ── Rule: take-profit ──────────────────────────────────────────────────
    elif pnl_pct >= (TAKE_PROFIT_PCT * 100):
        action   = "SELL"
        priority = 2
        reasons.append(f"Take-profit target reached: {pnl_pct:.1f}% (target: +{TAKE_PROFIT_PCT*100:.0f}%)")

    # ── Rule: max hold duration ────────────────────────────────────────────
    if action == "HOLD" and hold_days is None:
        # Cannot run the rule, so say that rather than quietly returning HOLD.
        # `entry_date_ledger` heals this from broker fill history on the next
        # pipeline run; a position still here afterwards is one the broker's
        # order window cannot reach back far enough to date.
        reasons.append(
            "Entry date unknown — max-hold rule could not be evaluated for this "
            "position (ledger gap; see entry_date_ledger)")
    elif action == "HOLD" and hold_days >= HOLD_DAYS_MAX:
        action   = "SELL"
        priority = 2
        reasons.append(f"Max hold period exceeded: {hold_days} days (limit: {HOLD_DAYS_MAX})")

    # ── Rule: momentum reversal on long positions ──────────────────────────
    if action == "HOLD" and not is_short:
        if mom["trend"] == "bearish" and pnl_pct < 0:
            action   = "SELL"
            priority = 2
            reasons.append(
                f"Momentum reversed bearish while underwater "
                f"(RSI={mom['rsi_14']}, {mom['pct_from_50ma']:+.1f}% vs 50MA)"
            )
        elif mom["trend"] == "bearish" and pnl_pct >= 0:
            action   = "WATCH"
            priority = 2
            reasons.append(
                f"Momentum turning bearish; still profitable — monitor closely "
                f"({mom['pct_from_50ma']:+.1f}% vs 50MA, RSI={mom['rsi_14']})"
            )

    # ── Rule: short recovery (cover) ───────────────────────────────────────
    if action == "HOLD" and is_short:
        if mom["above_50ma"]:
            action   = "SELL"
            priority = 2
            reasons.append(f"Short recovered above 50MA — cover position ({mom['pct_from_50ma']:+.1f}% vs 50MA)")

    # ── Rule: position overweight — three-tier enforcement (board 2026-04-27) ─
    #
    #   Tier 3  (>11% = 110% of limit):  TRIM — normal priority
    #   Tier 2  (>15% = 150% of limit):  TRIM — critical priority 1
    #   Tier 1  (>18%):                  SELL — concentration has compounded past
    #                                    the point where a partial trim is enough
    #
    # The Manager Agent adds a hard override at 15% that cannot be waived by
    # any other signal (PDO, canary, momentum).
    _MAX_PCT = MAX_POSITION_SIZE_PCT * 100   # e.g. 10.0
    if not is_short and weight_pct > (_MAX_PCT * 1.80):
        action   = "SELL"
        priority = 1
        reasons.append(
            f"EXTREME concentration: {weight_pct:.1f}% of portfolio — "
            f"exceeds forced-sell threshold (>{_MAX_PCT * 1.80:.0f}%). "
            f"Idiosyncratic risk now dominates. Full exit required."
        )
    elif not is_short and weight_pct > (_MAX_PCT * 1.50):
        action   = "TRIM"
        priority = 1
        reasons.append(
            f"CRITICAL overweight: {weight_pct:.1f}% of portfolio "
            f"(limit: {_MAX_PCT:.0f}%, critical threshold: {_MAX_PCT * 1.50:.0f}%). "
            f"Immediate trim required."
        )
    elif not is_short and weight_pct > (_MAX_PCT * 1.10):
        action   = "TRIM"
        priority = 2
        reasons.append(
            f"Position overweight: {weight_pct:.1f}% of portfolio "
            f"(limit: {_MAX_PCT:.0f}%, trim threshold: {_MAX_PCT * 1.10:.0f}%)"
        )

    # ── Rule: strong momentum — add opportunity ────────────────────────────
    if action == "HOLD" and not is_short:
        if mom["trend"] == "bullish" and mom["rsi_14"] and mom["rsi_14"] < 65 and pnl_pct > 0:
            if weight_pct < (MAX_POSITION_SIZE_PCT * 100 * 0.75):
                action   = "ADD"
                priority = 3
                reasons.append(
                    f"Bullish momentum with room to add: RSI={mom['rsi_14']}, "
                    f"position only {weight_pct:.1f}% of portfolio"
                )

    # ── Default hold rationale ─────────────────────────────────────────────
    if not reasons:
        reasons.append(
            f"Within all parameters. P&L={pnl_pct:+.1f}%, "
            f"hold={_hold_str(hold_days)}, weight={weight_pct:.1f}%, "
            f"momentum={mom['trend']}"
        )

    # ── Fable 5 LLM position commentary ───────────────────────────────────
    # Token-gated: only fires for actionable positions (SELL/TRIM/ADD/WATCH),
    # never plain HOLDs — most positions most days need no commentary.
    llm_commentary = None
    if action != "HOLD":
        try:
            from apex_llm import reason as _r, is_available
            if is_available("review_agent"):
                _ctx = (
                    f"{ticker} ({'short' if is_short else 'long'}) | P&L {pnl_pct:+.1f}% | "
                    f"${avg_entry:.2f}→${cur_price:.2f} | {_hold_str(hold_days)} | {weight_pct:.1f}% wt | "
                    f"{mom.get('trend','?')}, RSI {mom.get('rsi_14','?')}, "
                    f"{mom.get('pct_from_50ma',0):+.1f}% vs 50MA\n"
                    f"Rule says: {action} — {'; '.join(r[:80] for r in reasons[:3])}\n"
                    "1-2 sentences: is this call right? Be sharp and specific."
                )
                _raw = _r(
                    "You are the Apex Capital Review Agent assessing an open position.",
                    _ctx,
                    agent_name="review_agent",
                    max_tokens=100,
                )
                if _raw:
                    llm_commentary = _raw.strip()
        except Exception:
            pass

    result = {
        "ticker":           ticker,
        "side":             "short" if is_short else "long",
        "qty":              qty,
        "avg_entry":        round(avg_entry,  2),
        "current_price":    round(cur_price,  2),
        "unrealized_pnl_pct": round(pnl_pct, 2),
        "hold_days":        hold_days,
        "weight_pct":       round(weight_pct, 2),
        "momentum":         mom,
        "action":           action,
        "priority":         priority,
        "reasons":          reasons,
    }
    if llm_commentary:
        result["llm_commentary"] = llm_commentary
    return result


# ---------------------------------------------------------------------------
# Watchlist opportunity scanner
# ---------------------------------------------------------------------------

def scan_new_opportunities(existing_tickers: set, portfolio_value: float,
                            n_open: int) -> list[dict]:
    """
    Scan a curated watchlist for strong long entries when capacity remains.
    Returns new BUY recommendations (action='BUY_NEW').
    """
    if n_open >= MAX_OPEN_POSITIONS:
        return []

    slots_available = MAX_OPEN_POSITIONS - n_open
    # Multi-sector watchlist — covers 9 GICS sectors so the scanner isn't
    # structurally biased toward any single theme.
    watchlist = [
        # Technology (highest alpha potential)
        "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "AVGO", "AMD",
        "MRVL", "TSM", "ORCL", "PANW", "CRWD", "PLTR", "ARM", "CRM", "ADBE",
        # Healthcare / Biotech
        "LLY",  "UNH",  "ABBV", "TMO",  "DHR",  "ISRG", "VRTX", "REGN",
        # Financials
        "JPM",  "GS",   "V",    "MA",   "BLK",  "SPGI",
        # Consumer Discretionary
        "TSLA", "HD",   "MCD",  "SBUX",
        # Consumer Staples (defensive)
        "PG",   "COST", "KO",
        # Industrials
        "CAT",  "DE",   "UNP",  "HON",  "GE",
        # Materials
        "LIN",  "APD",
        # Communication Services
        "NFLX", "DIS",  "SPOT",
        # Utilities / Infrastructure (defensive yield)
        "NEE",  "AMT",
    ]
    candidates = []

    for ticker in watchlist:
        if ticker in existing_tickers:
            continue
        df  = _fetch_price_history(ticker)
        mom = _momentum_score(df)
        if mom["trend"] == "bullish" and mom["rsi_14"] and 40 < mom["rsi_14"] < 65:
            candidates.append({
                "ticker":        ticker,
                "side":          "long",
                "qty":           None,
                "avg_entry":     None,
                "current_price": None,
                "unrealized_pnl_pct": 0.0,
                "hold_days":     0,
                "weight_pct":    0.0,
                "momentum":      mom,
                "action":        "BUY_NEW",
                "priority":      3,
                "reasons": [
                    f"New entry opportunity: bullish trend, RSI={mom['rsi_14']}, "
                    f"{mom['pct_from_50ma']:+.1f}% vs 50MA"
                ],
            })

    # Sort by distance above 50MA (strongest momentum first)
    candidates.sort(key=lambda x: x["momentum"]["pct_from_50ma"] or 0, reverse=True)
    return candidates[:slots_available]


# ---------------------------------------------------------------------------
# Main review function
# ---------------------------------------------------------------------------

def run_review(entry_dates: Optional[dict] = None, quiet: bool = False) -> dict:
    """
    Full review cycle.  Returns the complete review report dict.
    entry_dates: {ticker: datetime} — pass from positions log if available.
    """
    from alpaca_trader import AlpacaTrader

    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — REVIEW AGENT")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    trader = AlpacaTrader()
    snap   = trader.get_portfolio_snapshot()
    acct   = trader.get_account()

    portfolio_value = acct["portfolio_value"]
    positions_raw   = snap["positions"]
    n_open          = snap["n_positions"]

    if entry_dates is None:
        entry_dates = _load_entry_dates()

    logger.info(f"  Portfolio value: ${portfolio_value:,.2f}  |  Open positions: {n_open}")

    # ── Analyze each open position ────────────────────────────────────────
    recommendations: list[dict] = []
    for pos in positions_raw:
        logger.info(f"  Analyzing {pos['ticker']} ...")
        rec = analyze_position(pos, portfolio_value, entry_dates)
        recommendations.append(rec)
        logger.info(f"    → {rec['action']} (priority {rec['priority']}): {rec['reasons'][0]}")

    # ── Sector concentration guard ────────────────────────────────────────
    if recommendations:
        logger.info("  Checking sector concentration limits...")
        recommendations = check_sector_concentration(recommendations, portfolio_value)

    # ── Scan for new opportunities ─────────────────────────────────────────
    existing = {r["ticker"] for r in recommendations}
    new_ops  = scan_new_opportunities(existing, portfolio_value, n_open)
    if new_ops:
        logger.info(f"  {len(new_ops)} new entry opportunity(s) identified")
        recommendations.extend(new_ops)

    # ── Sort by priority then action urgency ──────────────────────────────
    action_order = {"SELL": 0, "TRIM": 1, "WATCH": 2, "ADD": 3, "BUY_NEW": 4, "HOLD": 5}
    recommendations.sort(key=lambda r: (r["priority"], action_order.get(r["action"], 9)))

    # ── Summary counts ─────────────────────────────────────────────────────
    summary = {a: 0 for a in ("SELL", "TRIM", "WATCH", "ADD", "BUY_NEW", "HOLD")}
    for r in recommendations:
        summary[r["action"]] = summary.get(r["action"], 0) + 1

    report = {
        "generated_at":    datetime.now().isoformat(),
        "agent":           "Review Agent",
        "account": {
            "portfolio_value": portfolio_value,
            "cash":            acct["cash"],
            "buying_power":    acct["buying_power"],
            "pnl_today":       acct["pnl_today"],
            "pnl_today_pct":   acct["pnl_today_pct"],
            "n_positions":     n_open,
        },
        "summary":         summary,
        "recommendations": recommendations,
    }

    # ── Persist for Manager Agent ──────────────────────────────────────────
    import os
    os.makedirs("data", exist_ok=True)
    with open(REVIEW_OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"\n  Review saved → {REVIEW_OUTPUT_PATH}")

    # ── Print report ───────────────────────────────────────────────────────
    if not quiet:
        _print_report(report)

    return report


def _load_entry_dates() -> dict:
    """
    Load position entry dates from state.json (the pipeline's single source of truth).

    Previously read from data/thematic_positions.json (written by the now-archived
    head_agent.py). That file is never created by the current pipeline.
    state.json["entry_dates"] is populated by main.py after every BUY execution
    and is the authoritative record of when each position was opened.
    """
    import os
    state_path = "data/state.json"
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path) as f:
            state = json.load(f)
        result = {}
        for ticker, date_str in state.get("entry_dates", {}).items():
            try:
                result[ticker] = datetime.fromisoformat(date_str)
            except Exception:
                pass
        return result
    except Exception:
        return {}


def _print_report(report: dict) -> None:
    divider = "─" * 64
    print(f"\n{'═'*64}")
    print("  APEX CAPITAL — REVIEW AGENT REPORT")
    print(f"  {report['generated_at']}")
    print(f"{'═'*64}")

    acct = report["account"]
    print(f"\n  Portfolio Value : ${acct['portfolio_value']:>12,.2f}")
    print(f"  Cash            : ${acct['cash']:>12,.2f}")
    print(f"  Buying Power    : ${acct['buying_power']:>12,.2f}")
    pnl_sign = "+" if acct["pnl_today"] >= 0 else ""
    print(f"  Today's P&L     : {pnl_sign}${abs(acct['pnl_today']):.2f} "
          f"({pnl_sign}{acct['pnl_today_pct']:.2f}%)")
    print(f"  Open Positions  : {acct['n_positions']}")

    summary = report["summary"]
    print(f"\n  Recommendation Summary:")
    for action, count in summary.items():
        if count > 0:
            bar = "█" * count
            print(f"    {action:<10} {bar}  ({count})")

    print(f"\n{divider}")
    print(f"  {'TICKER':<6} {'ACTION':<10} {'P&L%':>7} {'HOLD':>5} {'WT%':>5}  REASON")
    print(divider)

    for rec in report["recommendations"]:
        action_icon = {
            "SELL":    "✗ SELL  ",
            "TRIM":    "▼ TRIM  ",
            "WATCH":   "⚑ WATCH ",
            "ADD":     "▲ ADD   ",
            "BUY_NEW": "★ BUY   ",
            "HOLD":    "  HOLD  ",
        }.get(rec["action"], rec["action"])

        pnl_str  = f"{rec['unrealized_pnl_pct']:+.1f}%" if rec["unrealized_pnl_pct"] is not None else "  —"
        hold_str = "  ?d" if rec["hold_days"] is None else f"{rec['hold_days']}d"
        wt_str   = f"{rec['weight_pct']:.1f}%" if rec["weight_pct"] else " —"

        print(f"  {rec['ticker']:<6} {action_icon}  {pnl_str:>7}  {hold_str:>4}  {wt_str:>4}  {rec['reasons'][0][:50]}")

    print(f"{'═'*64}\n")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    quiet_flag = "--quiet" in sys.argv
    run_review(quiet=quiet_flag)
