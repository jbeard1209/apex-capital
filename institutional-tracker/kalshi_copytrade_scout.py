#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI COPY-TRADE SCOUT
======================================
Decides WHO kalshi_copytrade.py follows. Runs daily: searches Kalshi's Social
leaderboards for profitable traders whose trades are visible, re-checks every
trader already followed, adds the best, and drops anyone who has slipped.

Chairman, 2026-11-04: "always be on the lookout for new traders and make sure
the traders you do track are still optimal ... so that you don't follow a bad
trader down a rabbit hole."

WHY THE BAR HAS SO MANY PARTS
-----------------------------
Measured 2026-11-04: 0 of the top 40 all-time earners and 3 of the top 100 by
volume show their trades. Of the visible ones, two were bots trading 1c and
99c contracts by the thousand. A leaderboard rank alone would have picked
them. So a trader must clear ALL of:

  visible        their trades endpoint is not "hidden"
  profitable now monthly P&L > 0. The board only lists the top 100, so anyone
                 off it is looked up in profile/metrics. Until 2026-11-09, off
                 the board counted as "not profitable": Scottaholic, +$97,928
                 this month, was rejected $1,204 below the 100th place cutoff.
  profitable ever all-time P&L from profile/metrics > 0 (a hot month on top of
                 a losing career is the luck we are trying not to copy)
  active         a fill in the last ACTIVE_DAYS days
  copyable       >= MIN_COPYABLE of their recent fills are ones the copier will
                 actually copy: TAKER fills priced inside the copier's band.
                 A market maker's profit lives in maker fills we skip.
  backtest       their last BT_PAGES x 100 fills, replayed through the
                 copier's own rules (kalshi_copytrade_backtest), must not have
                 lost money over >= BT_MIN_SETTLED settled markets, and must
                 copy >= BT_MIN_COPY_RATE of their fills. Added 2026-11-09:
                 their P&L is not ours, and four traders up this month would
                 have lost us 11-57% per dollar copied.
  our own record once >= MIN_OWN_COPIES markets we copied them into have
                 settled, our realised copy P&L there must not be negative —
                 their profit is irrelevant if copying them loses ours.

The search pool is the leaderboards plus a remembered watchlist: everyone seen
showing their trades, and everyone we have ever copied. Visible traders are so
rare (7 of 227 on 2026-11-09) that one must not be forgotten for slipping off a
top-100 list.

Survivors are ranked by backtest ROI (those with >= BT_MIN_SETTLED settled
replayed copies), then the rest by monthly P&L. The top MAX_LEADERS are followed. A
current leader is kept while still eligible and ranked inside 2 x MAX_LEADERS,
so a trader is not dropped and re-added over a one-place shuffle.
Traders the Chairman pins (`--pin NICK`) are never auto-removed, but are still
graded, and the report says when one fails the bar.

Usage:
    python3 kalshi_copytrade_scout.py --report     # search + grade + update leaders
    python3 kalshi_copytrade_scout.py --dry-run    # grade only, change nothing
    python3 kalshi_copytrade_scout.py --pin NICK | --unpin NICK
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import kalshi_client as kc
import kalshi_copytrade as ct
import kalshi_copytrade_backtest as bt

REPORT_FILE = ct.DATA_DIR / "kalshi_copytrade_scout_report.json"
SOCIAL = ct.SOCIAL_BASE

MAX_LEADERS = 3
ACTIVE_DAYS = 3
MIN_COPYABLE = 0.30
MIN_OWN_COPIES = 10
REQUEST_GAP_S = 1.2
_RESULTS = {}                # ticker -> settled result, for one scout run
_MARKETS = {}                # ticker -> market, for one scout run's backtests
# Backtest gate (2026-11-09). See kalshi_copytrade_backtest.py for the replay.
BT_PAGES = 5                 # 500 fills: weeks of history for most traders
BT_MIN_SETTLED = 10          # same bar as MIN_OWN_COPIES, on replayed copies
BT_MIN_FILLS_FOR_RATE = 200  # judge the copy rate only on a real sample
BT_MIN_COPY_RATE = 0.02      # below this, following them buys almost nothing
BOARDS = [("projected_pnl", "monthly"), ("projected_pnl", "weekly"),
          ("projected_pnl", "all_time"), ("volume", "monthly")]


def _get(path, retries=3):
    url = f"{SOCIAL}/{path}"
    for attempt in range(retries + 1):
        time.sleep(REQUEST_GAP_S)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": kc.USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(15 * (attempt + 1))
                continue
            raise


def leaderboard(metric, period, limit=100):
    d = _get(f"leaderboard?metric_name={metric}&limit={limit}&time_period={period}")
    return [r for r in d.get("rank_list") or [] if r.get("nickname")]


def grade_trades(nick, trades, now):
    """(copyable_share, last_fill_age_days) from a profile's recent fills."""
    if not trades:
        return 0.0, None
    me = nick.lower()
    ok = 0
    for t in trades:
        p = kc.parse_price(t.get("price_dollars"))
        if (str(t.get("taker_nickname") or "").lower() == me and p is not None
                and ct.MIN_PRICE <= p <= ct.MAX_PRICE):
            ok += 1
    newest = max((ct._parse_ts(t.get("create_date")) for t in trades
                  if ct._parse_ts(t.get("create_date"))), default=None)
    age = (now - newest).total_seconds() / 86400 if newest else None
    return ok / len(trades), age


def market_result(ticker, cache):
    """'yes' | 'no' once the market has settled, else None. Cached by ticker."""
    if ticker in cache:
        return cache[ticker]
    try:
        m = kc._get(f"/markets/{urllib.parse.quote(str(ticker))}").get("market") or {}
    except Exception:
        return None
    res = str(m.get("result") or "").lower()
    res = res if res in ("yes", "no") else None
    if res:
        cache[ticker] = res
    return res


def own_copy_record(nick, rows=None, cache=None, result=market_result):
    """(n_settled_markets, pnl_usd) of OUR live copies of this leader.

    Accounted on the YES book only, which is how V2 orders are expressed:
    a bid of n at p pays n*p, an ask of n at p receives n*p, and the net YES
    position settles at $1 or $0. Fill price is taken as the order's limit,
    which can only overstate cost (IOC fills at the limit or better), so the
    estimate errs toward calling a leader worse than they are. Markets still
    open are left out entirely — an open position has no P&L yet.
    """
    rows = ct.ledger_rows() if rows is None else rows
    cache = {} if cache is None else cache
    by_ticker = {}
    for r in rows:
        if r.get("leader") != nick or r.get("status") != "placed":
            continue
        n = kc.parse_size(r.get("fill_count")) or 0.0
        p = r.get("price")
        if not n or p is None:
            continue
        cash, pos = by_ticker.get(r["ticker"], (0.0, 0.0))
        if r.get("side") == "bid":
            cash, pos = cash - n * float(p), pos + n
        else:
            cash, pos = cash + n * float(p), pos - n
        cash -= kc.fee_cents(float(p), max(1, int(n))) / 100.0
        by_ticker[r["ticker"]] = (cash, pos)
    n_settled, pnl = 0, 0.0
    for ticker, (cash, pos) in by_ticker.items():
        res = result(ticker, cache)
        if res is None:
            continue
        n_settled += 1
        pnl += cash + (pos if res == "yes" else 0.0)
    return n_settled, round(pnl, 2)


def monthly_pnl_lookup(nick):
    """This month's P&L in dollars from the trader's own profile, or None if it
    cannot be read. profile/metrics uses 1/10,000 of a dollar (heforgotpoland,
    2026-11-09: 1220663193 here, $121,734 on the board, a few hours apart)."""
    try:
        m = _get("profile/metrics?time_period=monthly&nickname="
                 f"{urllib.parse.quote(nick)}").get("metrics") or {}
        return round(float(m.get("pnl")) / 10000.0, 2)
    except Exception:
        return None


def run_backtest(nick, first_page):
    """Replay this trader's recent fills through the copier's own rules
    (kalshi_copytrade_backtest). Returns a short summary, or {"error": ...}:
    a backtest that cannot run leaves the trader UNPROVEN, never failed, for
    the same reason an unreadable board must not unfollow everyone."""
    try:
        fills, _vis = bt.fetch_history(nick, pages=BT_PAGES, get=_get, first=first_page)
        markets = bt.fetch_markets([t.get("ticker") for t in fills], cache=_MARKETS)
        r = bt.backtest(nick, fills, markets, cfg=ct.load_config())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    u = r["unit"]
    return {"fills": r["fills"], "history_days": r["history_days"],
            "copy_rate": r["copy_rate"], "settled": u["settled"], "wins": u["wins"],
            "pnl_usd": u["settled_pnl"], "roi": u["settled_roi"], "t_stat": u["t_stat"],
            "capped_pnl_usd": r["capped"]["settled_pnl"]}


def backtest_fails(b):
    """Why the backtest rules a trader out ([] if it doesn't).

    Their P&L says nothing about OURS: we copy only their taker fills inside
    our band and settle window, 2c worse. Measured 2026-11-09 over 20 visible
    traders, four of them up this month (sink.wheat, Ginobuck, degen247365,
    beatingthebooks) would have lost us 11-57% per dollar copied; Scottaholic,
    +$97k this month, trades almost nothing we can copy. Judged on one
    contract per fill (the signal, before our caps truncate it); the bar is
    the same as the own-record rule: lost money over >= 10 settled markets."""
    if not b or b.get("error"):
        return []
    fails = []
    if b["fills"] >= BT_MIN_FILLS_FOR_RATE and b["copy_rate"] < BT_MIN_COPY_RATE:
        fails.append(f"backtest: only {b['copy_rate']:.0%} of their last "
                     f"{b['fills']} fills are ones we would copy")
    if b["settled"] >= BT_MIN_SETTLED and b["pnl_usd"] < 0:
        fails.append(f"backtest: copying their last {b['fills']} fills would have lost "
                     f"${-b['pnl_usd']:.2f} over {b['settled']} settled markets "
                     f"(ROI {b['roi']:+.0%})")
    return fails


def rank_key(g):
    """Backtest-proven traders first, best ROI first; then the unproven
    (too few settled replayed copies) by this month's P&L, as before."""
    b = g.get("backtest") or {}
    if (b.get("settled") or 0) >= BT_MIN_SETTLED and b.get("roi") is not None:
        return (0, -b["roi"])
    return (1, -(g["monthly_pnl_usd"] or 0))


def evaluate(nick, monthly_pnl, now):
    """One trader -> {'eligible': bool, 'fails': [...], ...}."""
    out = {"nickname": nick, "monthly_pnl_usd": monthly_pnl, "fails": []}
    try:
        tr = _get(f"trades?nickname={urllib.parse.quote(nick)}")
    except Exception as e:
        out["fails"].append(f"trades unreadable ({type(e).__name__})")
        out["eligible"] = False
        return out
    if tr.get("visibility_state") == "hidden":
        out["fails"].append("trades hidden")
        out["eligible"] = False
        return out
    share, age = grade_trades(nick, tr.get("trades") or [], now)
    out.update(copyable_share=round(share, 2),
               last_fill_days=None if age is None else round(age, 2))
    try:
        m = _get(f"profile/metrics?nickname={urllib.parse.quote(nick)}").get("metrics") or {}
        # profile/metrics reports P&L in 1/10,000 of a dollar (4thewin: 77423861156
        # here vs 7742386 dollars on the leaderboard, 2026-11-04)
        out["all_time_pnl_usd"] = round(float(m.get("pnl")) / 10000.0, 2)
    except Exception:
        out["all_time_pnl_usd"] = None
    if monthly_pnl is None:
        monthly_pnl = out["monthly_pnl_usd"] = monthly_pnl_lookup(nick)
    out["backtest"] = run_backtest(nick, tr)
    n_own, pnl_own = own_copy_record(nick, cache=_RESULTS)
    out.update(own_closed_copies=n_own, own_copy_pnl_usd=pnl_own)

    if monthly_pnl is None:
        out["fails"].append("this month's P&L unreadable")
    elif monthly_pnl <= 0:
        out["fails"].append("not profitable this month")
    if out["all_time_pnl_usd"] is None or out["all_time_pnl_usd"] <= 0:
        out["fails"].append("all-time P&L not positive")
    if age is None or age > ACTIVE_DAYS:
        out["fails"].append(f"no fill in {ACTIVE_DAYS} days")
    if share < MIN_COPYABLE:
        out["fails"].append(f"only {share:.0%} of fills copyable (taker, "
                            f"{ct.MIN_PRICE:.0%}-{ct.MAX_PRICE:.0%})")
    if n_own >= MIN_OWN_COPIES and pnl_own < 0:
        out["fails"].append(f"our {n_own} copies lost ${-pnl_own:.2f}")
    out["fails"] += backtest_fails(out["backtest"])
    out["eligible"] = not out["fails"]
    return out


def scout(dry_run=False, now=None):
    now = now or datetime.now(timezone.utc)
    cfg_raw = ct._read_json(ct.CONFIG_FILE, {})
    cfg_raw = cfg_raw if isinstance(cfg_raw, dict) else {}
    current = list(cfg_raw.get("leaders") or [])
    pinned = list(cfg_raw.get("pinned") or [])
    watchlist = list(cfg_raw.get("watchlist") or [])
    copied = sorted({r["leader"] for r in ct.ledger_rows() if r.get("leader")})

    monthly = {}
    pool = []
    for metric, period in BOARDS:
        try:
            rows = leaderboard(metric, period)
        except Exception:
            continue
        for r in rows:
            if metric == "projected_pnl" and period == "monthly":
                monthly[r["nickname"]] = float(r.get("value") or 0)
            if r["nickname"] not in pool:
                pool.append(r["nickname"])
    for n in current + pinned + watchlist + copied:
        if n not in pool:
            pool.append(n)
    if not monthly:
        # Without the monthly board every trader fails "profitable now", and
        # acting on that would unfollow everyone because a fetch failed.
        return {"at": now.isoformat(), "error": "monthly leaderboard unreadable; "
                "leaders left unchanged", "leaders": current}

    graded = [evaluate(n, monthly.get(n), now) for n in pool]
    eligible = sorted((g for g in graded if g["eligible"]), key=rank_key)
    rank = {g["nickname"]: i for i, g in enumerate(eligible)}

    keep = [n for n in current if n in pinned
            or (n in rank and rank[n] < 2 * MAX_LEADERS)]
    new = list(keep)
    for g in eligible:
        if len([n for n in new if n not in pinned]) >= MAX_LEADERS:
            break
        if g["nickname"] not in new:
            new.append(g["nickname"])
    for p in pinned:
        if p not in new:
            new.append(p)

    by = {g["nickname"]: g for g in graded}
    changes = ([{"added": n, "why": "eligible, rank "
                 f"{rank[n] + 1} (backtest ROI, then monthly P&L)"} for n in new if n not in current]
               + [{"removed": n, "why": "; ".join(by.get(n, {}).get("fails") or [])
                   or "out-ranked"} for n in current if n not in new])
    report = {"at": now.isoformat(), "dry_run": dry_run, "pool_size": len(pool),
              "visible": sum(1 for g in graded if "trades hidden" not in g["fails"]),
              "eligible": [g for g in eligible],
              # why each visible trader failed: without this, "0 pass" cannot be
              # explained after the run without grading all of them again
              "rejected": [{"nickname": g["nickname"], "fails": g["fails"],
                            "backtest": g.get("backtest")} for g in graded
                           if not g["eligible"] and "trades hidden" not in g["fails"]],
              "leaders_before": current, "leaders_after": new, "changes": changes,
              "pinned_failing": [by[p] for p in pinned if p in by and not by[p]["eligible"]],
              "own_record_rule_active": any(g.get("own_closed_copies", 0) >= MIN_OWN_COPIES
                                            for g in graded)}
    if not dry_run:
        cfg_raw["leaders"] = new
        cfg_raw["watchlist"] = sorted(set(watchlist) | {
            g["nickname"] for g in graded if "trades hidden" not in g["fails"]
            and "copyable_share" in g})
        ct._write_json(ct.CONFIG_FILE, cfg_raw)
    ct._write_json(REPORT_FILE, report)
    return report


def _bt_text(b):
    if not b:
        return "backtest: not run"
    if b.get("error"):
        return f"backtest: failed ({b['error']})"
    roi = "n/a" if b["roi"] is None else f"{b['roi']:+.0%}"
    t = "" if b["t_stat"] is None else f", t={b['t_stat']:+.1f}"
    proof = ("  (|t| < 2: not distinguishable from luck)"
             if b["t_stat"] is not None and abs(b["t_stat"]) < 2 else "")
    return (f"backtest: {b['fills']} fills over {b['history_days']}d, copy {b['copy_rate']:.0%}, "
            f"{b['settled']} settled, {b['wins']}W, ${b['pnl_usd']:+.2f} per contract-copy, "
            f"ROI {roi}{t}{proof}")


def _print(r):
    if r.get("error"):
        print(f"SCOUT: {r['error']}")
        return
    print(f"KALSHI COPY-TRADE SCOUT — {r['at'][:19]}Z{' (dry run)' if r['dry_run'] else ''}")
    print(f"  searched {r['pool_size']} traders, {r['visible']} show their trades, "
          f"{len(r['eligible'])} pass every check")
    for g in r["eligible"][:10]:
        print(f"    {g['nickname']:<26} month ${g['monthly_pnl_usd']:>11,.0f}  "
              f"all-time ${g['all_time_pnl_usd']:>11,.0f}  "
              f"copyable {g['copyable_share']:.0%}  last fill {g['last_fill_days']}d")
        print(f"      {_bt_text(g.get('backtest'))}")
    for g in r.get("rejected", []):
        print(f"    rejected {g['nickname']:<26} {'; '.join(g['fails'])}")
    print(f"  following: {', '.join(r['leaders_after']) or '(nobody qualifies)'}")
    for c in r["changes"]:
        k = "added" if "added" in c else "removed"
        print(f"    {k.upper():<8} {c[k]} — {c['why']}")
    for g in r["pinned_failing"]:
        print(f"    PINNED BUT FAILING {g['nickname']}: {'; '.join(g['fails'])}")
    if not r["own_record_rule_active"]:
        print("  note: our-own-copy-P&L check not active yet (needs "
              f"{MIN_OWN_COPIES} settled markets copied from one leader)")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pin")
    ap.add_argument("--unpin")
    a = ap.parse_args(argv)
    if a.pin or a.unpin:
        cfg = ct._read_json(ct.CONFIG_FILE, {})
        cfg = cfg if isinstance(cfg, dict) else {}
        pins = [p for p in cfg.get("pinned") or [] if p != (a.unpin or a.pin)]
        if a.pin:
            pins.append(a.pin)
        cfg["pinned"] = pins
        ct._write_json(ct.CONFIG_FILE, cfg)
        print(f"Pinned: {', '.join(pins) or '(none)'}")
        return 0
    _print(scout(dry_run=a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
