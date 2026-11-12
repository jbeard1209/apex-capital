#!/usr/bin/env python3
"""
APEX CAPITAL — POD P&L HISTORY
==============================
One dated row per day for the whole firm and for every pod, in
`data/pod_pnl_history.json`. The pods' counterpart of kalshi_pnl_history.py.

Chairman, 2026-11-12: every desk's P&L must be kept day by day, not only the
Kalshi desk's. Until this module the pods had a SNAPSHOT (`pod_pnl_report.json`,
overwritten by every main.py run) and nothing else. Yesterday's pod figures
were gone the moment today's run wrote over them.

WHAT EACH DAILY ROW HOLDS
-------------------------
firm      Back-filled from performance_history.json, which the daily
          performance_snapshot.py has written from BROKER equity since
          2026-08. Exact for every day it has a snapshot:
          portfolio_value, cash, n_positions, pnl_today_usd, pnl_inception_usd.
pods      Point-in-time, from pod_allocations.json, written only on a day the
          recorder ran and then frozen (`marked: true`). Per pod:
            budget, deployed_value, budget_remaining, n_positions
            cost_basis_usd   sum of qty x avg_entry
            unrealised_usd   sum of qty x (current_price - avg_entry)
            realised_lifetime_usd   pod_ledger's running realised P&L
            pnl_change_usd   (unrealised + realised) minus the same figure on
                             the previous marked day: the pod's P&L between the
                             two marks, buys and sells included.
unattributed  Positions the broker holds that no pod has claimed. They have no
          entry price in the ledger, so their unrealised P&L is None, and their
          market value is shown so pods + unattributed can be compared to the
          firm's positions.

Back-filled days carry `"marked": false` and `pods: null`. Pod figures for
those days were never stored and cannot be rebuilt; a reconstruction would be
today's book passed off as last month's (memory `nan-is-not-none`: None means
not measured, never zero).

WHICH HOST
----------
pod_allocations.json is host-local (no state sync between hosts). The VM runs
the pipeline around the clock, so the VM's recorder is the one that counts;
each row says which host wrote it.

Usage:
    python3 pod_pnl_history.py --record     # cron: upsert today + backfill firm
    python3 pod_pnl_history.py --report     # print the table
    python3 pod_pnl_history.py --json       # print the file (read-only)
"""

import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
HISTORY_FILE = DATA_DIR / "pod_pnl_history.json"
ALLOCATIONS_FILE = DATA_DIR / "pod_allocations.json"
PERFORMANCE_FILE = DATA_DIR / "performance_history.json"
MAX_ROWS = 730

FIRM_FIELDS = ("portfolio_value", "cash", "n_positions", "pnl_today_usd",
               "pnl_today_pct", "pnl_inception_usd", "pnl_inception_pct")
POINT_IN_TIME = ("marked", "pods", "unattributed", "host", "recorded_at",
                 "allocations_updated_at")


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _num(v):
    """A finite float, or None. NaN and strings are 'not measured'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def firm_rows(perf):
    """{date: firm-fields} from performance_history.json's snapshots."""
    out = {}
    snaps = perf.get("snapshots") if isinstance(perf, dict) else None
    for s in snaps or []:
        d = s.get("snapshot_date") if isinstance(s, dict) else None
        if not d:
            continue
        # NaN would pass through as a plausible cell; it is "not measured".
        row = {k: _num(s.get(k)) for k in FIRM_FIELDS}
        if row["n_positions"] is not None:
            row["n_positions"] = int(row["n_positions"])
        out[d] = row
    return out


def pod_snapshot(pod):
    """The point-in-time figures for one pod. A position with no usable entry
    price makes the pod's cost and unrealised P&L None, not a partial sum."""
    positions = pod.get("positions") or {}
    cost = unreal = 0.0
    complete = True
    for p in positions.values():
        q, a, c = _num(p.get("qty")), _num(p.get("avg_entry")), _num(p.get("current_price"))
        if q is None or a is None or c is None or a <= 0:
            complete = False
            continue
        sign = -1.0 if p.get("side") == "short" else 1.0
        cost += q * a
        unreal += sign * q * (c - a)
    return {
        "name": pod.get("name"),
        "budget": _num(pod.get("budget")),
        "deployed_value": _num(pod.get("deployed_value")),
        "budget_remaining": _num(pod.get("budget_remaining")),
        "n_positions": len(positions),
        "cost_basis_usd": round(cost, 2) if complete else None,
        "unrealised_usd": round(unreal, 2) if complete else None,
        # pod_ledger started keeping this on 2026-11-09 and leaves it unset
        # until a pod's first sell after that, so unset means 0 since then.
        "realised_lifetime_usd": round(_num(pod.get("pnl_realised_lifetime")) or 0.0, 2),
        "tickers": sorted(positions),
    }


def unattributed_snapshot(alloc):
    u = alloc.get("unattributed") if isinstance(alloc, dict) else None
    if not isinstance(u, dict):
        return None
    vals = [(_num(v.get("qty")), _num(v.get("price"))) for v in u.values()
            if isinstance(v, dict)]
    known = [abs(q) * p for q, p in vals if q is not None and p is not None]
    return {"n_positions": len(u),
            "market_value": round(sum(known), 2) if len(known) == len(vals) else None,
            "unrealised_usd": None,
            "tickers": sorted(u)}


def _pnl_level(p):
    if p is None or p.get("unrealised_usd") is None:
        return None
    return p["unrealised_usd"] + (p.get("realised_lifetime_usd") or 0.0)


def _link_changes(rows):
    """pnl_change_usd for each pod on each marked day, against the previous
    marked day. The first mark of a pod has nothing to compare to: None."""
    prev = {}
    for r in rows:
        if not r.get("marked") or not r.get("pods"):
            continue
        for pid, p in r["pods"].items():
            now_level = _pnl_level(p)
            before = prev.get(pid)
            p["pnl_change_usd"] = (round(now_level - before[1], 2)
                                   if now_level is not None and before
                                   and before[1] is not None else None)
            p["change_since"] = before[0] if before else None
            prev[pid] = (r["date"], now_level)


def record(now=None, alloc=None, perf=None, existing=None, write=True, host=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date().isoformat()
    alloc = _read_json(ALLOCATIONS_FILE, None) if alloc is None else alloc
    perf = _read_json(PERFORMANCE_FILE, {}) if perf is None else perf
    existing = _read_json(HISTORY_FILE, {}) if existing is None else existing
    old = {r.get("date"): r for r in (existing.get("snapshots") or [])
           if isinstance(r, dict)} if isinstance(existing, dict) else {}

    firm = firm_rows(perf)
    dates = sorted(set(firm) | set(old) | {today})
    rows = []
    for d in dates:
        row = {"date": d, "firm": firm.get(d) or (old.get(d) or {}).get("firm")}
        prior = old.get(d) or {}
        if prior.get("marked") and d != today:
            # Frozen: an earlier day's pod mark is never rewritten.
            row.update({k: prior.get(k) for k in POINT_IN_TIME})
        else:
            row.update(marked=False, pods=None, unattributed=None, host=None,
                       recorded_at=None, allocations_updated_at=None)
        rows.append(row)

    t = rows[-1] if rows[-1]["date"] == today else None
    if t is not None and isinstance(alloc, dict) and isinstance(alloc.get("pods"), dict):
        t.update(marked=True,
                 pods={pid: pod_snapshot(p) for pid, p in sorted(alloc["pods"].items())},
                 unattributed=unattributed_snapshot(alloc),
                 host=host or socket.gethostname(),
                 recorded_at=now.isoformat(timespec="seconds"),
                 allocations_updated_at=alloc.get("last_updated"))
    _link_changes(rows)
    rows = rows[-MAX_ROWS:]

    doc = {"description": "Daily firm + per-pod P&L. firm = broker snapshots "
                          "(performance_history.json); pods = pod_allocations.json "
                          "marked on the day, null where never measured.",
           "sector": "pods",
           "last_written": now.isoformat(timespec="seconds"),
           "summary": summary(rows),
           "snapshots": rows}
    if write:
        _atomic_write(HISTORY_FILE, doc)
    return doc


def summary(rows):
    marked = [r for r in rows if r.get("marked")]
    last = marked[-1] if marked else None
    firm_last = next((r for r in reversed(rows) if r.get("firm")), None)
    return {"days": len(rows), "days_marked": len(marked),
            "first_date": rows[0]["date"] if rows else None,
            "latest_mark": last["date"] if last else None,
            "firm_latest": firm_last["firm"] if firm_last else None,
            "firm_latest_date": firm_last["date"] if firm_last else None,
            "pods_latest": last["pods"] if last else None,
            "unattributed_latest": last["unattributed"] if last else None}


def _atomic_write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pod_pnl_hist.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=1, default=str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


CSV_COLS = ("date", "pod", "marked", "budget", "deployed_value", "budget_remaining",
            "n_positions", "cost_basis_usd", "unrealised_usd",
            "realised_lifetime_usd", "pnl_change_usd")
FIRM_CSV_COLS = ("date",) + FIRM_FIELDS


def history_csv(doc):
    """One line per pod per marked day. Empty cell = not measured."""
    def cell(v):
        return "" if v is None else str(v)
    lines = [",".join(CSV_COLS)]
    for r in doc.get("snapshots") or []:
        for pid, p in sorted((r.get("pods") or {}).items()):
            vals = {"date": r["date"], "pod": pid, "marked": r.get("marked"), **p}
            lines.append(",".join(cell(vals.get(c)) for c in CSV_COLS))
        u = r.get("unattributed")
        if u:
            vals = {"date": r["date"], "pod": "unattributed", "marked": r.get("marked"),
                    "deployed_value": u.get("market_value"),
                    "n_positions": u.get("n_positions")}
            lines.append(",".join(cell(vals.get(c)) for c in CSV_COLS))
    return "\n".join(lines) + "\n"


def firm_csv(doc):
    def cell(v):
        return "" if v is None else str(v)
    lines = [",".join(FIRM_CSV_COLS)]
    for r in doc.get("snapshots") or []:
        f = r.get("firm") or {}
        lines.append(",".join(cell(r["date"] if c == "date" else f.get(c))
                              for c in FIRM_CSV_COLS))
    return "\n".join(lines) + "\n"


def report(doc):
    out = ["POD P&L HISTORY", "=" * 70]
    s = doc.get("summary") or {}
    out.append(f"{s.get('days')} days ({s.get('first_date')} on), "
               f"{s.get('days_marked')} with pod marks; latest mark {s.get('latest_mark')}")
    f = s.get("firm_latest") or {}
    if f:
        out.append(f"Firm {s.get('firm_latest_date')}: value ${f.get('portfolio_value')}, "
                   f"today ${f.get('pnl_today_usd')}, since inception ${f.get('pnl_inception_usd')}")
    for pid, p in (s.get("pods_latest") or {}).items():
        out.append(f"  {pid:<12} deployed ${p.get('deployed_value')}  "
                   f"unrealised ${p.get('unrealised_usd')}  "
                   f"change ${p.get('pnl_change_usd')}  ({p.get('n_positions')} pos)")
    u = s.get("unattributed_latest")
    if u:
        out.append(f"  {'unattributed':<12} value ${u.get('market_value')}  "
                   f"({u.get('n_positions')} pos, no entry price)")
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--record" in argv:
        doc = record()
        print(report(doc))
        return 0
    doc = _read_json(HISTORY_FILE, None)
    if not isinstance(doc, dict):
        print(json.dumps({"sector": "pods", "error": "no history recorded yet"})
              if "--json" in argv else "No pod P&L history recorded yet.")
        return 1
    print(json.dumps(doc, default=str) if "--json" in argv else report(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
