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
import logging
import os
import tempfile
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
    """Write pod_allocations.json atomically using a temp file + rename."""
    # A FIXED ".json.tmp" path is shared by every concurrent writer, and
    # open(tmp, "w") truncates BEFORE flock() is acquired — so a second writer
    # empties the first writer's temp file mid-dump, and the first writer then
    # renames the torn result over the live file. The lock cannot help: the
    # damage is done at open(). mkstemp gives each writer its own name, which
    # makes the lock unnecessary — os.replace is already atomic.
    fd, tmp = tempfile.mkstemp(dir=str(Path(_ALLOCATIONS_PATH).parent),
                               prefix=".pod_ledger.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _ALLOCATIONS_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _f(v, default: float = 0.0) -> float:
    """float() that refuses NaN and inf.

    `float(x or 0.0)` was used throughout this module and it is not safe here:
    NaN is truthy, survives the `or`, passes every `is not None` guard and then
    poisons every sum it touches into NaN (see memory `nan-is-not-none`). It
    also silently turns None into 0.0, which reads as "this pod holds nothing"
    rather than "this was not measured" — so callers that need that distinction
    must check for None themselves, not pass a default.
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return x


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
        pod["cash_balance"] = round(_f(pod.get("cash_balance")) + proceeds, 6)
        # Realised P&L, kept so budgets can follow performance (2026-11-09).
        # Until then nothing recorded it: pnl_lifetime was 0.0 for every pod.
        realised = qty * (price - _f(pos.get("avg_entry"), price))
        pod["pnl_realised_lifetime"] = round(
            _f(pod.get("pnl_realised_lifetime")) + realised, 2)

        remaining = held - qty
        if remaining <= 1e-9:
            del positions[ticker]
            log.info("SELL (full close) %s x%.2f @ $%.2f in %s (proceeds $%.2f)", ticker, qty, price, pod_id, proceeds)
        else:
            pos["qty"] = remaining
            log.info("SELL (partial) %s x%.2f @ $%.2f in %s (proceeds $%.2f, %.2f remaining)", ticker, qty, price, pod_id, proceeds, remaining)
    else:
        raise ValueError(f"Unknown side '{side}'")

    # Recalculate deployed value and budget headroom. `cash_balance` is the
    # pod's remaining ENTITLEMENT, not money it holds — see the semantics note
    # in sync_from_broker. Adding it to a market value is the arithmetic that
    # made the pods report $104,646 against $98,965 of real equity, so
    # `total_value` is None rather than a plausible wrong number.
    pos_val = sum(p["qty"] * p["current_price"] for p in positions.values())
    pod["positions_value"] = round(pos_val, 2)
    pod["deployed_value"] = round(pos_val, 2)
    pod["budget_remaining"] = round(pod["cash_balance"], 2)
    pod["total_value"] = None

    # Every path that creates a position must also say who owns it, or the
    # ledger goes back to being told about a quarter of the firm's trades.
    # Written into `data` rather than via claim(), which does its own
    # load/save — that round trip would be overwritten by the _save() below.
    data.setdefault("attribution", {})[str(ticker).upper()] = {
        "pod": pod_id, "source": signal_source or "record_trade", "at": now}

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
        pod["deployed_value"] = round(total_pos_val, 2)
        pod["budget_remaining"] = round(pod["cash_balance"], 2)
        pod["total_value"] = None      # see sync_from_broker's semantics note

        # pnl_today as weighted average % move across positions
        if total_pos_val > 0:
            pod["pnl_today"] = round(pnl_today_contrib / total_pos_val, 4)

    data["last_updated"] = now
    _save(data)
    log.info("Prices updated for %d tickers", len(price_dict))


# ===========================================================================
# ATTRIBUTION — who owns which position, recorded where every order passes
# ===========================================================================
#
# On 2026-08-19 the pod ledger and the broker shared exactly ONE symbol out of
# 33 (GLD), and disagreed on its quantity. The ledger claimed 13 positions the
# broker does not hold, all opened 2026-05-14; the broker held 19 the ledger
# had never heard of. The ledger had not described the firm's book for three
# months.
#
# The cause is the bug class this firm keeps re-finding: `record_trade` is
# called from TWO modules (manager_agent, main.py's ETF pod) across four call
# sites, while FIFTEEN modules call `TradingClient.submit_order`. Attribution
# was recorded on a quarter of the paths that create the thing being
# attributed, so the ledger did not drift — it simply stopped being told.
#
# So the claim is recorded at the same choke point the order stamp and the risk
# gate use. A caller that never declares a pod does not vanish: it lands in
# `unattributed`, which is a visible bucket with a count and a dollar value.
# An absence you can see is a finding; an absence you cannot see is what this
# ledger has been for three months.

UNATTRIBUTED = "unattributed"

_attribution_stack: List[str] = []


class attributing:
    """Declare which pod the orders inside this block belong to.

        with pod_ledger.attributing("pod3_macro"):
            trader.client.submit_order(req)

    Deliberately a declaration and not an inference. The choke point can see
    the calling MODULE, and mapping module->pod would be a guess — head_agent
    could plausibly be pod1_rd or firm-level, and a guess recorded as a fact is
    indistinguishable downstream from a measurement.
    """

    def __init__(self, pod_id: str):
        self.pod_id = str(pod_id)

    def __enter__(self):
        _attribution_stack.append(self.pod_id)
        return self

    def __exit__(self, *exc):
        try:
            _attribution_stack.pop()
        except IndexError:      # pragma: no cover — unbalanced, but never raise
            pass
        return False


def current_attribution() -> Optional[str]:
    """The innermost declared pod, or None. None means 'nobody said'."""
    return _attribution_stack[-1] if _attribution_stack else None


def claim(symbol: str, pod_id: Optional[str] = None, source: str = "") -> None:
    """Record that `pod_id` owns `symbol`. Never raises — the caller is a hook.

    An undeclared order is claimed for `unattributed` WITH the submitting
    module recorded, so the gap has a name and a suspect rather than being
    silently absent.
    """
    try:
        sym = str(symbol).upper().strip()
        if not sym:
            return
        pod = str(pod_id) if pod_id else (current_attribution() or UNATTRIBUTED)
        data = _load()
        book = data.setdefault("attribution", {})
        prior = book.get(sym)
        if prior and prior.get("pod") not in (pod, UNATTRIBUTED) and pod == UNATTRIBUTED:
            # An undeclared add to a position a pod already owns does not
            # take it away from that pod.
            return
        book[sym] = {"pod": pod, "source": source or "unknown", "at": _now_iso()}
        data["last_updated"] = _now_iso()
        _save(data)
    except Exception as e:                                  # pragma: no cover
        log.warning("attribution claim failed for %s: %s", symbol, e)


def attribution_map() -> Dict[str, str]:
    """{SYMBOL: pod_id}, merging the explicit book with existing pod holdings."""
    data = _load()
    out: Dict[str, str] = {}
    for pod_id, pod in data.get("pods", {}).items():
        for sym in pod.get("positions", {}):
            out[str(sym).upper()] = pod_id
    for sym, rec in (data.get("attribution") or {}).items():
        out.setdefault(str(sym).upper(), rec.get("pod", UNATTRIBUTED))
    return out


def cap_buy_qty(qty: int, price: float, held_value: float, equity: float,
                cap_pct: Optional[float] = None) -> tuple:
    """(qty, reason): a pod BUY shrunk so the position stays within the firm's
    single-name cap (config.MAX_POSITION_SIZE_PCT of equity). reason is "" when
    the order fits as asked.

    2026-11-09: Pod 3 split its $30k budget into ~$10k per ETF and bought TLT,
    XLP and XLV at ~10.4% of a $96k account each. review_agent flags anything
    over 7.7% for a TRIM, the Canary was ORANGE, and the manager turns a TRIM
    into a full SELL on an ORANGE day: all three were sold two hours after they
    filled, at a small loss plus the spread. A pod may not buy what the firm's
    own risk rule will sell on its next review."""
    if cap_pct is None:
        try:
            import config
            cap_pct = float(config.MAX_POSITION_SIZE_PCT)
        except Exception:
            cap_pct = 0.07
    qty, price, equity = int(qty), _f(price), _f(equity)
    if qty <= 0 or price <= 0 or equity <= 0:
        return 0, "cannot size: qty, price or equity is not positive"
    room = cap_pct * equity - max(0.0, _f(held_value))
    fit = max(0, int(room // price))
    if fit >= qty:
        return qty, ""
    return fit, ("single-name cap %.0f%% of $%s equity leaves room for %d of %d"
                 % (cap_pct * 100, format(round(equity), ","), fit, qty))


def firm_buy_block(canary_level: str = "", cro_level: str = "",
                   cvar_pct: float = 0.0, cvar_limit_pct: float = 5.0) -> Optional[str]:
    """Why no pod may buy right now, or None. The same three gates the manager
    applies to its own buys (manager_agent.run_manager: Canary ORANGE/RED,
    CRO HIGH/CRITICAL, 1-day CVaR over 5% of the portfolio).

    2026-11-09 13:45: the manager rejected XLP under the CVaR breaker ("All
    BUY/ADD orders blocked this session"); at 13:54 Pod 3 bought XLP anyway,
    because the ETF pod's orders never passed through any of these gates."""
    if str(canary_level).upper() in ("ORANGE", "RED"):
        return "Canary %s: new buys blocked" % str(canary_level).upper()
    if str(cro_level).upper() in ("HIGH", "CRITICAL"):
        return "CRO risk %s: new buys blocked" % str(cro_level).upper()
    if _f(cvar_pct) > cvar_limit_pct:
        return ("CVaR breaker: 1-day CVaR %.1f%% of portfolio exceeds %.0f%%"
                % (_f(cvar_pct), cvar_limit_pct))
    return None


def holder(ticker: str) -> Optional[tuple]:
    """(pod_id, qty) of the pod whose ledger holds a long in `ticker`, or None.

    A sell must be booked to the pod that bought, not to whichever pod the
    selling decision's source_agent name suggests. Until 2026-11-09 the manager
    guessed the pod from source_agent, which defaults to pod1_rd: when it sold
    Pod 3's TLT/XLP/XLV that day the ledger raised "No position in TLT to sell
    in pod1_rd", the warning was swallowed, and Pod 3 kept the positions (and
    not the cash) on paper."""
    sym = str(ticker).upper().strip()
    for pod_id, pod in (_load().get("pods") or {}).items():
        for t, pos in (pod.get("positions") or {}).items():
            if str(t).upper() == sym and _f(pos.get("qty")) > 0:
                return pod_id, _f(pos.get("qty"))
    return None


def sync_from_broker(broker_positions: Dict[str, dict],
                     apply: bool = True) -> dict:
    """Make the BROKER the source of truth for every position the pods claim.

    `update_prices()` marked prices and left quantities alone, which is why a
    claim could survive the position being closed. The broker is the only
    record both hosts share (there is no state sync between the Mac and the
    VM), so it is the only thing entitled to say what the firm owns.

    Three outcomes, all of them recorded rather than assumed:

      * the broker holds it       -> quantity AND price are overwritten from
                                     the broker; a changed quantity is reported
                                     as `requantified`, not quietly applied
      * the broker does not       -> the claim is RETIRED into `retired_claims`
                                     with its last known marks. Deleting it
                                     would make a position that was closed
                                     elsewhere look like one that never existed
      * nobody claims it          -> it goes to `unattributed`, with a value

    Writes only on the designated writer host: the ledger is host-local state,
    and two hosts reconciling the same shared account against their own private
    copies is the failure this firm already had once.
    """
    data = _load()
    pods = data.get("pods", {})
    bp = {str(k).upper(): v for k, v in (broker_positions or {}).items()}

    def _f(v, default=0.0) -> float:
        try:
            x = float(v)
            return x if x == x and x not in (float("inf"), float("-inf")) else default
        except (TypeError, ValueError):
            return default

    # Who claims what, and how much of it, so a symbol two pods claim can be
    # split rather than double-counted.
    claimed_qty: Dict[str, float] = {}
    for pod in pods.values():
        for sym, pos in pod.get("positions", {}).items():
            s = str(sym).upper()
            claimed_qty[s] = claimed_qty.get(s, 0.0) + abs(_f(pos.get("qty")))

    retired: List[dict] = []
    requantified: List[dict] = []
    deployed: Dict[str, float] = {}
    attributed: set = set()          # survives the sync, i.e. some pod owns it

    for pod_id, pod in pods.items():
        positions = pod.get("positions", {})
        keep: Dict[str, dict] = {}
        pod_deployed = 0.0
        for sym_raw, pos in list(positions.items()):
            sym = str(sym_raw).upper()
            broker = bp.get(sym)
            b_qty = _f(broker.get("qty")) if broker else 0.0
            if not broker or b_qty == 0.0:
                retired.append({
                    "symbol": sym, "pod": pod_id,
                    "claimed_qty": _f(pos.get("qty")),
                    "last_price": _f(pos.get("current_price"), _f(pos.get("avg_entry"))),
                    "opened_at": pos.get("opened_at"),
                    "retired_at": _now_iso(),
                    "reason": "the broker does not hold it — closed elsewhere, or "
                              "never opened on this host",
                })
                continue
            share = 1.0
            total_claim = claimed_qty.get(sym, 0.0)
            mine = abs(_f(pos.get("qty")))
            if total_claim > 0 and mine > 0:
                share = mine / total_claim
            new_qty = b_qty * share
            new_price = _f(broker.get("price"), _f(pos.get("current_price")))
            if abs(new_qty - _f(pos.get("qty"))) > 1e-6:
                requantified.append({
                    "symbol": sym, "pod": pod_id,
                    "ledger_qty": _f(pos.get("qty")),
                    "broker_qty": round(new_qty, 6),
                    "delta_value": round(
                        (abs(new_qty) - abs(_f(pos.get("qty")))) * new_price, 2),
                })
            pos = dict(pos)
            pos["qty"] = round(new_qty, 6)
            pos["current_price"] = new_price
            keep[sym] = pos
            attributed.add(sym)
            pod_deployed += abs(new_qty) * new_price
        deployed[pod_id] = round(pod_deployed, 2)
        if apply:
            pod["positions"] = keep

    unattributed = {s: {"qty": _f(v.get("qty")), "price": _f(v.get("price"))}
                    for s, v in bp.items()
                    if _f(v.get("qty")) != 0.0 and s not in attributed}
    unattributed_value = round(
        sum(abs(v["qty"]) * v["price"] for v in unattributed.values()), 2)

    breaches: List[dict] = []
    if apply:
        for pod_id, pod in pods.items():
            budget = _f(pod.get("budget"), _f(pod.get("budget_inception")))
            dep = deployed.get(pod_id, 0.0)
            pod["deployed_value"] = dep
            pod["positions_value"] = dep
            # `cash_balance` never was money. There is ONE cash balance and it
            # is at the broker; three pods each holding their own is a category
            # error that let $40,998 of "pod cash" stand against $13,262 real.
            # It is budget headroom — how much more this pod may deploy.
            pod["budget_remaining"] = round(max(0.0, budget - dep), 2)
            pod["cash_balance"] = pod["budget_remaining"]
            # `total_value` used to be headroom + market value: an entitlement
            # added to a price. That sum is what made the pods report $104,646
            # against $98,965 of real equity. There is no such quantity, so the
            # key is retired rather than redefined — a reader that wants it now
            # gets None and fails, instead of a number that looks right.
            pod["total_value"] = None
            pod["_value_semantics"] = (
                "deployed_value = broker market value of attributed positions. "
                "budget_remaining = unspent entitlement, a claim on FIRM cash, "
                "not cash this pod holds. They are not addable. Firm value is "
                "broker equity — see reconciliation_check."
            )
            if dep > budget + 1.0:
                breaches.append({"pod_id": pod_id, "budget": budget,
                                 "deployed": dep, "over": round(dep - budget, 2)})

        data["attribution"] = data.get("attribution", {})
        for sym in unattributed:
            data["attribution"].setdefault(
                sym, {"pod": UNATTRIBUTED, "source": "found at broker, unclaimed",
                      "at": _now_iso()})
        data["unattributed"] = unattributed
        if retired:
            data.setdefault("retired_claims", []).extend(retired)
            data["retired_claims"] = data["retired_claims"][-500:]
        data["last_updated"] = _now_iso()
        # Every host reconciles its OWN copy, deliberately — no single_writer
        # gate here. The gate exists to stop two hosts making conflicting
        # DECISIONS; this makes none. It is a pure projection of shared broker
        # truth onto host-local claims, it is idempotent, and gating it would
        # leave the non-designated host's ledger stale forever while still
        # flagging on it — which is the "always fires, never closable" failure
        # this whole rewrite exists to remove. The two hosts will disagree about
        # WHO owns a symbol (each only saw its own orders) and that disagreement
        # is honest: it shows up as `unattributed` on the host that didn't place
        # it. Record which host wrote this so the difference is readable.
        try:
            import single_writer
            data["reconciled_by_host"] = single_writer.this_host()
        except Exception:
            data["reconciled_by_host"] = None
        _save(data)

    return {
        "as_of": _now_iso(),
        "applied": bool(apply),
        "broker_symbols": len(bp),
        "attributed_symbols": sorted(attributed),
        "retired_claims": retired,
        "requantified": requantified,
        "deployed_by_pod": deployed,
        "unattributed_symbols": sorted(unattributed),
        "unattributed_value": unattributed_value,
        "unattributed_pct_of_book": round(
            unattributed_value / sum(abs(_f(v.get("qty"))) * _f(v.get("price"))
                                     for v in bp.values()) * 100.0, 1)
            if bp and sum(abs(_f(v.get("qty"))) * _f(v.get("price"))
                          for v in bp.values()) > 0 else 0.0,
        "budget_breaches": breaches,
    }


def reconcile_to_broker(
    broker_positions: Dict[str, dict],
    broker_equity: float,
    broker_cash: float,
    apply: bool = True,
) -> dict:
    """Sync the pods to the broker, then state what still does not close.

    Why this exists (2026-08-19)
    ----------------------------
    `reconciliation_check` had been raising

        [HIGH] LEDGER_DRIFT — pod ledger ($104,284) diverges -6.3% from the real
                              account ($98,066) ... Reconcile pod cash/positions
                              to Alpaca.

    on every run for weeks. Three things were wrong with it as a finding.

    First, `update_prices()` — documented at the top of this module as "call
    daily to mark positions to market" — **had no caller anywhere in the firm**.
    A ledger that is never revalued does not drift; it stops being a
    measurement.

    Second, the flag named a total and no cause. "-6.3%" cannot be acted on.
    One number for several unrelated defects is a number nobody can close,
    which is why it stayed open.

    Third, and worst: the two sides of the subtraction were not the same kind
    of thing. The pod total was budget headroom PLUS market value — an
    entitlement added to a price — and it was being compared against broker
    equity. Some of the "drift" was not error at all, it was the units.

    So the comparison is now a conservation identity between two quantities
    that genuinely are the same thing:

        sum of pod DEPLOYED value  ==  market value of the broker's positions

    Anything left over is unattributed exposure, and it has symbols. Cash is
    not in the identity because pods do not hold cash — there is one cash
    balance and it is at the broker. Firm value is broker equity; this module
    no longer offers an opinion on it.

    This deliberately does NOT invent positions, move budget between pods, or
    scale anything to make the sum come out right. A reconciliation that forces
    agreement destroys the evidence of the thing it was built to find.

    `broker_positions` is {SYMBOL: {"qty": float, "price": float}}.
    """
    bp = {str(k).upper(): v for k, v in (broker_positions or {}).items()}

    def _f(v, default=0.0) -> float:
        try:
            x = float(v)
            return x if x == x and abs(x) != float("inf") else default
        except (TypeError, ValueError):
            return default

    sync = sync_from_broker(bp, apply=apply)

    data = _load()
    pods = data.get("pods", {})
    reserve_bal = _f(data.get("reserve", {}).get("balance"))

    deployed_total = round(sum(_f(v) for v in sync["deployed_by_pod"].values()), 2)
    broker_pos_value = round(
        sum(abs(_f(v.get("qty"))) * _f(v.get("price")) for v in bp.values()), 2)

    # The identity. Positive residual = the pods claim more exposure than the
    # broker shows; negative = the broker holds exposure no pod owns.
    residual = round(deployed_total - broker_pos_value, 2)

    causes = _drift_causes(
        residual=residual,
        unattributed_value=sync["unattributed_value"],
        unattributed_symbols=sync["unattributed_symbols"],
        retired=sync["retired_claims"],
        requantified=sync["requantified"],
        breaches=sync["budget_breaches"],
    )

    return {
        "as_of": sync["as_of"],
        "identity": "sum(pod deployed_value) == market value of broker positions",
        "broker_equity": round(_f(broker_equity), 2),
        "broker_cash": round(_f(broker_cash), 2),
        "broker_positions_value": broker_pos_value,
        "pod_deployed_total": deployed_total,
        "deployed_by_pod": sync["deployed_by_pod"],
        "residual": residual,
        "residual_pct": round(residual / broker_pos_value * 100.0, 2)
                        if broker_pos_value else 0.0,
        "unattributed_symbols": sync["unattributed_symbols"],
        "unattributed_value": sync["unattributed_value"],
        "unattributed_pct_of_book": sync["unattributed_pct_of_book"],
        "retired_claims": [r["symbol"] for r in sync["retired_claims"]],
        "requantified": sync["requantified"],
        "budget_breaches": sync["budget_breaches"],
        "reserve_balance": reserve_bal,
        "reserve_is_not_at_broker": reserve_bal > 0.0,
        "firm_value_source": "broker equity — the pod ledger does not compute it",
        "causes": causes,
    }


# ---------------------------------------------------------------------------
# Budgets that are backed by cash and follow performance (2026-11-09)
# ---------------------------------------------------------------------------
#
# Chairman, 2026-11-09, on investigator-20261010-etf-pod-budget-unbacked:
# "balance the budget yourself, by the past performances over the past
# months/years". The defect: each pod had a fixed $30k budget, so the pods'
# combined headroom ($70,767 on 10-10) bore no relation to the cash the broker
# would actually let the firm spend (~$770 above the 20% floor). Pod 3 was
# refused at the broker on every buy, its board re-approved the same buys four
# times a day, and its health check read OK because it counted queued orders.
#
# The fix is an identity, not a tuning: each pod's budget is what it already
# holds plus a share of the cash the firm can actually deploy,
#
#     budget_i = deployed_i + weight_i * max(0, cash - |shorts| - floor_pct * equity)
#
# Short-sale proceeds sit in broker cash but are owed back: on 2026-11-09 cash
# was $112,877 against $96,021 of equity because of $16,856 of ORCL/AVGO
# shorts. Counting them as spendable would hand the pods money the firm owes.
#
# so the pods' combined headroom equals deployable cash exactly, and a board
# can only size buys the broker will accept. Positions are never sold to make
# room (option (b) was not chosen), and a pod above its new budget keeps what
# it holds: budget_remaining is clamped at zero, the same as sync_from_broker.
#
# weight_i follows each pod's return on its inception budget: realised P&L
# (recorded by record_trade from 2026-11-09 on; nothing recorded it before)
# plus unrealised P&L on what it holds now. A pod that never traded scores 0
# and gets a neutral weight. Weights are floored at PERF_WEIGHT_FLOOR so one
# bad stretch cannot starve a pod of capital forever, and no pod can take more
# than PERF_WEIGHT_CAP of new cash.

PERF_WEIGHT_FLOOR = 0.25
PERF_WEIGHT_CAP = 0.60


def pod_performance(data: Optional[dict] = None) -> Dict[str, dict]:
    """{pod_id: {realised, unrealised, return}} from the ledger itself."""
    data = _load() if data is None else data
    out = {}
    for pod_id, pod in (data.get("pods") or {}).items():
        unreal = 0.0
        for pos in (pod.get("positions") or {}).values():
            q = abs(_f(pos.get("qty")))
            unreal += q * (_f(pos.get("current_price"), _f(pos.get("avg_entry")))
                           - _f(pos.get("avg_entry")))
        real = _f(pod.get("pnl_realised_lifetime"))
        base = _f(pod.get("budget_inception"), 30000.0) or 30000.0
        out[pod_id] = {"realised": round(real, 2), "unrealised": round(unreal, 2),
                       "return": round((real + unreal) / base, 4)}
    return out


# Pods that take no share of new cash until they trade. A pod's own "idle"
# field (true/false) overrides this default, so un-idling is a data change.
IDLE_PODS = {"pod2_ml": "idle by Chairman decision 2026-07-03 "
                        "(ML model below the 0.57 AUC live gate)"}


def is_idle(pod_id: str, pod: dict) -> bool:
    """2026-11-09: the first rebalance gave the idle ML pod a third of the
    firm's deployable cash ($25.6k) that it will never spend, leaving the two
    pods that trade a third each. Idle pods keep what they hold (budget =
    deployed) and get no new cash."""
    flag = (pod or {}).get("idle")
    if isinstance(flag, bool):
        return flag
    return pod_id in IDLE_PODS


def performance_weights(perf: Dict[str, dict]) -> Dict[str, float]:
    """Raw score max(FLOOR, 1 + return), normalised, then no pod above CAP
    (excess shared out pro rata among the rest)."""
    raw = {p: max(PERF_WEIGHT_FLOOR, 1.0 + _f(v.get("return"))) for p, v in perf.items()}
    tot = sum(raw.values())
    if not raw or tot <= 0:
        return {}
    w = {p: r / tot for p, r in raw.items()}
    for _ in range(len(w)):
        over = {p: x for p, x in w.items() if x > PERF_WEIGHT_CAP + 1e-12}
        if not over or len(w) * PERF_WEIGHT_CAP < 1.0:
            break
        excess = sum(x - PERF_WEIGHT_CAP for x in over.values())
        rest = {p: x for p, x in w.items() if p not in over}
        rt = sum(rest.values())
        for p in over:
            w[p] = PERF_WEIGHT_CAP
        for p, x in rest.items():
            w[p] = x + excess * (x / rt if rt else 1.0 / len(rest))
    return {p: round(x, 6) for p, x in w.items()}


def rebalance_budgets(broker_equity: float, broker_cash: float,
                      floor_pct: Optional[float] = None,
                      apply: bool = True,
                      short_value: float = 0.0) -> dict:
    """Set every pod's budget to deployed + its weighted share of deployable
    cash. Call after sync_from_broker, so deployed values are the broker's."""
    if floor_pct is None:
        try:
            import config
            floor_pct = float(config.MIN_CASH_FLOOR_PCT)
        except Exception:
            floor_pct = 0.20
    data = _load()
    pods = data.get("pods") or {}
    equity, cash = _f(broker_equity), _f(broker_cash)
    shorts = abs(_f(short_value))
    deployable = round(max(0.0, cash - shorts - floor_pct * equity), 2)
    perf = pod_performance(data)
    idle = sorted(p for p, pod in pods.items() if is_idle(p, pod))
    weights = performance_weights({p: v for p, v in perf.items() if p not in idle})
    plan = {}
    for pod_id, pod in pods.items():
        dep = _f(pod.get("deployed_value"), _f(pod.get("positions_value")))
        share = round(deployable * weights.get(pod_id, 0.0), 2)
        plan[pod_id] = {"deployed": round(dep, 2), "weight": weights.get(pod_id, 0.0),
                        "return": perf.get(pod_id, {}).get("return"),
                        "budget_before": _f(pod.get("budget"), _f(pod.get("budget_inception"))),
                        "budget": round(dep + share, 2), "headroom": share,
                        "idle": pod_id in idle}
        if apply:
            pod["budget"] = plan[pod_id]["budget"]
            pod["budget_remaining"] = share
            pod["cash_balance"] = share
    report = {"as_of": _now_iso(), "equity": round(equity, 2), "cash": round(cash, 2),
              "short_proceeds_excluded": round(shorts, 2),
              "floor_pct": floor_pct, "deployable_cash": deployable,
              "headroom_total": round(sum(v["headroom"] for v in plan.values()), 2),
              "pods": plan, "idle_pods": idle,
              "rule": "budget = deployed + weight x max(0, cash - |shorts| - floor x equity); "
                      "weight from return on inception budget, floored "
                      f"{PERF_WEIGHT_FLOOR}, capped {PERF_WEIGHT_CAP}"}
    if apply:
        data["budget_rebalance"] = report
        _save(data)
    return report


def _drift_causes(residual: float,
                  unattributed_value: float,
                  unattributed_symbols: List[str],
                  retired: List[dict],
                  requantified: List[dict],
                  breaches: List[dict]) -> List[str]:
    """Named, individually closable reasons the identity does not close.

    Ordered largest-first so the flag can quote the one that actually matters.
    Each line names ONE defect with ONE owner. An empty list with a non-zero
    residual is itself informative: the gap is real and none of the known
    causes explains it — that is the case worth escalating.
    """
    out: List[Tuple[float, str]] = []

    if unattributed_value:
        shown = ", ".join(sorted(unattributed_symbols)[:8])
        more = ("" if len(unattributed_symbols) <= 8
                else f" +{len(unattributed_symbols) - 8} more")
        out.append((abs(unattributed_value),
                    f"${unattributed_value:,.0f} across "
                    f"{len(unattributed_symbols)} broker position(s) belong to "
                    f"no pod, so no pod's budget is charged for them: "
                    f"{shown}{more}"))

    if retired:
        claimed = sum(float(r.get("claimed_qty") or 0.0)
                      * float(r.get("last_price") or 0.0) for r in retired)
        shown = ", ".join(sorted(r["symbol"] for r in retired)[:8])
        out.append((abs(claimed),
                    f"{len(retired)} pod position(s) the broker does not hold "
                    f"(${claimed:,.0f} of claimed value, now retired): {shown}"))

    if requantified:
        worst = max(requantified,
                    key=lambda r: abs(float(r.get("delta_value") or 0.0)))
        out.append((abs(float(worst.get("delta_value") or 0.0)),
                    f"{len(requantified)} position(s) held at a quantity the "
                    f"broker disagrees with — largest {worst['symbol']} "
                    f"{worst.get('ledger_qty')} vs {worst.get('broker_qty')} "
                    f"at the broker"))

    for b in breaches:
        out.append((abs(float(b.get("over") or 0.0)),
                    f"{b.get('pod_id')} has deployed ${b.get('deployed'):,.0f} "
                    f"against a ${b.get('budget'):,.0f} budget "
                    f"(${b.get('over'):,.0f} over)"))

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
        "budget": pod.get("budget", pod.get("budget_inception")),
        "budget_remaining": pod.get("budget_remaining", pod.get("cash_balance")),
        "deployed_value": pod.get("deployed_value", pod.get("positions_value")),
        "cash_balance": pod.get("cash_balance"),
        "positions_value": pod.get("positions_value"),
        "total_value": pod.get("total_value"),
        "pnl_today": pod.get("pnl_today"),
        "pnl_lifetime": pod.get("pnl_lifetime"),
        "n_positions": len(positions),
        "positions": positions,
    }


def get_firm_summary() -> dict:
    """Per-pod state. Deliberately does NOT return a firm total.

    It used to: `firm_total_value` summed each pod's `total_value` plus the
    reserve. `total_value` was budget headroom PLUS market value — an
    entitlement added to a price — and the reserve is a book entry with no
    counterpart at the broker. The sum came to $104,646 against $98,965 of real
    equity, and nothing about it was fixable, because the quantity it named
    does not exist.

    The firm's value is the broker's equity. Pods are an ATTRIBUTION of that
    equity, not custody accounts holding their own money, so `deployed_value`
    (real, from the broker) and `budget_remaining` (an entitlement, a claim on
    firm cash) are returned as two numbers that must not be added.
    """
    data = _load()
    pods = data.get("pods", {})
    reserve = data.get("reserve", {})

    pod_summaries = {}
    for pod_id, pod in pods.items():
        pod_summaries[pod_id] = {
            "name": pod.get("name"),
            "budget": pod.get("budget", pod.get("budget_inception")),
            "budget_remaining": pod.get("budget_remaining",
                                        pod.get("cash_balance")),
            "deployed_value": pod.get("deployed_value",
                                      pod.get("positions_value")),
            "cash_balance": pod.get("cash_balance"),
            "positions_value": pod.get("positions_value"),
            "pnl_today": pod.get("pnl_today"),
            "pnl_lifetime": pod.get("pnl_lifetime"),
            "n_positions": len(pod.get("positions", {})),
        }

    return {
        "as_of": data.get("last_updated"),
        "firm_total_value": None,
        "firm_total_value_note": (
            "Removed on purpose. The firm's value is broker equity — read it "
            "from reconciliation_check / data/firm_value.json. Summing pod "
            "totals added entitlements to market values and overstated the "
            "firm by $5,681."),
        "deployed_total": round(sum(
            float(p.get("deployed_value") or p.get("positions_value") or 0.0)
            for p in pods.values()), 2),
        "budget_remaining_total": round(sum(
            float(p.get("budget_remaining") if p.get("budget_remaining") is not None
                  else (p.get("cash_balance") or 0.0))
            for p in pods.values()), 2),
        "unattributed": data.get("unattributed", {}),
        "reserve": reserve,
        "reserve_note": ("An entitlement the board may allocate, not cash held "
                         "anywhere. It has no counterpart at the broker."),
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
        # `total_value` is gone on purpose (see get_firm_summary). The
        # denominator for "how much of this pod is deployed" is its BUDGET —
        # a constant the pod was granted — not headroom-plus-market-value,
        # which moved every time a price moved and so made the percentages
        # mean nothing.
        budget = _f(pod.get("budget"), _f(pod.get("budget_inception")))
        pos_val = _f(pod.get("deployed_value"), _f(pod.get("positions_value")))
        remaining = _f(pod.get("budget_remaining"), _f(pod.get("cash_balance")))

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

        unspent_pct = round(remaining / budget * 100, 2) if budget > 0 else 100.0
        deployed_pct = round(pos_val / budget * 100, 2) if budget > 0 else 0.0

        report["pods"][pod_id] = {
            "name": pod.get("name"),
            "total_value": None,
            "budget": budget,
            "budget_remaining": remaining,
            "deployed_value": pos_val,
            "cash_balance": remaining,
            "positions_value": pos_val,
            "unspent_pct_of_budget": unspent_pct,
            "deployed_pct_of_budget": deployed_pct,
            "cash_pct": unspent_pct,
            "positions_pct": deployed_pct,
            "pnl_today": pod.get("pnl_today", 0.0),
            "pnl_lifetime": pod.get("pnl_lifetime", 0.0),
            "n_positions": len(positions),
            "top_positions": top_positions,
            "worst_positions": worst_positions,
        }

    # Firm-level. NOT a total: this report used to add the reserve to a sum of
    # pod `total_value`, which was entitlement + market value. It reported
    # $104,646 while the account held $98,965. The firm's value is broker
    # equity and this module is not the place that reads it.
    reserve_bal = _f(data.get("reserve", {}).get("balance"))
    report["firm_total_value"] = None
    report["firm_total_value_note"] = (
        "Broker equity is the firm's value — see reconciliation_check / "
        "data/firm_value.json. Pod budgets are entitlements and are not "
        "addable to market values.")
    report["deployed_total"] = round(
        sum(_f(p.get("deployed_value"), _f(p.get("positions_value")))
            for p in pods.values()), 2)
    report["budget_remaining_total"] = round(
        sum(_f(p.get("budget_remaining"), _f(p.get("cash_balance")))
            for p in pods.values()), 2)
    report["reserve_balance"] = reserve_bal
    report["unattributed_value"] = round(sum(
        abs(_f(v.get("qty"))) * _f(v.get("price"))
        for v in data.get("unattributed", {}).values()), 2)

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
    print(f"Deployed (broker market value, attributed): "
          f"${report['deployed_total']:,.2f}")
    print(f"Budget remaining (entitlement, NOT cash): "
          f"${report['budget_remaining_total']:,.2f}")
    print(f"Unattributed at broker: ${report['unattributed_value']:,.2f}")
    print(f"Firm value: {report['firm_total_value_note']}")
    for pid, pd in report["pods"].items():
        print(f"  {pid}: deployed=${pd['deployed_value']:,.2f} "
              f"({pd['deployed_pct_of_budget']}% of ${pd['budget']:,.0f} budget), "
              f"pnl_today={pd['pnl_today']}")
