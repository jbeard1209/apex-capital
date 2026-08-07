#!/usr/bin/env python3
"""
APEX CAPITAL — SINGLE-WRITER GUARD
===================================
Only one host may OPEN risk on the shared Alpaca account. Any host may CLOSE it.

WHY THIS MODULE EXISTS
----------------------
On 2026-08-06 an audit of `client_order_id` stamps showed this, per day, on the
one paper account:

    2026-08-03   apexcapitalc 5,  mac 1
    2026-08-04   apexcapitalc 6,  mac 10
    2026-08-05   apexcapitalc 10, mac 0     <- Mac asleep 07:55-22:09
    2026-08-06   apexcapitalc 3,  mac 7

Two hosts, deciding independently, sizing against the same capital, neither able
to see the other's `data/`. Plus `apex-macbookair17-manageragent-*` on 07-31: a
third machine with live API keys that ran the pipeline once.

This is the Oracle incident (memory `oracle_third_host`) recurring in a form the
existing detectors could not see. `foreign_order_detector` asks "is this order
ours?" — every one of these IS ours, so it reported OK. The question nobody was
asking is "how many of us are there?"

WHY THE GUARD IS ASYMMETRIC
---------------------------
The obvious guard — "only the designated host may submit" — is unsafe here,
because the designated host is exactly the thing that goes missing. On Aug 5 the
Mac slept through a whole trading day; on other days ssh to the VM times out
from the Mac. If the guard had blocked the non-designated host outright, a
stop-loss would have been blocked too, and the firm would sit in a losing
position with no way to exit. Blocking an ENTRY costs an opportunity. Blocking
an EXIT costs money and has no ceiling.

So:  opening or increasing exposure  -> designated host only
     reducing or closing exposure    -> ALWAYS allowed, from any host

and when the position lookup itself fails, the two directions fail differently:
entries fail CLOSED, exits fail OPEN. Uncertainty must never trap a position.

SHADOW MODE IS THE DEFAULT
--------------------------
With `APEX_TRADING_HOST` unset the guard allows everything and only records what
it *would* have blocked, exactly like the sign-off ledger's shadow phase
(memory `signoff_ledger_autonomy`). Turning a live two-host firm into a one-host
firm is the Chairman's decision, not a side effect of deploying this file. Flip
it by setting the env var on the designated host and only there:

    APEX_TRADING_HOST=apexcapitalc

Consumers: order_ledger.install() (the one submit_order choke point),
cit_pipeline_integrity_agent.check_single_writer.
Guarded by test_pipeline_smoke.py::TestSingleWriterGuard.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent / "data"
BLOCK_LOG = DATA / "single_writer_log.json"
MAX_LOG_ENTRIES = 500

# How long a positions snapshot may be reused inside the submit hook. Short,
# because it gates a risk decision; non-zero, because a burst of exits would
# otherwise make one API call per order and rate-limit the very orders that are
# trying to get us out.
POSITION_TTL_S = 45.0

_pos_cache = {"at": 0.0, "map": None}


def designated_host() -> str:
    """The host permitted to open risk, or "" for shadow mode."""
    from order_ledger import _clean
    return _clean(os.getenv("APEX_TRADING_HOST", ""), 12) if os.getenv(
        "APEX_TRADING_HOST", "").strip() else ""


def this_host() -> str:
    from order_ledger import _host_tag
    return _host_tag()


def mode() -> str:
    return "enforce" if designated_host() else "shadow"


def _positions_map(client=None):
    """{SYMBOL: signed_qty} from the broker, or None if it cannot be determined.

    None is a real answer and callers must branch on it. Returning {} on failure
    would read as "we hold nothing", which makes every sell look like it opens a
    short and every buy look like it opens a long — the guard would then be
    deciding from fabricated flatness. See memory `nan-is-not-none`.
    """
    now = time.time()
    if _pos_cache["map"] is not None and now - _pos_cache["at"] < POSITION_TTL_S:
        return _pos_cache["map"]
    try:
        if client is None:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                os.environ["ALPACA_API_KEY"],
                os.environ["ALPACA_SECRET_KEY"],
                paper=True,
            )
        out = {}
        for p in client.get_all_positions():
            out[str(p.symbol).upper()] = float(p.qty)   # already signed: shorts < 0
        _pos_cache.update(at=now, map=out)
        return out
    except Exception as e:
        logger.warning(f"  [single_writer] position lookup failed: {e}")
        return None


def is_risk_reducing(symbol, side, qty, positions) -> bool | None:
    """Does this order move an existing position toward zero? None = cannot tell.

    `positions` is the signed map from `_positions_map`. A buy is risk-reducing
    only against a short, a sell only against a long — and never beyond flat: a
    sell of 300 against a 100-share long closes 100 and OPENS a 200-share short,
    which is an entry wearing an exit's clothes. That asymmetry is the whole
    reason this returns a verdict per order instead of per side.
    """
    if positions is None:
        return None
    try:
        held = float(positions.get(str(symbol).upper(), 0.0))
        q = abs(float(qty))
    except Exception:
        return None
    if held == 0.0 or q == 0.0:
        return False
    s = str(side).lower()
    closing = ("sell" in s and held > 0) or ("buy" in s and held < 0)
    return bool(closing and q <= abs(held))


def check(symbol="", side="", qty=0, positions=None) -> tuple:
    """(allowed, verdict, reason) for one order. Never raises.

    verdict is one of ALLOW_SHADOW / ALLOW_DESIGNATED / ALLOW_EXIT /
    BLOCK_ENTRY / BLOCK_UNKNOWN, so the log says which rule fired rather than
    just that something did.
    """
    try:
        des, me = designated_host(), this_host()
        if not des:
            return (True, "ALLOW_SHADOW", "shadow mode: APEX_TRADING_HOST unset")
        if me == des:
            return (True, "ALLOW_DESIGNATED", f"{me} is the designated writer")

        if positions is None:
            positions = _positions_map()
        reducing = is_risk_reducing(symbol, side, qty, positions)

        if reducing is True:
            return (True, "ALLOW_EXIT",
                    f"{me} is not the writer, but this reduces an open position")
        if reducing is None:
            # Cannot tell. Let exits through, hold entries back.
            if "sell" in str(side).lower():
                return (True, "ALLOW_EXIT",
                        f"{me} is not the writer; position unknown, "
                        f"allowing sell rather than risk trapping a position")
            return (False, "BLOCK_UNKNOWN",
                    f"{me} is not the writer and the position could not be read")
        return (False, "BLOCK_ENTRY",
                f"{me} is not the designated writer ({des}); this opens exposure")
    except Exception as e:
        # A broken guard must not stop the firm from trading, and above all must
        # not stop it from exiting.
        logger.warning(f"  [single_writer] check failed open: {e}")
        return (True, "ALLOW_SHADOW", f"guard error: {e}")


def log(symbol, side, qty, verdict, reason, allowed) -> None:
    """Append one guard decision. Never raises."""
    try:
        entries = []
        if BLOCK_LOG.exists():
            entries = json.loads(BLOCK_LOG.read_text()) or []
        entries.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "host": this_host(),
            "designated": designated_host(),
            "mode": mode(),
            "symbol": str(symbol).upper(),
            "side": str(side),
            "qty": str(qty),
            "verdict": verdict,
            "reason": reason,
            "allowed": bool(allowed),
        })
        DATA.mkdir(exist_ok=True)
        BLOCK_LOG.write_text(json.dumps(entries[-MAX_LOG_ENTRIES:], indent=2))
    except Exception:
        pass


# ── Cross-host divergence detection ───────────────────────────────────────────
# This is the detector that would have caught the fork on day one. It reads the
# ONE medium both hosts can always see — the broker account — so it needs no
# ssh, no shared disk, and no cooperation from the other host. Every previous
# liveness check asked a host about itself; a host that cannot be reached cannot
# answer, and silence was being read as absence. Orders cannot stay silent.

def hosts_trading_recently(orders, hours=48, now=None) -> dict:
    """{host_tag: order_count} over the window, from stamped client_order_ids.

    `orders` is raw Alpaca order dicts (or objects). Unstamped orders are
    counted under "UNSTAMPED" rather than dropped — dropping them is how a
    pre-cutover or foreign order becomes invisible to the very check meant to
    find extra writers.
    """
    from order_ledger import parse_stamp
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    tally = {}
    for o in orders or []:
        get = o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))
        raw = get("submitted_at") or get("created_at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < cutoff:
            continue
        st = parse_stamp(get("client_order_id"))
        tag = st["host"] if st else "UNSTAMPED"
        tally[tag] = tally.get(tag, 0) + 1
    return tally


def divergence_verdict(tally, expected=None) -> tuple:
    """(severity, message). expected = the one host that should be trading.

    Returns SKIP when the window is empty: no orders is not evidence of one
    writer, it is evidence of nothing, and reporting OK there is precisely the
    false all-clear this firm keeps rebuilding (memory `presence-is-not-currency`).
    """
    if not tally:
        return ("SKIP", "no orders in the window — cannot tell how many hosts are trading")
    expected = expected or designated_host()
    real = {h: n for h, n in tally.items() if h != "UNSTAMPED"}
    unstamped = tally.get("UNSTAMPED", 0)

    if len(real) > 1:
        detail = ", ".join(f"{h}={n}" for h, n in sorted(real.items()))
        return ("CRITICAL",
                f"{len(real)} hosts placed orders on the shared account: {detail}. "
                f"They size against the same capital and cannot see each other.")
    if unstamped:
        return ("HIGH",
                f"{unstamped} order(s) carry no apex stamp — placed by something "
                f"that is not running current code, or not by this firm at all.")
    if expected and real and expected not in real:
        only = next(iter(real))
        return ("HIGH",
                f"all orders came from '{only}', but the designated writer is "
                f"'{expected}' — the designated host is not the one trading.")
    return ("PASS", f"single writer confirmed: {list(real)[0]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"host={this_host()}  designated={designated_host() or '(unset)'}  mode={mode()}")
    try:
        from dotenv import load_dotenv
        load_dotenv()
        import urllib.request
        base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        req = urllib.request.Request(
            base + "/v2/orders?status=all&limit=500&direction=desc",
            headers={"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
                     "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
        tally = hosts_trading_recently(json.load(urllib.request.urlopen(req, timeout=30)))
        sev, msg = divergence_verdict(tally)
        print(f"\n48h tally: {tally}\n[{sev}] {msg}")
    except Exception as e:
        print(f"could not query broker: {e}")
