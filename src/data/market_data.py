"""
Market Data Provider — Data Engineering Team
Fetches OHLCV, fundamentals, and volume data via yfinance.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)


class MarketDataProvider:
    """
    Provides price/volume data and fundamental metrics.
    Primary data source: Yahoo Finance (yfinance).

    In production, upgrade to Polygon.io, Bloomberg, or Refinitiv
    for real-time feeds and higher reliability.
    """

    def __init__(self, cache_ttl_minutes: int = 60):
        self._cache: Dict[str, Tuple[pd.DataFrame, datetime]] = {}
        self._cache_ttl = timedelta(minutes=cache_ttl_minutes)

    def get_ohlcv(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data for a single ticker.
        Returns DataFrame with columns: Open, High, Low, Close, Volume, Adj Close
        """
        cache_key = f"{ticker}_{start}_{end}_{interval}"
        if cache_key in self._cache:
            df, ts = self._cache[cache_key]
            if datetime.now() - ts < self._cache_ttl:
                return df

        try:
            df = yf.download(
                ticker,
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if df.empty:
                logger.warning(f"No data returned for {ticker}")
                return pd.DataFrame()

            df.index = pd.to_datetime(df.index)
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            self._cache[cache_key] = (df, datetime.now())
            return df

        except Exception as e:
            logger.error(f"Failed to fetch OHLCV for {ticker}: {e}")
            return pd.DataFrame()

    def get_multiple_ohlcv(
        self,
        tickers: List[str],
        start: str,
        end: str,
    ) -> Dict[str, pd.DataFrame]:
        """Batch-fetch OHLCV for multiple tickers."""
        result = {}
        # Download in batches to avoid rate limits
        batch_size = 20
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            try:
                raw = yf.download(
                    batch,
                    start=start,
                    end=end,
                    auto_adjust=True,
                    progress=False,
                    group_by="ticker",
                )
                if raw.empty:
                    continue
                for ticker in batch:
                    if len(batch) == 1:
                        result[ticker] = raw
                    else:
                        if ticker in raw.columns.get_level_values(0):
                            result[ticker] = raw[ticker].dropna()
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Batch download failed for {batch}: {e}")

        return result

    def get_close_prices(
        self, tickers: List[str], start: str, end: str
    ) -> pd.DataFrame:
        """
        Returns a DataFrame of adjusted close prices, one column per ticker.
        """
        try:
            raw = yf.download(
                tickers,
                start=start,
                end=end,
                auto_adjust=True,
                progress=False,
            )
            if raw.empty:
                return pd.DataFrame()

            if isinstance(raw.columns, pd.MultiIndex):
                return raw["Close"].dropna(how="all")
            else:
                return raw[["Close"]].rename(columns={"Close": tickers[0]})

        except Exception as e:
            logger.error(f"Failed to fetch close prices: {e}")
            return pd.DataFrame()

    def get_volume_profile(self, ticker: str, lookback_days: int = 60) -> dict:
        """
        Compute volume statistics used for block detection:
        - Average daily volume (ADV)
        - Volume standard deviation
        - Recent relative volume
        """
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
        df = self.get_ohlcv(ticker, start, end)

        if df.empty or "Volume" not in df.columns:
            return {}

        adv = df["Volume"].rolling(20).mean().iloc[-1]
        vol_std = df["Volume"].rolling(20).std().iloc[-1]
        recent_vol = df["Volume"].iloc[-1]
        relative_vol = recent_vol / adv if adv > 0 else 0

        return {
            "ticker": ticker,
            "adv_20d": adv,
            "vol_std_20d": vol_std,
            "last_volume": recent_vol,
            "relative_volume": relative_vol,
            "vwap_last": self._compute_vwap(df.tail(1)).iloc[-1] if not df.empty else None,
        }

    def _compute_vwap(self, df: pd.DataFrame) -> pd.Series:
        """Compute Volume Weighted Average Price."""
        if df.empty:
            return pd.Series()
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = (typical_price * df["Volume"]).cumsum() / df["Volume"].cumsum()
        return vwap

    def get_fundamentals(self, ticker: str) -> dict:
        """
        Fetch key fundamental metrics for a stock.
        """
        try:
            t = yf.Ticker(ticker)
            info = t.info
            return {
                "ticker": ticker,
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "pb_ratio": info.get("priceToBook"),
                "ps_ratio": info.get("priceToSalesTrailing12Months"),
                "dividend_yield": info.get("dividendYield"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "beta": info.get("beta"),
                "short_ratio": info.get("shortRatio"),
                "avg_volume": info.get("averageVolume"),
                "float_shares": info.get("floatShares"),
                "institutional_ownership_pct": info.get("heldPercentInstitutions", 0) * 100,
            }
        except Exception as e:
            logger.debug(f"Failed to fetch fundamentals for {ticker}: {e}")
            return {"ticker": ticker}

    def compute_returns(self, prices: pd.DataFrame) -> pd.DataFrame:
        """Compute daily log returns from price DataFrame."""
        return np.log(prices / prices.shift(1)).dropna()

    def compute_rolling_metrics(
        self, ticker: str, start: str, end: str
    ) -> pd.DataFrame:
        """
        Compute rolling metrics useful for signal generation:
        - 20d, 50d, 200d SMA
        - RSI(14)
        - ATR(14)
        - Bollinger Bands
        - On-Balance Volume (OBV)
        """
        df = self.get_ohlcv(ticker, start, end)
        if df.empty:
            return pd.DataFrame()

        close = df["Close"]
        volume = df["Volume"]

        # Moving averages
        df["sma20"] = close.rolling(20).mean()
        df["sma50"] = close.rolling(50).mean()
        df["sma200"] = close.rolling(200).mean()

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-10)
        df["rsi14"] = 100 - (100 / (1 + rs))

        # ATR
        high_low = df["High"] - df["Low"]
        high_close = (df["High"] - close.shift()).abs()
        low_close = (df["Low"] - close.shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14).mean()

        # Bollinger Bands
        df["bb_mid"] = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * bb_std
        df["bb_lower"] = df["bb_mid"] - 2 * bb_std

        # OBV
        obv = [0]
        for i in range(1, len(df)):
            if close.iloc[i] > close.iloc[i - 1]:
                obv.append(obv[-1] + volume.iloc[i])
            elif close.iloc[i] < close.iloc[i - 1]:
                obv.append(obv[-1] - volume.iloc[i])
            else:
                obv.append(obv[-1])
        df["obv"] = obv

        # Relative volume (vs 20d avg)
        df["rel_volume"] = volume / volume.rolling(20).mean()

        return df
