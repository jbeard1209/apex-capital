#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI P&L HISTORY
=================================
The Kalshi copy-trade desk's daily P&L record, kept the way the rest of the
firm keeps its own: one dated row per day in a JSON file
(`data/kalshi_pnl_history.json`, the Kalshi counterpart of
`performance_history.json`), plus a current "sector" summary in the same file
for the dashboard and the hourly apex-reports push.

Chairman, 2026-11-11: Kalshi performance must be "recorded and tracked the same
way the other sectors/pods are ... with back-filled history". Until this module
the desk's P&L existed only as a number `kalshi_copytrade_manager.py --report`
recomputed on demand. Nothing kept yesterday's figure, so "how did Kalshi do
last week" could only be answered by re-deriving it from the ledger, and the
open-position mark at the time was lost for good.

WHAT EACH DAILY ROW HOLDS (UTC dates, real-money rows only — paper is excluded)
-------------------------------------------------------------------------------
Ledger-derived, recomputed every run, so back-filled days are exact:
  orders_filled / orders_errored / orders_paper   counts by the day sent
  spent_usd          cost of opening fills that day (the cap measures this)
  realised_usd       P&L of copies whose market SETTLED that day
  cum_spent_usd, cum_realised_usd                 running totals
Point-in-time, written only on the day itself and then frozen:
  unrealised_usd     open copies marked at the market's last trade price
  total_usd          cum_realised + unrealised
  open_positions     tickers still held
  balance_by_shard   Kalshi's per-shard cash (the copier can only spend shard 0)
  exchange_portfolio_usd   Kalshi's own `portfolio_value` figure, recorded
                     verbatim as a second, broker-side number. Its exact
                     definition is not documented where we checked (2026-11-11
                     it read $15.39 against $16.13 cash and ~$2.50 of marked
                     positions), so nothing here compares against it yet

A back-filled day has `"marked": false` and `unrealised_usd: null`. Historical
marks were never recorded and cannot be recovered; a reconstructed one would be
today's price passed off as last week's, so the field says "not measured"
instead of inventing it (memory `nan-is-not-none`: None means not measured,
never zero).

Usage:
    python3 kalshi_pnl_history.py --record     # cron: upsert today + backfill
    python3 kalshi_pnl_history.py --report     # print the history table
    python3 kalshi_pnl_history.py --json       # print the summary as JSON
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import kalshi_client as kc
import kalshi_copytrade as ct
import kalshi_copytrade_manager as mgr

HISTORY_FILE = ct.DATA_DIR / "kalshi_pnl_history.json"
MAX_ROWS = 730                 # two years of days
POINT_IN_TIME = ("marked", "unrealised_usd", "total_usd", "open_positions",
                 "balance_by_shard", "balance_total_usd", "exchange_portfolio_usd",
                 "recorded_at")


def _date(ts):
    t = ct._parse_ts(ts) if isinstance(ts, str) else ts
    return t.astimezone(timezone.utc).date().isoformat() if t else None


def _open_cost(r):
    """Opening cost of one filled row, the same rule as books()['spent']."""
    n = kc.parse_size(r.get("fill_count")) or 0.0
    p = r.get("price")
    if not n or p is None or r.get("reduce_only"):
        return 0.0
    p = float(p)
    return n * (p if r.get("side") == "bid" else 1.0 - p)


def settled_on(market):
    """UTC date a settled market settled, or None while it is open."""
    if not market or str(market.get("result") or "").lower() not in ("yes", "no"):
        return None
    for k in ("settlement_ts", "close_time", "expiration_time"):
        d = _date(market.get(k)) if market.get(k) else None
        if d:
            return d
    return None


def shard_balances(resp):
    """GET /portfolio/balance -> ({shard: dollars}, total dollars, portfolio dollars).
    Any part that cannot be read is None; nothing is defaulted to zero."""
    per = None
    try:
        rows = (resp or {}).get("balance_breakdown")
        if isinstance(rows, list):
            per = {str(int(r.get("exchange_index", 0))): round(float(r.get("balance")), 4)
                   for r in rows}
            if any(v != v for v in per.values()):
                per = None
    except (TypeError, ValueError, AttributeError):
        per = None

    def _cents(k):
        try:
            v = float((resp or {})[k]) / 100.0
            return None if v != v else round(v, 2)
        except (KeyError, TypeError, ValueError):
            return None
    return per, _cents("balance"), _cents("portfolio_value")


def build_rows(rows, market, today):
    """Ledger-derived daily rows from the first ledger day through `today`."""
    days = {}

    def day(d):
        return days.setdefault(d, {"date": d, "orders_filled": 0, "orders_errored": 0,
                                   "orders_paper": 0, "spent_usd": 0.0,
                                   "realised_usd": 0.0, "markets_settled": 0,
                                   "markets_won": 0})
    for r in rows:
        d = _date(r.get("at"))
        if not d:
            continue
        s = r.get("status")
        if s == "placed" and (kc.parse_size(r.get("fill_count")) or 0):
            day(d)["orders_filled"] += 1
            day(d)["spent_usd"] += _open_cost(r)
        elif s == "error":
            day(d)["orders_errored"] += 1
        elif s == "paper":
            day(d)["orders_paper"] += 1
        else:
            day(d)                      # a day with only skips still exists

    for ticker, b in mgr.books(rows).items():
        m = market(ticker)
        realised, _u, settled = mgr.value_book(b, m)
        if settled:
            d = settled_on(m) or today
            day(d)["realised_usd"] += realised
            day(d)["markets_settled"] += 1
            day(d)["markets_won"] += 1 if realised > 0 else 0

    if not days:
        return []
    first = min(days)
    out, cum_s, cum_r = [], 0.0, 0.0
    d = datetime.fromisoformat(first).date()
    end = datetime.fromisoformat(max(today, max(days))).date()
    while d <= end:
        row = day(d.isoformat())
        cum_s += row["spent_usd"]
        cum_r += row["realised_usd"]
        row["spent_usd"] = round(row["spent_usd"], 2)
        row["realised_usd"] = round(row["realised_usd"], 2)
        row["cum_spent_usd"] = round(cum_s, 2)
        row["cum_realised_usd"] = round(cum_r, 2)
        row.update(marked=False, unrealised_usd=None, total_usd=None)
        out.append(row)
        d += timedelta(days=1)
    return out


def snapshot_now(rows, market, balance_resp):
    """The point-in-time half of today's row, plus the open-position list."""
    unreal, open_pos = 0.0, []
    for ticker, b in mgr.books(rows).items():
        m = market(ticker)
        _r, u, settled = mgr.value_book(b, m)
        if settled:
            continue
        # A flat but unsettled book still carries its cash in `u` (bought and
        # sold before settlement); it counts, the way performance() counts it,
        # but it is not an open position.
        unreal += u
        if abs(b["pos"]) < 1e-9:
            continue
        mark = kc.parse_price((m or {}).get("last_price_dollars"))
        open_pos.append({"ticker": ticker, "leader": b["leader"],
                         "yes_contracts": round(b["pos"], 2),
                         "mark": mark, "unrealised_usd": round(u, 2),
                         "opened_at": b["opened_at"]})
    per, total, port = shard_balances(balance_resp) if balance_resp is not None \
        else (None, None, None)
    return {"marked": True, "unrealised_usd": round(unreal, 2),
            "open_positions": open_pos, "balance_by_shard": per,
            "balance_total_usd": total, "exchange_portfolio_usd": port}


def status_now(rows, now):
    cfg = ct.load_config()
    is_live, why = ct.armed()
    halt = ct._read_json(ct.HALT_FILE, None) if ct.HALT_FILE.exists() else None
    return {"armed": is_live, "not_armed_reason": why or None,
            "halted": ct.HALT_FILE.exists(),
            # The copier writes {"at", "last_error"}; the manager's breaker
            # writes {"reason"}. Read both or a real halt shows no cause.
            "halt_reason": ((halt.get("reason") or halt.get("last_error"))
                            if isinstance(halt, dict) else None),
            "halted_at": halt.get("at") if isinstance(halt, dict) else None,
            "leaders": list(cfg.get("leaders") or []),
            "per_market_cap_usd": cfg.get("per_trade_usd"),
            "per_day_cap_usd": cfg.get("per_day_usd"),
            "spent_today_usd": round(ct.spent_today(rows, now=now, live=True), 2)}


def _write(doc):
    fd, tmp = tempfile.mkstemp(dir=str(HISTORY_FILE.parent), prefix=".kalshi_pnl.",
                               suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=1, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, HISTORY_FILE)


def record(now=None, rows=None, market=None, balance=None, write=True):
    """Rebuild the ledger half of every row, freeze earlier point-in-time
    halves, and stamp today's. Idempotent: run it as often as you like."""
    now = now or datetime.now(timezone.utc)
    rows = ct.ledger_rows() if rows is None else rows
    cache = {}
    market = market or (lambda t: mgr._market(t, cache))
    if balance is None:
        try:
            balance = kc.get_balance()
        except Exception:
            balance = None              # recorded as not measured, not as $0
    today = now.date().isoformat()

    prior = ct._read_json(HISTORY_FILE, {})
    prior_rows = {r.get("date"): r for r in (prior.get("snapshots") or [])} \
        if isinstance(prior, dict) else {}

    snaps = build_rows(rows, market, today)
    for r in snaps:
        old = prior_rows.get(r["date"])
        if r["date"] != today and old and old.get("marked"):
            r.update({k: old.get(k) for k in POINT_IN_TIME})
        if r.get("marked") and r.get("unrealised_usd") is not None:
            r["total_usd"] = round(r["cum_realised_usd"] + r["unrealised_usd"], 2)
    pit = snapshot_now(rows, market, balance)
    if snaps and snaps[-1]["date"] == today:
        snaps[-1].update({k: v for k, v in pit.items() if k != "open_positions"})
        snaps[-1]["open_positions"] = [p["ticker"] for p in pit["open_positions"]]
        snaps[-1]["recorded_at"] = now.isoformat(timespec="seconds")
        snaps[-1]["total_usd"] = round(snaps[-1]["cum_realised_usd"]
                                       + pit["unrealised_usd"], 2)
    snaps = snaps[-MAX_ROWS:]

    perf = mgr.performance(rows=rows, now=now, market=market)
    doc = {
        "description": ("Kalshi copy-trade desk: one row per UTC day, real money "
                        "only. Ledger fields are exact for every day; marks "
                        "(unrealised/total/balances) exist only for days the "
                        "recorder ran (marked=true). Written by kalshi_pnl_history.py."),
        "sector": "kalshi",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status_now(rows, now),
        "summary": {
            "realised_usd": snaps[-1]["cum_realised_usd"] if snaps else 0.0,
            "unrealised_usd": pit["unrealised_usd"],
            "total_usd": round((snaps[-1]["cum_realised_usd"] if snaps else 0.0)
                               + pit["unrealised_usd"], 2),
            "spent_usd": snaps[-1]["cum_spent_usd"] if snaps else 0.0,
            "pnl_today_usd": (perf.get("today") or {}).get("total_usd"),
            "balance_by_shard": pit["balance_by_shard"],
            "balance_total_usd": pit["balance_total_usd"],
            "exchange_portfolio_usd": pit["exchange_portfolio_usd"],
            "n_open_positions": len(pit["open_positions"]),
        },
        "periods": {k: perf[k] for k in mgr.PERIODS if k in perf},
        "by_leader": perf.get("by_leader", {}),
        "open_positions": pit["open_positions"],
        "snapshots": snaps,
        "last_written": now.isoformat(timespec="seconds"),
    }
    if write:
        _write(doc)
    return doc


def _m(v):
    return "      —" if v is None else f"{v:+7.2f}"


def print_report(doc):
    s, st = doc["summary"], doc["status"]
    print("KALSHI P&L HISTORY  (real money, UTC days)")
    print(f"  generated : {doc['generated_at']}")
    print(f"  status    : {'ARMED' if st['armed'] else 'not armed'}"
          f"{' / HALTED: ' + str(st['halt_reason'])[:120] if st['halted'] else ''}")
    print(f"  total P&L : {s['total_usd']:+.2f}  (realised {s['realised_usd']:+.2f}, "
          f"open {s['unrealised_usd']:+.2f}) on ${s['spent_usd']:.2f} spent")
    print(f"  balances  : {s['balance_by_shard']}  portfolio {s['exchange_portfolio_usd']}")
    print("  date         filled  err  paper   spent  realised  cum_real    open   total")
    for r in doc["snapshots"]:
        print(f"  {r['date']}  {r['orders_filled']:6d} {r['orders_errored']:4d} "
              f"{r['orders_paper']:6d}  {r['spent_usd']:6.2f}  {r['realised_usd']:+8.2f}"
              f"  {r['cum_realised_usd']:+8.2f} {_m(r.get('unrealised_usd'))} "
              f"{_m(r.get('total_usd'))}{'' if r.get('marked') else '  (backfill)'}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--record" in argv:
        doc = record()
        print(f"{doc['generated_at']} kalshi_pnl_history: {len(doc['snapshots'])} day(s), "
              f"total {doc['summary']['total_usd']:+.2f}")
        return 0
    doc = ct._read_json(HISTORY_FILE, None)
    if not isinstance(doc, dict):
        doc = record(write=False)
    if "--json" in argv:
        print(json.dumps(doc, indent=1, default=str))
    else:
        print_report(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
