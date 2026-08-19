"""
APEX CAPITAL — POD LEDGER
==========================
Budget enforcement and P&L tracking for the three-pod investment structure.

All trade execution MUST go through this module:
  1. can_trade(pod_id, ticker, qty, price, side) -> (bool, reason_str)
  2. record_trade(pod_id, ticker, qty, price, side, signal_source)
  3. update_prices(price_dict)   — call daily to mark positions to market
  4. get_pod_summary(pod_id)     — returns current state of a pod
  5. get_firm_summary()          — returns all pods + reserve

The ledger is the single source of truth for pod budgets. No pod may spend
money it does not have. The Manager Agent checks this before every order.
"""

import json
import fcntl
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [POD_LEDGER] %(levelname)s %(message)s")
log = logging.getLogger("pod_ledger")

_ALLOCATIONS_PATH = Path(__file__).parent / "data" / "pod_allocations.json"
_REPORT_PATH = Path(__file__).parent / "data" / "pod_pnl_report.json"

VALID_SIDES = {"long", "short"}


# ---------------------------------------------------------------------------
# Internal I/O
# ---------------------------------------------------------------------------

def _load() -> dict:
    """Read pod_allocations.json and return parsed dict."""
    if not _ALLOCATIONS_PATH.exists():
        log.warning(f"pod_allocations.json not found — returning empty state")
        return {"pods": {}, "reserve": {"balance": 0.0}, "last_updated": ""}
    try:
        with open(_ALLOCATIONS_PATH, "r") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise ValueError(f"Corrupted pod_allocations.json: {e}")


def _save(data: dict) -> None:
    """Write pod_allocations.json atomically using a temp file + rename.
    Uses fcntl.flock to prevent concurrent writes.
    """
    tmp_path = _ALLOCATIONS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            json.dump(data, fh, indent=2)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    tmp_path.replace(_ALLOCATIONS_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def can_trade(
    pod_id: str,
    ticker: str,
    qty: float,
    price: float,
    side: str = "long",
) -> Tuple[bool, str]:
    """
    Check whether a pod can execute a trade.

    Returns (True, "OK") if the trade is allowed, or (False, reason) if not.

    Rules:
      - pod_id must exist
      - qty must be > 0
      - price must be > 0
      - side must be 'long' or 'short'
      - BUY (long, entering): cash_balance >= qty * price
      - SELL (closing a long): position must exist with qty >= sell_qty
    """
    data = _load()
    pods = data.get("pods", {})

    if pod_id not in pods:
        return False, f"Pod '{pod_id}' does not exist. Valid pods: {list(pods.keys())}"

    if side not in VALID_SIDES:
        return False, f"Invalid side '{side}'. Must be one of {VALID_SIDES}"

    if qty <= 0:
        return False, f"qty must be > 0, got {qty}"

    if price <= 0:
        return False, f"price must be > 0, got {price}"

    pod = pods[pod_id]
    cost = qty * price

    # Selling (closing a long position) — check position exists with enough qty
    if side == "short":
        positions = pod.get("positions", {})
        if ticker not in positions:
            return False, f"Cannot sell {ticker}: no position in {pod_id}"
        held_qty = positions[ticker].get("qty", 0)
        if held_qty < qty:
            return False, (
                f"Cannot sell {qty} of {ticker}: only {held_qty} held in {pod_id}"
            )
        return True, "OK"

    # Buying (long) — check cash
    cash = pod.get("cash_balance", 0.0)
    if cash < cost:
        return False, (
            f"Insufficient cash in {pod_id}: need ${cost:,.2f}, "
            f"have ${cash:,.2f} (shortfall ${cost - cash:,.2f})"
        )

    return True, "OK"


def record_trade(
    pod_id: str,
    ticker: str,
    qty: float,
    price: float,
    side: str,
    signal_source: str = "unknown",
) -> dict:
    """
    Record a completed trade in the ledger and update pod balances.

    For BUY (side='long'):
      - Deduct qty * price from cash_balance
      - Add or update position

    For SELL (side='short'):
      - Add qty * price proceeds to cash_balance
      - Reduce or remove position

    Returns updated pod summary dict.
    Raises ValueError if basic sanity checks fail (use can_trade first).
    """
    data = _load()
    pods = data["pods"]

    if pod_id not in pods:
        raise ValueError(f"Pod '{pod_id}' not found")

    pod = pods[pod_id]
    positions = pod.setdefault("positions", {})
    cost = qty * price
    now = _now_iso()

    if side == "long":
        # BUY
        if pod["cash_balance"] < cost:
            raise ValueError(
                f"cash_balance ${pod['cash_balance']:,.2f} < trade cost ${cost:,.2f} for {ticker} in {pod_id}"
            )
        pod["cash_balance"] = round(pod["cash_balance"] - cost, 6)

        if ticker in positions:
            pos = positions[ticker]
            old_qty = pos["qty"]
            old_avg = pos["avg_entry"]
            new_qty = old_qty + qty
            new_avg = ((old_qty * old_avg) + (qty * price)) / new_qty
            pos["qty"] = new_qty
            pos["avg_entry"] = round(new_avg, 6)
            pos["current_price"] = price
        else:
            positions[ticker] = {
                "qty": qty,
                "avg_entry": price,
                "current_price": price,
                "side": "long",
                "signal_source": signal_source,
                "opened_at": now,
            }

        log.info("BUY %s x%.2f @ $%.2f in %s (cost $%.2f)", ticker, qty, price, pod_id, cost)

    elif side == "short":
        # SELL
        if ticker not in positions:
            raise ValueError(f"No position in {ticker} to sell in {pod_id}")
        pos = positions[ticker]
        held = pos["qty"]
        if held < qty:
            raise ValueError(f"Cannot sell {qty} of {ticker}: only {held} held")

        proceeds = qty * price
        pod["cash_balance"] = round(pod["cash_balance"] + proceeds, 6)

        remaining = held - qty
        if remaining <= 1e-9:
            del positions[ticker]
            log.info("SELL (full close) %s x%.2f @ $%.2f in %s (proceeds $%.2f)", ticker, qty, price, pod_id, proceeds)
        else:
            pos["qty"] = remaining
            log.info("SELL (partial) %s x%.2f @ $%.2f in %s (proceeds $%.2f, %.2f remaining)", ticker, qty, price, pod_id, proceeds, remaining)
    else:
        raise ValueError(f"Unknown side '{side}'")

    # Recalculate positions_value and total_value
    pos_val = sum(p["qty"] * p["current_price"] for p in positions.values())
    pod["positions_value"] = round(pos_val, 2)
    pod["total_value"] = round(pod["cash_balance"] + pos_val, 2)

    data["last_updated"] = now
    _save(data)
    return get_pod_summary(pod_id)


def update_prices(price_dict: Dict[str, float]) -> None:
    """
    Mark positions to market using a {ticker: price} dict.
    Updates current_price on each matching position across all pods.
    Recalculates positions_value, total_value, and pnl_today (% move today).
    """
    data = _load()
    now = _now_iso()

    for pod_id, pod in data["pods"].items():
        positions = pod.get("positions", {})
        pnl_today_contrib = 0.0
        total_pos_val = 0.0

        for ticker, pos in positions.items():
            if ticker in price_dict:
                old_price = pos.get("current_price", price_dict[ticker])
                new_price = price_dict[ticker]
                pct_move = ((new_price - old_price) / old_price * 100.0) if old_price > 0 else 0.0
                pos["current_price"] = new_price
                pos_value = pos["qty"] * new_price
                pnl_today_contrib += pct_move * pos_value
                total_pos_val += pos_value
            else:
                total_pos_val += pos["qty"] * pos.get("current_price", 0.0)

        pod["positions_value"] = round(total_pos_val, 2)
        pod["total_value"] = round(pod["cash_balance"] + total_pos_val, 2)

        # pnl_today as weighted average % move across positions
        if total_pos_val > 0:
            pod["pnl_today"] = round(pnl_today_contrib / total_pos_val, 4)

    data["last_updated"] = now
    _save(data)
    log.info("Prices updated for %d tickers", len(price_dict))


def reconcile_to_broker(
    broker_positions: Dict[str, dict],
    broker_equity: float,
    broker_cash: float,
    apply: bool = True,
) -> dict:
    """Mark the pod ledger to the broker and DECOMPOSE what still does not match.

    Why this exists (2026-08-19)
    ----------------------------
    `reconciliation_check` had been raising

        [HIGH] LEDGER_DRIFT — pod ledger ($104,284) diverges -6.3% from the real
                              account ($98,066) ... Reconcile pod cash/positions
                              to Alpaca.

    on every run for weeks. Two things were wrong with it as a finding.

    First, `update_prices()` — documented at the top of this module as "call
    daily to mark positions to market" — **had no caller anywhere in the firm**.
    Every ETF-pod position still carried `current_price == avg_entry` exactly,
    i.e. it had never been marked once since it was opened. A ledger that is
    never revalued does not drift, it simply stops being a measurement.

    Second, the flag named a total and no cause. "-6.3%" cannot be acted on:
    stale prices are a bug in this module, a broker position no pod claims is a
    bug in trade attribution, and a cash gap is neither. One number for three
    unrelated defects is a number nobody can close, which is why it stayed open.

    So this marks to market from broker prices — the honest, mechanical part —
    and then reports the residual split by cause. It deliberately does NOT
    invent pod positions, move cash between pods, or scale `total_value` to make
    the sum come out right. A reconciliation that forces agreement destroys the
    evidence of the thing it was built to find.

    `broker_positions` is {SYMBOL: {"qty": float, "price": float}}.
    Returns the decomposition; writes marked prices back when `apply`.
    """
    data = _load()
    pods = data.get("pods", {})
    bp = {str(k).upper(): v for k, v in (broker_positions or {}).items()}

    stale_delta = 0.0
    unpriced: List[str] = []
    ledgered: Dict[str, float] = {}          # SYMBOL -> qty claimed across pods

    for pod_id, pod in pods.items():
        positions = pod.get("positions", {})
        pos_val = 0.0
        for ticker, pos in positions.items():
            sym = str(ticker).upper()
            qty = float(pos.get("qty", 0.0) or 0.0)
            ledgered[sym] = ledgered.get(sym, 0.0) + qty
            old = float(pos.get("current_price", 0.0) or 0.0)
            new = None
            if sym in bp:
                try:
                    cand = float(bp[sym].get("price"))
                    new = cand if cand == cand and cand > 0 else None
                except (TypeError, ValueError):
                    new = None
            if new is None:
                # No broker price: keep the old one and say so. Marking to zero
                # would "reconcile" the pod by deleting its value.
                unpriced.append(sym)
                pos_val += qty * old
                continue
            stale_delta += qty * (new - old)
            if apply:
                pos["current_price"] = new
            pos_val += qty * new
        if apply:
            pod["positions_value"] = round(pos_val, 2)
            pod["total_value"] = round(float(pod.get("cash_balance", 0.0) or 0.0) + pos_val, 2)

    if apply:
        data["last_updated"] = _now_iso()
        _save(data)

    pod_total = sum(float(p.get("total_value", 0.0) or 0.0) for p in pods.values())
    reserve_bal = float(data.get("reserve", {}).get("balance", 0.0) or 0.0)

    unledgered = {s: v for s, v in bp.items()
                  if float(v.get("qty", 0) or 0) != 0.0 and s not in ledgered}
    phantom = {s: q for s, q in ledgered.items()
               if s not in bp or float(bp[s].get("qty", 0) or 0) == 0.0}

    def _mv(v) -> float:
        try:
            return abs(float(v.get("qty", 0) or 0) * float(v.get("price", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    unledgered_value = round(sum(_mv(v) for v in unledgered.values()), 2)
    pod_cash = round(sum(float(p.get("cash_balance", 0.0) or 0.0) for p in pods.values()), 2)

    residual = round(pod_total + reserve_bal - float(broker_equity or 0.0), 2)

    return {
        "as_of": _now_iso(),
        "broker_equity": round(float(broker_equity or 0.0), 2),
        "broker_cash": round(float(broker_cash or 0.0), 2),
        "pod_total_value": round(pod_total, 2),
        "reserve_balance": reserve_bal,
        "pod_cash_sum": pod_cash,
        "residual": residual,
        "residual_pct": round(residual / float(broker_equity) * 100.0, 2)
                        if broker_equity else 0.0,
        "marked_to_market": round(stale_delta, 2),
        "unpriced_symbols": sorted(set(unpriced)),
        "unledgered_symbols": sorted(unledgered),
        "unledgered_value": unledgered_value,
        "phantom_symbols": sorted(phantom),
        "reserve_is_not_at_broker": reserve_bal > 0.0,
        "causes": _drift_causes(residual, reserve_bal, unledgered_value,
                                pod_cash, float(broker_cash or 0.0), phantom),
    }


def _drift_causes(residual, reserve_bal, unledgered_value, pod_cash,
                  broker_cash, phantom) -> List[str]:
    """Named, individually closable reasons the two totals disagree.

    Ordered largest-first so the flag can quote the one that actually matters.
    An empty list with a non-zero residual is itself informative: it means the
    gap is real and none of the known causes explains it.
    """
    out: List[Tuple[float, str]] = []
    if reserve_bal:
        out.append((reserve_bal,
                    f"the ${reserve_bal:,.0f} reserve is a book entry with no "
                    f"broker counterpart — it inflates the pod total by "
                    f"construction, not by drift"))
    if unledgered_value:
        out.append((unledgered_value,
                    f"${unledgered_value:,.0f} of broker positions belong to no "
                    f"pod, so the pod total cannot see them"))
    if phantom:
        out.append((0.0,
                    f"{len(phantom)} pod position(s) the broker does not hold: "
                    f"{', '.join(sorted(phantom)[:6])}"))
    cash_gap = round(pod_cash - broker_cash, 2)
    if abs(cash_gap) > 1.0:
        out.append((abs(cash_gap),
                    f"pod cash sums to ${pod_cash:,.0f} against ${broker_cash:,.0f} "
                    f"at the broker (${cash_gap:+,.0f})"))
    out.sort(key=lambda t: -t[0])
    return [msg for _, msg in out]


def get_pod_summary(pod_id: str) -> dict:
    """Return a clean summary dict for the requested pod."""
    data = _load()
    pods = data.get("pods", {})

    if pod_id not in pods:
        raise ValueError(f"Pod '{pod_id}' not found. Valid: {list(pods.keys())}")

    pod = pods[pod_id]
    positions = pod.get("positions", {})

    return {
        "pod_id": pod_id,
        "name": pod.get("name"),
        "strategy": pod.get("strategy"),
        "budget_inception": pod.get("budget_inception"),
        "cash_balance": pod.get("cash_balance"),
        "positions_value": pod.get("positions_value"),
        "total_value": pod.get("total_value"),
        "pnl_today": pod.get("pnl_today"),
        "pnl_lifetime": pod.get("pnl_lifetime"),
        "n_positions": len(positions),
        "positions": positions,
    }


def get_firm_summary() -> dict:
    """Return summary of all pods + reserve + firm total."""
    data = _load()
    pods = data.get("pods", {})
    reserve = data.get("reserve", {})

    pod_summaries = {}
    firm_total = reserve.get("balance", 0.0)

    for pod_id, pod in pods.items():
        tv = pod.get("total_value", 0.0)
        firm_total += tv
        pod_summaries[pod_id] = {
            "name": pod.get("name"),
            "cash_balance": pod.get("cash_balance"),
            "positions_value": pod.get("positions_value"),
            "total_value": tv,
            "pnl_today": pod.get("pnl_today"),
            "pnl_lifetime": pod.get("pnl_lifetime"),
            "n_positions": len(pod.get("positions", {})),
        }

    return {
        "as_of": data.get("last_updated"),
        "firm_total_value": round(firm_total, 2),
        "reserve": reserve,
        "pods": pod_summaries,
    }


def generate_daily_report() -> dict:
    """
    Write data/pod_pnl_report.json with per-pod P&L breakdown.
    Returns the report dict.
    """
    data = _load()
    pods = data.get("pods", {})
    now = _now_iso()
    report = {"generated_at": now, "pods": {}}

    for pod_id, pod in pods.items():
        positions = pod.get("positions", {})
        total_val = pod.get("total_value", 0.0)
        pos_val = pod.get("positions_value", 0.0)
        cash = pod.get("cash_balance", 0.0)

        # Per-position P&L
        pos_details = []
        for ticker, pos in positions.items():
            avg = pos.get("avg_entry", 0.0)
            cur = pos.get("current_price", avg)
            qty = pos.get("qty", 0.0)
            unrealized_pct = ((cur - avg) / avg * 100.0) if avg > 0 else 0.0
            unrealized_dollar = (cur - avg) * qty
            pos_details.append({
                "ticker": ticker,
                "qty": qty,
                "avg_entry": avg,
                "current_price": cur,
                "unrealized_pct": round(unrealized_pct, 4),
                "unrealized_dollar": round(unrealized_dollar, 2),
                "market_value": round(qty * cur, 2),
            })

        pos_details.sort(key=lambda x: x["unrealized_pct"], reverse=True)
        top_positions = pos_details[:3] if pos_details else []
        worst_positions = pos_details[-3:] if len(pos_details) >= 3 else (pos_details[::-1] if pos_details else [])

        cash_pct = round(cash / total_val * 100, 2) if total_val > 0 else 100.0
        pos_pct = round(pos_val / total_val * 100, 2) if total_val > 0 else 0.0

        report["pods"][pod_id] = {
            "name": pod.get("name"),
            "total_value": total_val,
            "cash_balance": cash,
            "positions_value": pos_val,
            "cash_pct": cash_pct,
            "positions_pct": pos_pct,
            "pnl_today": pod.get("pnl_today", 0.0),
            "pnl_lifetime": pod.get("pnl_lifetime", 0.0),
            "n_positions": len(positions),
            "top_positions": top_positions,
            "worst_positions": worst_positions,
        }

    # Firm-level totals
    reserve_bal = data.get("reserve", {}).get("balance", 0.0)
    firm_total = reserve_bal + sum(p.get("total_value", 0.0) for p in pods.values())
    report["firm_total_value"] = round(firm_total, 2)
    report["reserve_balance"] = reserve_bal

    with open(_REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)

    log.info("Daily P&L report written to %s", _REPORT_PATH)
    return report


# ---------------------------------------------------------------------------
# CLI / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    print("\n=== APEX CAPITAL — FIRM SUMMARY ===")
    summary = get_firm_summary()
    pprint.pprint(summary)

    print("\n=== POD 1 DETAIL ===")
    pprint.pprint(get_pod_summary("pod1_rd"))

    print("\n=== GENERATING DAILY REPORT ===")
    report = generate_daily_report()
    print(f"Firm total: ${report['firm_total_value']:,.2f}")
    for pid, pd in report["pods"].items():
        print(f"  {pid}: total=${pd['total_value']:,.2f}, cash%={pd['cash_pct']}%, pos%={pd['positions_pct']}%, pnl_today={pd['pnl_today']}")
