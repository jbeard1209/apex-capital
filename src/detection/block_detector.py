"""
Block Purchase Detector — Research Team
Identifies stealth institutional accumulation in market data.

Key concept: Large institutions (Citadel, Renaissance, etc.) break large
orders into smaller "blocks" to avoid price impact. We detect this pattern
by looking for:
  1. Elevated volume with minimal price movement (stealth accumulation)
  2. Consistent buying pressure over multiple days
  3. Volume spikes on flat/slightly down days (institutions buying dips)
  4. Divergence between price and OBV (accumulation signal)
  5. VWAP deviations suggesting large buyer presence
"""

import logging
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class BlockPurchaseSignal:
    """Represents a detected institutional block purchase signal."""

    def __init__(
        self,
        ticker: str,
        date: datetime,
        signal_type: str,
        confidence: float,
        volume: float,
        rel_volume: float,
        price: float,
        price_change_pct: float,
        notes: str = "",
    ):
        self.ticker = ticker
        self.date = date
        self.signal_type = signal_type   # 'STEALTH_BUY', 'ACCUMULATION', 'BREAKOUT_BUY'
        self.confidence = confidence      # 0.0 to 1.0
        self.volume = volume
        self.rel_volume = rel_volume
        self.price = price
        self.price_change_pct = price_change_pct
        self.notes = notes

    def __repr__(self):
        return (
            f"BlockSignal({self.ticker} | {self.signal_type} | "
            f"Conf={self.confidence:.2f} | "
            f"RelVol={self.rel_volume:.1f}x | "
            f"PriceΔ={self.price_change_pct:+.2f}% | "
            f"{self.date.strftime('%Y-%m-%d') if hasattr(self.date, 'strftime') else self.date})"
        )

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "date": str(self.date),
            "signal_type": self.signal_type,
            "confidence": round(self.confidence, 4),
            "volume": int(self.volume),
            "rel_volume": round(self.rel_volume, 2),
            "price": round(self.price, 2),
            "price_change_pct": round(self.price_change_pct, 4),
            "notes": self.notes,
        }


class BlockDetector:
    """
    Multi-signal block purchase detector.

    Detection methods:
    1. Stealth Buy: High volume + small price move (institutional absorption)
    2. Accumulation Pattern: Consistent volume above ADV over N days
    3. OBV Divergence: Price flat/down but OBV rising (hidden demand)
    4. VWAP Anchor: Price consistently trades above VWAP (buyer-controlled)
    5. Dip Buying: Volume spikes on down-price days (institutions buying dips)
    """

    def __init__(
        self,
        min_block_shares: int = 10_000,
        volume_spike_multiplier: float = 2.0,
        avg_volume_window: int = 20,
        accumulation_days: int = 5,
        stealth_buy_max_price_change_pct: float = 0.5,
    ):
        self.min_block_shares = min_block_shares
        self.volume_spike_multiplier = volume_spike_multiplier
        self.avg_volume_window = avg_volume_window
        self.accumulation_days = accumulation_days
        self.stealth_max_price_change = stealth_buy_max_price_change_pct

    def detect_all(self, ticker: str, df: pd.DataFrame) -> List[BlockPurchaseSignal]:
        """
        Run all detection methods on OHLCV data.
        Returns list of signals, deduplicated by date.
        """
        if df.empty or len(df) < self.avg_volume_window + 10:
            return []

        df = self._prepare(df)
        signals = []

        signals.extend(self._detect_stealth_buy(ticker, df))
        signals.extend(self._detect_accumulation_pattern(ticker, df))
        signals.extend(self._detect_obv_divergence(ticker, df))
        signals.extend(self._detect_dip_buying(ticker, df))
        signals.extend(self._detect_vwap_anchoring(ticker, df))

        # Deduplicate: keep highest-confidence signal per date
        seen: Dict[str, BlockPurchaseSignal] = {}
        for sig in signals:
            key = str(sig.date)
            if key not in seen or sig.confidence > seen[key].confidence:
                seen[key] = sig

        return sorted(seen.values(), key=lambda s: str(s.date), reverse=True)

    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add computed columns needed for detection."""
        df = df.copy()
        close = df["Close"]
        volume = df["Volume"]

        df["adv"] = volume.rolling(self.avg_volume_window).mean()
        df["rel_vol"] = volume / df["adv"]
        df["price_change_pct"] = close.pct_change() * 100
        df["dollar_volume"] = close * volume

        # OBV
        obv = [0.0]
        for i in range(1, len(df)):
            if close.iloc[i] > close.iloc[i - 1]:
                obv.append(obv[-1] + volume.iloc[i])
            elif close.iloc[i] < close.iloc[i - 1]:
                obv.append(obv[-1] - volume.iloc[i])
            else:
                obv.append(obv[-1])
        df["obv"] = obv

        # Intraday VWAP proxy (typical price * volume cumulative)
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        df["typical_price"] = typical

        # 5-day OBV trend
        df["obv_5d_change"] = df["obv"].diff(5)
        df["price_5d_change"] = close.diff(5) / close.shift(5) * 100

        return df

    def _detect_stealth_buy(
        self, ticker: str, df: pd.DataFrame
    ) -> List[BlockPurchaseSignal]:
        """
        Stealth Buy: Volume > X * ADV but price barely moves.
        Classic signature of an institution absorbing supply quietly.
        """
        signals = []
        for i in range(self.avg_volume_window, len(df)):
            row = df.iloc[i]
            if pd.isna(row["adv"]) or row["adv"] == 0:
                continue

            vol_ok = row["rel_vol"] >= self.volume_spike_multiplier
            price_ok = abs(row["price_change_pct"]) <= self.stealth_max_price_change
            size_ok = row["Volume"] >= self.min_block_shares

            if vol_ok and price_ok and size_ok:
                # Confidence increases with rel_vol and decreases with price move
                confidence = min(
                    0.95,
                    0.5 + (row["rel_vol"] - self.volume_spike_multiplier) * 0.1
                    - abs(row["price_change_pct"]) * 0.05,
                )
                signals.append(BlockPurchaseSignal(
                    ticker=ticker,
                    date=df.index[i],
                    signal_type="STEALTH_BUY",
                    confidence=max(0.1, confidence),
                    volume=row["Volume"],
                    rel_volume=row["rel_vol"],
                    price=row["Close"],
                    price_change_pct=row["price_change_pct"],
                    notes=(
                        f"Vol={row['Volume']:,.0f} ({row['rel_vol']:.1f}x ADV), "
                        f"price Δ={row['price_change_pct']:+.2f}%"
                    ),
                ))
        return signals

    def _detect_accumulation_pattern(
        self, ticker: str, df: pd.DataFrame
    ) -> List[BlockPurchaseSignal]:
        """
        Accumulation Pattern: N consecutive days of above-average volume
        with flat-to-positive price action.
        """
        signals = []
        n = self.accumulation_days
        min_vol_ratio = 1.3  # At least 1.3x ADV for each of the N days

        for i in range(self.avg_volume_window + n, len(df)):
            window = df.iloc[i - n : i]
            if window["adv"].isna().any():
                continue

            # All days have elevated volume
            all_elevated = (window["rel_vol"] >= min_vol_ratio).all()
            # Net price change is relatively small (accumulation, not breakout)
            price_start = df.iloc[i - n]["Close"]
            price_end = df.iloc[i]["Close"]
            net_change = (price_end - price_start) / price_start * 100

            # Accumulation: elevated volume, price between -3% and +5%
            if all_elevated and -3 <= net_change <= 5:
                avg_rel_vol = window["rel_vol"].mean()
                confidence = min(0.90, 0.55 + (avg_rel_vol - 1.3) * 0.1)
                signals.append(BlockPurchaseSignal(
                    ticker=ticker,
                    date=df.index[i],
                    signal_type="ACCUMULATION",
                    confidence=max(0.1, confidence),
                    volume=window["Volume"].sum(),
                    rel_volume=avg_rel_vol,
                    price=df.iloc[i]["Close"],
                    price_change_pct=net_change,
                    notes=(
                        f"{n}-day accum: avg vol={avg_rel_vol:.1f}x ADV, "
                        f"net price Δ={net_change:+.2f}%"
                    ),
                ))
        return signals

    def _detect_obv_divergence(
        self, ticker: str, df: pd.DataFrame
    ) -> List[BlockPurchaseSignal]:
        """
        OBV Divergence: OBV rising while price is flat or falling.
        This is a classic "hidden accumulation" signal — volume is going
        in at the ask (buyers), but price isn't moving yet.
        """
        signals = []
        for i in range(self.avg_volume_window + 5, len(df)):
            row = df.iloc[i]
            if pd.isna(row.get("obv_5d_change")) or pd.isna(row.get("price_5d_change")):
                continue

            obv_rising = row["obv_5d_change"] > 0
            price_flat_or_down = row["price_5d_change"] <= 1.0  # price up less than 1%

            if obv_rising and price_flat_or_down:
                # Normalize OBV change
                obv_pct = row["obv_5d_change"] / (abs(row["obv"]) + 1) * 100
                confidence = min(0.85, 0.45 + obv_pct * 0.5)
                if confidence < 0.5:
                    continue

                signals.append(BlockPurchaseSignal(
                    ticker=ticker,
                    date=df.index[i],
                    signal_type="OBV_DIVERGENCE",
                    confidence=max(0.1, confidence),
                    volume=row["Volume"],
                    rel_volume=row["rel_vol"],
                    price=row["Close"],
                    price_change_pct=row["price_5d_change"],
                    notes=(
                        f"OBV 5d Δ={row['obv_5d_change']:+,.0f}, "
                        f"price 5d Δ={row['price_5d_change']:+.2f}%"
                    ),
                ))
        return signals

    def _detect_dip_buying(
        self, ticker: str, df: pd.DataFrame
    ) -> List[BlockPurchaseSignal]:
        """
        Dip Buying: Price drops but volume spikes — institutions buying weakness.
        This is how large firms accumulate without moving price upward.
        """
        signals = []
        for i in range(self.avg_volume_window, len(df)):
            row = df.iloc[i]
            if pd.isna(row["adv"]):
                continue

            price_down = row["price_change_pct"] < -0.3  # at least -0.3% down day
            vol_spike = row["rel_vol"] >= 1.5
            close_recovery = (
                row["Close"] > (row["Low"] + (row["High"] - row["Low"]) * 0.5)
            )  # Closed in upper half of range (buying into dip)

            if price_down and vol_spike and close_recovery:
                confidence = min(0.88, 0.50 + row["rel_vol"] * 0.05)
                signals.append(BlockPurchaseSignal(
                    ticker=ticker,
                    date=df.index[i],
                    signal_type="DIP_BUY",
                    confidence=max(0.1, confidence),
                    volume=row["Volume"],
                    rel_volume=row["rel_vol"],
                    price=row["Close"],
                    price_change_pct=row["price_change_pct"],
                    notes=(
                        f"Dip: price Δ={row['price_change_pct']:+.2f}%, "
                        f"vol={row['rel_vol']:.1f}x, closed in upper half"
                    ),
                ))
        return signals

    def _detect_vwap_anchoring(
        self, ticker: str, df: pd.DataFrame
    ) -> List[BlockPurchaseSignal]:
        """
        VWAP Anchoring: Institutional buyers anchor price above VWAP,
        signaling they are defending a price level.
        Uses a rolling 5-day VWAP proxy.
        """
        signals = []
        # 5-day rolling VWAP proxy
        df = df.copy()
        df["vwap_5d"] = (
            (df["typical_price"] * df["Volume"]).rolling(5).sum()
            / df["Volume"].rolling(5).sum()
        )

        for i in range(self.avg_volume_window + 5, len(df)):
            row = df.iloc[i]
            if pd.isna(row.get("vwap_5d")):
                continue

            above_vwap = row["Close"] > row["vwap_5d"]
            vol_elevated = row["rel_vol"] >= 1.2

            if above_vwap and vol_elevated:
                vwap_premium = (row["Close"] - row["vwap_5d"]) / row["vwap_5d"] * 100
                confidence = min(0.80, 0.40 + row["rel_vol"] * 0.04 + vwap_premium * 0.02)
                if confidence < 0.50:
                    continue

                signals.append(BlockPurchaseSignal(
                    ticker=ticker,
                    date=df.index[i],
                    signal_type="VWAP_ANCHOR",
                    confidence=max(0.1, confidence),
                    volume=row["Volume"],
                    rel_volume=row["rel_vol"],
                    price=row["Close"],
                    price_change_pct=row["price_change_pct"],
                    notes=(
                        f"Price {vwap_premium:+.2f}% above 5d VWAP, "
                        f"vol={row['rel_vol']:.1f}x"
                    ),
                ))
        return signals

    def score_ticker(self, ticker: str, df: pd.DataFrame) -> dict:
        """
        Generate a comprehensive institutional interest score for a ticker.
        Returns a dict with composite score and signal breakdown.
        """
        signals = self.detect_all(ticker, df)
        if not signals:
            return {
                "ticker": ticker,
                "composite_score": 0.0,
                "signal_count": 0,
                "signals": [],
            }

        # Score by signal type weights
        weights = {
            "STEALTH_BUY": 1.0,
            "ACCUMULATION": 0.9,
            "OBV_DIVERGENCE": 0.8,
            "DIP_BUY": 0.85,
            "VWAP_ANCHOR": 0.7,
        }

        # Only look at recent signals (last 30 days)
        if hasattr(df.index[-1], "to_pydatetime"):
            cutoff = df.index[-1] - pd.Timedelta(days=30)
            recent = [s for s in signals if pd.Timestamp(s.date) >= cutoff]
        else:
            recent = signals[:10]

        if not recent:
            return {"ticker": ticker, "composite_score": 0.0, "signal_count": 0, "signals": []}

        composite = sum(
            s.confidence * weights.get(s.signal_type, 0.5) for s in recent
        ) / max(len(recent), 1)

        return {
            "ticker": ticker,
            "composite_score": round(min(1.0, composite), 4),
            "signal_count": len(recent),
            "signals": [s.to_dict() for s in recent],
        }
