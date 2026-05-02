# =============================================================================
# ALPACA TRADER — Paper Trading Integration
# =============================================================================
# Connects to Alpaca's paper trading API to execute buy/sell orders,
# monitor positions, enforce stop-loss / take-profit rules, and
# report daily portfolio state.
# =============================================================================

import logging
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

import yfinance as yf
import numpy as np

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    STARTING_CAPITAL, MAX_POSITION_SIZE_PCT, STOP_LOSS_PCT,
    TAKE_PROFIT_PCT, MAX_OPEN_POSITIONS, HOLD_DAYS_MAX,
    HEDGE_ETF, HEDGE_POSITION_PCT, SHORT_POSITION_SIZE_PCT,
    MAX_SHORT_POSITIONS, SHORT_CANDIDATES,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alpaca Client Wrapper
# ---------------------------------------------------------------------------

class AlpacaTrader:
    """
    Wraps the alpaca-py SDK for paper trading operations.
    """

    def __init__(self):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import (
                MarketOrderRequest, LimitOrderRequest,
                GetAssetsRequest, GetOrdersRequest,
            )
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, AssetClass
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest

            self.client = TradingClient(
                api_key=ALPACA_API_KEY,
                secret_key=ALPACA_SECRET_KEY,
                paper=True,
            )
            self.data_client = StockHistoricalDataClient(
                api_key=ALPACA_API_KEY,
                secret_key=ALPACA_SECRET_KEY,
            )

            # Store class references
            self._MarketOrderRequest  = MarketOrderRequest
            self._GetOrdersRequest    = GetOrdersRequest
            self._StockLatestQuoteRequest = StockLatestQuoteRequest
            self._OrderSide           = OrderSide
            self._TimeInForce         = TimeInForce
            self._OrderStatus         = OrderStatus

            # ── Paper vs live safeguard ───────────────────────────────────
            # Hard assertion: if the URL doesn't contain "paper", abort immediately.
            # This prevents accidentally running the live trading bot against real money
            # if someone changes ALPACA_BASE_URL in config.py or the .env file.
            if "paper" not in ALPACA_BASE_URL.lower():
                raise RuntimeError(
                    f"SAFETY ABORT: ALPACA_BASE_URL='{ALPACA_BASE_URL}' does not contain 'paper'. "
                    f"This system is configured for paper trading only. "
                    f"To use live trading, remove this assertion deliberately."
                )

            logger.info("Alpaca paper trading client initialized")
            logger.info(f"  Trading mode: PAPER (URL: {ALPACA_BASE_URL})")
        except ImportError:
            logger.error("alpaca-py not installed. Run: pip install alpaca-py")
            raise
        except Exception as e:
            logger.error(f"Alpaca init error: {e}")
            raise

    # -----------------------------------------------------------------------
    # Account Info
    # -----------------------------------------------------------------------

    def get_account(self) -> dict:
        """Return account summary as a dict."""
        acct = self.client.get_account()
        return {
            "portfolio_value": float(acct.portfolio_value),
            "cash":            float(acct.cash),
            "buying_power":    float(acct.buying_power),
            "equity":          float(acct.equity),
            "last_equity":     float(acct.last_equity),
            "pnl_today":       float(acct.equity) - float(acct.last_equity),
            "pnl_today_pct":   ((float(acct.equity) / float(acct.last_equity)) - 1) * 100
                               if float(acct.last_equity) > 0 else 0.0,
        }

    def get_positions(self) -> pd.DataFrame:
        """Return all open positions as a DataFrame."""
        positions = self.client.get_all_positions()
        if not positions:
            return pd.DataFrame()

        rows = []
        for p in positions:
            rows.append({
                "ticker":        p.symbol,
                "qty":           float(p.qty),
                "avg_entry":     float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value":  float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_pct":float(p.unrealized_plpc) * 100,
                "side":          p.side,
            })
        return pd.DataFrame(rows)

    # -----------------------------------------------------------------------
    # Price Fetching
    # -----------------------------------------------------------------------

    def get_latest_price(self, ticker: str) -> Optional[float]:
        """Get the latest ask price for a ticker."""
        try:
            req  = self._StockLatestQuoteRequest(symbol_or_symbols=ticker)
            data = self.data_client.get_stock_latest_quote(req)
            return float(data[ticker].ask_price)
        except Exception as e:
            logger.warning(f"Could not get price for {ticker}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Order Execution
    # -----------------------------------------------------------------------

    def get_volatility(self, ticker: str, window: int = 20) -> float:
        """
        Fetch 20-day annualised historical volatility for a ticker.
        Returns a float (e.g. 0.30 = 30% annualised vol).
        Falls back to 0.30 if data unavailable (conservative default).
        """
        try:
            df = yf.download(ticker, period="60d", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if len(df) < window + 1:
                return 0.30
            returns = df["Close"].pct_change().dropna()
            daily_std = float(returns.tail(window).std())
            return daily_std * (252 ** 0.5)   # annualise
        except Exception:
            return 0.30   # fallback: assume 30% annualised vol

    def calculate_position_size(self, ticker: str,
                                conviction_score: float = 1.0) -> tuple[int, float]:
        """
        Volatility-adjusted, conviction-weighted position sizing.

        Algorithm (two multipliers applied to MAX_POSITION_SIZE_PCT):

        1. Conviction multiplier  (40% → 100% of max based on star rating):
             5★ → 100%  |  4★ → 85%  |  3★ → 70%  |  2★ → 55%  |  1★ → 40%

        2. Volatility scalar (inverse volatility — lower vol = larger position):
             vol ≤ 15%   → 1.20  (scale UP for stable, low-vol names)
             15% < vol ≤ 25% → 1.00  (neutral — baseline)
             25% < vol ≤ 40% → 0.80  (scale DOWN for elevated vol)
             vol  > 40%  → 0.60  (scale DOWN significantly for high-vol names)

        Final allocation = MAX_POSITION_SIZE_PCT × conviction_mult × vol_scalar
        Hard capped at MAX_POSITION_SIZE_PCT (conviction+vol can't exceed the limit).

        Returns (shares, estimated_cost)
        """
        acct  = self.get_account()
        price = self.get_latest_price(ticker)

        if price is None or price <= 0:
            return 0, 0.0

        # ── 1. Conviction multiplier ────────────────────────────────────
        # Normalise conviction: if passed as 1-5 star int, convert to 0.0-1.0
        if conviction_score > 1.0:
            conviction_norm = min(conviction_score / 5.0, 1.0)
        else:
            conviction_norm = max(0.1, min(conviction_score, 1.0))

        CONVICTION_MIN_PCT = 0.40
        conviction_mult = CONVICTION_MIN_PCT + (1.0 - CONVICTION_MIN_PCT) * conviction_norm

        # ── 2. Volatility scalar ─────────────────────────────────────────
        vol = self.get_volatility(ticker)
        if vol <= 0.15:
            vol_scalar = 1.20
        elif vol <= 0.25:
            vol_scalar = 1.00
        elif vol <= 0.40:
            vol_scalar = 0.80
        else:
            vol_scalar = 0.60

        # ── 3. Combined allocation ───────────────────────────────────────
        raw_alloc = MAX_POSITION_SIZE_PCT * conviction_mult * vol_scalar
        alloc_pct = min(raw_alloc, MAX_POSITION_SIZE_PCT)  # never exceed hard cap

        max_dollars = min(
            acct["portfolio_value"] * alloc_pct,
            acct["buying_power"],
        )

        shares = int(max_dollars / price)
        cost   = shares * price

        logger.info(
            f"  Position sizing [{ticker}]: "
            f"conviction={conviction_score:.1f}★ ({conviction_mult*100:.0f}%) × "
            f"vol={vol*100:.0f}% (scalar {vol_scalar:.2f}) "
            f"→ alloc={alloc_pct*100:.1f}% of portfolio "
            f"= ${cost:.2f} ({shares} shares)"
        )

        return shares, cost

    def check_portfolio_correlation(self, new_ticker: str,
                                    warn_threshold: float = 0.70) -> dict:
        """
        Pairwise correlation check before adding a new position.

        Fetches 60 days of daily returns for new_ticker and all currently
        held positions.  Flags any existing position with |correlation| > warn_threshold
        (default 0.70) — adding a highly correlated name increases concentration risk
        without true diversification.

        Returns:
          {
            "ticker":         new_ticker,
            "correlations":   {held_ticker: float, ...},
            "high_corr":      [held_ticker, ...],  # above threshold
            "safe_to_add":    bool,                # True if no high correlations
            "max_corr":       float,               # highest absolute correlation found
          }
        """
        positions = self.get_positions()
        result = {
            "ticker":       new_ticker,
            "correlations": {},
            "high_corr":    [],
            "safe_to_add":  True,
            "max_corr":     0.0,
        }

        if positions.empty:
            return result   # No existing positions — always safe

        held = list(positions["ticker"].values)
        tickers_to_fetch = [new_ticker] + held

        try:
            raw = yf.download(tickers_to_fetch, period="60d",
                              progress=False, auto_adjust=True)
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw["Close"]
            else:
                closes = raw[["Close"]] if "Close" in raw.columns else raw

            returns = closes.pct_change().dropna()

            if new_ticker not in returns.columns:
                return result   # Can't compute — data unavailable

            new_ret = returns[new_ticker]
            for ht in held:
                if ht not in returns.columns:
                    continue
                corr = float(new_ret.corr(returns[ht]))
                result["correlations"][ht] = round(corr, 3)
                if abs(corr) >= warn_threshold:
                    result["high_corr"].append(ht)
                if abs(corr) > result["max_corr"]:
                    result["max_corr"] = abs(corr)

            result["safe_to_add"] = len(result["high_corr"]) == 0

            if result["high_corr"]:
                logger.warning(
                    f"  ⚠️  Correlation warning [{new_ticker}]: highly correlated with "
                    f"{result['high_corr']} (threshold: {warn_threshold:.0%}). "
                    f"Adding this position increases concentration risk."
                )
            else:
                logger.info(
                    f"  Correlation check [{new_ticker}]: max |corr| = {result['max_corr']:.2f} "
                    f"— no concentration concerns. ✓"
                )

        except Exception as e:
            logger.debug(f"  Correlation check failed for {new_ticker}: {e}")

        return result

    def submit_buy(self, ticker: str, conviction_score: float = 1.0) -> Optional[dict]:
        """
        Submit a market buy order. Higher conviction → larger position (capped at max).
        conviction_score: 1–5 (star rating) or 0.0–1.0 (normalised float).
        """
        positions = self.get_positions()
        if not positions.empty and len(positions) >= MAX_OPEN_POSITIONS:
            logger.info(f"  Max positions ({MAX_OPEN_POSITIONS}) reached, skipping {ticker}")
            return None

        # Check if already holding this ticker
        if not positions.empty and ticker in positions["ticker"].values:
            logger.info(f"  Already holding {ticker}, skipping duplicate buy")
            return None

        shares, cost = self.calculate_position_size(ticker, conviction_score=conviction_score)

        if shares <= 0:
            logger.warning(f"  Cannot size position for {ticker} (insufficient buying power or price unavailable)")
            return None

        # Run pairwise correlation check — warn but don't block (Manager Agent decides)
        corr_result = self.check_portfolio_correlation(ticker)
        if not corr_result["safe_to_add"]:
            logger.warning(
                f"  [Correlation] {ticker} is highly correlated with "
                f"{corr_result['high_corr']} — consider reducing size or skipping"
            )

        try:
            order_req = self._MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=self._OrderSide.BUY,
                time_in_force=self._TimeInForce.DAY,
            )
            order = self.client.submit_order(order_req)
            logger.info(f"  BUY ORDER: {shares} shares of {ticker} @ ~${cost/shares:.2f} = ~${cost:.2f}")
            return {
                "id":     str(order.id),
                "ticker": ticker,
                "qty":    shares,
                "side":   "buy",
                "status": str(order.status),
            }
        except Exception as e:
            logger.error(f"  Buy order failed for {ticker}: {e}")
            return None

    def submit_sell(self, ticker: str, reason: str = "signal") -> Optional[dict]:
        """Submit a market sell order for the full position in ticker."""
        try:
            order_req = self._MarketOrderRequest(
                symbol=ticker,
                qty=None,         # Will sell entire position
                side=self._OrderSide.SELL,
                time_in_force=self._TimeInForce.DAY,
            )
            # Alpaca requires specifying qty — get it from positions
            positions = self.get_positions()
            if positions.empty or ticker not in positions["ticker"].values:
                logger.warning(f"  No position in {ticker} to sell")
                return None

            qty = int(positions[positions["ticker"] == ticker]["qty"].values[0])
            order_req = self._MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=self._OrderSide.SELL,
                time_in_force=self._TimeInForce.DAY,
            )
            order = self.client.submit_order(order_req)
            logger.info(f"  SELL ORDER ({reason}): {qty} shares of {ticker}")
            return {
                "id":     str(order.id),
                "ticker": ticker,
                "qty":    qty,
                "side":   "sell",
                "reason": reason,
                "status": str(order.status),
            }
        except Exception as e:
            logger.error(f"  Sell order failed for {ticker}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Risk Management — Stop Loss / Take Profit / Max Hold
    # -----------------------------------------------------------------------

    def enforce_risk_rules(self, entry_dates: dict,
                           exclude_tickers: set = None) -> list[dict]:
        """
        Check all open positions for stop-loss, take-profit, and max hold violations.
        entry_dates: {ticker: datetime_of_entry}
        exclude_tickers: set of tickers already closed this cycle (Step 0 stale-exit).
          Skipping them avoids duplicate sell orders and double sell_log entries.
        Returns list of executed sell order dicts.
        """
        positions = self.get_positions()
        executed  = []
        skip      = exclude_tickers or set()

        if positions.empty:
            return executed

        today = datetime.now()

        for _, pos in positions.iterrows():
            ticker     = pos["ticker"]

            if ticker in skip:
                logger.info(
                    f"  [enforce_risk_rules] Skipping {ticker} — "
                    f"already closed by stale-exit enforcement this cycle."
                )
                continue

            pnl_pct    = pos["unrealized_pct"]
            entry_date = entry_dates.get(ticker, today - timedelta(days=1))
            hold_days  = (today - entry_date).days

            reason = None

            if pnl_pct <= -(STOP_LOSS_PCT * 100):
                reason = f"stop_loss ({pnl_pct:.1f}%)"
            elif pnl_pct >= (TAKE_PROFIT_PCT * 100):
                reason = f"take_profit ({pnl_pct:.1f}%)"
            elif hold_days >= HOLD_DAYS_MAX:
                reason = f"max_hold ({hold_days} days)"

            if reason:
                result = self.submit_sell(ticker, reason=reason)
                if result:
                    result["pnl_pct"] = pnl_pct
                    executed.append(result)

        return executed

    # -----------------------------------------------------------------------
    # Portfolio Snapshot
    # -----------------------------------------------------------------------

    def get_portfolio_snapshot(self) -> dict:
        """Full portfolio snapshot for daily reporting."""
        acct      = self.get_account()
        positions = self.get_positions()

        total_invested  = 0.0
        total_unrealized = 0.0

        if not positions.empty:
            total_invested   = positions["market_value"].sum()
            total_unrealized = positions["unrealized_pl"].sum()

        return {
            "timestamp":         datetime.now().isoformat(),
            "portfolio_value":   acct["portfolio_value"],
            "cash":              acct["cash"],
            "buying_power":      acct["buying_power"],
            "total_invested":    total_invested,
            "unrealized_pnl":    total_unrealized,
            "pnl_today":         acct["pnl_today"],
            "pnl_today_pct":     acct["pnl_today_pct"],
            "n_positions":       len(positions),
            "positions":         positions.to_dict("records") if not positions.empty else [],
            "return_vs_start":   acct["pnl_today_pct"],  # day-over-day return from Alpaca
        }


    # -----------------------------------------------------------------------
    # Long-Short Strategy — Regime Hedge + Momentum Shorts
    # -----------------------------------------------------------------------

    def get_spy_regime(self) -> tuple[bool, float, float]:
        """
        Check whether SPY is above or below its 200-day moving average.
        Returns (is_bull: bool, spy_price: float, ma200: float)
        """
        try:
            spy = yf.download("SPY", period="300d", progress=False, auto_adjust=True)
            if isinstance(spy.columns, pd.MultiIndex):
                spy.columns = spy.columns.get_level_values(0)
            current = float(spy["Close"].iloc[-1])
            ma200   = float(spy["Close"].iloc[-200:].mean())
            return current >= ma200, current, ma200
        except Exception as e:
            logger.warning(f"Could not fetch SPY regime data: {e}")
            return True, 0.0, 0.0   # Default to bull if data unavailable

    def manage_regime_hedge(self) -> dict:
        """
        Bear regime  → buy SH (inverse S&P500 ETF) at 20% of portfolio.
        Bull regime  → sell SH if held, free cash for longs.
        Returns action summary dict.
        """
        is_bull, spy_price, ma200 = self.get_spy_regime()
        pct_from_ma = ((spy_price - ma200) / ma200 * 100) if ma200 > 0 else 0
        positions   = self.get_positions()
        has_hedge   = (not positions.empty and
                       HEDGE_ETF in positions["ticker"].values)

        if is_bull:
            regime_str = f"BULL (SPY {pct_from_ma:+.1f}% above 200MA)"
            if has_hedge:
                logger.info(f"  Regime flipped to BULL — closing {HEDGE_ETF} hedge")
                self.submit_sell(HEDGE_ETF, reason="regime_bull")
                return {"action": "sold_hedge", "regime": regime_str}
            logger.info(f"  {regime_str} — no hedge needed")
            return {"action": "none", "regime": regime_str}
        else:
            regime_str = f"BEAR (SPY {pct_from_ma:+.1f}% below 200MA)"
            if has_hedge:
                logger.info(f"  {regime_str} — {HEDGE_ETF} hedge already active")
                return {"action": "hedge_active", "regime": regime_str}
            # Buy SH
            acct          = self.get_account()
            hedge_dollars = acct["portfolio_value"] * HEDGE_POSITION_PCT
            price         = self.get_latest_price(HEDGE_ETF)
            if price and price > 0:
                shares = int(hedge_dollars / price)
                if shares > 0:
                    try:
                        order_req = self._MarketOrderRequest(
                            symbol=HEDGE_ETF,
                            qty=shares,
                            side=self._OrderSide.BUY,
                            time_in_force=self._TimeInForce.DAY,
                        )
                        self.client.submit_order(order_req)
                        logger.info(
                            f"  HEDGE BUY: {shares} shares {HEDGE_ETF} "
                            f"@ ~${price:.2f} = ~${shares*price:,.0f} "
                            f"({HEDGE_POSITION_PCT*100:.0f}% of portfolio)"
                        )
                        return {"action": "bought_hedge", "regime": regime_str,
                                "shares": shares, "cost": shares * price}
                    except Exception as e:
                        logger.error(f"  Hedge order failed: {e}")
            return {"action": "hedge_failed", "regime": regime_str}

    def manage_tail_hedge(self, cro_risk_level: str) -> dict:
        """
        Proactive tail hedge — board directive 2026-04-27.

        Policy:
          HIGH / CRITICAL  → maintain a 5% SH allocation regardless of whether
                             SPY is above or below its 200-day MA.  This is a
                             pre-emptive hedge: we buy protection while it is
                             still cheap (low VIX environment) rather than
                             waiting for the market to confirm a bear regime.
          ELEVATED or lower → tail hedge is NOT held; the automatic 200MA
                             regime hedge (manage_regime_hedge) governs.

        This method is called AFTER manage_regime_hedge() so it can inspect
        the resulting position without interfering with the regime hedge logic.
        If manage_regime_hedge() already bought a full 20% SH position, this
        method does nothing (position already exists and is larger than 5%).
        """
        TAIL_HEDGE_PCT    = 0.05      # 5% of portfolio when risk is elevated
        TAIL_HEDGE_TIERS  = {"HIGH", "CRITICAL"}

        positions = self.get_positions()
        has_sh    = (not positions.empty and
                     HEDGE_ETF in positions["ticker"].values)

        if cro_risk_level not in TAIL_HEDGE_TIERS:
            # Risk is ELEVATED or below — if a tail hedge (≤7%) is held from a
            # prior HIGH cycle, close it so the 200MA logic governs cleanly.
            if has_sh:
                sh_rows = positions[positions["ticker"] == HEDGE_ETF]
                acct    = self.get_account()
                sh_value = float(sh_rows["market_value"].values[0]) if not sh_rows.empty else 0
                sh_pct   = (sh_value / acct["portfolio_value"] * 100) if acct["portfolio_value"] > 0 else 0
                if sh_pct <= 7.0:
                    logger.info(
                        f"  TAIL HEDGE: CRO risk dropped to {cro_risk_level} — "
                        f"closing proactive {HEDGE_ETF} position ({sh_pct:.1f}% of portfolio)"
                    )
                    self.submit_sell(HEDGE_ETF, reason="tail_hedge_risk_reduced")
                    return {"action": "sold_tail_hedge", "cro_risk_level": cro_risk_level,
                            "reason": f"CRO risk reduced to {cro_risk_level}"}
            return {"action": "none", "cro_risk_level": cro_risk_level}

        # Risk is HIGH or CRITICAL — ensure SH is held at ~5%
        if has_sh:
            logger.info(
                f"  TAIL HEDGE: {HEDGE_ETF} already held during {cro_risk_level} risk — no action"
            )
            return {"action": "hedge_active", "cro_risk_level": cro_risk_level}

        # Buy the tail hedge
        acct          = self.get_account()
        hedge_dollars = acct["portfolio_value"] * TAIL_HEDGE_PCT
        price         = self.get_latest_price(HEDGE_ETF)
        if not price or price <= 0:
            logger.warning("  TAIL HEDGE: Could not fetch SH price — skipping")
            return {"action": "hedge_failed", "cro_risk_level": cro_risk_level}

        shares = int(hedge_dollars / price)
        if shares <= 0:
            logger.warning("  TAIL HEDGE: Insufficient capital for SH position")
            return {"action": "hedge_skipped", "cro_risk_level": cro_risk_level}

        try:
            order_req = self._MarketOrderRequest(
                symbol=HEDGE_ETF,
                qty=shares,
                side=self._OrderSide.BUY,
                time_in_force=self._TimeInForce.DAY,
            )
            self.client.submit_order(order_req)
            logger.info(
                f"  TAIL HEDGE BUY: {shares} × {HEDGE_ETF} @ ~${price:.2f} "
                f"= ~${shares * price:,.0f} ({TAIL_HEDGE_PCT*100:.0f}% of portfolio) "
                f"| CRO risk: {cro_risk_level}"
            )
            return {
                "action":          "bought_tail_hedge",
                "cro_risk_level":  cro_risk_level,
                "shares":          shares,
                "cost":            shares * price,
                "pct_of_portfolio": TAIL_HEDGE_PCT * 100,
            }
        except Exception as e:
            logger.error(f"  TAIL HEDGE order failed: {e}")
            return {"action": "hedge_failed", "cro_risk_level": cro_risk_level}

    def get_momentum_short_candidates(self) -> list[str]:
        """
        Screen SHORT_CANDIDATES for confirmed downtrend:
          - Current price below 50-day MA
          - Current price below 200-day MA
        Returns tickers that pass both conditions (worst first).
        """
        confirmed = []
        for ticker in SHORT_CANDIDATES:
            try:
                df = yf.download(ticker, period="250d", progress=False, auto_adjust=True)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if len(df) < 50:
                    continue
                price = float(df["Close"].iloc[-1])
                ma50  = float(df["Close"].iloc[-50:].mean())
                ma200 = float(df["Close"].iloc[-200:].mean()) if len(df) >= 200 else ma50
                pct_from_50  = (price - ma50)  / ma50  * 100
                pct_from_200 = (price - ma200) / ma200 * 100
                if price < ma50 and price < ma200:
                    confirmed.append((ticker, pct_from_50 + pct_from_200))
                    logger.info(
                        f"    SHORT candidate: {ticker} — "
                        f"{pct_from_50:+.1f}% vs 50MA, {pct_from_200:+.1f}% vs 200MA"
                    )
            except Exception as e:
                logger.warning(f"  Could not screen {ticker}: {e}")
        # Sort by worst (most below MAs) first
        confirmed.sort(key=lambda x: x[1])
        return [t for t, _ in confirmed[:MAX_SHORT_POSITIONS]]

    def manage_momentum_shorts(self) -> list[dict]:
        """
        Open short positions on confirmed momentum short candidates.
        Covers (closes) shorts on any that have recovered above their 50MA.
        Returns list of actions taken.
        """
        positions = self.get_positions()
        actions   = []

        # 1. Cover any existing shorts that have recovered
        if not positions.empty:
            existing_shorts = positions[positions["qty"] < 0]["ticker"].tolist()
            for ticker in existing_shorts:
                try:
                    df    = yf.download(ticker, period="60d", progress=False, auto_adjust=True)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    price = float(df["Close"].iloc[-1])
                    ma50  = float(df["Close"].iloc[-50:].mean()) if len(df) >= 50 else price
                    pnl   = float(positions[positions["ticker"]==ticker]["unrealized_pct"].values[0])
                    # Cover if: price recovered above 50MA OR stop-loss hit (+10% loss on short)
                    if price > ma50:
                        logger.info(f"  COVER SHORT {ticker} — recovered above 50MA")
                        self.submit_sell(ticker, reason="short_cover_recovery")
                        actions.append({"action": "covered", "ticker": ticker, "reason": "recovery"})
                    elif pnl <= -10.0:
                        logger.info(f"  COVER SHORT {ticker} — stop loss hit ({pnl:.1f}%)")
                        self.submit_sell(ticker, reason="short_stop_loss")
                        actions.append({"action": "covered", "ticker": ticker, "reason": "stop_loss"})
                except Exception as e:
                    logger.warning(f"  Could not evaluate short {ticker}: {e}")

        # 2. Open new shorts on confirmed candidates
        acct           = self.get_account()
        positions      = self.get_positions()  # refresh after covers
        current_shorts = [] if positions.empty else positions[positions["qty"] < 0]["ticker"].tolist()

        if len(current_shorts) >= MAX_SHORT_POSITIONS:
            logger.info(f"  Max short positions ({MAX_SHORT_POSITIONS}) reached")
            return actions

        candidates = self.get_momentum_short_candidates()
        for ticker in candidates:
            if ticker in current_shorts:
                continue
            if len(current_shorts) >= MAX_SHORT_POSITIONS:
                break
            price = self.get_latest_price(ticker)
            if not price or price <= 0:
                continue
            short_dollars = acct["portfolio_value"] * SHORT_POSITION_SIZE_PCT
            shares        = int(short_dollars / price)
            if shares <= 0:
                continue
            try:
                order_req = self._MarketOrderRequest(
                    symbol=ticker,
                    qty=shares,
                    side=self._OrderSide.SELL,   # Sell short = submit SELL with no position
                    time_in_force=self._TimeInForce.DAY,
                )
                self.client.submit_order(order_req)
                logger.info(
                    f"  SHORT ORDER: {shares} shares {ticker} "
                    f"@ ~${price:.2f} = ~${shares*price:,.0f}"
                )
                current_shorts.append(ticker)
                actions.append({"action": "shorted", "ticker": ticker,
                                "shares": shares, "price": price})
            except Exception as e:
                logger.error(f"  Short order failed for {ticker}: {e}")

        return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    trader = AlpacaTrader()
    snap   = trader.get_portfolio_snapshot()
    print(f"\nPortfolio Value: ${snap['portfolio_value']:,.2f}")
    print(f"Cash:            ${snap['cash']:,.2f}")
    print(f"Today's P&L:     ${snap['pnl_today']:+,.2f} ({snap['pnl_today_pct']:+.2f}%)")
    print(f"Return vs Start: {snap['return_vs_start']:+.2f}%")
    print(f"Open Positions:  {snap['n_positions']}")
