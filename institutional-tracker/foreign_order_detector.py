#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — FOREIGN ORDER DETECTOR
# =============================================================================
# The one check that survives common-mode failure: watch the SHARED RESOURCE.
#
# Why: the firm's ~40 protections all run on this host, checking this host —
# so a second actor (the forgotten GCP VM trading June-3 code, a stale cron on
# any machine, anything holding the keys) is invisible to all of them. But every
# actor must touch the SAME Alpaca paper account. So: pull the account's actual
# order history and demand that every order be explained by a LOCAL decision
# record. Unexplained orders = another brain is trading. This is how the
# two-brains problem (discovered 2026-07-03 after a month) gets caught in a day.
#
# Heuristic v1: match account orders (last 72h) to local decision evidence
# (manager_decisions, state buy/sell logs, etf orders) by SYMBOL + calendar day.
# An order with no local record for that symbol/day = FOREIGN — escalate to the
# Chairman queue + firm channel. Heuristic, so the report says "investigate",
# not "certain" (a missing local log can also mean local logging broke — which
# is ALSO worth an alarm).
#
# Called from pod_health_monitor.run() daily. Standalone: python3 foreign_order_detector.py
# =============================================================================

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("foreign_orders")

DATA = Path(__file__).parent / "data"
CHAIRMAN = DATA / "chairman_action_required.json"
FIRM_CHANNEL = DATA / "firm_channel.json"
REPORT = DATA / "foreign_order_report.json"

LOOKBACK_HOURS = 72

# The firm's designated hedge instruments, traded directly by main.py Step 2b
# (regime hedge) WITHOUT writing manager_decisions/buy_log entries. A hedge
# order on a day the pipeline completed is the firm acting, not a foreign
# actor — 7 SH orders false-alarmed HIGH to the Chairman on 2026-07-13.
HEDGE_TICKERS = {"SH"}


def _load(p, d):
    try:
        return json.loads(p.read_text()) if p.exists() else d
    except Exception:
        return d


def _local_decision_index() -> set:
    """(SYMBOL, YYYY-MM-DD) pairs this host can explain."""
    idx = set()
    # manager decisions (list of runs, each with decisions[])
    md = _load(DATA / "manager_decisions.json", [])
    runs = md if isinstance(md, list) else [md]
    for run in runs:
        day = str(run.get("generated_at", ""))[:10]
        for dec in run.get("decisions", []) or []:
            t = (dec.get("ticker") or "").upper()
            if t and day:
                idx.add((t, day))
    # state buy/sell logs
    st = _load(DATA / "state.json", {})
    for log in (st.get("buy_log") or []), (st.get("sell_log") or []):
        for e in log:
            t = (e.get("ticker") or "").upper()
            d = str(e.get("date", ""))[:10]
            if t and d:
                idx.add((t, d))
    # ETF pod orders (main.py Step 4d executes these locally)
    eo = _load(DATA / "etf_pod_orders.json", [])
    items = eo if isinstance(eo, list) else eo.get("orders", [])
    for o in items or []:
        t = (o.get("ticker") or o.get("symbol") or "").upper()
        d = str(o.get("date") or o.get("created_at") or "")[:10]
        if t and d:
            idx.add((t, d))
    return idx


def run_check() -> dict:
    """Pull account orders and flag ones no local record explains."""
    out = {"checked_at": datetime.now().isoformat(), "lookback_h": LOOKBACK_HOURS,
           "orders_seen": 0, "foreign": [], "status": "OK"}
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL,
                                                    after=after, limit=200))
    except Exception as e:
        out["status"] = "ERROR"
        out["error"] = f"could not pull account orders: {e}"
        REPORT.write_text(json.dumps(out, indent=2, default=str))
        return out

    local = _local_decision_index()
    for o in orders:
        out["orders_seen"] += 1
        sym = str(getattr(o, "symbol", "")).upper()
        sub = getattr(o, "submitted_at", None)
        day = str(sub)[:10] if sub else ""
        # explained if any local record touches this symbol on this or the prior day
        prev = (datetime.fromisoformat(day) - timedelta(days=1)).strftime("%Y-%m-%d") if day else ""
        if (sym, day) in local or (sym, prev) in local:
            continue
        if sym in HEDGE_TICKERS:
            # per-day evidence: our pipeline ran that day (its dated log exists) —
            # the heartbeat only remembers the LATEST run, which missed 2-3 day old
            # hedge orders inside the 72h lookback.
            logdir = DATA.parent / "logs"
            if any((logdir / f"cron_{d.replace('-', '')}.log").exists()
                   for d in (day, prev) if d):
                continue  # regime hedge executed by our own pipeline that day
        out["foreign"].append({"symbol": sym, "submitted_at": str(sub),
                               "side": str(getattr(o, "side", "")),
                               "qty": str(getattr(o, "qty", "")),
                               "status": str(getattr(o, "status", ""))})

    if out["foreign"]:
        out["status"] = "FOREIGN_ORDERS"
        # escalate once per day to the Chairman queue
        actions = _load(CHAIRMAN, [])
        today = datetime.now().strftime("%Y-%m-%d")
        tag = f"[ForeignOrders] {today}"
        if isinstance(actions, list) and not any(
                (a.get("description") or "").startswith(tag) for a in actions
                if a.get("status") == "open"):
            syms = ", ".join(sorted({f['symbol'] for f in out['foreign']})[:8])
            actions.append({
                "id": f"FOD-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "source": "Foreign Order Detector", "severity": "HIGH", "status": "open",
                "resolution_note": "", "ts": datetime.now().isoformat(),
                "description": (f"{tag} — {len(out['foreign'])} account order(s) with NO local "
                                f"decision record ({syms}). Either another host is trading this "
                                "account (e.g. the GCP VM) or local decision logging broke. "
                                "Both demand investigation."),
            })
            CHAIRMAN.write_text(json.dumps(actions, indent=2, default=str))
        fc = _load(FIRM_CHANNEL, [])
        if isinstance(fc, list):
            fc.append({"ts": datetime.now().isoformat(), "date": today,
                       "agent": "Foreign Order Detector", "category": "SECURITY",
                       "priority": "HIGH",
                       "content": f"🚨 {len(out['foreign'])} unexplained account order(s) — "
                                  "possible second actor on the Alpaca account."})
            FIRM_CHANNEL.write_text(json.dumps(fc[-500:], indent=2, default=str))

    REPORT.write_text(json.dumps(out, indent=2, default=str))
    logger.info(f"  [ForeignOrders] {out['orders_seen']} orders, "
                f"{len(out['foreign'])} unexplained → {out['status']}")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = run_check()
    print(json.dumps({k: r[k] for k in ("status", "orders_seen")} |
                     {"foreign": r["foreign"][:5]}, indent=2, default=str))
