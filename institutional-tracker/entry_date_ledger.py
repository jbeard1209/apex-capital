#!/usr/bin/env python3
"""Rebuild `state.json["entry_dates"]` from the broker's fill history.

WHY THIS EXISTS
---------------
`entry_dates` is the firm's record of *when* each position was opened. Every
time-based rule reads it: `review_agent`'s max-hold exit, `position_aging_agent`,
CIT's earnings-proximity check. It was written only by `main.py`, only on the
host that placed the order, and it drifted three separate ways:

1. **TRIM deleted it.** `main.py` popped the entry date for `("SELL", "TRIM")`
   alike, but a TRIM sells 40% and by construction *always leaves a position*.
   Every trimmed winner was orphaned the moment it was trimmed.
2. **The other host wrote it.** Positions opened on the Mac never appeared in
   the VM's ledger — there is no state sync between hosts (CLAUDE.md), so a
   host-local ledger of a shared account is wrong by design.
3. **Pre-cutover fills** carry no host stamp and no ledger row at all.

And the failure was silent, because of what the readers do with a missing date:
`review_agent` substituted `now - 1 day`, so an unledgered position read as
"bought yesterday" **forever** and the max-hold exit could never fire on it
(memory `fallback-that-means-act`). 13 of the account's positions were in that
state when `coherence_laws.law_positions_conserve` finally compared the two.

THE FIX IS TO STOP KEEPING A LOCAL LEDGER OF A SHARED FACT.
The broker knows when we bought. It is the one medium both hosts share and the
one record that cannot disagree with itself, so `entry_dates` becomes a
*projection* of fill history rather than a diary each host keeps privately.
That removes causes 2 and 3 outright: it does not matter which host placed the
order, or whether the order was stamped.

WHAT IT REFUSES TO DO
---------------------
If the broker read fails, it reconciles nothing. An unreadable broker is not a
flat book, and "I could not ask" must never be spent as "there is nothing there"
— that is the mistake that would drop every entry date in one pass.
If a symbol's fill history does not reach back far enough to date the position,
the date is left ABSENT rather than guessed. Absent is a state the readers now
handle honestly; a guess is one they cannot tell from a measurement.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "data" / "state.json"

logger = logging.getLogger("entry_date_ledger")

# How the entry date was arrived at. Carried so a caller can tell a measured
# date from a bounded one, and so neither can be mistaken for a guess.
BASIS_FLAT_CROSSING = "flat_crossing"   # saw the position go 0 -> non-zero
BASIS_WINDOW_START = "window_start"     # history starts mid-position; date is
                                        # "at or before" this fill


# ---------------------------------------------------------------------------
# Pure core — everything injected, so the tests never touch the network
# ---------------------------------------------------------------------------

def _fills(orders: List[dict], symbol: str) -> List[Tuple[datetime, float]]:
    """[(filled_at, signed_qty)] for one symbol, oldest first.

    Unfilled orders are dropped: a submitted order that never filled changed
    nothing about what we hold, and counting it would date a position from a
    trade that did not happen.
    """
    out = []
    for o in orders or []:
        get = o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))
        if (get("symbol") or "").upper() != symbol.upper():
            continue
        raw = get("filled_at")
        qty = get("filled_qty")
        if not raw or not qty:
            continue
        try:
            ts = raw if isinstance(raw, datetime) else datetime.fromisoformat(
                str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            q = float(qty)
        except (ValueError, TypeError):
            continue
        if q == 0:
            continue
        side = str(get("side") or "").lower()
        out.append((ts, -q if side.startswith("sell") else q))
    out.sort(key=lambda t: t[0])
    return out


def entry_from_fills(orders: List[dict], symbol: str,
                     held_qty: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    """(YYYY-MM-DD, basis) for the CURRENT run of `symbol`, or (None, None).

    "Current run" means: the stretch of fills since the position was last flat.
    A position closed and re-opened is dated from the re-open, not the original
    purchase — which is what every holding-period rule means by "how long have
    we held this".

    Walking forward and remembering the last 0 -> non-zero crossing is what makes
    trims invisible here: a partial sell moves the running total but never
    returns it to zero, so it cannot reset or erase the entry. That is the whole
    bug, expressed as an invariant instead of a branch.

    `held_qty` is the broker's current quantity, and it is what makes the answer
    checkable rather than merely plausible. The order window is finite — 500
    orders — so a position opened before it starts leaves a walk that looks
    perfectly well-formed and is missing its beginning. Replaying the fills has
    to RECONSTRUCT the quantity the broker reports; if it doesn't, fills are
    missing and the crossing this walk found is an artifact of where the window
    happens to begin, not the day we opened. In that case the earliest fill we
    can see is reported as a bound instead — the position is at least that old.
    """
    fills = _fills(orders, symbol)
    if not fills:
        return None, None

    if held_qty is not None and float(held_qty) != 0:
        # Walk BACKWARD from the broker's quantity, which is known to be true.
        # The forward walk below starts from an assumed zero, so when the window
        # opens mid-position it never sees the book go flat — and a full exit
        # followed by a re-buy inside the window was missed, dating the re-buy
        # from the window's first fill. XLE, 2026-10-12: max-hold sold 153 at
        # 13:45, ETF pod 3 re-bought 151 at 20:15, and the next morning the
        # one-day-old position was sold again as "max_hold (89 days)". Anchored
        # on the broker, a missing OLD fill cannot hide a RECENT re-open.
        pos = float(held_qty)
        for ts, q in reversed(fills):
            before = pos - q
            if abs(before) < 1e-6:
                return ts.strftime("%Y-%m-%d"), BASIS_FLAT_CROSSING
            pos = before
        # Never flat inside the window: the position predates it — a bound.
        # It errs toward the position looking OLDER, so a max-hold exit fires
        # sooner rather than never, and unlike an absent date it lets the
        # time-based rules run at all.
        return fills[0][0].strftime("%Y-%m-%d"), BASIS_WINDOW_START

    running = 0.0
    opened_at: Optional[datetime] = None
    for ts, q in fills:
        was_flat = (running == 0)
        running += q
        if was_flat and running != 0:
            opened_at = ts
        elif running == 0:
            opened_at = None

    if opened_at is not None and held_qty is None:
        return opened_at.strftime("%Y-%m-%d"), BASIS_FLAT_CROSSING

    if running != 0 and held_qty is None:
        return fills[0][0].strftime("%Y-%m-%d"), BASIS_WINDOW_START

    return None, None


def reconcile(entry_dates: Optional[dict],
              orders: List[dict],
              positions: Dict[str, float]) -> dict:
    """Project fill history onto the ledger. Pure; returns a report.

    `positions` is {symbol: qty} from the broker — the caller must have a REAL
    answer. Passing `{}` means "the broker says we hold nothing", which is a
    legitimate state and will correctly drop every entry. Passing an unread
    lookup is the caller's bug to prevent, not this function's to guess at.

    Existing dates are kept as-is. A date already in the ledger was written when
    the trade happened and is at least as good as one reconstructed from a
    truncated order window; re-deriving it every run would also make the ledger
    jitter as old orders age out of the broker's history.
    """
    entry_dates = dict(entry_dates or {})
    held = {s.upper(): q for s, q in (positions or {}).items() if q}

    healed: Dict[str, str] = {}
    unknown: List[str] = []
    for sym in sorted(held):
        if entry_dates.get(sym):
            continue
        date_str, basis = entry_from_fills(orders, sym, held_qty=held[sym])
        if date_str:
            entry_dates[sym] = date_str
            healed[sym] = f"{date_str} ({basis})"
        else:
            # Left ABSENT on purpose. The readers treat an absent date as
            # "unknown, do not run time-based rules and say so" — a guess here
            # would be indistinguishable from a measurement downstream.
            unknown.append(sym)

    dropped = sorted(s for s in list(entry_dates) if s.upper() not in held)
    for sym in dropped:
        entry_dates.pop(sym, None)

    return {"entry_dates": entry_dates, "healed": healed,
            "dropped": dropped, "unknown": unknown}


# ---------------------------------------------------------------------------
# I/O wrapper
# ---------------------------------------------------------------------------

def reconcile_state_file(state: Optional[dict] = None,
                         orders: Optional[List[dict]] = None,
                         positions: Optional[Dict[str, float]] = None,
                         save: bool = True) -> Optional[dict]:
    """Reconcile the live ledger. Returns the report, or None if it refused.

    Refuses (returns None) whenever either source could not be read. Both the
    order history and the position map come from the broker, and a half-read
    broker produces a confidently wrong ledger: no orders would make every
    position "unknown", and no positions would drop the entire ledger as
    phantom.
    """
    import single_writer

    if positions is None:
        # `_positions_map()` reads the API keys straight from the environment and
        # returns None if they aren't there — which this function is required to
        # treat as "the broker is unreadable". Without this, running the module
        # as a script (nothing has imported config) looks exactly like a broker
        # outage: a real failure mode, just not the one being reported.
        single_writer._ensure_env_loaded()
        positions = single_writer._positions_map()
    if positions is None:
        logger.warning("broker positions unreadable — reconciling nothing "
                       "(an unreadable broker is not a flat book)")
        return None

    if orders is None:
        try:
            orders = single_writer.fetch_broker_orders(limit=500)
        except Exception as e:
            logger.warning("broker order history unreadable (%s) — reconciling "
                           "nothing", e)
            return None

    inline = state is not None
    if state is None:
        try:
            state = json.loads(STATE_PATH.read_text())
        except (OSError, ValueError) as e:
            logger.warning("state.json unreadable (%s) — reconciling nothing", e)
            return None

    report = reconcile(state.get("entry_dates"), orders, positions)
    state["entry_dates"] = report["entry_dates"]

    if save and not inline:
        try:
            STATE_PATH.write_text(json.dumps(state, indent=2))
        except OSError as e:
            logger.error("could not write state.json: %s", e)
            return None

    if report["healed"] or report["dropped"] or report["unknown"]:
        logger.info("entry_dates reconciled — %d healed, %d phantom dropped, "
                    "%d undatable", len(report["healed"]),
                    len(report["dropped"]), len(report["unknown"]))
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing state.json")
    args = ap.parse_args()

    report = reconcile_state_file(save=not args.dry_run)
    if report is None:
        print("REFUSED — a source was unreadable, nothing was changed.")
        return 2

    print("=" * 72)
    print("ENTRY DATE LEDGER — projected from broker fill history"
          + ("  [DRY RUN]" if args.dry_run else ""))
    print("=" * 72)
    for sym, why in sorted(report["healed"].items()):
        print(f"  + {sym:6} {why}")
    for sym in report["dropped"]:
        print(f"  - {sym:6} dropped — no position at the broker")
    for sym in report["unknown"]:
        print(f"  ? {sym:6} held, but fill history does not reach its opening")
    print("-" * 72)
    print(f"  {len(report['entry_dates'])} position(s) dated, "
          f"{len(report['healed'])} healed, {len(report['dropped'])} dropped, "
          f"{len(report['unknown'])} undatable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
