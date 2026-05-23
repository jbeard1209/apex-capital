#!/usr/bin/env python3
"""
APEX CAPITAL — HISTORICAL LABEL BUILDER (FAST v2)
===================================================
Two-phase approach for maximum speed:

  Phase 1 — EDGAR parse (rate-limited, sequential)
    Parse all remaining 13F XMLs from EDGAR to collect every ticker that
    appears across all filings.  Progress is checkpointed so restarts are free.

  Phase 2 — Bulk price pre-warm (fast, parallel)
    Download price history for every unique ticker in one shot using yfinance
    batch calls (200 tickers per call, threaded).  Store everything in a
    shared in-memory dict (and optionally a parquet cache on disk so restarts
    are even faster).

  Phase 3 — Label computation (CPU-bound, parallel)
    Process all filings using the pre-warmed price cache — pure in-memory
    arithmetic, no network calls.  4 workers, each filing takes < 2 seconds.

Expected runtime (747 remaining filings):
  Phase 1 EDGAR parse: ~15–25 min  (0.5 s/request × 2 req/filing)
  Phase 2 price warm:  ~5–10 min   (bulk yfinance, ~4 000 unique tickers)
  Phase 3 labelling:   ~25–40 min  (pure computation at ~2 s/filing)
  Total:               ~45–75 min  vs 9+ days for the original builder

Usage
-----
  python3 predictive_model/historical_label_builder_fast.py
  python3 predictive_model/historical_label_builder_fast.py --workers 4
  python3 predictive_model/historical_label_builder_fast.py --skip-prewarm
  python3 predictive_model/historical_label_builder_fast.py --limit 50
"""

import sys, os, sqlite3, time, logging, json, argparse, threading, pickle
from datetime import datetime, date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import (
    get_db, parse_holdings_from_accession, resolve_ticker,
)
import predictive_model.edgar_historical_scraper as _edgar_scraper
from predictive_model.data_collector import _ensure_collector_tables

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("label_builder_fast")

DB_PATH         = Path(__file__).parent.parent / "data" / "historical_13f.db"
PROGRESS_PATH   = Path(__file__).parent.parent / "data" / "label_builder_progress.json"
PRICE_CACHE_PATH= Path(__file__).parent.parent / "data" / "price_cache.pkl"
TICKER_MAP_PATH = Path(__file__).parent.parent / "data" / "filing_tickers.json"

# ── Shared state ──────────────────────────────────────────────────────────────
_price_cache:  dict = {}          # {ticker: pd.Series of Close}
_cache_lock  = threading.Lock()
_prog_lock   = threading.Lock()
_log_lock    = threading.Lock()
_tls         = threading.local()  # thread-local DB connections


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_conn() -> sqlite3.Connection:
    if not hasattr(_tls, "conn") or _tls.conn is None:
        conn = sqlite3.connect(str(DB_PATH), timeout=90, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=20000")
        _tls.conn = conn
    return _tls.conn


def _migrate_schema(conn):
    new_cols = [
        ("is_historical",           "INTEGER DEFAULT 0"),
        ("price_momentum_20d",      "REAL"),
        ("price_momentum_5d",       "REAL"),
        ("amendment_flag",          "INTEGER DEFAULT 0"),
        ("position_size_pct_aum",   "REAL"),
        ("sector_rotation_score",   "REAL"),
        ("days_since_ipo",          "INTEGER"),
        ("short_interest_pct",      "REAL"),
        ("price_vs_52w_high",       "REAL"),
        ("filing_quarter_consensus","REAL"),
        ("insider_buy_flag",        "INTEGER DEFAULT 0"),
        ("insider_net_shares_30d",  "INTEGER"),
        ("risk_adjusted_20d",       "REAL"),
        ("risk_adjusted_5d",        "REAL"),
        ("volatility_20d",          "REAL"),
        # Market context at filing date (backfilled from price cache)
        ("rd_regime_bull",          "INTEGER"),   # 1=bull, 0=neutral, -1=bear (SPY vs 200MA)
        ("rd_vix",                  "REAL"),      # VIX level on filing date
        ("rd_spy_rsi",              "REAL"),      # SPY 14-day RSI on filing date
        ("rd_ticker_rsi",           "REAL"),      # ticker 14-day RSI on filing date
        ("rd_ticker_pct_from_50ma", "REAL"),      # ticker % above/below 50MA on filing date
        # Commodity momentum — market-wide macro signals, 100% backfillable from USO/GLD/UUP
        ("oil_momentum_20d",        "REAL"),      # USO 20-day return (oil demand proxy)
        ("gold_momentum_20d",       "REAL"),      # GLD 20-day return (risk-off / inflation hedge)
        ("dxy_momentum_20d",        "REAL"),      # UUP 20-day return (USD strength; headwind for equities)
        # Earnings surprise proxy — price reaction > ±3% in 90 days before filing
        ("earnings_surprise_flag",  "INTEGER"),   # +1 BEAT / 0 in-line / -1 MISS
        # Form 4 enrichment — richer insider signal (log-dollar amount + officer type)
        ("insider_buy_dollars",     "REAL"),      # log10(net buy USD + 1), 0 if no net buy
        ("insider_is_ceo_cfo",      "INTEGER DEFAULT 0"),  # 1 if CEO/CFO/President is net buyer
        # Sector rotation signal — sector ETF 20d return minus SPY 20d return at filing date
        ("sector_momentum_vs_spy",  "REAL"),      # positive = sector outperforming market
    ]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_snapshots)")}
    for col, defn in new_cols:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE pipeline_snapshots ADD COLUMN {col} {defn}")
            except Exception:
                pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Progress tracking
# ══════════════════════════════════════════════════════════════════════════════

def _load_progress() -> set:
    if PROGRESS_PATH.exists():
        try:
            with open(PROGRESS_PATH) as f:
                return set(json.load(f).get("done", []))
        except Exception:
            pass
    return set()


def _save_progress(done: set):
    PROGRESS_PATH.parent.mkdir(exist_ok=True)
    with open(PROGRESS_PATH, "w") as f:
        json.dump({"done": list(done), "updated": datetime.now().isoformat()}, f)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — collect tickers from all EDGAR filings
# ══════════════════════════════════════════════════════════════════════════════

def phase1_collect_tickers(filings) -> dict:
    """
    Parse every filing's XML from EDGAR and cache the FULL holdings list
    (ticker + value_usd + shares) so Phase 3 never needs to re-fetch EDGAR.

    Cache format: {accession: [{ticker, value_usd, shares}, ...]}
    Old format (list of strings) is detected and re-processed automatically.

    Results saved to TICKER_MAP_PATH — restarts are free.
    Returns {accession: [{ticker, value_usd, shares}, ...]}.
    """
    holdings_map: dict = {}
    if TICKER_MAP_PATH.exists():
        try:
            with open(TICKER_MAP_PATH) as f:
                raw = json.load(f)
            # Detect old format (list of strings) → needs re-processing
            for acc, val in raw.items():
                if val and isinstance(val[0], dict):
                    holdings_map[acc] = val   # new format ✓
                # old format (strings) → skip, will re-process
        except Exception:
            pass

    todo = [f for f in filings if f["accession"] not in holdings_map]
    if not todo:
        n_hold = sum(len(v) for v in holdings_map.values())
        logger.info(f"  Phase 1: all {len(filings)} filings cached "
                    f"({n_hold:,} holdings total)")
        return holdings_map

    logger.info(f"  Phase 1: parsing {len(todo)} filing(s) from EDGAR "
                f"({len(holdings_map)} already cached)...")

    conn = get_db()

    for i, filing in enumerate(todo):
        if _edgar_scraper._SEC_BLOCKED:
            logger.error("  !! SEC blocked during Phase 1 — stopping.")
            break

        acc  = filing["accession"]
        cik  = filing["cik"]
        inst = filing["institution"]

        try:
            raw_holdings = parse_holdings_from_accession(cik, acc)
            parsed = []
            if raw_holdings:
                for h in raw_holdings:
                    t = h.get("ticker", "")
                    if not t:
                        t = resolve_ticker(h.get("cusip", ""), h.get("name", ""), conn)
                    if t and len(t) <= 6:
                        parsed.append({
                            "ticker":    t,
                            "value_usd": h.get("value_usd", 0),
                            "shares":    h.get("shares", 0),
                            "cusip":     h.get("cusip", ""),
                        })
            holdings_map[acc] = parsed
        except Exception as e:
            holdings_map[acc] = []
            logger.debug(f"  Phase 1 error ({inst}): {e}")

        if (i + 1) % 20 == 0 or (i + 1) == len(todo):
            with open(TICKER_MAP_PATH, "w") as f:
                json.dump(holdings_map, f)
            n_hold = sum(len(v) for v in holdings_map.values())
            logger.info(f"  Phase 1: {i+1}/{len(todo)} parsed | "
                        f"{n_hold:,} holdings cached | "
                        f"{len(holdings_map):,}/{len(filings)} filings")

    with open(TICKER_MAP_PATH, "w") as f:
        json.dump(holdings_map, f)

    all_tickers: set = set()
    for h_list in holdings_map.values():
        all_tickers.update(h["ticker"] for h in h_list)
    logger.info(f"  Phase 1 complete: {len(all_tickers):,} unique tickers | "
                f"{sum(len(v) for v in holdings_map.values()):,} total holdings")
    return holdings_map


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — bulk price pre-warm
# ══════════════════════════════════════════════════════════════════════════════

def phase2_prewarm_prices(all_tickers: set, filings) -> None:
    """
    Download price history for every unique ticker covering the full date range
    of all filings.  Stores results in the global _price_cache dict and
    optionally persists to PRICE_CACHE_PATH so restarts avoid re-downloading.
    """
    global _price_cache

    # Load from disk cache if available
    if PRICE_CACHE_PATH.exists():
        try:
            logger.info(f"  Phase 2: loading price cache from disk ({PRICE_CACHE_PATH.stat().st_size // 1024 // 1024} MB)...")
            with open(PRICE_CACHE_PATH, "rb") as f:
                _price_cache = pickle.load(f)
            logger.info(f"  Phase 2: {len(_price_cache):,} tickers loaded from disk cache")
        except Exception as e:
            logger.warning(f"  Phase 2: disk cache load failed ({e}) — re-downloading")
            _price_cache = {}

    # Find what's missing
    missing = [t for t in all_tickers if t not in _price_cache]
    if not missing:
        logger.info(f"  Phase 2: all {len(all_tickers):,} tickers already cached ✓")
        return

    # Date range: cover all filing dates with buffer for forward returns
    # filings is a list of sqlite3.Row objects — use f["col"] not f.get("col")
    dates = []
    for f in filings:
        try:
            fd = f["filing_date"]   # sqlite3.Row supports key access
            if fd:
                dates.append(date.fromisoformat(str(fd)[:10]))
        except Exception:
            pass
    start = min(dates) - timedelta(days=365 + 10)
    end   = max(dates) + timedelta(days=40)

    logger.info(
        f"  Phase 2: downloading {len(missing):,} missing tickers "
        f"({start} → {end})..."
    )

    import yfinance as yf
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    BATCH       = 300   # yfinance handles 300 tickers reliably per call
    PARALLEL    = 3     # 3 concurrent batch downloads (~3× speed)
    n_batches   = (len(missing) + BATCH - 1) // BATCH
    total_downloaded = 0
    _cache_lock = threading.Lock()

    start_s = start.isoformat()
    end_s   = end.isoformat()

    def _download_batch(batch_tickers: list, batch_num: int) -> int:
        """Download one batch and merge into _price_cache. Returns count added."""
        count = 0
        try:
            df = yf.download(
                batch_tickers,
                start=start_s,
                end=end_s,
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if df.empty:
                return 0

            close_df = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df

            local: dict = {}
            for ticker in batch_tickers:
                try:
                    if isinstance(close_df, pd.DataFrame) and ticker in close_df.columns:
                        s = close_df[ticker].dropna()
                    elif isinstance(close_df, pd.Series):
                        s = close_df.dropna()
                    else:
                        continue
                    if len(s) > 0:
                        local[ticker] = s
                        count += 1
                except Exception:
                    pass

            with _cache_lock:
                _price_cache.update(local)

        except Exception as e:
            logger.debug(f"  Phase 2 batch {batch_num} error: {e}")

        return count

    batches = [(missing[i:i + BATCH], i // BATCH + 1)
               for i in range(0, len(missing), BATCH)]

    logger.info(f"  Phase 2: {n_batches} batches × {BATCH} tickers, "
                f"{PARALLEL} parallel workers")

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futs = {pool.submit(_download_batch, b, n): n for b, n in batches}
        completed = 0
        for fut in as_completed(futs):
            completed += 1
            total_downloaded += fut.result()
            if completed % 3 == 0 or completed == n_batches:
                logger.info(
                    f"  Phase 2: {completed}/{n_batches} batches done — "
                    f"{total_downloaded:,}/{len(missing):,} tickers cached"
                )
            # Checkpoint every 9 batches so a crash doesn't lose all work
            if completed % 9 == 0:
                try:
                    with open(PRICE_CACHE_PATH, "wb") as _f:
                        pickle.dump(_price_cache, _f, protocol=4)
                except Exception:
                    pass

    logger.info(f"  Phase 2 complete: {len(_price_cache):,} tickers in price cache")

    # Persist to disk for future restarts
    try:
        logger.info(f"  Phase 2: saving price cache to disk...")
        with open(PRICE_CACHE_PATH, "wb") as f:
            pickle.dump(_price_cache, f, protocol=4)
        size_mb = PRICE_CACHE_PATH.stat().st_size // 1024 // 1024
        logger.info(f"  Phase 2: cache saved ({size_mb} MB) → {PRICE_CACHE_PATH}")
    except Exception as e:
        logger.warning(f"  Phase 2: disk cache save failed (non-fatal): {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Price helpers (cache-based, no network calls)
# ══════════════════════════════════════════════════════════════════════════════

def _px(ticker: str, target: date):
    s = _price_cache.get(ticker)
    if s is None or len(s) == 0:
        return None
    import pandas as pd
    ts = pd.Timestamp(target)
    before = s[s.index <= ts]
    return float(before.iloc[-1]) if not before.empty else None


def _fwd_ret(ticker: str, base: date, cal_days: int):
    s = _price_cache.get(ticker)
    if s is None:
        return None
    entry = _px(ticker, base)
    if not entry or entry == 0:
        return None
    exit_px = _px(ticker, base + timedelta(days=cal_days))
    if exit_px is None:
        return None
    return (exit_px - entry) / entry


def _mom20(ticker: str, base: date):
    s = _price_cache.get(ticker)
    if s is None:
        return None
    now  = _px(ticker, base)
    prev = _px(ticker, base - timedelta(days=28))
    if now and prev and prev > 0:
        return (now - prev) / prev
    return None


def _mom5(ticker: str, base: date):
    """5-day (1-week) price momentum — captures fast-moving conviction near filing date."""
    s = _price_cache.get(ticker)
    if s is None:
        return None
    now  = _px(ticker, base)
    prev = _px(ticker, base - timedelta(days=7))   # 7 calendar days ≈ 5 trading days
    if now and prev and prev > 0:
        return (now - prev) / prev
    return None


def _vs52w(ticker: str, base: date):
    s = _price_cache.get(ticker)
    if s is None or len(s) == 0:
        return None
    import pandas as pd
    ts = pd.Timestamp(base)
    window = s[(s.index >= ts - pd.Timedelta(days=365)) & (s.index <= ts)]
    if window.empty:
        return None
    high = float(window.max())
    px   = _px(ticker, base)
    if high and px and high > 0:
        return (px - high) / high
    return None


def _vol20(ticker: str, base: date):
    s = _price_cache.get(ticker)
    if s is None or len(s) == 0:
        return None
    import pandas as pd
    ts = pd.Timestamp(base)
    window = s[(s.index >= ts - pd.Timedelta(days=30)) & (s.index <= ts)]
    if len(window) < 5:
        return None
    rets = window.pct_change().dropna()
    if len(rets) < 3:
        return None
    return float(rets.std() * (252 ** 0.5))


def _rsi(ticker: str, base: date, window: int = 14) -> Optional[float]:
    """Standard Wilder RSI from the price cache. Needs ~16 trading days of history."""
    import pandas as pd
    s = _price_cache.get(ticker)
    if s is None or len(s) == 0:
        return None
    ts   = pd.Timestamp(base)
    hist = s[(s.index >= ts - pd.Timedelta(days=55)) & (s.index <= ts)]
    if len(hist) < window + 2:
        return None
    delta    = hist.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window, min_periods=window).mean().iloc[-1]
    avg_loss = loss.rolling(window, min_periods=window).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    return float(100 - 100 / (1 + avg_gain / avg_loss))


def _pct_50ma(ticker: str, base: date) -> Optional[float]:
    """% above/below 50-day moving average at base date."""
    import pandas as pd
    s = _price_cache.get(ticker)
    if s is None or len(s) == 0:
        return None
    ts   = pd.Timestamp(base)
    hist = s[(s.index >= ts - pd.Timedelta(days=90)) & (s.index <= ts)]
    if len(hist) < 40:
        return None
    ma50 = float(hist.iloc[-min(50, len(hist)):].mean())
    px   = _px(ticker, base)
    if px and ma50 > 0:
        return (px - ma50) / ma50
    return None


def _regime_bull_at(base: date) -> Optional[int]:
    """
    Market regime at base date based on SPY vs 200-day MA.
    Returns 1 (bull), -1 (bear), 0 (neutral), None if no SPY data.
    """
    import pandas as pd
    s = _price_cache.get("SPY")
    if s is None or len(s) == 0:
        return None
    ts   = pd.Timestamp(base)
    hist = s[(s.index >= ts - pd.Timedelta(days=320)) & (s.index <= ts)]
    if len(hist) < 100:
        return 0
    ma200 = float(hist.iloc[-min(200, len(hist)):].mean())
    px    = _px("SPY", base)
    if px and ma200 > 0:
        pct = (px - ma200) / ma200
        if pct > 0.02:
            return 1
        if pct < -0.05:
            return -1
    return 0


def _vix_at(base: date) -> Optional[float]:
    """VIX close on or before base date."""
    return _px("^VIX", base)


def _spy_rsi_at(base: date) -> Optional[float]:
    """SPY 14-day RSI at base date."""
    return _rsi("SPY", base)


def _oil_mom_at(base: date) -> Optional[float]:
    """USO 20-day momentum at base date (oil price proxy)."""
    return _mom20("USO", base)


def _gold_mom_at(base: date) -> Optional[float]:
    """GLD 20-day momentum at base date (gold / risk-off proxy)."""
    return _mom20("GLD", base)


def _dxy_mom_at(base: date) -> Optional[float]:
    """UUP 20-day momentum at base date (USD strength / DXY proxy)."""
    return _mom20("UUP", base)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — process filings (pure computation, cache-backed)
# ══════════════════════════════════════════════════════════════════════════════

def _process_one(filing, ticker_map: dict, fund_aum_map: dict) -> int:
    """
    Phase 3 worker: process one filing using only the pre-cached holdings map.
    ZERO EDGAR network calls — all data comes from ticker_map and _price_cache.
    """
    conn        = _get_conn()
    acc         = filing["accession"]
    institution = filing["institution"]
    fdate_str   = filing["filing_date"]
    form        = filing["form"]

    try:
        fdate = date.fromisoformat(fdate_str)
    except Exception:
        return 0

    # Get pre-resolved holdings from Phase 1 cache — NO EDGAR call
    cached_holdings = ticker_map.get(acc, [])
    if not cached_holdings:
        return 0   # filing produced no parseable holdings

    # Normalize: support both dict format and legacy string format
    h_map: dict = {}
    for item in cached_holdings:
        if isinstance(item, dict):
            h_map[item["ticker"]] = item
        else:
            h_map[item] = {"ticker": item, "value_usd": 0, "shares": 0}

    total_value = sum(h.get("value_usd", 0) for h in h_map.values())
    with _prog_lock:
        fund_aum_map[institution] = max(fund_aum_map.get(institution, 0), total_value)
    total_aum    = fund_aum_map[institution] or 1
    amendment_flag = 1 if form == "13F-HR/A" else 0
    now_ts       = datetime.now().isoformat()

    # Days-since-filing-period calculation
    try:
        period = filing["period_of_report"] if "period_of_report" in filing.keys() else ""
        days_since = (fdate - date.fromisoformat(period)).days if period else 45
    except Exception:
        days_since = 45

    rows = []
    for ticker, h in h_map.items():
        value_usd = h.get("value_usd", 0)
        pos_pct   = value_usd / total_aum

        # Skip if already exists
        if conn.execute(
            "SELECT id FROM pipeline_snapshots "
            "WHERE snapshot_date=? AND ticker=? AND institution=? AND is_historical=1",
            (fdate_str, ticker, institution)
        ).fetchone():
            continue

        # All price lookups from cache — O(1)
        entry_px     = _px(ticker, fdate)
        momentum_20d = _mom20(ticker, fdate)
        momentum_5d  = _mom5(ticker, fdate)
        vs_52w       = _vs52w(ticker, fdate)
        vol_20d      = _vol20(ticker, fdate)
        ret_1d       = _fwd_ret(ticker, fdate, 2)
        ret_5d       = _fwd_ret(ticker, fdate, 8)
        ret_20d      = _fwd_ret(ticker, fdate, 29)

        # Market context at filing date (R&D bridge features — historical backfill)
        regime_bull    = _regime_bull_at(fdate)
        vix            = _vix_at(fdate)
        spy_rsi        = _spy_rsi_at(fdate)
        ticker_rsi     = _rsi(ticker, fdate)
        ticker_pct_50ma = _pct_50ma(ticker, fdate)
        # Commodity momentum — market-wide, 100% backfillable from USO/GLD/UUP
        oil_mom        = _oil_mom_at(fdate)
        gold_mom       = _gold_mom_at(fdate)
        dxy_mom        = _dxy_mom_at(fdate)

        risk_adj_20d = None
        risk_adj_5d  = None
        if ret_20d is not None and vol_20d and vol_20d > 0:
            risk_adj_20d = ret_20d / (vol_20d / (252**0.5) * (20**0.5))
        if ret_5d is not None and vol_20d and vol_20d > 0:
            risk_adj_5d  = ret_5d  / (vol_20d / (252**0.5) * (5**0.5))

        n_inst = conn.execute(
            """SELECT COUNT(DISTINCT h2.institution)
               FROM holdings h2 JOIN filings f2 ON h2.filing_id=f2.id
               WHERE h2.ticker=? AND f2.filing_date BETWEEN ? AND ?""",
            (ticker,
             (fdate - timedelta(days=95)).isoformat(),
             (fdate + timedelta(days=5)).isoformat())
        ).fetchone()[0] or 1

        conviction = min(100.0, n_inst * 20 + value_usd / 1e7)

        rows.append((
            fdate_str, now_ts, ticker, institution, conviction,
            n_inst, "HISTORICAL_13F", days_since,
            0, 0, pos_pct, entry_px,
            ret_1d, ret_5d, ret_20d,
            1 if ret_20d is not None else 0,
            1, momentum_20d, momentum_5d, amendment_flag,
            pos_pct, vs_52w,
            risk_adj_20d, risk_adj_5d, vol_20d,
            regime_bull, vix, spy_rsi, ticker_rsi, ticker_pct_50ma,
            oil_mom, gold_mom, dxy_mom,
        ))

    if rows:
        try:
            conn.executemany(
                """INSERT OR IGNORE INTO pipeline_snapshots
                   (snapshot_date, snapshot_ts, ticker, institution, conviction_score,
                    n_institutions, signal_type, days_since_filing, herding_flag,
                    quant_consensus, portfolio_weight, entry_price,
                    outcome_1d, outcome_5d, outcome_20d, outcome_filled,
                    is_historical, price_momentum_20d, price_momentum_5d, amendment_flag,
                    position_size_pct_aum, price_vs_52w_high,
                    risk_adjusted_20d, risk_adjusted_5d, volatility_20d,
                    rd_regime_bull, rd_vix, rd_spy_rsi, rd_ticker_rsi, rd_ticker_pct_from_50ma,
                    oil_momentum_20d, gold_momentum_20d, dxy_momentum_20d)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"  Insert error ({institution} {fdate_str}): {e}")
            return 0

    return len(rows)


def phase3_label(filings, ticker_map: dict, n_workers: int = 4,
                 done: set = None, limit: int = None) -> tuple[int, int]:
    """
    Process all remaining filings using pre-warmed price cache.
    Returns (filings_processed, rows_inserted).
    """
    if done is None:
        done = _load_progress()

    todo = [f for f in filings if f["accession"] not in done]
    if limit:
        todo = todo[:limit]

    if not todo:
        logger.info("  Phase 3: nothing to process — all filings done")
        return 0, 0

    logger.info(f"  Phase 3: labelling {len(todo)} filing(s) with {n_workers} worker(s)...")

    fund_aum_map: dict = {}
    done_lock = threading.Lock()
    total_rows = 0
    total_files = 0

    def _worker(filing):
        if _edgar_scraper._SEC_BLOCKED:
            return filing["accession"], -1
        try:
            n = _process_one(filing, ticker_map, fund_aum_map)
            return filing["accession"], n
        except Exception as e:
            with _log_lock:
                logger.warning(f"  Worker error ({filing['institution']} "
                               f"{filing['filing_date']}): {e}")
            return filing["accession"], 0

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_worker, f): f for f in todo}
        for i, future in enumerate(as_completed(futures), 1):
            if _edgar_scraper._SEC_BLOCKED:
                logger.error("  !! SEC block — cancelling.")
                pool.shutdown(wait=False, cancel_futures=True)
                break

            acc, n = future.result()
            if n == -1:
                continue

            total_rows  += max(0, n)
            total_files += 1

            with done_lock:
                done.add(acc)

            f_obj = futures[future]
            with _log_lock:
                logger.info(
                    f"  Phase 3 [{i}/{len(todo)}]  "
                    f"{f_obj['institution'][:28]:<28}  "
                    f"{f_obj['filing_date']}  → {n:,}"
                )

            if total_files % 20 == 0:
                with _prog_lock:
                    _save_progress(done)
                db_lbl = _get_conn().execute(
                    "SELECT COUNT(*) FROM pipeline_snapshots "
                    "WHERE is_historical=1 AND outcome_20d IS NOT NULL"
                ).fetchone()[0]
                elapsed_rate = total_files / max(1, (
                    datetime.now() - _T0).total_seconds() / 60)
                eta_min = (len(todo) - i) / max(0.01, elapsed_rate)
                logger.info(
                    f"  ── Checkpoint: {total_rows:,} new rows | "
                    f"{db_lbl:,} labeled in DB | "
                    f"rate {elapsed_rate:.1f} f/min | "
                    f"ETA {eta_min:.0f} min ──"
                )

    _save_progress(done)
    return total_files, total_rows


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL — populate rd_* columns on existing rows
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_spy_vix_cached(filings) -> None:
    """
    Quick-download SPY and ^VIX if not already in the price cache.
    Needed when running backfill standalone (price cache may be empty).
    """
    if "SPY" in _price_cache and "^VIX" in _price_cache:
        return
    import yfinance as yf
    dates = []
    for f in filings:
        try:
            fd = f["filing_date"] if hasattr(f, "__getitem__") else getattr(f, "filing_date", None)
            if fd:
                dates.append(date.fromisoformat(str(fd)[:10]))
        except Exception:
            pass
    if not dates:
        return
    start = (min(dates) - timedelta(days=365)).isoformat()
    end   = (max(dates) + timedelta(days=40)).isoformat()
    logger.info(f"  Backfill: downloading SPY + ^VIX ({start} → {end})...")
    try:
        df = yf.download(["SPY", "^VIX"], start=start, end=end,
                         auto_adjust=True, progress=False)
        close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df
        for sym in ["SPY", "^VIX"]:
            if sym in close.columns:
                _price_cache[sym] = close[sym].dropna()
        logger.info(f"  Backfill: SPY/^VIX cached ✓")
    except Exception as e:
        logger.warning(f"  Backfill: SPY/^VIX download failed: {e}")


def backfill_rd_features(conn, filings=None, batch_size: int = 500) -> int:
    """
    Update all historical rows where rd_regime_bull IS NULL with real
    market-context values computed from the price cache.

    This is idempotent — safe to re-run; already-filled rows are skipped.
    Returns number of rows updated.
    """
    import pandas as pd

    # Ensure SPY + ^VIX are in cache
    if filings is not None:
        _ensure_spy_vix_cached(filings)

    rows_to_update = conn.execute(
        """SELECT ROWID, ticker, snapshot_date
           FROM pipeline_snapshots
           WHERE is_historical=1 AND rd_regime_bull IS NULL
           ORDER BY snapshot_date"""
    ).fetchall()

    if not rows_to_update:
        logger.info("  Backfill: all rows already have rd_* features ✓")
        return 0

    logger.info(f"  Backfill: updating {len(rows_to_update):,} rows with rd_* market features...")
    updated = 0
    errors  = 0
    batch   = []

    for rowid, ticker, snap_date in rows_to_update:
        try:
            base            = date.fromisoformat(str(snap_date)[:10])
            regime_bull     = _regime_bull_at(base)
            vix             = _vix_at(base)
            spy_rsi         = _spy_rsi_at(base)
            ticker_rsi_val  = _rsi(ticker, base)
            pct_50ma_val    = _pct_50ma(ticker, base)
            batch.append((regime_bull, vix, spy_rsi, ticker_rsi_val, pct_50ma_val, rowid))
        except Exception as e:
            errors += 1
            logger.debug(f"  Backfill compute error row {rowid} ({ticker} {snap_date}): {e}")
            continue

        if len(batch) >= batch_size:
            conn.executemany(
                """UPDATE pipeline_snapshots SET
                   rd_regime_bull=?, rd_vix=?, rd_spy_rsi=?,
                   rd_ticker_rsi=?, rd_ticker_pct_from_50ma=?
                   WHERE ROWID=?""",
                batch,
            )
            conn.commit()
            updated += len(batch)
            batch   = []
            logger.info(f"  Backfill: {updated:,}/{len(rows_to_update):,} rows updated...")

    if batch:
        conn.executemany(
            """UPDATE pipeline_snapshots SET
               rd_regime_bull=?, rd_vix=?, rd_spy_rsi=?,
               rd_ticker_rsi=?, rd_ticker_pct_from_50ma=?
               WHERE ROWID=?""",
            batch,
        )
        conn.commit()
        updated += len(batch)

    logger.info(f"  Backfill complete: {updated:,} rows updated, {errors} errors")
    return updated


def _ensure_commodity_cached(filings) -> None:
    """
    Download USO, GLD, UUP if not already in the price cache.
    Needed for commodity momentum backfill when price cache was built before these tickers were added.
    """
    needed = {"USO", "GLD", "UUP"}
    missing = needed - set(_price_cache.keys())
    if not missing:
        return
    import yfinance as yf
    dates = []
    for f in filings:
        try:
            fd = f["filing_date"] if hasattr(f, "__getitem__") else getattr(f, "filing_date", None)
            if fd:
                dates.append(date.fromisoformat(str(fd)[:10]))
        except Exception:
            pass
    if not dates:
        return
    start = (min(dates) - timedelta(days=60)).isoformat()
    end   = (max(dates) + timedelta(days=40)).isoformat()
    logger.info(f"  Commodity backfill: downloading USO/GLD/UUP ({start} → {end})...")
    try:
        df = yf.download(list(missing), start=start, end=end,
                         auto_adjust=True, progress=False)
        import pandas as pd
        close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df
        for sym in missing:
            if sym in close.columns:
                _price_cache[sym] = close[sym].dropna()
        logger.info(f"  Commodity backfill: USO/GLD/UUP cached ✓")
    except Exception as e:
        logger.warning(f"  Commodity backfill: USO/GLD/UUP download failed: {e}")


def backfill_commodity_features(conn, filings=None, batch_size: int = 500) -> int:
    """
    Update all historical rows where oil_momentum_20d IS NULL with commodity
    momentum values computed from the price cache (USO, GLD, UUP).

    Idempotent — safe to re-run; already-filled rows are skipped.
    Returns number of rows updated.
    """
    # Ensure USO, GLD, UUP are in cache
    if filings is not None:
        _ensure_commodity_cached(filings)

    rows_to_update = conn.execute(
        """SELECT ROWID, snapshot_date
           FROM pipeline_snapshots
           WHERE is_historical=1 AND oil_momentum_20d IS NULL
           ORDER BY snapshot_date"""
    ).fetchall()

    if not rows_to_update:
        logger.info("  Commodity backfill: all rows already have commodity momentum ✓")
        return 0

    logger.info(f"  Commodity backfill: updating {len(rows_to_update):,} rows with oil/gold/dxy momentum...")
    updated = 0
    errors  = 0
    batch   = []

    for rowid, snap_date in rows_to_update:
        try:
            base     = date.fromisoformat(str(snap_date)[:10])
            oil_mom  = _oil_mom_at(base)
            gold_mom = _gold_mom_at(base)
            dxy_mom  = _dxy_mom_at(base)
            batch.append((oil_mom, gold_mom, dxy_mom, rowid))
        except Exception as e:
            errors += 1
            logger.debug(f"  Commodity backfill compute error row {rowid} ({snap_date}): {e}")
            continue

        if len(batch) >= batch_size:
            conn.executemany(
                """UPDATE pipeline_snapshots SET
                   oil_momentum_20d=?, gold_momentum_20d=?, dxy_momentum_20d=?
                   WHERE ROWID=?""",
                batch,
            )
            conn.commit()
            updated += len(batch)
            batch   = []
            logger.info(f"  Commodity backfill: {updated:,}/{len(rows_to_update):,} rows updated...")

    if batch:
        conn.executemany(
            """UPDATE pipeline_snapshots SET
               oil_momentum_20d=?, gold_momentum_20d=?, dxy_momentum_20d=?
               WHERE ROWID=?""",
            batch,
        )
        conn.commit()
        updated += len(batch)

    logger.info(f"  Commodity backfill complete: {updated:,} rows updated, {errors} errors")
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL — earnings_surprise_flag using price cache large-move proxy
# ══════════════════════════════════════════════════════════════════════════════

def _earnings_surprise_from_cache(ticker: str, filing_date: date) -> int:
    """
    Compute earnings surprise proxy from the pre-warmed price cache.
    Looks for the largest single-day move > ±3% in the 90 days before filing_date.
    Returns +1 (beat), -1 (miss), 0 (in-line / no data).
    Requires the ticker to already be in _price_cache.
    """
    import pandas as pd
    s = _price_cache.get(ticker)
    if s is None or len(s) < 5:
        return 0

    ts_end   = pd.Timestamp(filing_date)
    ts_start = ts_end - pd.Timedelta(days=100)
    window   = s[(s.index >= ts_start) & (s.index <= ts_end)]
    if len(window) < 5:
        return 0

    daily_ret = window.pct_change().dropna()
    big_moves = daily_ret[daily_ret.abs() > 0.03]
    if big_moves.empty:
        return 0

    max_move = float(big_moves.loc[big_moves.abs().idxmax()])
    if max_move > 0.03:
        return 1
    elif max_move < -0.03:
        return -1
    return 0


def backfill_earnings_surprise(conn, batch_size: int = 500) -> int:
    """
    Populate earnings_surprise_flag for all historical rows where it is NULL.
    Uses the price cache — call AFTER phase2_prewarm_prices or loading disk cache.

    Returns number of rows updated.
    """
    rows_to_update = conn.execute(
        """SELECT ROWID, ticker, snapshot_date
           FROM pipeline_snapshots
           WHERE is_historical=1 AND earnings_surprise_flag IS NULL
           ORDER BY snapshot_date"""
    ).fetchall()

    if not rows_to_update:
        logger.info("  Earnings backfill: all rows already have earnings_surprise_flag ✓")
        return 0

    logger.info(f"  Earnings backfill: updating {len(rows_to_update):,} rows "
                f"with earnings_surprise_flag from price cache...")
    updated = 0
    errors  = 0
    batch   = []

    for rowid, ticker, snap_date in rows_to_update:
        try:
            base  = date.fromisoformat(str(snap_date)[:10])
            flag  = _earnings_surprise_from_cache(ticker, base)
            batch.append((flag, rowid))
        except Exception as e:
            errors += 1
            logger.debug(f"  Earnings backfill compute error row {rowid} "
                         f"({ticker} {snap_date}): {e}")
            continue

        if len(batch) >= batch_size:
            conn.executemany(
                "UPDATE pipeline_snapshots SET earnings_surprise_flag=? WHERE ROWID=?",
                batch,
            )
            conn.commit()
            updated += len(batch)
            batch   = []
            logger.info(f"  Earnings backfill: {updated:,}/{len(rows_to_update):,} rows updated...")

    if batch:
        conn.executemany(
            "UPDATE pipeline_snapshots SET earnings_surprise_flag=? WHERE ROWID=?",
            batch,
        )
        conn.commit()
        updated += len(batch)

    # Check fill rate
    filled = conn.execute(
        """SELECT COUNT(*) FROM pipeline_snapshots
           WHERE is_historical=1 AND earnings_surprise_flag IS NOT NULL"""
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM pipeline_snapshots WHERE is_historical=1"
    ).fetchone()[0]
    fill_pct = filled / max(total, 1)

    non_zero = conn.execute(
        """SELECT COUNT(*) FROM pipeline_snapshots
           WHERE is_historical=1 AND earnings_surprise_flag != 0"""
    ).fetchone()[0]

    logger.info(f"  Earnings backfill complete: {updated:,} rows updated, {errors} errors")
    logger.info(f"  Fill rate: {fill_pct:.1%} ({filled:,}/{total:,}) | "
                f"Non-zero flags: {non_zero:,} ({non_zero/max(filled,1):.1%})")
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL — insider_buy_dollars + insider_is_ceo_cfo from form4 table
# ══════════════════════════════════════════════════════════════════════════════

def backfill_form4_enrichment(conn, batch_size: int = 500) -> int:
    """
    Populate insider_buy_dollars and insider_is_ceo_cfo for historical rows
    where these columns are NULL.

    For historical rows, the form4_signals table likely has no matching data
    (form4_collector only started collecting going-forward). These will default
    to 0 — identical to the previous binary insider_buy_flag=0.

    Returns number of rows updated.
    """
    import math
    CEO_CFO_KEYWORDS = {"chief executive", "ceo", "chief financial", "cfo", "president"}

    rows_to_update = conn.execute(
        """SELECT ROWID, ticker, snapshot_date
           FROM pipeline_snapshots
           WHERE is_historical=1 AND insider_buy_dollars IS NULL
           ORDER BY snapshot_date"""
    ).fetchall()

    if not rows_to_update:
        logger.info("  Form4 enrichment backfill: all rows already have insider_buy_dollars ✓")
        return 0

    logger.info(f"  Form4 enrichment backfill: updating {len(rows_to_update):,} rows "
                f"(most will be 0 for historical data)...")
    updated = 0
    batch   = []

    for rowid, ticker, snap_date in rows_to_update:
        try:
            base   = date.fromisoformat(str(snap_date)[:10])
            start  = (base - timedelta(days=30)).isoformat()
            end    = base.isoformat()

            f4_rows = conn.execute(
                """SELECT role, shares_acquired, shares_disposed, price_per_share, value_usd
                   FROM form4_signals
                   WHERE ticker = ? AND transaction_date BETWEEN ? AND ?""",
                (ticker, start, end)
            ).fetchall()

            net_dollars = 0.0
            is_ceo_cfo  = 0
            for r in f4_rows:
                acq  = r["shares_acquired"]  or 0
                disp = r["shares_disposed"]  or 0
                val  = r["value_usd"]        or 0.0
                pps  = r["price_per_share"]  or 0.0
                role = (r["role"] or "").lower()
                if pps > 0:
                    net_dollars += (acq - disp) * pps
                else:
                    net_dollars += val if acq > disp else -val
                if acq > disp and any(kw in role for kw in CEO_CFO_KEYWORDS):
                    is_ceo_cfo = 1

            buy_dollars = 0.0
            if net_dollars > 0:
                capped = min(net_dollars, 50_000_000.0)
                buy_dollars = round(math.log10(capped + 1), 4)

            batch.append((buy_dollars, is_ceo_cfo, rowid))
        except Exception as e:
            logger.debug(f"  Form4 backfill compute error row {rowid}: {e}")
            batch.append((0.0, 0, rowid))

        if len(batch) >= batch_size:
            conn.executemany(
                """UPDATE pipeline_snapshots
                   SET insider_buy_dollars=?, insider_is_ceo_cfo=?
                   WHERE ROWID=?""",
                batch,
            )
            conn.commit()
            updated += len(batch)
            batch   = []
            logger.info(f"  Form4 enrichment backfill: {updated:,}/{len(rows_to_update):,} done...")

    if batch:
        conn.executemany(
            """UPDATE pipeline_snapshots
               SET insider_buy_dollars=?, insider_is_ceo_cfo=?
               WHERE ROWID=?""",
            batch,
        )
        conn.commit()
        updated += len(batch)

    logger.info(f"  Form4 enrichment backfill complete: {updated:,} rows updated")
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL — sector_momentum_vs_spy using price cache
# ══════════════════════════════════════════════════════════════════════════════

# GICS sector → sector ETF (mirrors feature_engineer._SECTOR_ETF_MAP)
_SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Financials":             "XLF",
    "Health Care":            "XLV",
    "Consumer Discretionary": "XLY",
    "Industrials":            "XLI",
    "Communication Services": "XLC",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
}
ALL_SECTOR_ETFS = list(_SECTOR_ETF_MAP.values())

# Cache: {ticker: sector_etf or None}
_sector_etf_cache: dict = {}


def _get_sector_etf_for_ticker(ticker: str) -> Optional[str]:
    """Look up sector ETF for a ticker using yfinance. Cached per process."""
    if ticker in _sector_etf_cache:
        return _sector_etf_cache[ticker]
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        etf  = _SECTOR_ETF_MAP.get(info.get("sector", ""))
        _sector_etf_cache[ticker] = etf
        return etf
    except Exception:
        _sector_etf_cache[ticker] = None
        return None


def _mom20_from_cache(sym: str, ref_date: date) -> Optional[float]:
    """20-day return for sym at ref_date using the shared _price_cache."""
    s = _price_cache.get(sym)
    if s is None or s.empty:
        return None
    try:
        import pandas as pd
        sub = s[s.index.date <= ref_date]
        if len(sub) < 5:
            return None
        recent = float(sub.iloc[-1])
        older  = float(sub.iloc[max(0, len(sub) - 21)])
        return (recent - older) / older if older > 0 else None
    except Exception:
        return None


def backfill_sector_momentum(conn, batch_size: int = 500) -> int:
    """
    Populate sector_momentum_vs_spy for all historical rows where it is NULL.
    Uses the shared _price_cache — call after phase2_prewarm_prices.

    For each row: looks up the ticker's GICS sector ETF, computes
    (sector_etf_20d_return - SPY_20d_return) at the snapshot_date.
    Rows with unknown sector or missing price data get 0.0.

    Returns number of rows updated.
    """
    rows_to_update = conn.execute(
        """SELECT ROWID, ticker, snapshot_date
           FROM pipeline_snapshots
           WHERE is_historical=1 AND sector_momentum_vs_spy IS NULL
           ORDER BY snapshot_date"""
    ).fetchall()

    if not rows_to_update:
        logger.info("  Sector momentum backfill: all rows already have sector_momentum_vs_spy ✓")
        return 0

    logger.info(f"  Sector momentum backfill: updating {len(rows_to_update):,} rows...")

    updated = 0
    errors  = 0
    batch   = []

    # Pre-warm sector ETF caches from price_cache if already loaded
    # (phase2 pre-warms all sector ETFs when main() adds them to all_tickers)

    for rowid, ticker, snap_date in rows_to_update:
        try:
            ref   = date.fromisoformat(str(snap_date)[:10])
            spy_r = _mom20_from_cache("SPY", ref)
            etf   = _get_sector_etf_for_ticker(ticker)
            etf_r = _mom20_from_cache(etf, ref) if etf else None

            if spy_r is not None and etf_r is not None:
                val = round(etf_r - spy_r, 6)
            else:
                val = 0.0

            batch.append((val, rowid))
        except Exception as e:
            logger.debug(f"  Sector momentum backfill error row {rowid}: {e}")
            batch.append((0.0, rowid))
            errors += 1

        if len(batch) >= batch_size:
            conn.executemany(
                "UPDATE pipeline_snapshots SET sector_momentum_vs_spy=? WHERE ROWID=?",
                batch,
            )
            conn.commit()
            updated += len(batch)
            batch    = []
            logger.info(f"  Sector momentum backfill: {updated:,}/{len(rows_to_update):,} done...")

    if batch:
        conn.executemany(
            "UPDATE pipeline_snapshots SET sector_momentum_vs_spy=? WHERE ROWID=?",
            batch,
        )
        conn.commit()
        updated += len(batch)

    logger.info(
        f"  Sector momentum backfill complete: {updated:,} rows updated, {errors} errors"
    )
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

_T0 = datetime.now()


def run(limit: int = None, fund_filter: str = None,
        n_workers: int = 4, skip_prewarm: bool = False) -> dict:

    logger.info("=" * 65)
    logger.info("  LABEL BUILDER FAST v2  (EDGAR→cache→parallel label)")
    logger.info(f"  Workers: {n_workers}  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 65)

    global _T0
    _T0 = datetime.now()

    # ── Setup ────────────────────────────────────────────────────────────────
    conn = get_db()
    _ensure_collector_tables(conn)
    _migrate_schema(conn)

    query  = "SELECT * FROM filings ORDER BY filing_date DESC"
    params: list = []
    if fund_filter:
        query  = "SELECT * FROM filings WHERE institution LIKE ? ORDER BY filing_date DESC"
        params = [f"%{fund_filter}%"]

    filings = conn.execute(query, params).fetchall()
    if limit:
        filings = filings[:limit]

    done = _load_progress()
    todo = [f for f in filings if f["accession"] not in done]
    logger.info(f"  {len(filings)} total | {len(done)} done | {len(todo)} remaining")

    if not todo:
        logger.info("  Nothing to do.")
        return {"status": "DONE", "rows_inserted": 0, "filings_processed": 0}

    # ── Phase 1: collect tickers ──────────────────────────────────────────
    ticker_map = phase1_collect_tickers(filings)

    all_tickers: set = set()
    for h_list in ticker_map.values():
        for h in h_list:
            if isinstance(h, dict):
                all_tickers.add(h["ticker"])
            else:
                all_tickers.add(h)   # old string format fallback
    # Always include market context tickers needed for rd_* and commodity feature backfill
    # Also include all sector ETFs so sector_momentum_vs_spy can be backfilled from cache
    all_tickers.update({"SPY", "^VIX", "USO", "GLD", "UUP"} | set(ALL_SECTOR_ETFS))
    logger.info(f"  Unique tickers across all filings: {len(all_tickers):,} (+ SPY/^VIX/USO/GLD/UUP)")

    # ── Phase 2: bulk price pre-warm ─────────────────────────────────────
    if not skip_prewarm:
        phase2_prewarm_prices(all_tickers, filings)
    else:
        logger.info("  Phase 2: skipped (--skip-prewarm)")

    # ── Phase 3: parallel label computation ──────────────────────────────
    n_files, n_rows = phase3_label(
        filings, ticker_map,
        n_workers=n_workers, done=done,
    )

    # ── Backfill rd_* features on any rows missing them ───────────────────
    # Runs automatically after phase 3; also safe to run standalone with --backfill-rd
    backfill_rd_features(conn, filings=filings)

    # ── Backfill commodity momentum features ──────────────────────────────
    # USO/GLD/UUP are now in all_tickers so phase2 should have them in cache
    backfill_commodity_features(conn, filings=filings)

    # ── Backfill earnings surprise flag (price-cache based) ───────────────
    backfill_earnings_surprise(conn)

    # ── Backfill Form 4 enrichment (insider_buy_dollars + insider_is_ceo_cfo)
    backfill_form4_enrichment(conn)

    # ── Backfill sector rotation vs SPY ──────────────────────────────────────
    # Sector ETFs were included in all_tickers so phase2 pre-warmed them
    backfill_sector_momentum(conn)

    db_total  = _get_conn().execute(
        "SELECT COUNT(*) FROM pipeline_snapshots WHERE is_historical=1"
    ).fetchone()[0]
    labeled   = _get_conn().execute(
        "SELECT COUNT(*) FROM pipeline_snapshots "
        "WHERE is_historical=1 AND outcome_20d IS NOT NULL"
    ).fetchone()[0]

    elapsed = (datetime.now() - _T0).total_seconds() / 60
    logger.info("=" * 65)
    logger.info(f"  DONE — {n_files} filings | {n_rows:,} new rows | {elapsed:.0f} min elapsed")
    logger.info(f"  DB total: {db_total:,}  |  Labeled: {labeled:,}")
    logger.info("=" * 65)

    return {
        "status":            "DONE",
        "filings_processed": n_files,
        "rows_inserted":     n_rows,
        "db_total":          db_total,
        "labeled":           labeled,
        "elapsed_min":       round(elapsed, 1),
        "completed_at":      datetime.now().isoformat(),
    }


if __name__ == "__main__":
    import pandas as pd   # needed by backfill helpers at module level

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",        type=int,  default=None)
    parser.add_argument("--fund",         type=str,  default=None)
    parser.add_argument("--workers",      type=int,  default=4)
    parser.add_argument("--skip-prewarm", action="store_true")
    parser.add_argument("--backfill-rd",  action="store_true",
                        help="Only backfill rd_* market features on existing rows (skip label building)")
    parser.add_argument("--backfill-commodity", action="store_true",
                        help="Only backfill oil/gold/dxy momentum on existing rows (skip label building)")
    parser.add_argument("--backfill-earnings", action="store_true",
                        help="Only backfill earnings_surprise_flag on existing rows (needs price cache)")
    parser.add_argument("--backfill-form4", action="store_true",
                        help="Only backfill insider_buy_dollars + insider_is_ceo_cfo on existing rows")
    parser.add_argument("--backfill-sector-momentum", action="store_true",
                        help="Only backfill sector_momentum_vs_spy on existing rows (needs price cache)")
    args = parser.parse_args()

    os.chdir(Path(__file__).parent.parent)

    if args.backfill_rd:
        # Standalone backfill mode — load price cache + update existing rows
        conn = get_db()
        _ensure_collector_tables(conn)
        _migrate_schema(conn)
        filings = conn.execute("SELECT * FROM filings ORDER BY filing_date").fetchall()

        # Load disk price cache (fastest path); download SPY/^VIX if missing
        if PRICE_CACHE_PATH.exists():
            logger.info(f"  Loading price cache from disk...")
            with open(PRICE_CACHE_PATH, "rb") as f:
                _price_cache.update(pickle.load(f))
            logger.info(f"  {len(_price_cache):,} tickers loaded from cache")

        _ensure_spy_vix_cached(filings)
        n = backfill_rd_features(conn, filings=filings)
        print(json.dumps({"status": "BACKFILL_DONE", "rows_updated": n}, indent=2))
    elif args.backfill_commodity:
        # Standalone commodity backfill — download USO/GLD/UUP and update existing rows
        conn = get_db()
        _ensure_collector_tables(conn)
        _migrate_schema(conn)
        filings = conn.execute("SELECT * FROM filings ORDER BY filing_date").fetchall()

        # Load disk price cache (USO/GLD/UUP may already be there if cache is recent)
        if PRICE_CACHE_PATH.exists():
            logger.info(f"  Loading price cache from disk...")
            with open(PRICE_CACHE_PATH, "rb") as f:
                _price_cache.update(pickle.load(f))
            logger.info(f"  {len(_price_cache):,} tickers loaded from cache")

        n = backfill_commodity_features(conn, filings=filings)
        print(json.dumps({"status": "COMMODITY_BACKFILL_DONE", "rows_updated": n}, indent=2))
    elif args.backfill_earnings:
        # Standalone earnings backfill — uses price cache to find large single-day moves
        conn = get_db()
        _ensure_collector_tables(conn)
        _migrate_schema(conn)

        # Load disk price cache
        if PRICE_CACHE_PATH.exists():
            logger.info(f"  Loading price cache from disk...")
            with open(PRICE_CACHE_PATH, "rb") as f:
                _price_cache.update(pickle.load(f))
            logger.info(f"  {len(_price_cache):,} tickers loaded from cache")
        else:
            logger.warning("  No price cache found — earnings backfill will use sparse yfinance fallback")

        n = backfill_earnings_surprise(conn)
        print(json.dumps({"status": "EARNINGS_BACKFILL_DONE", "rows_updated": n}, indent=2))
    elif args.backfill_form4:
        # Standalone Form 4 enrichment backfill
        conn = get_db()
        _ensure_collector_tables(conn)
        _migrate_schema(conn)
        n = backfill_form4_enrichment(conn)
        print(json.dumps({"status": "FORM4_ENRICHMENT_BACKFILL_DONE", "rows_updated": n}, indent=2))
    elif args.backfill_sector_momentum:
        # Standalone sector momentum backfill — load price cache + download sector ETFs if missing
        conn = get_db()
        _ensure_collector_tables(conn)
        _migrate_schema(conn)
        if PRICE_CACHE_PATH.exists():
            logger.info(f"  Loading price cache from disk...")
            with open(PRICE_CACHE_PATH, "rb") as f:
                _price_cache.update(pickle.load(f))
            logger.info(f"  {len(_price_cache):,} tickers loaded from cache")
        # Download any sector ETFs not yet in cache
        missing_etfs = [e for e in ALL_SECTOR_ETFS if e not in _price_cache]
        if missing_etfs or "SPY" not in _price_cache:
            logger.info(f"  Downloading {len(missing_etfs)+1} sector ETFs + SPY...")
            filings = conn.execute("SELECT * FROM filings ORDER BY filing_date").fetchall()
            phase2_prewarm_prices(set(missing_etfs) | {"SPY"}, filings)
        n = backfill_sector_momentum(conn)
        print(json.dumps({"status": "SECTOR_MOMENTUM_BACKFILL_DONE", "rows_updated": n}, indent=2))
    else:
        result = run(
            limit=args.limit,
            fund_filter=args.fund,
            n_workers=args.workers,
            skip_prewarm=args.skip_prewarm,
        )
        print(json.dumps(result, indent=2, default=str))
