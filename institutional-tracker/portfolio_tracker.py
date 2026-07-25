"""
portfolio_tracker.py -- Universal position tracker for all Apex Capital lanes.
Single source of truth for portfolio_positions.json.
"""

import json
import os
import datetime

try:
    import yfinance as yf
except ImportError:
    yf = None

DATA_FILE = "data/portfolio_positions.json"

LANE_CAPITALS = {
    "institutional_tracker": 100000.00,
    "ml_shortterm":          15000.00,
    "ml_longterm":           15000.00,
    "first_eagle":           5000.00,
}


class PortfolioTracker:
    DATA_FILE = DATA_FILE

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self):
        if not os.path.exists(self.DATA_FILE):
            return {"positions": [], "last_updated": None}
        with open(self.DATA_FILE, "r") as f:
            return json.load(f)

    def _save(self, data):
        data["last_updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        os.makedirs(os.path.dirname(self.DATA_FILE), exist_ok=True)
        with open(self.DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def add_position(self, lane, ticker, entry_price, shares,
                     entry_date, thesis_id=None, capital_deployed=None):
        """Add a new open position. Idempotent on (lane, ticker, entry_date)."""
        data = self._load()
        if capital_deployed is None:
            capital_deployed = round(entry_price * shares, 2)
        position = {
            "lane":             lane,
            "ticker":           ticker.upper(),
            "entry_price":      float(entry_price),
            "shares":           float(shares),
            "capital_deployed": float(capital_deployed),
            "entry_date":       str(entry_date),
            "status":           "open",
            "thesis_id":        thesis_id,
            "exit_price":       None,
            "exit_date":        None,
            "realized_pnl":     None,
        }
        data["positions"].append(position)
        self._save(data)
        return position

    def close_position(self, lane, ticker, exit_price, exit_date):
        """Mark the first open matching position as closed and record realized P&L."""
        data = self._load()
        ticker = ticker.upper()
        for pos in data["positions"]:
            if pos["lane"] == lane and pos["ticker"] == ticker and pos["status"] == "open":
                pos["exit_price"]   = float(exit_price)
                pos["exit_date"]    = str(exit_date)
                pos["status"]       = "closed"
                pos["realized_pnl"] = round((float(exit_price) - pos["entry_price"]) * pos["shares"], 2)
                self._save(data)
                return pos
        raise ValueError("No open position found for lane={} ticker={}".format(lane, ticker))

    def get_current_positions(self, lane=None):
        """Return list of open positions, optionally filtered by lane."""
        data = self._load()
        positions = [p for p in data["positions"] if p["status"] == "open"]
        if lane:
            positions = [p for p in positions if p["lane"] == lane]
        return positions

    # ------------------------------------------------------------------
    # Price fetching helpers
    # ------------------------------------------------------------------

    def _fetch_prices(self, tickers):
        """Return {ticker: current_price} dict. Handles yfinance errors gracefully."""
        prices = {}
        if not tickers:
            return prices
        if yf is None:
            return prices
        try:
            unique = list(set(tickers))
            data = yf.download(unique, period="2d", auto_adjust=True, progress=False)
            if data.empty:
                return prices
            # yfinance returns MultiIndex when >1 ticker, flat when 1
            if len(unique) == 1:
                ticker = unique[0]
                try:
                    prices[ticker] = float(data["Close"].dropna().iloc[-1])
                except Exception:
                    pass
            else:
                close = data["Close"]
                for t in unique:
                    try:
                        prices[t] = float(close[t].dropna().iloc[-1])
                    except Exception:
                        pass
        except Exception:
            pass
        return prices

    # ------------------------------------------------------------------
    # Portfolio value & P&L
    # ------------------------------------------------------------------

    def get_portfolio_value(self):
        """Fetch current prices via yfinance and compute market values."""
        positions = self.get_current_positions()
        tickers   = [p["ticker"] for p in positions]
        prices    = self._fetch_prices(tickers)

        result = {"positions": [], "total_deployed": 0.0, "total_market_value": 0.0}
        for pos in positions:
            deployed      = pos["capital_deployed"]
            price         = prices.get(pos["ticker"])
            market_value  = round(price * pos["shares"], 2) if price is not None else None
            unrealized    = round(market_value - deployed, 2) if market_value is not None else None
            result["positions"].append({
                "lane":           pos["lane"],
                "ticker":         pos["ticker"],
                "shares":         pos["shares"],
                "entry_price":    pos["entry_price"],
                "current_price":  price,
                "capital_deployed": deployed,
                "market_value":   market_value,
                "unrealized_pnl": unrealized,
            })
            result["total_deployed"]     += deployed
            if market_value is not None:
                result["total_market_value"] += market_value
        result["total_deployed"]     = round(result["total_deployed"], 2)
        result["total_market_value"] = round(result["total_market_value"], 2)
        return result

    def get_pnl_summary(self):
        """Realized + unrealized P&L by lane and total."""
        data      = self._load()
        portfolio = self.get_portfolio_value()

        # Build lookup for unrealized by lane+ticker
        unrealized_map = {}
        for p in portfolio["positions"]:
            key = (p["lane"], p["ticker"])
            unrealized_map[key] = p.get("unrealized_pnl") or 0.0

        lanes = {}
        total_realized   = 0.0
        total_unrealized = 0.0
        total_deployed   = 0.0

        for pos in data["positions"]:
            lane = pos["lane"]
            if lane not in lanes:
                lanes[lane] = {"realized": 0.0, "unrealized": 0.0, "deployed": 0.0}

            if pos["status"] == "closed" and pos["realized_pnl"] is not None:
                lanes[lane]["realized"] += pos["realized_pnl"]
                total_realized          += pos["realized_pnl"]
            elif pos["status"] == "open":
                key       = (lane, pos["ticker"])
                unreal    = unrealized_map.get(key, 0.0)
                deployed  = pos["capital_deployed"]
                lanes[lane]["unrealized"] += unreal
                lanes[lane]["deployed"]   += deployed
                total_unrealized          += unreal
                total_deployed            += deployed

        summary = {
            "by_lane":         {},
            "total_deployed":  round(total_deployed, 2),
            "total_realized":  round(total_realized, 2),
            "total_unrealized": round(total_unrealized, 2),
            "total_pnl":       round(total_realized + total_unrealized, 2),
        }
        for lane, vals in lanes.items():
            summary["by_lane"][lane] = {
                "deployed":   round(vals["deployed"], 2),
                "realized":   round(vals["realized"], 2),
                "unrealized": round(vals["unrealized"], 2),
                "total_pnl":  round(vals["realized"] + vals["unrealized"], 2),
            }
        return summary

    def get_lane_exposure(self, lane):
        """Sector breakdown and ticker concentration for a given lane."""
        positions = self.get_current_positions(lane=lane)
        if not positions:
            return {"lane": lane, "positions": [], "sector_exposure": {}, "ticker_concentration": {}}

        tickers = [p["ticker"] for p in positions]
        prices  = self._fetch_prices(tickers)

        total_value = 0.0
        pos_values  = []
        for pos in positions:
            price = prices.get(pos["ticker"])
            value = (price * pos["shares"]) if price is not None else pos["capital_deployed"]
            pos_values.append((pos["ticker"], value))
            total_value += value

        # Ticker concentration
        ticker_conc = {}
        for ticker, value in pos_values:
            ticker_conc[ticker] = round((value / total_value * 100), 2) if total_value > 0 else 0.0

        # Sector exposure via yfinance
        sector_map = {}
        if yf is not None:
            for ticker in tickers:
                try:
                    info   = yf.Ticker(ticker).info
                    sector = info.get("sector", "Unknown")
                except Exception:
                    sector = "Unknown"
                sector_map[ticker] = sector

        sector_values = {}
        for ticker, value in pos_values:
            sector = sector_map.get(ticker, "Unknown")
            sector_values[sector] = sector_values.get(sector, 0.0) + value

        sector_pct = {}
        for sector, value in sector_values.items():
            sector_pct[sector] = round((value / total_value * 100), 2) if total_value > 0 else 0.0

        return {
            "lane":                 lane,
            "total_value":          round(total_value, 2),
            "positions":            pos_values,
            "sector_exposure":      sector_pct,
            "ticker_concentration": ticker_conc,
        }


# ------------------------------------------------------------------
# Quick smoke test
# ------------------------------------------------------------------
if __name__ == "__main__":
    pt = PortfolioTracker()
    print("PNL summary:", pt.get_pnl_summary())
    print("Current positions:", pt.get_current_positions())
    print("Portfolio value:", pt.get_portfolio_value())
