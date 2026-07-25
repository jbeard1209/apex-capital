#!/usr/bin/env python3
"""
fe_web_research.py -- First Eagle Web Research Module
Pulls real-time info from free sources for a given ticker.
Caches to data/web_research_cache/{ticker}.json with 3-day TTL.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR   = Path(__file__).parent
CACHE_DIR  = BASE_DIR / "data" / "web_research_cache"
CACHE_TTL  = timedelta(days=3)

BULLISH_SIGNALS = [
    "record revenue", "beat", "raised guidance", "buyback",
    "dividend increase", "new contract", "market share",
]
BEARISH_SIGNALS = [
    "miss", "warning", "lawsuit", "sec investigation", "ceo resign",
    "layoffs", "guidance cut", "debt downgrade", "recall", "fraud", "restatement",
]

HEADERS = {"User-Agent": "ApexCapital/1.0 research@apexcapital.com"}


def _fetch_url(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _load_cache(ticker: str) -> dict | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ticker}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        retrieved_at = datetime.fromisoformat(data.get("retrieved_at", "2000-01-01T00:00:00+00:00"))
        if datetime.now(timezone.utc) - retrieved_at < CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _save_cache(ticker: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ticker}.json"
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _fetch_edgar_filings(ticker: str) -> list[dict]:
    """Fetch recent SEC EDGAR filings (10-K/10-Q/8-K) for the last 90 days."""
    filings = []
    try:
        today    = datetime.now(timezone.utc).date()
        start_dt = (today - timedelta(days=90)).isoformat()
        end_dt   = today.isoformat()
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&forms=10-K,10-Q,8-K"
            f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}"
        )
        raw = _fetch_url(url)
        data = json.loads(raw)
        hits = data.get("hits", {}).get("hits", [])
        for hit in hits[:20]:
            src = hit.get("_source", {})
            filings.append({
                "form_type":   src.get("form_type", ""),
                "filed_at":    src.get("file_date", ""),
                "company":     src.get("display_names", [ticker])[0] if src.get("display_names") else ticker,
                "description": src.get("period_of_report", ""),
                "accession":   src.get("accession_no", ""),
            })
    except Exception as e:
        filings.append({"error": str(e)})
    return filings


def _fetch_yahoo_headlines(ticker: str) -> list[dict]:
    """Fetch last 10 Yahoo Finance RSS headlines for the ticker."""
    headlines = []
    try:
        url = (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={ticker}&region=US&lang=en-US"
        )
        raw = _fetch_url(url)
        root = ET.fromstring(raw)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item")
        for item in items[:10]:
            title = item.findtext("title", "").strip()
            pub   = item.findtext("pubDate", "").strip()
            link  = item.findtext("link", "").strip()
            headlines.append({
                "title":    title,
                "date":     pub,
                "link":     link,
            })
    except Exception as e:
        headlines.append({"error": str(e)})
    return headlines


def _analyze_sentiment(headlines: list[dict]) -> tuple[str, list[str], list[str]]:
    """Return (sentiment, bullish_signals, red_flags) from headline list."""
    bullish_found = []
    bearish_found = []
    all_text = " ".join(h.get("title", "").lower() for h in headlines if "title" in h)

    for phrase in BULLISH_SIGNALS:
        if phrase in all_text:
            bullish_found.append(phrase)
    for phrase in BEARISH_SIGNALS:
        if phrase in all_text:
            bearish_found.append(phrase)

    if len(bullish_found) > len(bearish_found):
        sentiment = "POSITIVE"
    elif len(bearish_found) > len(bullish_found):
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"

    return sentiment, bullish_found, bearish_found


def _build_summary(
    ticker: str,
    filings: list[dict],
    headlines: list[dict],
    sentiment: str,
    bullish: list[str],
    red_flags: list[str],
) -> str:
    filing_count  = sum(1 for f in filings if "form_type" in f and f["form_type"])
    headline_count = sum(1 for h in headlines if "title" in h)
    parts = [
        f"{ticker} has {filing_count} recent SEC filing(s) in the past 90 days"
        f" and {headline_count} recent news headline(s).",
    ]
    if sentiment == "POSITIVE":
        parts.append(
            f"News sentiment is POSITIVE with signals including: {', '.join(bullish[:3])}."
        )
    elif sentiment == "NEGATIVE":
        parts.append(
            f"News sentiment is NEGATIVE with concern signals: {', '.join(red_flags[:3])}."
        )
    else:
        parts.append("News sentiment is NEUTRAL with no dominant bullish or bearish signals.")
    if red_flags:
        parts.append(
            f"Red flags detected: {', '.join(red_flags[:3])}. Monitor closely."
        )
    return " ".join(parts)


def get_company_research(ticker: str) -> dict:
    """
    Pull real-time web research for a ticker from free sources.
    Results are cached to data/web_research_cache/{ticker}.json with a 3-day TTL.

    Returns:
        dict with keys: ticker, retrieved_at, recent_filings, recent_headlines,
        headline_sentiment, bullish_signals, red_flags, research_summary
    """
    ticker = ticker.upper().strip()

    cached = _load_cache(ticker)
    if cached:
        return cached

    now = datetime.now(timezone.utc).isoformat()

    # a) EDGAR filings
    filings = _fetch_edgar_filings(ticker)
    time.sleep(0.5)

    # b) Yahoo Finance RSS headlines
    headlines = _fetch_yahoo_headlines(ticker)
    time.sleep(0.5)

    # c) Sentiment analysis
    sentiment, bullish_signals, red_flags = _analyze_sentiment(headlines)

    # d) Plain-English summary
    summary = _build_summary(ticker, filings, headlines, sentiment, bullish_signals, red_flags)

    result = {
        "ticker":             ticker,
        "retrieved_at":       now,
        "recent_filings":     filings,
        "recent_headlines":   headlines,
        "headline_sentiment": sentiment,
        "bullish_signals":    bullish_signals,
        "red_flags":          red_flags,
        "research_summary":   summary,
    }

    _save_cache(ticker, result)
    return result


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(json.dumps(get_company_research(t), indent=2))
