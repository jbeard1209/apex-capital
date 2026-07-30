#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — ORDER LEDGER (attribution chokepoint for every account order)
# =============================================================================
# Why this exists (2026-07-30):
#   A BUY of 163 XLE was submitted at 15:36:21 UTC and filled 4s later on the
#   firm's Alpaca account. No GCP VM cron job, no Mac cron job (the Mac crontab
#   is empty), and no Claude scheduled task accounted for it; no VM log file was
#   written in that minute. It was genuinely unattributed — and
#   foreign_order_detector reported "22 orders, 0 unexplained → OK".
#
#   The detector's bar was SYMBOL + CALENDAR DAY: an account order was
#   "explained" if any local record mentioned that ticker that day. The firm had
#   made an XLE decision earlier that day (a cover attempt that failed), so a
#   ghost XLE order was waved through. A detector built to catch a second brain
#   trading our account could be satisfied by our own unrelated paperwork.
#
# The fix: stop inferring attribution, and RECORD it at submission time.
#   Every order the firm places carries a signed `client_order_id`:
#       apex-<host>-<source-module>-<uuid12>
#   The stamp travels with the order THROUGH Alpaca, so any host can verify an
#   order is ours without needing the placing host's files. That matters here:
#   the regime hedge runs on the VM while this detector often runs on the Mac,
#   and the previous host-local check false-alarmed HIGH on our own hedge every
#   day until it was whitelisted by symbol (see foreign_order_detector header).
#   An order-id stamp is the same allowance without the blanket symbol hole.
#
# Why a monkey-patch and not 16 edits: `submit_order` is called from 16 sites
# across 10 modules (alpaca_trader x6, main.py x3, manager_agent, board_meeting,
# deputy_monitor, head_agent, fi_executor, canary_agent). Stamping only some of
# them would make the REST look foreign — turning a detector fix into a false-
# positive flood. So intercept at the layer every caller shares, exactly as
# mail_guard.py does for smtplib. config.py calls install() on import, so this
# is active in every process, including VM-only scripts that never run here.
#
# The stamp also answers "WHAT placed this order?" — the question that could not
# be answered on 2026-07-30 — because the source module is captured from the
# call stack and written into the id itself.
# =============================================================================

import json
import logging
import re
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("order_ledger")

DATA = Path(__file__).parent / "data"
LEDGER = DATA / "order_ledger.json"

# Orders submitted at/after this instant MUST carry an apex stamp or they are
# foreign. Set to the moment this shipped to BOTH hosts. Orders older than it
# predate stamping and fall back to the legacy symbol+day heuristic, so the
# cutover cannot retroactively brand weeks of legitimate history as ghosts.
# Firm-wide constant on purpose: a per-host "activated_at" would drift and flag
# whichever host deployed second.
STRICT_FROM = "2026-07-30T16:30:00+00:00"

MAX_LEDGER_ENTRIES = 5000
_PREFIX = "apex"
_INSTALLED = False

# Modules that merely relay a submission; skip them when attributing a source so
# the stamp names the agent that DECIDED, not the plumbing it went through.
_RELAY_MODULES = {"order_ledger", "alpaca_trader"}


def _clean(s: str, limit: int) -> str:
    """Sanitize to [a-z0-9] so the 4-field stamp stays split('-')-parseable."""
    return (re.sub(r"[^a-z0-9]", "", str(s).lower()) or "unknown")[:limit]


def _host_tag() -> str:
    return _clean(socket.gethostname().split(".")[0], 12)


def _caller_module() -> str:
    """Nearest frame outside this module, alpaca's SDK, and pure relays."""
    import inspect
    fallback = ""
    try:
        for fr in inspect.stack()[1:]:
            mod = Path(fr.filename).stem
            if "site-packages" in fr.filename or mod == "order_ledger":
                continue
            if mod in _RELAY_MODULES:
                fallback = fallback or mod   # remember, but keep looking for the decider
                continue
            return _clean(mod, 24)
    except Exception:
        pass
    return _clean(fallback or "unknown", 24)


def make_stamp(source: str = "") -> str:
    """apex-<host>-<source>-<uuid12>. Unique per order (Alpaca requires it)."""
    src = _clean(source, 24) if source else _caller_module()
    return f"{_PREFIX}-{_host_tag()}-{src}-{uuid.uuid4().hex[:12]}"


def parse_stamp(client_order_id):
    """Return {host, source, nonce} for an apex-signed id, else None.

    NB: no `dict | None` annotation — the Mac runs Python 3.9 and that syntax is
    3.10+. Writing it here crashed this module on import, and config.py's
    try/except would have swallowed the crash and left stamping silently OFF.
    Exactly the international_analyst.py failure, reproduced same-day."""
    if not client_order_id:
        return None
    parts = str(client_order_id).split("-")
    if len(parts) != 4 or parts[0] != _PREFIX:
        return None
    host, source, nonce = parts[1], parts[2], parts[3]
    # Every field must be non-empty. "apex--x-abc" used to parse as a valid stamp
    # with host="", which the detector would then report as ATTRIBUTED to nobody —
    # a clean bill of health naming no placer. The whole point of the stamp is to
    # answer "which host, which module"; a stamp that cannot answer is not a stamp.
    if not (host and source and nonce):
        return None
    return {"host": host, "source": source, "nonce": nonce}


def is_apex_order(client_order_id) -> bool:
    return parse_stamp(client_order_id) is not None


def _load_ledger() -> list:
    try:
        return json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    except Exception:
        return []


def record(order_id, client_order_id, symbol, side, qty, source=""):
    """Append one placed order. Never raises — a ledger failure must not stop a
    trade, and a missing entry still gets caught by the stamp check."""
    try:
        led = _load_ledger()
        led.append({
            "order_id": str(order_id),
            "client_order_id": str(client_order_id),
            "symbol": str(symbol).upper(),
            "side": str(side),
            "qty": str(qty),
            "source": source or (parse_stamp(client_order_id) or {}).get("source", ""),
            "host": _host_tag(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        DATA.mkdir(exist_ok=True)
        LEDGER.write_text(json.dumps(led[-MAX_LEDGER_ENTRIES:], indent=2))
    except Exception as e:
        logger.warning(f"  [order_ledger] could not record order: {e}")


def known_order_ids() -> set:
    """Broker + client order ids this host has placed."""
    ids = set()
    for e in _load_ledger():
        for k in ("order_id", "client_order_id"):
            v = e.get(k)
            if v:
                ids.add(str(v))
    return ids


def install():
    """Patch TradingClient.submit_order so EVERY caller is stamped + recorded."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from alpaca.trading.client import TradingClient
    except Exception as e:                      # SDK absent (CI, some VM scripts)
        logger.debug(f"  [order_ledger] alpaca SDK unavailable: {e}")
        return

    original = TradingClient.submit_order

    def _submit_order(self, order_data=None, *args, **kwargs):
        req = order_data if order_data is not None else kwargs.get("order_data")
        src = _caller_module()
        stamp = None
        try:
            existing = getattr(req, "client_order_id", None)
            if req is not None and not existing:
                stamp = make_stamp(src)
                req.client_order_id = stamp
            else:
                stamp = existing
        except Exception:
            # Some request objects reject attribute writes; the order must still
            # go through. It stays attributable via the local ledger below.
            stamp = None

        order = original(self, order_data, *args, **kwargs) if order_data is not None \
            else original(self, *args, **kwargs)

        try:
            record(getattr(order, "id", ""),
                   stamp or getattr(order, "client_order_id", ""),
                   getattr(order, "symbol", getattr(req, "symbol", "")),
                   getattr(order, "side", getattr(req, "side", "")),
                   getattr(order, "qty", getattr(req, "qty", "")),
                   source=src)
        except Exception:
            pass
        return order

    TradingClient.submit_order = _submit_order
    _INSTALLED = True
    logger.info("  [order_ledger] installed — every order is stamped and recorded")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    led = _load_ledger()
    print(f"ledger entries: {len(led)}  (strict from {STRICT_FROM})")
    for e in led[-15:]:
        print(f"  {e['ts'][:19]}  {e['symbol']:6} {e['side']:24} "
              f"qty={e['qty']:>6}  src={e['source']}  host={e['host']}")
