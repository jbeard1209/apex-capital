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

SHADOW IS THE DEFAULT; ENFORCE IS LIVE AS OF 2026-08-07
-------------------------------------------------------
With `APEX_TRADING_HOST` unset the guard allows everything and only records what
it *would* have blocked, exactly like the sign-off ledger's shadow phase
(memory `signoff_ledger_autonomy`). Turning a live two-host firm into a one-host
firm is the Chairman's decision, not a side effect of deploying this file — and
on 2026-08-07 the Chairman made it: the VM is the writer.

    APEX_TRADING_HOST=apexcapitalc      # in .env, on BOTH hosts

Firm-wide on purpose, and it is the same value everywhere. The var names WHICH
host may open risk, it does not mean "I am that host" — the check compares it to
`this_host()`. Setting it only on the designated host would leave every other
host in shadow mode, i.e. unguarded, which is the exact inverse of the intent.
`deploy.sh` scp's `.env` to the VM, so one edit covers both.

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


def _ensure_env_loaded() -> None:
    """Load .env if nothing has yet. Cheap, idempotent, and load-order-proof.

    The setting lives in .env, which config.py loads at import — but this guard
    sits under the Alpaca SDK, and a caller that reaches submit_order without
    importing config would read an unset variable and silently fall back to
    shadow mode. Enforcement that quietly disables itself depending on import
    order is worse than no enforcement, because the logs still say "enforce".
    """
    if os.environ.get("APEX_TRADING_HOST") is not None:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(Path(__file__).resolve().parent / ".env"),
                    override=False)
    except Exception:
        pass


def designated_host() -> str:
    """The host permitted to open risk, or "" for shadow mode."""
    from order_ledger import _clean
    _ensure_env_loaded()
    raw = os.getenv("APEX_TRADING_HOST", "").strip()
    return _clean(raw, 12) if raw else ""


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

def _stamped_orders(orders):
    """Yield (timestamp, host_tag) for every order we can place in time.

    Shared by the count-based and the recency-based checks below so the two can
    never disagree about which orders exist or who placed them.
    """
    from order_ledger import parse_stamp
    for o in orders or []:
        get = o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))
        raw = get("submitted_at") or get("created_at")
        if not raw:
            continue
        try:
            ts = raw if isinstance(raw, datetime) else datetime.fromisoformat(
                str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        st = parse_stamp(get("client_order_id"))
        yield ts, (st["host"] if st else "UNSTAMPED")


def hosts_trading_recently(orders, hours=48, now=None) -> dict:
    """{host_tag: order_count} over the window, from stamped client_order_ids.

    `orders` is raw Alpaca order dicts (or objects). Unstamped orders are
    counted under "UNSTAMPED" rather than dropped — dropping them is how a
    pre-cutover or foreign order becomes invisible to the very check meant to
    find extra writers.

    This answers "how many of us are there?" and NOTHING else. It is a count
    inside a window, so a host that traded at the start of the window and then
    died is indistinguishable from one still working. For that, hosts_last_seen().
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    tally = {}
    for ts, tag in _stamped_orders(orders):
        if ts < cutoff:
            continue
        tally[tag] = tally.get(tag, 0) + 1
    return tally


def hosts_last_seen(orders, now=None) -> dict:
    """{host_tag: datetime of that host's most recent order}. No window.

    The question hosts_trading_recently() cannot answer, and the reason this
    firm ran exit-only for eight days without noticing.

    On 2026-08-06 the GCP VM stopped. Its last six orders still sat inside the
    48h window, so the tally read `apexcapitalc=6` and was read as "the
    designated writer is alive and trading". It had placed all six in a
    ten-hour burst at the START of the window and then gone quiet for good.
    **A count says a host WAS here; only a timestamp says it still IS.**
    """
    seen = {}
    for ts, tag in _stamped_orders(orders):
        if tag not in seen or ts > seen[tag]:
            seen[tag] = ts
    return seen


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


# How long the designated writer may go without placing an order before the
# silence is itself a finding. Generous on purpose: orders are bursty (the VM
# averaged ~1.2/day over 21 days, with observed 14h gaps between them), so a
# tight bound would cry wolf on an ordinary quiet afternoon. The sharp signal is
# not the absolute silence — it is silence WHILE ANOTHER HOST WAS TRADING, which
# needs no market calendar to interpret.
DEFAULT_WRITER_GRACE_HOURS = 26.0


def writer_silence_verdict(last_seen, expected=None, now=None,
                           grace_hours=DEFAULT_WRITER_GRACE_HOURS) -> tuple:
    """(severity, message) — has the designated writer stopped writing?

    Deliberately a SEPARATE function from divergence_verdict(), which returns on
    its first matching branch. On 2026-08-07 three hosts appeared in the window,
    so it reported CRITICAL "3 hosts placed orders on the shared account" and
    never reached the question of whether one of those three had died. It was
    telling the truth about the fork while the writer's corpse sat in its own
    tally. Both checks must run; neither may mask the other.

    Corroboration beats the calendar. If another host placed an order well after
    the writer's last one, the market was demonstrably open and the writer was
    demonstrably absent — no holiday table required, and no way for a long
    weekend to fake it. Without a second host to corroborate, an absolute bound
    is all that is left, so the verdict downgrades to HIGH and says why rather
    than claiming a confidence it has not earned.
    """
    now = now or datetime.now(timezone.utc)
    expected = expected if expected is not None else designated_host()
    if not expected:
        return ("SKIP", "no designated writer (shadow mode) — nothing to watch")
    if not last_seen:
        return ("SKIP", "no orders on record — cannot tell whether the writer works")

    writer_last = last_seen.get(expected)
    others = {h: t for h, t in last_seen.items()
              if h != expected and h != "UNSTAMPED"}
    newest_other = max(others.values()) if others else None

    if writer_last is None:
        if newest_other is None:
            return ("SKIP", f"no orders from '{expected}' and none from any other "
                            f"host either — that is evidence of nothing")
        return ("CRITICAL",
                f"the designated writer '{expected}' has placed NO order on record "
                f"while {sorted(others)} did (most recent "
                f"{(now - newest_other).total_seconds() / 3600.0:.0f}h ago). The host "
                f"that is supposed to open this firm's positions is absent.")

    silence_h = (now - writer_last).total_seconds() / 3600.0

    if newest_other is not None and newest_other > writer_last:
        lag_h = (newest_other - writer_last).total_seconds() / 3600.0
        if lag_h > grace_hours:
            return ("CRITICAL",
                    f"the designated writer '{expected}' has been silent "
                    f"{silence_h:.0f}h (last order {writer_last.isoformat()[:16]}), "
                    f"but {sorted(others)} kept trading for {lag_h:.0f}h after that — "
                    f"the market was open and the writer was not there. The firm can "
                    f"only REDUCE exposure until this host returns or another is "
                    f"designated in APEX_TRADING_HOST.")

    if silence_h > grace_hours:
        return ("HIGH",
                f"the designated writer '{expected}' has been silent {silence_h:.0f}h "
                f"(last order {writer_last.isoformat()[:16]}) and no other host traded "
                f"after it — so a market closure cannot be told apart from an outage "
                f"from here. Verify the host directly.")

    return ("PASS",
            f"designated writer '{expected}' last traded {silence_h:.1f}h ago")


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
        orders = json.load(urllib.request.urlopen(req, timeout=30))
        tally = hosts_trading_recently(orders)
        sev, msg = divergence_verdict(tally)
        print(f"\n48h tally: {tally}\n[{sev}] {msg}")

        # Both, always. The count check answers "how many?" and the recency
        # check answers "is the writer still here?" — one cannot stand in for
        # the other, and reporting only the first is what hid an 8-day outage.
        seen = hosts_last_seen(orders)
        now = datetime.now(timezone.utc)
        print("\nlast seen:")
        for h, t in sorted(seen.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {h:14} {t.isoformat()[:16]}  "
                  f"({(now - t).total_seconds() / 3600.0:6.1f}h ago)")
        sev2, msg2 = writer_silence_verdict(seen)
        print(f"[{sev2}] {msg2}")
    except Exception as e:
        print(f"could not query broker: {e}")
