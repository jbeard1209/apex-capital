"""
ml_alt_data_agent.py
====================
Apex Capital ML Research Team
-------------------------------
Enriches the ML pipeline with free alternative data signals:
  1. Yahoo Finance News Sentiment  — per-ticker headline scoring
  2. SEC EDGAR Form 4 Insiders     — market-wide insider buy/sell pressure
  3. FRED Economic Data            — VIX, yield spread, consumer sentiment

All three sources are wrapped independently; failure of one does not abort others.
Results written to data/alt_data_signals.json.
Negative signal threshold breaches generate REVIEW entries in data/ml_suggestions.json.

Schedule: every 4 hours via cron (managed externally).
Entry point: run()
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
import yfinance as yf

from email_sender import send_alert_email

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
SIGNALS_PATH = DATA_DIR / "alt_data_signals.json"
SUGGESTIONS_PATH = DATA_DIR / "ml_suggestions.json"
SECTOR_FILE = DATA_DIR / "sector_assignments.json"

DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "NVDA", "MSFT", "GOOGL",
    "AMZN", "META", "TSLA", "JPM", "GS",
]
MAX_WATCHLIST_TICKERS = 20

# Sentiment word lists
POSITIVE_WORDS = frozenset(["beat", "surge", "record", "upgrade", "buy", "soar",
                             "rally", "strong", "outperform", "raised"])
NEGATIVE_WORDS = frozenset(["miss", "drop", "downgrade", "sell", "cut", "loss",
                             "warning", "weak", "disappoint", "decline", "fall"])

# EDGAR
EDGAR_USER_AGENT = "ApexCapital research@apexcapital.com"
EDGAR_SEARCH_URL = (
    "https://efts.sec.gov/LATEST/search-index"
    "?q=%22form+4%22&dateRange=custom&startdt={start}&enddt={end}&forms=4"
)
EDGAR_TIMEOUT = 20   # seconds

# FRED (no API key required for raw CSV endpoint)
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
FRED_TIMEOUT = 15
FRED_SERIES = {
    "VIXCLS": "fred_vix",
    "T10Y2Y": "fred_yield_spread_10y2y",
    "UMCSENT": "consumer_sentiment",
}
FRED_3M_SURPRISE_SERIES = "UMCSENT"   # series for which we compute surprise

# Alert thresholds
ALERT_NEWS_SENTIMENT = -0.5
ALERT_INSIDER_PRESSURE = -0.4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ml_alt_data_agent")

# ---------------------------------------------------------------------------
# Watchlist loader
# ---------------------------------------------------------------------------

def load_watchlist() -> list[str]:
    """
    Load top-20 tickers from sector_assignments.json.
    Falls back to DEFAULT_WATCHLIST if file missing or parse fails.
    """
    if not SECTOR_FILE.exists():
        log.warning("sector_assignments.json not found; using default watchlist.")
        return DEFAULT_WATCHLIST.copy()

    try:
        with open(SECTOR_FILE) as fh:
            data = json.load(fh)

        tickers: list[str] = []
        # Collect tickers from all sectors in all_scored list
        for sector in data.get("all_scored", []):
            watchlist = sector.get("watchlist", [])
            if isinstance(watchlist, list):
                tickers.extend(watchlist)

        # Remove duplicates, preserve order
        seen: set[str] = set()
        unique: list[str] = []
        for t in tickers:
            if t not in seen:
                seen.add(t)
                unique.append(t)

        result = unique[:MAX_WATCHLIST_TICKERS]
        if result:
            log.info("Loaded %d tickers from sector_assignments.json.", len(result))
            return result
        else:
            log.warning("No tickers parsed from sector_assignments.json; using default watchlist.")
            return DEFAULT_WATCHLIST.copy()

    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("Failed to parse sector_assignments.json (%s); using default watchlist.", exc)
        return DEFAULT_WATCHLIST.copy()

# ---------------------------------------------------------------------------
# Source 1: Yahoo Finance News Sentiment
# ---------------------------------------------------------------------------

def _score_headline(title: str) -> int:
    """
    Score a single news headline.
    +1 for positive signal, -1 for negative, 0 for neutral.
    Case-insensitive word-boundary match.
    """
    lower = title.lower()
    words_in_title = set(lower.replace("-", " ").split())
    if words_in_title & POSITIVE_WORDS:
        return 1
    if words_in_title & NEGATIVE_WORDS:
        return -1
    return 0


def _is_recent(timestamp_unix: Any, cutoff_hours: int = 24) -> bool:
    """Return True if unix timestamp is within cutoff_hours of now."""
    try:
        ts = datetime.fromtimestamp(int(timestamp_unix), tz=timezone.utc)
        return ts >= datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)
    except (TypeError, ValueError, OSError):
        return True  # treat unparse-able timestamps as recent (conservative)


def fetch_news_sentiment(watchlist: list[str]) -> dict[str, Any]:
    """
    Fetch recent news for each ticker via yfinance and score headlines.

    Returns:
        {
          "ticker_sentiment": {"SPY": 0.3, ...},
          "market_news_sentiment": 0.15,
          "errors": ["TSLA: no news returned", ...]
        }
    """
    log.info("=== Source 1: Yahoo Finance News Sentiment ===")
    ticker_sentiment: dict[str, float] = {}
    errors: list[str] = []

    for ticker in watchlist:
        try:
            obj = yf.Ticker(ticker)
            news_items = obj.news or []

            recent_scores: list[int] = []
            for item in news_items:
                pub_time = item.get("providerPublishTime") or item.get("publishTime")
                if pub_time and not _is_recent(pub_time, cutoff_hours=24):
                    continue
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                recent_scores.append(_score_headline(title))

            if recent_scores:
                sentiment = float(sum(recent_scores)) / len(recent_scores)
                ticker_sentiment[ticker] = round(sentiment, 4)
                log.debug("%s: %d headlines → sentiment %.3f", ticker, len(recent_scores), sentiment)
            else:
                ticker_sentiment[ticker] = 0.0
                log.debug("%s: no recent headlines — defaulting to 0.0.", ticker)

        except Exception as exc:
            msg = f"{ticker}: {exc}"
            log.warning("News fetch failed for %s: %s", ticker, exc)
            errors.append(msg)
            ticker_sentiment[ticker] = 0.0  # neutral fallback

        # Small delay to be polite to Yahoo Finance
        time.sleep(0.2)

    # Market-wide average
    scores = list(ticker_sentiment.values())
    market_sentiment = round(float(sum(scores)) / len(scores), 4) if scores else 0.0
    log.info("News sentiment complete — %d tickers, market avg: %.3f",
             len(ticker_sentiment), market_sentiment)

    return {
        "ticker_sentiment": ticker_sentiment,
        "market_news_sentiment": market_sentiment,
        "errors": errors,
    }

# ---------------------------------------------------------------------------
# Source 2: SEC EDGAR Form 4 Insider Transactions
# ---------------------------------------------------------------------------

def _parse_edgar_hits(hits: list[dict]) -> tuple[float, float]:
    """
    Parse EDGAR search hits to extract total purchase and sale values.
    Returns (total_purchase_value, total_sale_value).

    Each hit has _source with fields like:
      - file_date, display_date_filed, entity_name, file_num, period_of_report
    For Form 4, the transaction details are in the actual filing XML —
    the search index does not expose shares/price directly.
    We approximate using filing count as a proxy:
      - Transactions with keywords like 'purchase'/'acquisition' vs 'sale'/'disposition'
        in entity metadata, or default to counting filings.
    """
    purchase_count = 0
    sale_count = 0

    for hit in hits:
        src = hit.get("_source", {})
        # EDGAR search index doesn't break out P/S at index level for Form 4
        # Use entity_name / description fields as proxy, or count equally
        desc = (src.get("description") or src.get("form_type") or "").lower()
        file_name = " ".join(src.get("file_names", [])).lower()
        combined = desc + " " + file_name

        if "purchase" in combined or "acquisition" in combined or "/a" in combined:
            purchase_count += 1
        elif "sale" in combined or "disposition" in combined:
            sale_count += 1
        else:
            # Neutral / unknown — split evenly
            purchase_count += 0.5
            sale_count += 0.5

    return float(purchase_count), float(sale_count)


def fetch_insider_sentiment() -> dict[str, Any]:
    """
    Query EDGAR for Form 4 filings from yesterday through today.
    Returns a dict with insider_buy_pressure and metadata.

    insider_buy_pressure = (purchases - sales) / (purchases + sales + 1)
    Range: approximately -1 (all sales) to +1 (all purchases).
    """
    log.info("=== Source 2: SEC EDGAR Form 4 Insider Transactions ===")

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    url = EDGAR_SEARCH_URL.format(start=str(yesterday), end=str(today))
    headers = {"User-Agent": EDGAR_USER_AGENT}

    try:
        resp = requests.get(url, headers=headers, timeout=EDGAR_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        log.error("EDGAR request timed out after %ds.", EDGAR_TIMEOUT)
        return {"insider_buy_pressure": None, "error": "EDGAR timeout"}
    except requests.exceptions.ConnectionError as exc:
        log.error("EDGAR connection error: %s", exc)
        return {"insider_buy_pressure": None, "error": f"Connection error: {exc}"}
    except requests.exceptions.HTTPError as exc:
        log.error("EDGAR HTTP error %s: %s", resp.status_code, exc)
        return {"insider_buy_pressure": None, "error": f"HTTP {resp.status_code}"}
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("EDGAR response parse error: %s", exc)
        return {"insider_buy_pressure": None, "error": f"Parse error: {exc}"}

    hits = data.get("hits", {}).get("hits", [])
    total_filings = data.get("hits", {}).get("total", {}).get("value", len(hits))
    log.info("EDGAR returned %d Form 4 filings (total: %s).", len(hits), total_filings)

    if not hits:
        log.warning("No Form 4 filings found for %s–%s.", yesterday, today)
        return {
            "insider_buy_pressure": 0.0,
            "total_filings": 0,
            "date_range": f"{yesterday} to {today}",
        }

    purchases, sales = _parse_edgar_hits(hits)
    buy_pressure = round(
        (purchases - sales) / (purchases + sales + 1.0), 4
    )
    log.info(
        "Insider sentiment — purchases: %.1f, sales: %.1f, buy_pressure: %.4f",
        purchases, sales, buy_pressure,
    )

    return {
        "insider_buy_pressure": buy_pressure,
        "total_purchase_proxy": round(purchases, 1),
        "total_sale_proxy": round(sales, 1),
        "total_filings": len(hits),
        "date_range": f"{yesterday} to {today}",
    }

# ---------------------------------------------------------------------------
# Source 3: FRED Economic Data
# ---------------------------------------------------------------------------

def _fetch_fred_series(series_id: str) -> Optional[pd.DataFrame]:
    """
    Download a FRED series as CSV and return as a DataFrame with columns [date, value].
    Returns None on any failure.
    """
    url = FRED_CSV_URL.format(series_id=series_id)
    try:
        resp = requests.get(url, timeout=FRED_TIMEOUT)
        resp.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(resp.text), parse_dates=["DATE"])
        df.columns = ["date", "value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"]).sort_values("date")
        log.debug("FRED %s: %d rows fetched, latest: %s = %.4f",
                  series_id, len(df), df["date"].iloc[-1].date(), df["value"].iloc[-1])
        return df
    except requests.exceptions.Timeout:
        log.error("FRED %s request timed out.", series_id)
    except requests.exceptions.HTTPError as exc:
        log.error("FRED %s HTTP error: %s", series_id, exc)
    except requests.exceptions.ConnectionError as exc:
        log.error("FRED %s connection error: %s", series_id, exc)
    except Exception as exc:
        log.error("FRED %s unexpected error: %s", series_id, exc, exc_info=True)
    return None


def _compute_3m_surprise(df: pd.DataFrame) -> Optional[float]:
    """
    Compute economic surprise = current value minus 3-month rolling average.
    Positive = current is better than recent trend.
    Returns None if insufficient data.
    """
    if df is None or len(df) < 2:
        return None

    latest_val = df["value"].iloc[-1]
    cutoff = df["date"].iloc[-1] - pd.DateOffset(months=3)
    three_month_window = df[df["date"] >= cutoff]["value"]

    if len(three_month_window) < 2:
        return None

    avg_3m = three_month_window.mean()
    surprise = round(float(latest_val - avg_3m), 4)
    return surprise


def fetch_fred_data() -> dict[str, Any]:
    """
    Fetch VIX, 10Y-2Y yield spread, and consumer sentiment from FRED.
    Computes consumer sentiment 3-month surprise.

    Returns a dict with keys matching output schema.
    """
    log.info("=== Source 3: FRED Economic Data ===")
    result: dict[str, Any] = {}
    errors: list[str] = []
    consumer_df = None

    for series_id, output_key in FRED_SERIES.items():
        df = _fetch_fred_series(series_id)
        if df is None or df.empty:
            log.warning("FRED series %s returned no data.", series_id)
            result[output_key] = None
            errors.append(f"FRED {series_id} unavailable")
            continue

        latest_val = round(float(df["value"].iloc[-1]), 4)
        result[output_key] = latest_val
        log.info("FRED %s (%s) = %.4f", series_id, output_key, latest_val)

        if series_id == FRED_3M_SURPRISE_SERIES:
            consumer_df = df

    # Compute consumer sentiment surprise
    if consumer_df is not None:
        surprise = _compute_3m_surprise(consumer_df)
        result["consumer_sentiment_surprise"] = surprise
        log.info("Consumer sentiment 3m surprise: %s", surprise)
    else:
        result["consumer_sentiment_surprise"] = None

    if errors:
        result["errors"] = errors

    return result

# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def write_signals(
    news: dict[str, Any],
    insider: dict[str, Any],
    fred: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge all signals into alt_data_signals.json.
    Returns the signals dict.
    """
    signals: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news_sentiment": news.get("ticker_sentiment", {}),
        "market_news_sentiment": news.get("market_news_sentiment"),
        "insider_buy_pressure": insider.get("insider_buy_pressure"),
        "fred_vix": fred.get("fred_vix"),
        "fred_yield_spread_10y2y": fred.get("fred_yield_spread_10y2y"),
        "consumer_sentiment": fred.get("consumer_sentiment"),
        "consumer_sentiment_surprise": fred.get("consumer_sentiment_surprise"),
        # Metadata for diagnostics
        "_meta": {
            "news_errors": news.get("errors", []),
            "insider_meta": {k: v for k, v in insider.items()
                             if k not in ("insider_buy_pressure",)},
            "fred_errors": fred.get("errors", []),
        },
    }

    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_PATH, "w") as fh:
        json.dump(signals, fh, indent=2, default=str)
    log.info("Alt data signals written → %s", SIGNALS_PATH)
    return signals

# ---------------------------------------------------------------------------
# Suggestions integration
# ---------------------------------------------------------------------------

def load_suggestions() -> list[dict[str, Any]]:
    if not SUGGESTIONS_PATH.exists():
        return []
    try:
        with open(SUGGESTIONS_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else data.get("suggestions", [])
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load suggestions: %s", exc)
        return []


def save_suggestions(suggestions: list[dict[str, Any]]) -> None:
    SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SUGGESTIONS_PATH, "w") as fh:
        json.dump(suggestions, fh, indent=2, default=str)
    log.info("Suggestions written → %s", SUGGESTIONS_PATH)


def _suggestion_exists_today(title_fragment: str, suggestions: list[dict]) -> bool:
    """Return True if a suggestion with this title fragment was added today."""
    today_str = datetime.now(timezone.utc).date().isoformat()
    for s in suggestions:
        existing_title = s.get("title", "").lower()
        if title_fragment.lower() in existing_title:
            created_raw = s.get("created_at") or s.get("timestamp") or ""
            if today_str in str(created_raw):
                return True
    return False


def maybe_add_negative_signal_suggestion(signals: dict[str, Any]) -> None:
    """
    Add REVIEW suggestion if market_news_sentiment < -0.5 OR insider_buy_pressure < -0.4.
    """
    mns = signals.get("market_news_sentiment")
    ibp = signals.get("insider_buy_pressure")

    triggered_news = mns is not None and mns < ALERT_NEWS_SENTIMENT
    triggered_insider = ibp is not None and ibp < ALERT_INSIDER_PRESSURE

    if not triggered_news and not triggered_insider:
        return

    title = "Significant negative alt-data signal today — consider waiting before new positions"
    suggestions = load_suggestions()

    if _suggestion_exists_today("negative alt-data signal", suggestions):
        log.info("Negative signal suggestion already added today — skipping duplicate.")
        return

    triggers: list[str] = []
    if triggered_news:
        triggers.append(f"market_news_sentiment={mns:.3f} (threshold: {ALERT_NEWS_SENTIMENT})")
    if triggered_insider:
        triggers.append(f"insider_buy_pressure={ibp:.3f} (threshold: {ALERT_INSIDER_PRESSURE})")

    rationale = (
        f"Alt-data signals as of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}: "
        + "; ".join(triggers) + ". "
        f"Additional context — "
        f"FRED VIX: {signals.get('fred_vix', 'N/A')}, "
        f"Yield Spread 10Y-2Y: {signals.get('fred_yield_spread_10y2y', 'N/A')}, "
        f"Consumer Sentiment: {signals.get('consumer_sentiment', 'N/A')} "
        f"(3m surprise: {signals.get('consumer_sentiment_surprise', 'N/A')}). "
        "Recommendation: pause new position entries until signals stabilize."
    )

    suggestion = {
        "id": f"alt_data_negative_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
        "category": "REVIEW",
        "title": title,
        "rationale": rationale,
        "market_news_sentiment": mns,
        "insider_buy_pressure": ibp,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING",
        "source": "ml_alt_data_agent",
    }
    suggestions.append(suggestion)
    save_suggestions(suggestions)
    log.warning("Negative alt-data suggestion added: %s", title)

# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

def maybe_send_alert(signals: dict[str, Any]) -> None:
    """Send email alert on significant negative alt-data signals."""
    mns = signals.get("market_news_sentiment")
    ibp = signals.get("insider_buy_pressure")

    triggered_news = mns is not None and mns < ALERT_NEWS_SENTIMENT
    triggered_insider = ibp is not None and ibp < ALERT_INSIDER_PRESSURE

    if not triggered_news and not triggered_insider:
        return

    subject = "[APEX ML] Negative Alt-Data Alert — review new positions"

    triggers_html = ""
    if triggered_news:
        triggers_html += f"<li>Market News Sentiment: <b>{mns:.3f}</b> (threshold: {ALERT_NEWS_SENTIMENT})</li>"
    if triggered_insider:
        triggers_html += f"<li>Insider Buy Pressure: <b>{ibp:.3f}</b> (threshold: {ALERT_INSIDER_PRESSURE})</li>"

    body = f"""
    <h3 style="color:#f0a500;">Negative Alt-Data Signal — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h3>
    <p>One or more alt-data indicators have crossed alert thresholds:</p>
    <ul>{triggers_html}</ul>
    <table border="1" cellpadding="6" style="border-collapse:collapse;font-size:13px;">
      <tr><th>Signal</th><th>Value</th></tr>
      <tr><td>Market News Sentiment</td><td>{mns}</td></tr>
      <tr><td>Insider Buy Pressure</td><td>{ibp}</td></tr>
      <tr><td>FRED VIX</td><td>{signals.get('fred_vix', 'N/A')}</td></tr>
      <tr><td>Yield Spread 10Y-2Y</td><td>{signals.get('fred_yield_spread_10y2y', 'N/A')}</td></tr>
      <tr><td>Consumer Sentiment</td><td>{signals.get('consumer_sentiment', 'N/A')}</td></tr>
      <tr><td>Consumer Sentiment Surprise</td><td>{signals.get('consumer_sentiment_surprise', 'N/A')}</td></tr>
    </table>
    <p style="color:#8b949e;font-size:12px;">
      Recommendation: pause new position entries until signals stabilize.
      See <code>data/alt_data_signals.json</code> for full ticker-level detail.
    </p>
    """

    try:
        sent = send_alert_email(subject, body)
        if sent:
            log.info("Alert email sent: %s", subject)
        else:
            log.warning("Alert email returned False (not sent).")
    except Exception as exc:
        log.error("Failed to send alert email: %s", exc)

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> None:
    log.info("=== ml_alt_data_agent starting ===")
    start_time = time.monotonic()

    watchlist = load_watchlist()
    log.info("Watchlist: %s", watchlist)

    # --- Source 1: News Sentiment ---
    news_result: dict[str, Any] = {}
    try:
        news_result = fetch_news_sentiment(watchlist)
    except Exception as exc:
        log.error("FATAL error in news sentiment source: %s", exc, exc_info=True)
        news_result = {
            "ticker_sentiment": {},
            "market_news_sentiment": None,
            "errors": [f"Unhandled exception: {exc}"],
        }

    # --- Source 2: EDGAR Insider Transactions ---
    insider_result: dict[str, Any] = {}
    try:
        insider_result = fetch_insider_sentiment()
    except Exception as exc:
        log.error("FATAL error in EDGAR insider source: %s", exc, exc_info=True)
        insider_result = {
            "insider_buy_pressure": None,
            "error": f"Unhandled exception: {exc}",
        }

    # --- Source 3: FRED Economic Data ---
    fred_result: dict[str, Any] = {}
    try:
        fred_result = fetch_fred_data()
    except Exception as exc:
        log.error("FATAL error in FRED data source: %s", exc, exc_info=True)
        fred_result = {
            "fred_vix": None,
            "fred_yield_spread_10y2y": None,
            "consumer_sentiment": None,
            "consumer_sentiment_surprise": None,
            "errors": [f"Unhandled exception: {exc}"],
        }

    # --- Write combined output ---
    signals = write_signals(news_result, insider_result, fred_result)

    # --- Log summary ---
    log.info(
        "Signals summary — news_sentiment: %s | insider_pressure: %s | "
        "vix: %s | yield_spread: %s | consumer_sentiment: %s",
        signals.get("market_news_sentiment"),
        signals.get("insider_buy_pressure"),
        signals.get("fred_vix"),
        signals.get("fred_yield_spread_10y2y"),
        signals.get("consumer_sentiment"),
    )

    # --- Threshold checks → suggestions + alerts ---
    try:
        maybe_add_negative_signal_suggestion(signals)
        maybe_send_alert(signals)
    except Exception as exc:
        log.error("Error during threshold check/alert: %s", exc, exc_info=True)

    elapsed = time.monotonic() - start_time
    log.info("=== ml_alt_data_agent complete in %.1fs ===", elapsed)


if __name__ == "__main__":
    run()
