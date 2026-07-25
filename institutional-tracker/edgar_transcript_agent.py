#!/usr/bin/env python3
"""
edgar_transcript_agent.py — EDGAR Earnings Call Transcript Agent (#20)
=======================================================================
Parses SEC EDGAR 8-K filings (earnings call transcripts / press releases)
for management tone indicators:
  - Uncertainty keywords ("headwinds", "challenging", "uncertain", "difficult")
  - Guidance cuts ("guidance reduced", "below expectations", "lower than expected")
  - Positive signals ("exceeded", "raised guidance", "strong", "outperformed")

Computes mgmt_tone_score per ticker (-1 to +1, negative = bearish tone).
Saves to data/edgar_tone_signals.json for use as ML feature.
Updates alt_data_signals.json with mgmt_tone_score.

Uses SEC EDGAR free API (no key required):
  https://efts.sec.gov/LATEST/search-index?q="earnings call"&dateRange=custom&...
  https://data.sec.gov/submissions/CIK{cik}.json

Runs: Weekly (Sunday 6 AM), and on-demand.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("edgar_transcript")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

TONE_SIGNALS   = os.path.join(DATA_DIR, "edgar_tone_signals.json")
TONE_CACHE     = os.path.join(DATA_DIR, "edgar_tone_cache.json")

UNCERTAINTY_WORDS = [
    "headwinds", "challenging", "uncertainty", "uncertain", "difficult",
    "pressure", "deteriorating", "weakness", "disappointing", "below expectations",
    "revised down", "guidance reduced", "lower than expected", "cautious",
    "concerns", "macro challenges", "slowing", "declining demand",
]
POSITIVE_WORDS = [
    "exceeded", "above expectations", "raised guidance", "strong demand",
    "outperformed", "record", "acceleration", "robust", "momentum",
    "raised full year", "increased guidance", "better than expected",
    "market share gains", "expanding margins",
]
GUIDANCE_CUT_WORDS = [
    "lowering guidance", "reduced guidance", "withdrawing guidance",
    "below consensus", "below analyst estimates", "miss", "revenue miss",
]

EDGAR_API = "https://efts.sec.gov/LATEST/search-index"
USER_AGENT = "Apex Capital Research research@apexcap.internal"


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except: pass
        raise


def score_text(text: str) -> float:
    """
    Score a text passage for management tone.
    Returns a float in [-1, +1]: negative = bearish/uncertain, positive = bullish/confident.
    """
    if not text:
        return 0.0
    
    text_lower = text.lower()
    
    neg_count  = sum(1 for w in UNCERTAINTY_WORDS if w in text_lower)
    neg_count += sum(2 for w in GUIDANCE_CUT_WORDS if w in text_lower)
    pos_count  = sum(1 for w in POSITIVE_WORDS if w in text_lower)
    
    total = neg_count + pos_count
    if total == 0:
        return 0.0
    
    raw = (pos_count - neg_count) / total
    return round(raw, 3)


def get_cik(ticker: str) -> Optional[str]:
    """Get SEC CIK number for a ticker from local cache or EDGAR."""
    # Check local cik_map
    cik_map = _load_json(os.path.join(DATA_DIR, "cik_map.json"), {})
    if ticker in cik_map:
        return cik_map[ticker]
    
    # Also check sec_ticker_map.json
    sec_map = _load_json(os.path.join(DATA_DIR, "sec_ticker_map.json"), {})
    if ticker in sec_map:
        return str(sec_map[ticker]).zfill(10)
    
    try:
        import requests
        resp = requests.get(
            f"https://efts.sec.gov/LATEST/search-index?q=\"{ticker}\"&dateRange=custom",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        # Simpler: use EDGAR company search
        resp2 = requests.get(
            f"https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={ticker}&type=8-K&dateb=&owner=include&count=1&search_text=&action=getcompany&output=atom",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        import re
        match = re.search(r"CIK=(\d+)", resp2.text)
        if match:
            cik = match.group(1).zfill(10)
            cik_map[ticker] = cik
            _save_json(os.path.join(DATA_DIR, "cik_map.json"), cik_map)
            return cik
    except Exception as e:
        logger.debug(f"  CIK lookup error for {ticker}: {e}")
    return None


def fetch_recent_8k_text(ticker: str, days_back: int = 30) -> Optional[str]:
    """
    Fetch the most recent 8-K filing text for a ticker.
    Returns the filing text (truncated to 5000 chars) or None.
    """
    # Check cache
    cache = _load_json(TONE_CACHE, {})
    cache_key = f"{ticker}_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    if cache_key in cache:
        return cache[cache_key]

    cik = get_cik(ticker)
    if not cik:
        return None

    try:
        import requests
        # Get recent 8-K filings
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={cutoff}&forms=8-K&_source=hits.hits._source.period_of_report,hits.hits._source.file_date,hits.hits._source.display_date_filed"
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return None
        
        # Get the first (most recent) filing's accession number
        first = hits[0].get("_source", {})
        acc_no = hits[0].get("_id", "").replace(":", "-")
        if not acc_no:
            return None

        # Fetch the filing index
        acc_clean = acc_no.replace("-", "")
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{acc_no}-index.htm"
        idx_resp = requests.get(idx_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if idx_resp.status_code != 200:
            return None

        # Find the primary document (htm/txt)
        doc_match = re.search(r'href="(/Archives/edgar/data/\d+/\d+/[^"]+\.(?:htm|txt))"', idx_resp.text)
        if not doc_match:
            return None

        doc_url = "https://www.sec.gov" + doc_match.group(1)
        doc_resp = requests.get(doc_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if doc_resp.status_code != 200:
            return None

        # Strip HTML
        text = re.sub(r"<[^>]+>", " ", doc_resp.text)
        text = re.sub(r"\s+", " ", text).strip()[:5000]
        
        # Cache it
        cache[cache_key] = text
        _save_json(TONE_CACHE, {k: v for k, v in list(cache.items())[-200:]})
        
        time.sleep(0.5)  # SEC rate limit courtesy
        return text

    except Exception as e:
        logger.debug(f"  8-K fetch error for {ticker}: {e}")
        return None


def analyze_ticker(ticker: str, days_back: int = 30) -> dict:
    """
    Analyze a ticker's recent 8-K filings for management tone.
    Returns {ticker, mgmt_tone_score, filing_count, summary}.
    """
    text = fetch_recent_8k_text(ticker, days_back)
    if not text:
        return {"ticker": ticker, "mgmt_tone_score": 0.0, "filing_count": 0, "summary": "No 8-K data"}

    score = score_text(text)
    
    # Extract tone keywords found
    text_lower = text.lower()
    neg_found = [w for w in UNCERTAINTY_WORDS[:5] if w in text_lower]
    pos_found = [w for w in POSITIVE_WORDS[:5] if w in text_lower]
    
    summary = f"Tone={score:+.2f}: pos=[{', '.join(pos_found[:3])}] neg=[{', '.join(neg_found[:3])}]"
    return {
        "ticker":           ticker,
        "mgmt_tone_score":  score,
        "filing_count":     1,
        "summary":          summary,
        "analyzed_at":      datetime.now(timezone.utc).isoformat(),
    }


def run_edgar_transcript_analysis(tickers: Optional[list[str]] = None, days_back: int = 30) -> dict:
    """Main entry point. Analyzes all positions + watchlist."""
    logger.info("Running EDGAR earnings transcript analysis...")
    
    if tickers is None:
        # Get tickers from positions + watchlist
        tickers = []
        try:
            from alpaca_trader import AlpacaTrader
            pos_df = AlpacaTrader().get_positions()
            if pos_df is not None and not pos_df.empty:
                tickers.extend(str(t).upper() for t in pos_df.get("symbol", []) if t)
        except Exception:
            pass
        fe_watchlist = _load_json(os.path.join(DATA_DIR, "fe_watchlist.json"), [])
        for item in (fe_watchlist if isinstance(fe_watchlist, list) else []):
            t = item.get("ticker","") or (item if isinstance(item, str) else "")
            if t:
                tickers.append(str(t).upper())
        tickers = list(set(tickers))[:30]

    if not tickers:
        logger.info("  No tickers to analyze.")
        return {"tickers_analyzed": 0, "results": {}}

    logger.info(f"  Analyzing {len(tickers)} tickers: {', '.join(tickers[:10])}...")
    
    results = {}
    alt_signals = _load_json(os.path.join(DATA_DIR, "alt_data_signals.json"), {})
    
    for ticker in tickers:
        result = analyze_ticker(ticker, days_back)
        results[ticker] = result
        score = result["mgmt_tone_score"]
        logger.info(f"  {ticker}: tone={score:+.2f} ({result['summary'][:60]})")
        
        # Update alt_data_signals
        if ticker not in alt_signals:
            alt_signals[ticker] = {}
        alt_signals[ticker]["mgmt_tone_score"] = score
        alt_signals[ticker]["mgmt_tone_updated_at"] = result.get("analyzed_at","")
        
        time.sleep(0.3)

    _save_json(os.path.join(DATA_DIR, "alt_data_signals.json"), alt_signals)
    _save_json(TONE_SIGNALS, results)

    # Flag bearish tones to firm_channel
    bearish = [t for t, r in results.items() if r["mgmt_tone_score"] < -0.3]
    if bearish:
        try:
            from agent_meeting import post_bulletin
            post_bulletin(
                author  = "EDGARTranscriptAgent",
                topic   = "risk",
                title   = f"Bearish Management Tone: {', '.join(bearish)}",
                body    = f"EDGAR transcript analysis flagged bearish management tone:\n" +
                          "\n".join(f"  {t}: {results[t]['summary']}" for t in bearish),
                urgency = "NORMAL",
                ttl_hours = 72.0,
            )
        except Exception:
            pass

    logger.info(f"  Done: {len(results)} analyzed, {len(bearish)} bearish")
    return {"tickers_analyzed": len(results), "results": results, "bearish": bearish}


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("EDGARTranscriptAgent")
        sys.exit(0)
    result = run_edgar_transcript_analysis()
    print(f"\nAnalyzed {result['tickers_analyzed']} tickers, {len(result.get('bearish',[]))} flagged bearish")
