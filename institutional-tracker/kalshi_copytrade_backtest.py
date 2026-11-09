#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI COPY-TRADE BACKTEST
=========================================
"What would OUR copies of this trader have made?" — answered from the trader's
own fill history, replayed through the copier's real rules.

WHY THIS EXISTS (2026-11-09)
----------------------------
The scout ranked traders by THEIR monthly P&L. That is the wrong number. The
copier only takes a slice of what a leader does: taker fills, priced 15-85c,
in markets that settle within 30 days, inside $1-per-market and payout caps,
2c worse than the leader paid. A trader can be +$100k this month and give us
nothing to copy (heforgotpoland: almost every fill is a maker fill in a 2028
election market), or give us only the part of their book that loses.
Our own-record rule (scout MIN_OWN_COPIES) fixes that only after 10 of OUR
copies settle, which at our caps takes weeks of real money per leader.

`GET /v1/social/trades?nickname=X&cursor=...` pages back through a visible
trader's fills (100 per page; found 2026-11-09, the copier only ever read page
one). So the copier's rules can be replayed against weeks of history today.

HOW THE REPLAY WORKS
--------------------
Fills are replayed oldest first. Each one goes through `kalshi_copytrade.decide`
and `settle_check` — the copier's own functions, not a re-implementation, so
the backtest cannot drift from what the live bot would do — as if seen `lag_s`
seconds after the leader's fill (the copier's measured median was 2.1s).
Accepted orders become paper ledger rows, so the per-market, daily and payout
caps bind exactly as they do live.

Our fill price is assumed to be the copier's LIMIT (leader price + slippage),
the worst price an IOC order can fill at. Orders that would NOT have filled
(price ran away) are counted as filled anyway, at that worst price. Both choices
can only make a leader look worse than copying them really was, except one:
a trade the market moved away from (often a winner) might in reality have
been missed. That is the backtest's known optimism; it is why the copy rate
is reported and why live results still decide (scout MIN_OWN_COPIES).

Exits (reduce_only) close at most what we hold (kalshi_copytrade.exit_check). A market counts once it has
settled yes/no; open markets are marked at the last price and reported
separately, never mixed into the settled P&L.

Two sizings are reported:
  capped  the live config ($ caps, payout cap, daily cap) — what the desk earns
  unit    one contract per copied fill, no $ caps — the signal's quality,
          without our tiny caps truncating it. Fees still charged.

Usage:
    python3 kalshi_copytrade_backtest.py NICK [NICK ...] [--pages 10] [--json]
"""

import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

import kalshi_client as kc
import kalshi_copytrade as ct

DEFAULT_PAGES = 10
REQUEST_GAP_S = 1.2      # the social API 429s under bursty polling


# ------------------------------------------------------------------- data --

def _social(path, retries=3):
    url = f"{ct.SOCIAL_BASE}/{path}"
    for attempt in range(retries + 1):
        time.sleep(REQUEST_GAP_S)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": kc.USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(15 * (attempt + 1))
                continue
            raise


def fetch_history(nick, pages=DEFAULT_PAGES, get=_social, first=None):
    """(fills newest-first, visibility_state). Stops early when the cursor ends.
    A page that fails after the first returns what was read so far: a shorter
    history is still a true history; a guessed one is not. `first` is an
    already-fetched first page (the scout has one), saving a request."""
    q = urllib.parse.quote(nick)
    d = first if first is not None else get(f"trades?nickname={q}")
    vis = d.get("visibility_state")
    fills = list(d.get("trades") or [])
    cur = d.get("cursor")
    for _ in range(max(pages, 1) - 1):
        if not cur or vis == "hidden":
            break
        try:
            d = get(f"trades?nickname={q}&cursor={urllib.parse.quote(cur)}")
        except Exception:
            break
        page = d.get("trades") or []
        if not page:
            break
        fills += page
        cur = d.get("cursor")
    return fills, vis


def fetch_markets(tickers, cache=None, get=None):
    """{ticker: market dict} for every ticker the exchange will describe, in
    batches of 100. Markets settled before Kalshi's historical cutoff live
    under /historical/markets, so misses are retried there. A ticker neither
    knows is simply absent — the replay then treats its settle date as
    unknown (skipped, like live) and its result as open."""
    cache = {} if cache is None else cache
    get = get or kc._get
    todo = [t for t in dict.fromkeys(tickers) if t and t not in cache]
    for path in ("/markets", "/historical/markets"):
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            for attempt in range(4):
                try:
                    d = get(path, {"tickers": ",".join(batch), "limit": 1000})
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        time.sleep(5 * (attempt + 1))
                        continue
                    d = {}
                    break
                except Exception:
                    d = {}
                    break
            else:
                d = {}
            for m in d.get("markets") or []:
                if m.get("ticker"):
                    cache[m["ticker"]] = m
            time.sleep(0.3)
        todo = [t for t in todo if t not in cache]
        if not todo:
            break
    return cache


def settle_date(market):
    if not market:
        return None
    return ct._parse_ts(market.get("expected_expiration_time")
                        or market.get("close_time") or "")


def result(market):
    r = str((market or {}).get("result") or "").lower()
    return r if r in ("yes", "no") else None


# ----------------------------------------------------------------- replay --

def replay(nick, fills, markets, cfg, lag_s=3.0, unit=False):
    """Replay a leader's fills through the copier's rules -> (orders, skips).

    `orders` are the paper ledger rows the live copier would have written;
    `skips` maps each skip reason (first clause, so "stale (31s old)" and
    "stale (40s old)" pool) to a count. Pure apart from what is passed in."""
    cfg = dict(cfg)
    if unit:
        cfg.update(per_trade_usd=1e9, per_day_usd=1e9, max_payout_usd=1e9)
    rows, skips = [], {}
    pos = {}
    lag = timedelta(seconds=lag_s)
    ordered = sorted((t for t in fills if t.get("trade_id")),
                     key=lambda t: str(t.get("create_date") or ""))
    for t in ordered:
        ts = ct._parse_ts(t.get("create_date"))
        if ts is None:
            continue
        now = ts + lag
        if unit:
            t = dict(t, count_fp="1", count=1)
        ticker = t.get("ticker")
        if unit:
            dec = _decide_unit(t, nick, cfg, now)
        else:
            dec = ct.decide(t, nick, cfg, ct.spent_today(rows, now=now), now=now,
                            market_spent=ct.market_spent(ticker, rows),
                            market_contracts=ct.market_contracts(ticker, rows))
        if dec["action"] == "order" and not dec.get("reduce_only"):
            dec = ct.settle_check(dec, cfg, now, settle_date(markets.get(ticker)))
            dec = ct.venue_check(dec, cfg, markets.get(ticker))
        if dec["action"] == "order" and dec.get("reduce_only"):
            dec = ct.exit_check(dec, pos.get(ticker, 0.0))   # the copier's own rule
        if dec["action"] == "skip":
            why = _bucket(dec.get("reason", "?"))
            skips[why] = skips.get(why, 0) + 1
            continue
        n = dec["count"]
        pos[ticker] = pos.get(ticker, 0.0) + (n if dec["side"] == "bid" else -n)
        rows.append({"at": now.isoformat(), "leader": nick, "status": "paper",
                     **{k: v for k, v in dec.items() if k != "action"}})
    return rows, skips


def _bucket(reason):
    """Pool skip reasons that differ only in their numbers: "YES at 0.03 is
    outside 0.15-0.85 (...)" and "YES at 0.04 ..." are one reason."""
    r = str(reason).split(" (")[0].split(":")[0]
    r = re.sub(r"\$?\d+(\.\d+)?", "#", r)
    return r.replace("YES at # is outside", "price outside").replace(
        "NO at # is outside", "price outside")


def _decide_unit(t, nick, cfg, now):
    """decide() for one contract with no $ caps: same direction, band, age and
    price rules; the caps are what `unit` sizing is there to remove."""
    return ct.decide(t, nick, cfg, 0.0, now=now, market_spent=0.0, market_contracts=0.0)


def pnl(rows, markets):
    """Settled and open P&L of paper rows, on the YES book (bid pays n*p, ask
    receives n*p, net YES position pays $1 if YES), taker fees included."""
    book = {}
    for r in rows:
        n, p = float(r["count"]), float(r["price"])
        b = book.setdefault(r["ticker"], {"cash": 0.0, "pos": 0.0, "spent": 0.0})
        if r["side"] == "bid":
            b["cash"] -= n * p
            b["pos"] += n
        else:
            b["cash"] += n * p
            b["pos"] -= n
        if not r.get("reduce_only"):
            b["spent"] += n * (p if r["side"] == "bid" else 1.0 - p)
        b["cash"] -= (kc.fee_cents(p, int(n)) or 0) / 100.0
    out = {"markets": len(book), "settled": 0, "wins": 0, "settled_pnl": 0.0,
           "settled_spent": 0.0, "open_pnl": 0.0, "open_spent": 0.0}
    per_market = []
    for ticker, b in book.items():
        m = markets.get(ticker)
        res = result(m)
        if res:
            v = b["cash"] + (b["pos"] if res == "yes" else 0.0)
            out["settled"] += 1
            out["wins"] += v > 0
            out["settled_pnl"] += v
            per_market.append(v)
            out["settled_spent"] += b["spent"]
        else:
            mark = kc.parse_price((m or {}).get("last_price_dollars"))
            if mark is None:
                mark = 0.0 if b["pos"] > 0 else 1.0     # unknown: assume the worst
            out["open_pnl"] += b["cash"] + b["pos"] * mark
            out["open_spent"] += b["spent"]
    for k in ("settled_pnl", "settled_spent", "open_pnl", "open_spent"):
        out[k] = round(out[k], 2)
    out["t_stat"] = t_stat(per_market)
    out["settled_roi"] = (round(out["settled_pnl"] / out["settled_spent"], 3)
                          if out["settled_spent"] else None)
    return out


def t_stat(xs):
    """Mean / standard error of per-market P&L. |t| < 2 means the backtest
    cannot tell this leader's copies from coin flips, whatever the total says.
    None below 5 markets: too few to estimate a spread at all."""
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    var = sum((x - mu) ** 2 for x in xs) / (n - 1)
    return round(mu / math.sqrt(var / n), 2) if var > 0 else None


def backtest(nick, fills, markets, cfg=None, lag_s=3.0):
    """Both sizings for one leader, plus how much of their activity we'd copy."""
    cfg = cfg or ct.load_config()
    span = [ct._parse_ts(t.get("create_date")) for t in fills]
    span = [s for s in span if s]
    out = {"nickname": nick, "fills": len(fills),
           "history_days": round((max(span) - min(span)).total_seconds() / 86400, 1)
           if len(span) > 1 else 0.0}
    for mode in ("capped", "unit"):
        rows, skips = replay(nick, fills, markets, cfg, lag_s=lag_s, unit=(mode == "unit"))
        out[mode] = {**pnl(rows, markets), "copies": len(rows), "skips": skips}
    out["copy_rate"] = round(out["unit"]["copies"] / len(fills), 3) if fills else 0.0
    return out


def backtest_leader(nick, pages=DEFAULT_PAGES, cfg=None, market_cache=None):
    fills, vis = fetch_history(nick, pages=pages)
    if vis == "hidden":
        return {"nickname": nick, "hidden": True}
    markets = fetch_markets([t.get("ticker") for t in fills], cache=market_cache)
    return backtest(nick, fills, markets, cfg=cfg)


def _line(r):
    if r.get("hidden"):
        return f"  {r['nickname']:<24} trades hidden"
    u, c = r["unit"], r["capped"]
    roi = "n/a" if u["settled_roi"] is None else f"{u['settled_roi']:+.0%}"
    return (f"  {r['nickname']:<24} {r['fills']:>5} fills/{r['history_days']:>5}d  "
            f"copy {r['copy_rate']:>5.0%}  unit: {u['settled']:>3} settled "
            f"{u['wins']:>3}W ${u['settled_pnl']:>7.2f} (ROI {roi})  "
            f"capped: ${c['settled_pnl']:>6.2f} on {c['settled']} mkts, "
            f"open ${c['open_pnl']:+.2f}")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("nicks", nargs="+")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cache = {}
    res = [backtest_leader(n, pages=a.pages, market_cache=cache) for n in a.nicks]
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print("KALSHI COPY-TRADE BACKTEST (copier's own rules, worst-case fills)")
        for r in res:
            print(_line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
