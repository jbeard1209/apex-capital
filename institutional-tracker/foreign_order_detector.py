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
# Heuristic v1 (RETIRED as the primary test, kept for pre-cutover history):
# match account orders to local decision evidence by SYMBOL + calendar day.
#
# Why v1 was not good enough — 2026-07-30: a BUY of 163 XLE at 15:36:21 UTC took
# five hours and a two-host forensic sweep to attribute. It turned out to be the
# firm's own first successful buy-to-cover, placed by the `com.apexcapital.catchup`
# LAUNCHD agent (the Mac schedules through launchd; `crontab -l` is empty, which
# is how the investigation initially cleared this host in error).
#
# Note what the detector said about it: EXPLAINED — but for the wrong reason. Not
# because it knew the catchup job placed it, but because the firm had made an
# unrelated XLE decision earlier that day. It would have said "explained" just the
# same had the order come from the Oracle host. "We thought about this ticker
# today" is not attribution: a check that passes a real ghost and passes a real
# cover with identical reasoning distinguishes nothing. The failure here was not a
# missed alarm, it was that the firm could not answer *what placed this order* —
# about its own order.
#
# v2 — match by ORDER ID. Every order the firm places now carries a signed
# client_order_id (`apex-<host>-<source>-<uuid>`, see order_ledger.py), applied
# at the shared TradingClient chokepoint so all 16 submission sites are covered.
# An order is ours if it carries that stamp or its id is in this host's ledger.
# Anything else is FOREIGN, even on a ticker we traded the same day.
#
# The stamp travels with the order through Alpaca, so it works cross-host: the
# regime hedge runs on the VM while this detector often runs on the Mac. That is
# also how the SH hedge allowance is now expressed — a stamped hedge order is
# explained by its stamp, not by a blanket "SH is always fine" symbol rule.
#
# Orders submitted before order_ledger.STRICT_FROM predate stamping and still
# use the v1 heuristic, so the cutover cannot retroactively brand weeks of
# legitimate history as ghosts.
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
    # Pipeline execution logs — some executors (ETF Pod Step 4d, momentum shorts)
    # CONSUME their order queue after filling, erasing queue-based evidence. The
    # dated cron logs are the durable proof ("Executed SELL 11 QQQ") and the disk
    # janitor keeps them 5 days > the 72h lookback. Parse execution lines.
    import re as _re
    logdir = DATA.parent / "logs"
    for lf in logdir.glob("cron_*.log"):
        day = lf.stem.replace("cron_", "")
        day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else ""
        if not day:
            continue
        try:
            txt = lf.read_text(errors="ignore")
        except Exception:
            continue
        for m in _re.finditer(r"Executed (?:BUY|SELL) [\d.]+ ([A-Z.]{1,6})", txt):
            idx.add((m.group(1).upper(), day))
        for m in _re.finditer(r"(?:BUY|SELL) \([^)]*\) ([A-Z.]{1,6}) x", txt):
            idx.add((m.group(1).upper(), day))
    return idx


def _parse_stamp(client_order_id):
    """Signed-stamp check, delegated so there is ONE definition of the format.
    Import-failure returns None (never True): if order_ledger is missing we must
    not silently start treating every order as ours."""
    try:
        import order_ledger
        return order_ledger.parse_stamp(client_order_id)
    except Exception:
        return None


def _known_ids() -> set:
    try:
        import order_ledger
        return order_ledger.known_order_ids()
    except Exception:
        return set()


def _strict_from():
    """The instant stamping went firm-wide. Unreadable => treat everything as
    pre-cutover (heuristic-only), which is the non-alarming direction."""
    try:
        import order_ledger
        return datetime.fromisoformat(order_ledger.STRICT_FROM)
    except Exception:
        return datetime.now(timezone.utc) + timedelta(days=3650)


def _is_pre_cutover(submitted_at, strict_from) -> bool:
    if submitted_at is None:
        return True
    try:
        sub = submitted_at
        if isinstance(sub, str):
            sub = datetime.fromisoformat(sub.replace("Z", "+00:00"))
        if sub.tzinfo is None:
            sub = sub.replace(tzinfo=timezone.utc)
        return sub < strict_from
    except Exception:
        return True          # unparseable timestamp => don't manufacture an alarm


def run_check(orders=None) -> dict:
    """Pull account orders and flag any the firm cannot prove it placed.

    `orders` is an injection seam for tests only — production always passes None
    and pulls live. Without it the classification logic (the part that actually
    decides whether to wake the Chairman) could only ever be exercised against
    whatever the broker happened to be holding, which is not a test."""
    out = {"checked_at": datetime.now().isoformat(), "lookback_h": LOOKBACK_HOURS,
           "orders_seen": 0, "foreign": [], "hedge": [], "attributed": [],
           "legacy_explained": 0, "status": "OK"}
    if orders is None:
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
    known = _known_ids()
    strict_from = _strict_from()
    for o in orders:
        out["orders_seen"] += 1
        sym = str(getattr(o, "symbol", "")).upper()
        sub = getattr(o, "submitted_at", None)
        day = str(sub)[:10] if sub else ""
        coid = getattr(o, "client_order_id", "") or ""
        oid = str(getattr(o, "id", "") or "")

        # --- v2: order-level attribution --------------------------------
        # A signed stamp identifies the placing host AND source module, and
        # survives the trip through Alpaca, so it works even when the order was
        # placed by the other host. This is the real test.
        stamp = _parse_stamp(coid)
        if stamp:
            out["attributed"].append({"symbol": sym, "submitted_at": str(sub),
                                      "host": stamp.get("host"),
                                      "source": stamp.get("source")})
            continue
        if oid in known or (coid and coid in known):
            out["attributed"].append({"symbol": sym, "submitted_at": str(sub),
                                      "host": "this", "source": "local_ledger"})
            continue

        # --- pre-cutover history: fall back to the v1 heuristic ----------
        # Only for orders that predate stamping. After the cutover an unstamped
        # order is foreign no matter what else we did with that ticker.
        if _is_pre_cutover(sub, strict_from):
            prev = (datetime.fromisoformat(day) - timedelta(days=1)).strftime("%Y-%m-%d") if day else ""
            if (sym, day) in local or (sym, prev) in local:
                out["legacy_explained"] += 1
                continue
            if sym in HEDGE_TICKERS:
                out["hedge"].append({"symbol": sym, "submitted_at": str(sub),
                                     "side": str(getattr(o, "side", "")),
                                     "qty": str(getattr(o, "qty", ""))})
                continue
            out["foreign"].append({"symbol": sym, "submitted_at": str(sub),
                                   "side": str(getattr(o, "side", "")),
                                   "qty": str(getattr(o, "qty", "")),
                                   "status": str(getattr(o, "status", "")),
                                   "why": "no local record (pre-stamp heuristic)"})
            continue

        # --- post-cutover: unstamped means unattributed, full stop ------
        # The old code let SH through on symbol alone. That hole is now closed:
        # the hedge runs through the same stamped chokepoint as everything else,
        # so a genuine hedge order is caught by the stamp check above and never
        # reaches here. An UNSTAMPED SH order is precisely what a second actor
        # replaying our old code would look like, so it escalates like any other.
        out["foreign"].append({"symbol": sym, "submitted_at": str(sub),
                               "side": str(getattr(o, "side", "")),
                               "qty": str(getattr(o, "qty", "")),
                               "status": str(getattr(o, "status", "")),
                               "client_order_id": str(coid),
                               "why": "no apex stamp and not in local ledger"})

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
                "description": (f"{tag} — {len(out['foreign'])} account order(s) the firm cannot "
                                f"prove it placed ({syms}): no apex client_order_id stamp and not "
                                "in this host's order ledger. Either another actor is trading this "
                                "account, or a firm code path is submitting orders outside the "
                                "stamped chokepoint. Both demand investigation."),
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
                f"{len(out['attributed'])} stamped, {out['legacy_explained']} pre-stamp, "
                f"{len(out['foreign'])} unexplained → {out['status']}")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = run_check()
    print(json.dumps({k: r[k] for k in ("status", "orders_seen")} |
                     {"foreign": r["foreign"][:5]}, indent=2, default=str))
