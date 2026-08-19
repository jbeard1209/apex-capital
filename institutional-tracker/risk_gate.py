#!/usr/bin/env python3
"""
APEX CAPITAL — PRE-TRADE RISK GATE
===================================
The cash floor and the position cap, enforced at the ONE point every order in
the firm passes through.

WHY THIS MODULE EXISTS
----------------------
`reconciliation_check` reported, standing, on 2026-08-19:

    [HIGH] CASH_FLOOR     cash is 6.0% of portfolio ($5,863) — below 20% floor.
                          A buy bypassed the cash floor guardrail.
    [HIGH] MAX_POSITIONS  21 positions open — exceeds 20 maximum.

The flag text says "bypassed", which reads like a check was defeated. It was
not. **The check was only ever on one of the sixteen paths that reach the
broker.** `AlpacaTrader.submit_buy` consults `MIN_CASH_FLOOR_PCT` and
`MAX_OPEN_POSITIONS`; every one of these does not:

    head_agent.py:161        OrderSide.BUY, "submit order directly with our
                             dollar-sized share count"
    alpaca_trader.py:645     regime hedge BUY  (SH)
    alpaca_trader.py:727     tail hedge BUY    (SH)
    alpaca_trader.py:927     short open (SELL with no position)
    fi_executor.py:431       FI BUY — has a 15%-of-FI_CAPITAL floor of its own,
                             which is not the firm's 20%-of-portfolio floor
    main.py:1465             ETF pod, side chosen at runtime

So a limit written in `config.py`, mirrored in `reconciliation_check.py`, and
reported on every run was, in fact, optional. This is the same shape as
`order_ledger` itself: stamping only some callers would have made the rest look
foreign, so the stamp went where every caller already had to go. A risk limit
belongs in exactly the same place, for exactly the same reason — a new caller
cannot forget a check it never had to know about.

WHY THE GATE IS ASYMMETRIC
--------------------------
Identical to `single_writer`, and for the same reason: blocking an ENTRY costs
an opportunity, blocking an EXIT costs money and has no ceiling. So only orders
that INCREASE exposure are gated. A sell against a long, a buy against a short,
a trim, a stop-loss — never blocked, whatever the account looks like. The firm
must always be able to de-risk, and a breach of the cash floor is *healed* by
selling.

The direction of the failure modes follows from that:

  - cannot classify the order (broker unreadable) -> ALLOW, and say so in
    `data/risk_gate_log.json`. A guard that takes the firm offline during an
    API outage is a bigger outage than the one it prevents — but an unverified
    pass must never be indistinguishable from a verified one, or the log
    becomes the "does an explanation exist?" alarm that stays green forever
    (memory `presence-is-not-currency`).
  - classified as an increase, and a limit is breached -> BLOCK, by raising.
    Returning None would let a caller record a position the broker never
    opened, which is the ledger-drift bug class this repo keeps re-learning.

WHAT IT DOES NOT DO
-------------------
It does not close the 21st position, and it does not raise cash. It stops the
breach deepening and makes every future entry answer for itself; draining an
existing breach is the firm's own exit logic, or the Chairman's decision.

There is no exemption list, deliberately — not even for the SH hedge, whose buy
is economically risk-reducing but mechanically cash-spending and slot-taking.
A hedge bought with cash the firm does not have is one of the ways it got to 6%.
If a hedge carve-out is wanted, that is a Chairman decision with a written
reason, not a constant quietly added here.

IN-FLIGHT RESERVATIONS
----------------------
The broker snapshot is TTL-cached and a market order is not a position until it
fills. Five buys inside one pipeline run would otherwise each see the same
pre-trade cash and the same position count, and all five would pass a check that
only one of them should. That race is how a cap of 20 ends up holding 21. Every
allowed entry therefore reserves its symbol and its estimated cost until the
broker confirms it, and the next check sizes against what is left.

Consumers: order_ledger.install() (the one submit_order choke point).
Guarded by test_pipeline_smoke.py::TestPreTradeRiskGate.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent / "data"
GATE_LOG = DATA / "risk_gate_log.json"
MAX_LOG_ENTRIES = 500

# Same TTL as single_writer's position cache and for the same reason: a burst of
# exits must not make one API call per order and rate-limit its own way out.
# Staleness inside the window is covered by the reservations below, not by
# hoping the window is short enough.
SNAPSHOT_TTL_S = 45.0

# How long a submitted-but-unconfirmed entry keeps its reservation. Long enough
# to cover a pipeline run's worth of orders, short enough that a rejected order
# does not sterilise a slot for the rest of the day.
INFLIGHT_TTL_S = 180.0

_snapshot_cache = {"at": 0.0, "snap": None}
_inflight: list = []          # [{"symbol", "cost", "at"}]


class RiskLimitBlocked(RuntimeError):
    """This order would breach a firm risk limit.

    Raised rather than returned as None for the same reason
    `SingleWriterBlocked` is: a caller that treats a blocked order as a placed
    one records a position the broker never opened."""


# ── the limits ───────────────────────────────────────────────────────────────

def limits() -> dict:
    """{"cash_floor": pct, "max_positions": int} — the STRICTER reading.

    Sourced from `reconciliation_check._mirror_check()`, which already holds an
    independent hardcoded copy of each guardrail and returns min/max against
    `config`. Reusing it means the gate and the reconciler cannot drift into two
    different ideas of the limit — the exact failure the mirror was built to
    catch, reproduced one layer down.

    Falls back to the documented constants if that import fails; a gate with no
    numbers would otherwise silently allow everything.
    """
    try:
        from reconciliation_check import _mirror_check
        bounds, _ = _mirror_check()
        return {"cash_floor": float(bounds["MIN_CASH_FLOOR_PCT"]),
                "max_positions": int(bounds["MAX_OPEN_POSITIONS"])}
    except Exception as e:                       # pragma: no cover — import guard
        logger.warning(f"  [risk_gate] could not read guardrail mirror: {e}")
        return {"cash_floor": 0.20, "max_positions": 20}


# ── the broker snapshot ──────────────────────────────────────────────────────

def _finite(x) -> Optional[float]:
    """float(x) if it is a real number, else None.

    NaN is the reason this exists: `float('nan') < 0.20` is False, so a NaN cash
    balance would pass the floor check and a NaN portfolio value would make the
    floor itself NaN. Every threshold in this module reads a number that came
    through here (memory `nan-is-not-none`).
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def snapshot(client=None, force=False):
    """{"cash", "portfolio_value", "positions": {SYM: {"qty", "price"}}} or None.

    One account read and one positions read, together, so the cash and the
    positions the gate reasons about are the same instant. None means "could not
    ask" — a real answer callers must branch on, never {} (which would read as a
    flat, fully-funded account and wave every entry through).
    """
    now = time.time()
    if not force and _snapshot_cache["snap"] is not None \
            and now - _snapshot_cache["at"] < SNAPSHOT_TTL_S:
        return _snapshot_cache["snap"]
    try:
        if client is None:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                os.environ["ALPACA_API_KEY"],
                os.environ["ALPACA_SECRET_KEY"],
                paper=True,
            )
        acct = client.get_account()
        cash = _finite(getattr(acct, "cash", None))
        pv = _finite(getattr(acct, "portfolio_value", None))
        if cash is None or pv is None or pv <= 0:
            logger.warning("  [risk_gate] account read gave no usable cash/equity")
            return None
        positions = {}
        for p in client.get_all_positions():
            q = _finite(getattr(p, "qty", None))
            px = _finite(getattr(p, "current_price", None))
            if q is None:
                continue
            positions[str(p.symbol).upper()] = {"qty": q, "price": px}
        snap = {"cash": cash, "portfolio_value": pv, "positions": positions}
        _snapshot_cache.update(at=now, snap=snap)
        return snap
    except Exception as e:
        logger.warning(f"  [risk_gate] broker snapshot failed: {e}")
        return None


def qty_map(snap) -> Optional[dict]:
    """{SYMBOL: signed_qty}, the shape `single_writer.is_risk_reducing` wants."""
    if not snap:
        return None
    return {s: v["qty"] for s, v in snap.get("positions", {}).items()}


# ── in-flight reservations ───────────────────────────────────────────────────

def reserve(symbol, cost, now=None) -> None:
    """Record an entry this process has submitted but not yet seen fill."""
    _inflight.append({"symbol": str(symbol).upper(),
                      "cost": _finite(cost) or 0.0,
                      "at": now if now is not None else time.time()})


def _live_inflight(snap, now=None):
    """(symbols, dollars) still outstanding. Prunes as a side effect.

    A reservation is retired when it ages out OR when the broker confirms the
    position, whichever comes first — otherwise a filled order would be counted
    twice, once in the snapshot and once here, and the gate would tighten itself
    below the real limit for three minutes after every buy.
    """
    now = now if now is not None else time.time()
    held = set((snap or {}).get("positions", {}))
    kept = [r for r in _inflight
            if now - r["at"] < INFLIGHT_TTL_S and r["symbol"] not in held]
    _inflight[:] = kept
    return {r["symbol"] for r in kept}, sum(r["cost"] for r in kept)


def clear_reservations() -> None:
    """Drop every reservation. For tests and for a fresh process."""
    _inflight.clear()
    _snapshot_cache.update(at=0.0, snap=None)


# ── the check ────────────────────────────────────────────────────────────────

def _is_buy(side) -> bool:
    return "buy" in str(side).lower()


def check(symbol="", side="", qty=0, price=None, snap=None, now=None) -> tuple:
    """(allowed, verdict, reason) for one order. Never raises.

    verdict is one of ALLOW_EXIT / ALLOW_WITHIN_LIMITS / ALLOW_UNVERIFIED /
    BLOCK_CASH_FLOOR / BLOCK_MAX_POSITIONS, so the log names the rule that
    fired rather than only that something did.
    """
    try:
        import single_writer

        if snap is None:
            snap = snapshot()
        positions = qty_map(snap)

        reducing = single_writer.is_risk_reducing(symbol, side, qty, positions)
        if reducing is True:
            return (True, "ALLOW_EXIT",
                    "reduces an open position — exits are never gated")

        if snap is None:
            # Could not read the account, so no limit can be evaluated. Allowing
            # is the lesser harm (see module header), but it is recorded, and
            # `unverified_streak()` below is what turns a run of these into a
            # finding instead of a habit.
            return (True, "ALLOW_UNVERIFIED",
                    "broker snapshot unavailable — limits could not be checked")

        lim = limits()
        sym = str(symbol).upper()
        # Re-validated even though snapshot() already did: a caller may pass a
        # hand-built snap, and a NaN reaching the comparison below would pass
        # the floor silently rather than fail it.
        cash = _finite(snap.get("cash"))
        pv = _finite(snap.get("portfolio_value"))
        if cash is None or pv is None or pv <= 0:
            return (True, "ALLOW_UNVERIFIED",
                    f"account snapshot is not usable (cash={snap.get('cash')!r}, "
                    f"equity={snap.get('portfolio_value')!r})")
        held_syms = {s for s, v in snap["positions"].items() if v["qty"] != 0}
        infl_syms, infl_cost = _live_inflight(snap, now=now)

        # ── position cap ─────────────────────────────────────────────────────
        # Adding to a position the firm already holds does not consume a slot,
        # so it is judged only by the cash floor below. A slot is consumed by a
        # symbol that is new to both the broker and the in-flight set.
        if sym not in held_syms and sym not in infl_syms:
            n_eff = len(held_syms | infl_syms)
            if n_eff >= lim["max_positions"]:
                pend = f" (+{len(infl_syms - held_syms)} in flight)" if infl_syms - held_syms else ""
                return (False, "BLOCK_MAX_POSITIONS",
                        f"{len(held_syms)} positions open{pend} against a cap of "
                        f"{lim['max_positions']}; {sym} would be a new one")

        # ── cash floor ───────────────────────────────────────────────────────
        # Only orders that SPEND cash can breach it. A short open raises cash,
        # so it is capped by slot count above and by MAX_SHORT_POSITIONS in
        # alpaca_trader, not by this rule.
        if _is_buy(side):
            cash_eff = cash - infl_cost
            floor_usd = pv * lim["cash_floor"]
            if cash_eff < floor_usd:
                return (False, "BLOCK_CASH_FLOOR",
                        f"cash is already ${cash_eff:,.0f} "
                        f"({cash_eff / pv * 100:.1f}%) against a "
                        f"{lim['cash_floor'] * 100:.0f}% floor "
                        f"(${floor_usd:,.0f}) — this buy would deepen the breach")

            cost = _order_cost(sym, qty, price, snap)
            if cost is not None and (cash_eff - cost) < floor_usd:
                return (False, "BLOCK_CASH_FLOOR",
                        f"buying {qty} {sym} (~${cost:,.0f}) would leave "
                        f"${cash_eff - cost:,.0f} "
                        f"({(cash_eff - cost) / pv * 100:.1f}%), below the "
                        f"{lim['cash_floor'] * 100:.0f}% floor")
            if cost is None:
                # The projection could not run. Say which half of the rule was
                # actually applied — a reason that claims more than it checked
                # is how a HIGH stops being read.
                return (True, "ALLOW_WITHIN_LIMITS",
                        f"cash ${cash_eff:,.0f} ({cash_eff / pv * 100:.1f}%) is "
                        f"above the floor; {sym} could not be priced, so the "
                        f"post-trade projection did not run")

        return (True, "ALLOW_WITHIN_LIMITS",
                f"within cap {lim['max_positions']} and floor "
                f"{lim['cash_floor'] * 100:.0f}%")
    except Exception as e:
        # A broken gate must not stop the firm trading, and above all must not
        # stop it exiting.
        logger.warning(f"  [risk_gate] check failed open: {e}")
        return (True, "ALLOW_UNVERIFIED", f"gate error: {e}")


def _order_cost(symbol, qty, price, snap) -> Optional[float]:
    """Estimated dollar cost, or None if the order genuinely cannot be priced.

    None is not "assume zero". A cost of zero would make every projection pass,
    turning an unpriceable order into an automatically compliant one.
    """
    p = _finite(price)
    if p is None:
        pos = (snap or {}).get("positions", {}).get(str(symbol).upper())
        if pos:
            p = _finite(pos.get("price"))
    if p is None:
        p = _quote(symbol)
    q = _finite(qty)
    if p is None or q is None or p <= 0:
        return None
    return abs(q) * p


def _quote(symbol) -> Optional[float]:
    """Last ask for a symbol the firm does not hold. Best effort, never raises."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        c = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"],
                                      os.environ["ALPACA_SECRET_KEY"])
        data = c.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=str(symbol).upper()))
        return _finite(data[str(symbol).upper()].ask_price)
    except Exception:
        return None


# ── the record ───────────────────────────────────────────────────────────────

def log(symbol, side, qty, verdict, reason, allowed) -> None:
    """Append one gate decision. Never raises.

    Routine passes are not logged — a log that records every allow is a log
    nobody reads. Blocks and unverified passes are, because those are the two
    states somebody has to act on.
    """
    try:
        entries = []
        if GATE_LOG.exists():
            entries = json.loads(GATE_LOG.read_text()) or []
        entries.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": str(symbol).upper(),
            "side": str(side),
            "qty": str(qty),
            "verdict": verdict,
            "reason": reason,
            "allowed": bool(allowed),
        })
        DATA.mkdir(exist_ok=True)
        GATE_LOG.write_text(json.dumps(entries[-MAX_LOG_ENTRIES:], indent=2))
    except Exception:
        pass


def unverified_streak(entries=None) -> int:
    """How many gate decisions in a row were ALLOW_UNVERIFIED.

    The one number that distinguishes "the API blipped once" from "this gate
    has not actually checked anything for a week". A guard whose failure mode
    is a silent pass needs a counter, or its own outage looks like quiet.
    """
    if entries is None:
        try:
            entries = json.loads(GATE_LOG.read_text()) if GATE_LOG.exists() else []
        except Exception:
            return 0
    n = 0
    for e in reversed(entries or []):
        if e.get("verdict") == "ALLOW_UNVERIFIED":
            n += 1
        else:
            break
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    lim = limits()
    print(f"cash floor: {lim['cash_floor'] * 100:.0f}%   "
          f"position cap: {lim['max_positions']}")
    snap = snapshot()
    if snap is None:
        print("broker unreadable — the gate would allow, unverified")
    else:
        held = {s for s, v in snap["positions"].items() if v["qty"] != 0}
        print(f"equity ${snap['portfolio_value']:,.0f}  "
              f"cash ${snap['cash']:,.0f} "
              f"({snap['cash'] / snap['portfolio_value'] * 100:.1f}%)  "
              f"positions {len(held)}")
        for sym, side in (("SPY", "buy"), ("NVDA", "buy")):
            allowed, verdict, reason = check(sym, side, 1, snap=snap)
            print(f"  {sym:5} {side:4} -> {verdict}: {reason}")
    streak = unverified_streak()
    if streak:
        print(f"\n⚠ last {streak} decision(s) were unverified")
