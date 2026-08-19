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
import os
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


class SingleWriterBlocked(RuntimeError):
    """This host is not permitted to open exposure on the shared account.

    Raised, not returned as None, on purpose: a caller that treats a blocked
    order as a successful one would record a position the broker never opened,
    and the local ledger would drift from the account. Loud beats silent —
    that is the whole bug class this repo keeps re-learning."""

try:
    from risk_gate import RiskLimitBlocked
except Exception:                                    # pragma: no cover
    class RiskLimitBlocked(RuntimeError):
        """Fallback so the `except` clause in the hook can never NameError.

        Defined defensively on purpose: config.py swallows import errors, so a
        module that fails to import here would otherwise disable its own guard
        while the logs still read as if it were installed."""


# Modules that merely relay a submission; skip them when attributing a source so
# the stamp names the agent that DECIDED, not the plumbing it went through.
_RELAY_MODULES = {"order_ledger", "alpaca_trader"}


def _clean(s: str, limit: int) -> str:
    """Sanitize to [a-z0-9] so the 4-field stamp stays split('-')-parseable."""
    return (re.sub(r"[^a-z0-9]", "", str(s).lower()) or "unknown")[:limit]


HOST_ID_FILE = DATA / ".host_id"


def _host_tag() -> str:
    """This host's STABLE identity. Assigned once, then never changes.

    It used to be `_clean(socket.gethostname()...)`, read fresh on every call.
    That is not an identity, it is a network condition. This laptop stamped
    orders as `mac` and, on 2026-07-31, as `macbookair17` — its own local ledger
    (a file no other machine can write) contains both, which is how we know they
    are one machine. macOS had renamed it `Mac.lan` -> `MacBook-Air-173.local`
    on a different network, and mDNS appends a collision suffix, so the name
    changes without anything about the machine changing.

    That broke attribution in both directions, and the second one is the one
    that matters:
      - one machine reads as several  -> a false "extra host" alarm, and it did
        fire: the single-writer detector counted 3 hosts where there are 2;
      - several machines read as one  -> `MacBook-Air-173` and `MacBook-Air-179`
        both truncate to `macbookair17` at the 12-char limit, so a genuinely
        foreign machine can inherit this one's identity and disappear into it.
        A detector that silently merges hosts is worse than no detector.

    So the tag is now persisted in `data/.host_id` (host-local, never synced,
    never committed) and seeded once from whatever the machine is called at
    first run. `APEX_HOST_ID` overrides it, which is also how a test or a
    freshly-imaged host pins its identity deliberately.
    """
    override = os.getenv("APEX_HOST_ID", "").strip()
    if override:
        return _clean(override, 12)
    try:
        if HOST_ID_FILE.exists():
            saved = _clean(HOST_ID_FILE.read_text().strip(), 12)
            if saved and saved != "unknown":
                return saved
    except Exception:
        pass

    tag = _clean(socket.gethostname().split(".")[0], 12)
    try:
        DATA.mkdir(exist_ok=True)
        HOST_ID_FILE.write_text(tag)
        logger.info(f"  [order_ledger] host identity assigned: {tag} "
                    f"(persisted to {HOST_ID_FILE.name}; hostname changes will "
                    f"no longer change it)")
    except Exception as e:
        # An unwritable data dir must not stop trading, but it does mean the
        # identity is only as stable as the hostname again — say so out loud
        # rather than letting it look assigned.
        logger.warning(f"  [order_ledger] could not persist host id: {e} — "
                       f"falling back to the live hostname, which can change")
    return tag


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

        # Single-writer guard. This is the only point every order in the firm
        # passes through, so it is the only place the check cannot be forgotten
        # by a new caller. In shadow mode (the default) it allows everything and
        # only records — see single_writer.py for why enforcement is a Chairman
        # decision and why it is asymmetric between entries and exits.
        try:
            import single_writer
            allowed, verdict, reason = single_writer.check(
                symbol=getattr(req, "symbol", ""),
                side=getattr(req, "side", ""),
                qty=getattr(req, "qty", 0),
            )
            if verdict != "ALLOW_SHADOW":
                single_writer.log(getattr(req, "symbol", ""),
                                  getattr(req, "side", ""),
                                  getattr(req, "qty", 0),
                                  verdict, reason, allowed)
            if not allowed:
                logger.error(f"  [single_writer] BLOCKED {getattr(req, 'side', '')} "
                             f"{getattr(req, 'qty', '')} {getattr(req, 'symbol', '')}: {reason}")
                raise SingleWriterBlocked(reason)
        except SingleWriterBlocked:
            raise
        except Exception as e:
            # The guard must never be the reason an order fails to reach the
            # broker. A guard that can take the firm offline is a bigger outage
            # than the one it prevents.
            logger.warning(f"  [order_ledger] single-writer guard unavailable: {e}")

        # Pre-trade risk limits. Here for the same reason the stamp is here: the
        # cash floor and position cap lived in AlpacaTrader.submit_buy, which is
        # one of sixteen paths to this line. head_agent, both hedge buys, the
        # short opener and the FI executor all reached the broker without ever
        # consulting them — which is what "a buy bypassed the cash floor
        # guardrail" actually meant. See risk_gate.py.
        _rg_entry, _rg_cost = False, 0.0
        try:
            import risk_gate
            _snap = risk_gate.snapshot()
            allowed, verdict, reason = risk_gate.check(
                symbol=getattr(req, "symbol", ""),
                side=getattr(req, "side", ""),
                qty=getattr(req, "qty", 0),
                price=getattr(req, "limit_price", None),
                snap=_snap,
            )
            if verdict != "ALLOW_WITHIN_LIMITS":
                risk_gate.log(getattr(req, "symbol", ""), getattr(req, "side", ""),
                              getattr(req, "qty", 0), verdict, reason, allowed)
            if not allowed:
                logger.error(f"  [risk_gate] BLOCKED {getattr(req, 'side', '')} "
                             f"{getattr(req, 'qty', '')} {getattr(req, 'symbol', '')}: {reason}")
                raise RiskLimitBlocked(reason)
            if verdict not in ("ALLOW_EXIT", "ALLOW_UNVERIFIED"):
                # An entry: it takes a slot. Only a BUY also takes cash — a
                # short open raises it, so reserving its notional would tighten
                # the floor against an order that loosens it.
                _rg_entry = True
                if "buy" in str(getattr(req, "side", "")).lower():
                    _rg_cost = risk_gate._order_cost(
                        getattr(req, "symbol", ""), getattr(req, "qty", 0),
                        getattr(req, "limit_price", None), _snap) or 0.0
        except RiskLimitBlocked:
            raise
        except Exception as e:
            logger.warning(f"  [order_ledger] risk gate unavailable: {e}")
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

        # Reserve the slot and the cash NOW, not when the fill appears. A market
        # order is not a position until it fills, so without this the next order
        # in the same run sizes against capital this one already spent — which
        # is how a cap of 20 comes to hold 21.
        if _rg_entry:
            try:
                import risk_gate
                risk_gate.reserve(getattr(req, "symbol", ""), _rg_cost)
            except Exception:
                pass

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
