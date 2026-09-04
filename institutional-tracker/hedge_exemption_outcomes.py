#!/usr/bin/env python3
"""
APEX CAPITAL — HEDGE EXEMPTION OUTCOME TRACKER
==============================================
The pre-trade risk gate exempts one class of order — a structural hedge that
actually reduces measured net beta — from the cash floor and the position cap.
The exemption is narrow by construction, escalated to the Chairman on every
use, and (until now) unmeasured after the fact. That was Jack's condition when
he ratified the carve-out on 2026-09-04:

    "ill allow it but track how it turns out and like if it turns out well then
    great it is ok to do but if it fails make sure there arent exemptions in
    the future or at least its a stricter policy"

So this module records what happens next. Every ALLOW_HEDGE_REDUCES_BETA
verdict writes an outcome row with the pre-trade state (net beta, cash,
equity). A watchdog closes each row when the position closes — or after
`_MAX_DAYS_OPEN` — and grades it against two questions the exemption is meant
to answer:

    (a) did net beta actually fall?
    (b) did the hedge lose money?

A hedge that lost money but shrank net beta still counts as WORKED — the
exemption's job is risk reduction, not P&L. A hedge that lost money AND left
net beta where it was counts as FAILED. UNKNOWN means the closing snapshot
could not be built and is neither.

If the recent failure rate crosses `THRESHOLD_FAILURE_RATE` over
`MIN_SAMPLE` graded outcomes, the carve-out is DISABLED automatically —
`hedge_carveout_state.json` flips `enabled=False`, and `risk_gate._hedge_verdict`
starts refusing exemptions. A Chairman queue entry names the failed outcomes.
Only a human editing the state file re-enables it.

This is the "if it fails, stricter policy" half of the ratification.

The scaffold is idempotent, never raises to the caller (a bookkeeping error in
here must not break a trade), and starts collecting from the first exemption
use — there have been none as of 2026-09-04.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent / "data"
OUTCOMES_PATH = DATA / "hedge_exemption_outcomes.json"
STATE_PATH    = DATA / "hedge_carveout_state.json"

# Grading policy
_MAX_DAYS_OPEN            = 10       # a hedge left on the book longer than
                                     # this is graded on the current snap even
                                     # if the position is still there — a
                                     # "hedge" that becomes a permanent short
                                     # is not what the carve-out authorised.
THRESHOLD_FAILURE_RATE    = 0.40     # >= this share of graded outcomes fails
                                     # → auto-disable. 40% is not "half"; a
                                     # carve-out that fails four times in ten
                                     # is a carve-out that costs more than it
                                     # buys, before the fifth failure lands.
MIN_SAMPLE                = 5        # smallest sample the auto-disable will
                                     # act on. Three failures in three uses
                                     # would otherwise trip the switch on
                                     # bad luck alone. Five is small enough
                                     # to react and large enough to distinguish
                                     # a pattern from a streak.

# Default state — used when STATE_PATH is missing. Jack ratified on
# 2026-09-04; anything before that date is inapplicable.
_DEFAULT_STATE = {
    "enabled": True,
    "chairman_ratified_at": "2026-09-04",
    "chairman_ratified_by": "Jack Beard, Co-Chairman",
    "threshold_failure_rate": THRESHOLD_FAILURE_RATE,
    "min_sample": MIN_SAMPLE,
    "disabled_at": None,
    "disabled_reason": None,
}


# ── state -------------------------------------------------------------------

def _load_state() -> dict:
    """Return current carve-out state, writing defaults on first read."""
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text())
            if isinstance(data, dict) and "enabled" in data:
                return data
    except Exception:
        pass
    # First-run bootstrap: persist so subsequent reads are stable.
    try:
        DATA.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(_DEFAULT_STATE, indent=2))
    except Exception:
        pass
    return dict(_DEFAULT_STATE)


def _save_state(state: dict) -> None:
    try:
        DATA.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"  [hedge_outcomes] could not persist state: {e}")


def is_enabled() -> bool:
    """Whether the risk gate should offer the hedge carve-out at all.

    Reads the state file. Returns True on any error — a broken tracker must
    not accidentally disable a Chairman-ratified rule; the alarm for that is
    the tracker's own health check, not a silent flip.
    """
    try:
        return bool(_load_state().get("enabled", True))
    except Exception:
        return True


# ── outcomes ----------------------------------------------------------------

def _load_outcomes() -> list:
    try:
        if OUTCOMES_PATH.exists():
            data = json.loads(OUTCOMES_PATH.read_text())
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_outcomes(rows: list) -> None:
    try:
        DATA.mkdir(exist_ok=True)
        OUTCOMES_PATH.write_text(json.dumps(rows, indent=2))
    except Exception as e:
        logger.warning(f"  [hedge_outcomes] could not persist outcomes: {e}")


def _finite(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pre_net_beta(snap: dict) -> Optional[float]:
    """Beta-weighted net exposure the exemption is meant to reduce.

    Returns None on error, which the outcome row records as pre_net_beta=None
    and the grader treats as UNKNOWN. Never guesses.
    """
    try:
        import portfolio_beta
        pv = _finite((snap or {}).get("portfolio_value"))
        positions = {s: {"qty": v.get("qty"), "price": v.get("price")}
                     for s, v in (snap or {}).get("positions", {}).items()}
        if pv is None:
            return None
        return portfolio_beta.net_beta(positions, pv).get("beta")
    except Exception as e:
        logger.debug(f"  [hedge_outcomes] pre_net_beta failed: {e}")
        return None


def record_use(symbol, side, qty, price, snap, reason) -> Optional[str]:
    """Note a fresh use of the hedge carve-out; returns the outcome id.

    Called from the risk gate right after the ALLOW_HEDGE_REDUCES_BETA
    verdict. Safe to call multiple times per pipeline run — the id includes a
    minute-resolution timestamp, so a resubmit inside the same minute is
    treated as one use and later resubmits as separate ones. That is the same
    dedup shape the Chairman escalation already uses.
    """
    try:
        sym = str(symbol).upper().strip()
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M")
        oid   = f"hedge-outcome-{sym}-{stamp}"
        rows  = _load_outcomes()
        if any(r.get("id") == oid for r in rows):
            return oid
        pv = _finite((snap or {}).get("portfolio_value"))
        cash = _finite((snap or {}).get("cash"))
        pre_beta = _pre_net_beta(snap)
        rows.append({
            "id": oid,
            "ts_open": now.isoformat(),
            "symbol": sym,
            "side": str(side),
            "qty": float(_finite(qty) or 0.0),
            "price": _finite(price),
            "pre_net_beta": pre_beta,
            "pre_cash_usd": cash,
            "pre_equity_usd": pv,
            "reason": str(reason or ""),
            "status": "OPEN",
            "ts_close": None,
            "close_price": None,
            "close_net_beta": None,
            "close_cash_usd": None,
            "pnl_usd": None,
            "verdict_reason": None,
        })
        _save_outcomes(rows)
        return oid
    except Exception as e:
        logger.warning(f"  [hedge_outcomes] record_use failed: {e}")
        return None


def _current_snapshot(client=None) -> Optional[dict]:
    """Fresh broker snap for grading. Never raises."""
    try:
        import risk_gate
        return risk_gate.snapshot(client=client, force=True)
    except Exception as e:
        logger.warning(f"  [hedge_outcomes] snapshot failed: {e}")
        return None


def _classify(row: dict, snap: Optional[dict]) -> tuple:
    """Given an OPEN row and the current snap, return (verdict, why, close_fields).

    verdict is "WORKED", "FAILED", "UNKNOWN", or None (meaning "stay OPEN").
    close_fields fills in ts_close, close_price, close_net_beta, close_cash_usd,
    pnl_usd for the outcome row when a verdict is issued.
    """
    if snap is None:
        return (None, "no snapshot available yet", {})

    sym = str(row.get("symbol", "")).upper()
    positions = (snap or {}).get("positions", {})
    still_held = sym in positions and _finite(positions[sym].get("qty"))

    # Age of the trade — string or None.
    try:
        opened = datetime.fromisoformat(row["ts_open"])
    except Exception:
        opened = None
    age_days = ((datetime.now(timezone.utc) - opened).days
                if opened is not None else None)

    # Grade when the position has closed OR the trade has aged past the max.
    if still_held and (age_days is None or age_days < _MAX_DAYS_OPEN):
        return (None, "position still open, not yet aged out", {})

    now = datetime.now(timezone.utc)
    pv = _finite((snap or {}).get("portfolio_value"))
    cash = _finite((snap or {}).get("cash"))
    close_beta = _pre_net_beta(snap)  # same call — it computes on any snap

    # Best-effort close price: last snap price for still-held, or a fresh
    # quote for closed positions. Missing → UNKNOWN.
    close_price = None
    if still_held:
        close_price = _finite(positions[sym].get("price"))
    if close_price is None:
        try:
            import risk_gate as _rg
            close_price = _rg._quote(sym)
        except Exception:
            close_price = None

    open_price = _finite(row.get("price"))
    qty = _finite(row.get("qty"))
    pnl_usd = None
    if all(v is not None for v in (close_price, open_price, qty)):
        # Sign follows the side: a BUY hedge held long profits when price rises;
        # a SELL/short hedge profits when price falls.
        side = str(row.get("side", "")).upper()
        direction = 1.0 if side in ("BUY", "LONG") else -1.0
        pnl_usd = direction * (close_price - open_price) * abs(qty)

    close_fields = {
        "ts_close": now.isoformat(),
        "close_price": close_price,
        "close_net_beta": close_beta,
        "close_cash_usd": cash,
        "pnl_usd": None if pnl_usd is None else round(pnl_usd, 2),
    }

    pre_beta = _finite(row.get("pre_net_beta"))
    if pre_beta is None or close_beta is None:
        return ("UNKNOWN",
                "beta coverage insufficient to grade risk reduction "
                "(pre or post net beta could not be measured)",
                close_fields)

    # A hedge WORKED if its own presence pulled the book toward flat — |beta|
    # today is less than |beta| at open — regardless of whether the hedge
    # itself made money. Risk reduction is the exemption's job.
    #
    # It FAILED if net beta did not fall AND the hedge lost money. Both halves
    # missing their targets is the case the Chairman needs to see.
    #
    # Everything else (beta unchanged with positive P&L, beta rose with
    # positive P&L, etc.) is UNKNOWN — the exemption's premise did not hold
    # but the trade did not cost anything, so grading it as a failure would
    # over-count and grading it as a win would let a broken premise pass.
    beta_shrank = abs(close_beta) < abs(pre_beta) - 1e-6
    lost_money  = pnl_usd is not None and pnl_usd < 0
    if beta_shrank:
        return ("WORKED",
                f"|net beta| fell from {abs(pre_beta):.3f} to "
                f"{abs(close_beta):.3f}; P&L "
                f"{'' if pnl_usd is None else f'${pnl_usd:,.0f}'}",
                close_fields)
    if lost_money and not beta_shrank:
        return ("FAILED",
                f"|net beta| did not fall ({abs(pre_beta):.3f} → "
                f"{abs(close_beta):.3f}) and hedge lost "
                f"${abs(pnl_usd):,.0f}",
                close_fields)
    return ("UNKNOWN",
            f"|net beta| did not fall ({abs(pre_beta):.3f} → "
            f"{abs(close_beta):.3f}) but the hedge did not lose money "
            f"({'flat/positive P&L' if pnl_usd is not None else 'P&L unknown'})",
            close_fields)


def grade_outcomes(client=None, snap=None) -> dict:
    """Grade every OPEN outcome that has aged out or whose position has closed.

    Returns {"graded": int, "worked": int, "failed": int, "unknown": int,
             "still_open": int, "total": int}. Never raises.
    """
    rows = _load_outcomes()
    if snap is None:
        snap = _current_snapshot(client=client)
    result = {"graded": 0, "worked": 0, "failed": 0, "unknown": 0,
              "still_open": 0, "total": len(rows)}
    dirty = False
    for row in rows:
        if row.get("status") != "OPEN":
            continue
        verdict, why, close_fields = _classify(row, snap)
        if verdict is None:
            result["still_open"] += 1
            continue
        row["status"] = verdict
        row["verdict_reason"] = why
        row.update(close_fields)
        result["graded"] += 1
        result[verdict.lower()] += 1
        dirty = True
    if dirty:
        _save_outcomes(rows)
    return result


def summary(recent_n: int = 20) -> dict:
    """Aggregate over the LAST `recent_n` graded outcomes.

    "Recent" is what matters for auto-disable: a carve-out that worked six
    months ago and has failed the last five uses is a carve-out that is now
    failing, and a fair rolling sample is the honest reading of that.
    """
    rows = _load_outcomes()
    graded = [r for r in rows if r.get("status") in ("WORKED", "FAILED", "UNKNOWN")]
    graded.sort(key=lambda r: r.get("ts_close") or "")
    tail = graded[-recent_n:] if recent_n and len(graded) > recent_n else graded
    worked  = sum(1 for r in tail if r["status"] == "WORKED")
    failed  = sum(1 for r in tail if r["status"] == "FAILED")
    unknown = sum(1 for r in tail if r["status"] == "UNKNOWN")
    # Only worked/failed count in the failure rate — UNKNOWN is not evidence.
    denom = worked + failed
    return {
        "sample_size": len(tail),
        "worked": worked,
        "failed": failed,
        "unknown": unknown,
        "failure_rate": (failed / denom) if denom > 0 else None,
        "graded_total": len(graded),
        "open_total": sum(1 for r in rows if r.get("status") == "OPEN"),
        "all_total": len(rows),
    }


def maybe_auto_tighten() -> dict:
    """If recent failure rate exceeds the policy threshold, disable the carve-out.

    Returns {"acted": bool, "reason": str, ...summary...}. Idempotent — a
    subsequent call with the state already flipped is a no-op. Errors are
    swallowed and the state stays whatever it was.
    """
    try:
        state = _load_state()
        summ  = summary(recent_n=max(MIN_SAMPLE * 2, 20))
        # Failing while already disabled is a no-op; the escalation happened
        # when the state was flipped.
        if not state.get("enabled", True):
            return {"acted": False, "reason": "already disabled", **summ}
        denom = summ["worked"] + summ["failed"]
        if denom < MIN_SAMPLE:
            return {"acted": False,
                    "reason": f"sample too small ({denom} < {MIN_SAMPLE})",
                    **summ}
        rate = summ["failure_rate"] or 0.0
        if rate < THRESHOLD_FAILURE_RATE:
            return {"acted": False,
                    "reason": f"failure rate {rate*100:.0f}% within policy "
                              f"({THRESHOLD_FAILURE_RATE*100:.0f}%)",
                    **summ}
        # Flip it.
        state["enabled"] = False
        state["disabled_at"] = datetime.now(timezone.utc).isoformat()
        state["disabled_reason"] = (
            f"Auto-disabled: {summ['failed']}/{denom} recent graded outcomes "
            f"failed (rate {rate*100:.0f}%, threshold "
            f"{THRESHOLD_FAILURE_RATE*100:.0f}%). Re-enable by editing "
            f"{STATE_PATH.name} after review.")
        _save_state(state)
        # Escalate to Chairman — this is what "stricter policy" looks like:
        # the exemption is off until a human turns it back on.
        try:
            import chairman_queue
            chairman_queue.submit([{
                "id": "hedge-carveout-auto-disable-"
                      + datetime.now(timezone.utc).strftime("%Y%m%d%H%M"),
                "dedup_key": "hedge_carveout_auto_disable",
                "severity": "HIGH",
                "category": "RISK_LIMITS",
                "description": (
                    "Hedge carve-out AUTO-DISABLED. " + state["disabled_reason"]
                    + " The pre-trade risk gate will now block every hedge that "
                    "would have used the exemption. Review the outcomes at "
                    + str(OUTCOMES_PATH) + " and decide whether to re-enable "
                    "(set enabled=true in " + STATE_PATH.name + ") or leave the "
                    "stricter policy in force."),
                "status": "awaiting_chairman",
                "raised_at": datetime.now(timezone.utc).isoformat(),
            }], source="hedge_exemption_outcomes")
        except Exception as e:
            logger.warning(f"  [hedge_outcomes] chairman escalation failed: {e}")
        return {"acted": True, "reason": state["disabled_reason"], **summ}
    except Exception as e:
        logger.warning(f"  [hedge_outcomes] maybe_auto_tighten failed: {e}")
        return {"acted": False, "reason": f"error: {e}"}


# ── CLI ---------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="Grade open outcomes and print summary without "
                         "auto-tightening.")
    ap.add_argument("--tighten", action="store_true",
                    help="Grade open outcomes and flip enabled=False if the "
                         "recent failure rate is over threshold.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = grade_outcomes()
    print(json.dumps({"grade": r, "summary": summary(),
                      "enabled": is_enabled()}, indent=2))
    if args.tighten:
        print(json.dumps({"auto_tighten": maybe_auto_tighten()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
