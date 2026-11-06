#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI COPY-TRADER
=================================
Follow named Kalshi traders and copy their trades seconds after they make them.

Chairman decision, 2026-11-04: "Live now, tiny caps". This replaces the
research-only posture of docs/kalshi_build_plan.md for THIS strategy only.

WHERE THE SIGNAL COMES FROM (measured 2026-11-04, not assumed)
--------------------------------------------------------------
Kalshi's official trade tape (`/trade-api/v2/markets/trades`) is anonymous.
The Social layer is not: `GET /v1/social/trades?nickname=X` returns a profile's
fills with millisecond timestamps, unauthenticated. Polling three visible
profiles for 4 minutes, a new fill appeared a median 2.1 s after its
`create_date` (n=16, max 16.2 s). Fast enough to copy.

The catch is coverage. That endpoint answers `visibility_state: "hidden"` for
0 of the top 40 traders by projected P&L and for 97 of the top 100 by volume.
Most of the people worth copying cannot be copied. `--add-leader` checks this
up front so a hidden profile is refused, not silently followed forever.

The endpoint is undocumented. It can change shape without notice, which is why
every field is parsed defensively and an unrecognised row is skipped, never
guessed at.

WHAT GETS COPIED, AND WHAT DOESN'T
----------------------------------
Only fills where the leader was the TAKER. For those, `taker_side` and
`taker_action` are the leader's own order, and they crossed the spread exactly
as our immediate-or-cancel copy will. Verified against the public tape on
2026-11-04: the social `price_dollars` is the YES price of the fill.

Maker fills are skipped. The row's taker_* fields then describe the
counterparty, and checked against the public tape they do not reliably say
what the leader did. Guessing a direction is how a copier ends up on the
opposite side of the person it follows.

    leader (taker)      our V2 order (YES book only)
    buy  yes        ->  bid, opens
    buy  no         ->  ask, opens        (selling YES = buying NO)
    sell yes        ->  ask, reduce_only  (exit; does nothing if we hold none)
    sell no         ->  bid, reduce_only

Opening trades priced outside [MIN_PRICE, MAX_PRICE] are skipped: the visible
high-volume profiles are bots trading 1c and 99c contracts by the thousand, and
at those prices a single contract's fee is most of the edge. The band was
5c-95c until 2026-11-06, when it let through a 6c longshot that Jack (rightly)
called a lottery ticket: $2.55 for a $42 payout. It is now 15c-85c, measured
on the price of the side we actually buy (the band is symmetric, so YES at
p and NO at 1-p are judged alike).

Only trades younger than `max_age_s` are copied. On a leader's first poll every
existing trade is marked seen and NOTHING is copied — following someone must
never mean buying their last 100 trades.

THE CAPS (the whole reason this is allowed to be live)
------------------------------------------------------
per_trade_usd (default $5) and per_day_usd (default $20, UTC day) come from
data/kalshi_copytrade_config.json. per_trade_usd is the most we will ever put
into ONE MARKET, fees included, summed over every copy we make in it — not a
cap per leader fill. Until 2026-11-06 it was checked per fill, and one leader
order that filled in three pieces was copied three times at ~$0.85 each:
$2.55 into one 6c market under a "$1 per trade" cap. Both are and are clipped to HARD_MAX_* in code so a
typo in that file cannot authorise $5,000. Exits are always allowed: blocking
an exit costs money with no ceiling (same asymmetry as single_writer.py).
Three consecutive order errors write data/.kalshi_copytrade_halted and stop
all copying until the Chairman deletes it.

ARMING (Chairman only — Claude never writes these files)
--------------------------------------------------------
Live orders need ALL of:
  1. KALSHI_ENV=live in .env, plus KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH
  2. data/.kalshi_armed            {"env": "live", "armed": true}  (kalshi_client)
  3. data/.kalshi_copytrade_armed  {"env": "live", "armed": true}  (this file)
Missing any one, the copier still runs and writes what it WOULD have done to
the ledger with status "paper". All three are gitignored, so arming is per-host
by construction: arm the VM only, or two hosts copy every trade twice.

Usage:
    python3 kalshi_copytrade.py --add-leader NICK     # refuses hidden profiles
    python3 kalshi_copytrade.py --remove-leader NICK
    python3 kalshi_copytrade.py --check-key           # signed balance read, spends nothing
    python3 kalshi_copytrade.py --run-for 55          # poll loop (cron: every minute)
    python3 kalshi_copytrade.py --report
"""

import fcntl
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import kalshi_client as kc

DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_FILE = DATA_DIR / "kalshi_copytrade_config.json"
STATE_FILE = DATA_DIR / "kalshi_copytrade_state.json"
LEDGER_FILE = DATA_DIR / "kalshi_copytrade_ledger.jsonl"
ARM_FILE = DATA_DIR / ".kalshi_copytrade_armed"
HALT_FILE = DATA_DIR / ".kalshi_copytrade_halted"
LOCK_FILE = DATA_DIR / ".kalshi_copytrade.lock"

SOCIAL_BASE = "https://api.elections.kalshi.com/v1/social"

DEFAULTS = {
    "leaders": [],
    "per_trade_usd": 5.0,
    "per_day_usd": 20.0,
    "max_age_s": 30.0,
    "slippage": 0.02,      # dollars of YES price we'll pay past the leader's fill
    "poll_s": 3.0,
    "max_payout_usd": 16.0,   # most one market can ever pay us; see payout_cap()
}
HARD_MAX_PER_TRADE = 25.0
HARD_MAX_PER_DAY = 100.0
HARD_MAX_PAYOUT = 50.0
PAYOUT_MULTIPLE = 5.0      # payout cap = this x per_trade_usd, under max_payout_usd
MIN_PRICE = 0.15   # was 0.05 until 2026-11-06; see the module docstring
MAX_PRICE = 0.85
MAX_CONSECUTIVE_ERRORS = 3


# ------------------------------------------------------------------ config --

def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    raw = _read_json(CONFIG_FILE, {})
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    cfg["leaders"] = [str(x) for x in (cfg.get("leaders") or []) if str(x).strip()]
    for k in ("per_trade_usd", "per_day_usd", "max_age_s", "slippage", "poll_s",
              "max_payout_usd"):
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
        if not cfg[k] > 0:       # also rejects NaN
            cfg[k] = DEFAULTS[k]
    cfg["per_trade_usd"] = min(cfg["per_trade_usd"], HARD_MAX_PER_TRADE)
    cfg["per_day_usd"] = min(cfg["per_day_usd"], HARD_MAX_PER_DAY)
    cfg["max_payout_usd"] = min(cfg["max_payout_usd"], HARD_MAX_PAYOUT)
    cfg["poll_s"] = max(cfg["poll_s"], 1.0)
    return cfg


def armed():
    """(True, "") only when every live gate passes; else (False, why)."""
    env = kc.env_name()
    if env != "live":
        return False, f"KALSHI_ENV={env}"
    if not kc.armed_for("live"):
        return False, "data/.kalshi_armed does not authorise live"
    d = _read_json(ARM_FILE, {})
    if not (isinstance(d, dict) and d.get("env") == "live" and d.get("armed") is True):
        return False, "data/.kalshi_copytrade_armed absent or not live"
    if HALT_FILE.exists():
        return False, "halted: data/.kalshi_copytrade_halted exists"
    return True, ""


# ------------------------------------------------------------------- feed --

def fetch_trades(nickname, timeout=10):
    """{'trades': [...], 'visibility_state': ...} for one profile. Raises on
    transport failure — an empty list must mean 'no trades', never 'unreachable'."""
    url = f"{SOCIAL_BASE}/trades?nickname={urllib.parse.quote(nickname)}"
    req = urllib.request.Request(url, headers={"User-Agent": kc.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    if not isinstance(d, dict) or not isinstance(d.get("trades", []), list):
        raise ValueError(f"unrecognised social/trades shape for {nickname}")
    return d


def _parse_ts(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- decision --

INTENT = {("yes", "buy"): ("bid", False),
          ("no", "buy"): ("ask", False),
          ("yes", "sell"): ("ask", True),
          ("no", "sell"): ("bid", True)}


def payout_cap(cfg):
    """Most a single market may pay us if it wins, in dollars (= contracts held,
    since each pays $1). Jack, 2026-11-06: "dont put more than 2 dollars on 8x".
    A cheap contract pays a big multiple, so a dollar cap alone lets the bot
    load up on longshots; this caps the payout too: PAYOUT_MULTIPLE x the
    per-market dollar cap, never above max_payout_usd (default $16 = $2 at 8x)
    or HARD_MAX_PAYOUT. At $1 per market that is $5: at most 5 contracts in
    any one market, so a 15c bet risks $0.75, not $1."""
    return min(PAYOUT_MULTIPLE * float(cfg.get("per_trade_usd", DEFAULTS["per_trade_usd"])),
               float(cfg.get("max_payout_usd", DEFAULTS["max_payout_usd"])),
               HARD_MAX_PAYOUT)


def decide(trade, leader, cfg, spent_today, now=None, market_spent=0.0,
           market_contracts=0.0):
    """Pure function: one leader fill -> an order dict, or a skip.

    `spent_today` is what we have already put into opening orders today;
    `market_spent` is what we have already put into THIS ticker, ever. The
    order may use only what both caps still leave, fees included.

    Returns {"action": "order", ...} or {"action": "skip", "reason": ...}.
    No I/O, so every branch is testable without the exchange.
    """
    now = now or datetime.now(timezone.utc)
    me = leader.lower()
    taker = str(trade.get("taker_nickname") or "").lower()
    maker = str(trade.get("maker_nickname") or "").lower()
    if taker != me:
        return {"action": "skip",
                "reason": "maker fill (leader's own direction unknown)" if maker == me
                else "leader not on this fill"}
    key = (str(trade.get("taker_side")).lower(), str(trade.get("taker_action")).lower())
    if key not in INTENT:
        return {"action": "skip", "reason": f"unrecognised side/action {key}"}
    side, reduce_only = INTENT[key]
    p = kc.parse_price(trade.get("price_dollars"))
    if p is None:
        return {"action": "skip", "reason": f"no usable price {trade.get('price_dollars')!r}"}
    ts = _parse_ts(trade.get("create_date"))
    if ts is None:
        return {"action": "skip", "reason": "no timestamp"}
    age = (now - ts).total_seconds()
    if age > cfg["max_age_s"]:
        return {"action": "skip", "reason": f"stale ({age:.0f}s old)", "lag_s": age}
    lc = kc.parse_size(trade.get("count_fp") if trade.get("count_fp") is not None
                       else trade.get("count"))
    if not lc:
        return {"action": "skip", "reason": "no size"}

    slip = cfg["slippage"]
    limit = min(0.99, p + slip) if side == "bid" else max(0.01, p - slip)
    limit = round(limit, 2)
    base = {"ticker": trade.get("ticker"), "side": side, "price": limit,
            "reduce_only": reduce_only, "leader_price": p, "lag_s": round(age, 2)}

    if reduce_only:
        n = math.floor(lc)
        if n < 1:
            return {"action": "skip", "reason": "exit smaller than one contract"}
        return {"action": "order", "count": n, "cost_usd": 0.0, **base}

    leader_cost = p if side == "bid" else 1.0 - p    # price of the side bought
    if not (MIN_PRICE - 1e-9 <= leader_cost <= MAX_PRICE + 1e-9):
        what = "YES" if side == "bid" else "NO"
        return {"action": "skip", "reason": f"{what} at {leader_cost:.2f} is outside "
                f"{MIN_PRICE:.2f}-{MAX_PRICE:.2f} (longshot/near-certain)"}
    per_contract = round(limit if side == "bid" else (1.0 - limit), 2)
    market_left = cfg["per_trade_usd"] - market_spent
    day_left = cfg["per_day_usd"] - spent_today
    budget = min(market_left, day_left)
    payout_left = payout_cap(cfg) - market_contracts
    n = min(math.floor(lc), math.floor(budget / per_contract + 1e-9),
            math.floor(payout_left + 1e-9))
    while n >= 1 and n * per_contract + (kc.fee_cents(per_contract, n) or 0) / 100 > budget + 1e-9:
        n -= 1
    if n < 1 and payout_left < 1:
        return {"action": "skip", "reason": f"payout cap: already hold "
                f"{market_contracts:g} contracts (max ${payout_cap(cfg):.0f} payout per market)"}
    if n < 1:
        which = "per-market" if market_left <= day_left else "daily"
        return {"action": "skip", "reason": f"{which} cap: ${max(budget, 0):.2f} left "
                f"buys no contract at ${per_contract:.2f} plus fee"}
    fee = (kc.fee_cents(per_contract, n) or 0) / 100
    return {"action": "order", "count": n,
            "cost_usd": round(n * per_contract + fee, 2), **base}


# ----------------------------------------------------------------- ledger --

def _append(row):
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def ledger_rows():
    try:
        lines = LEDGER_FILE.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _opening_cost(r):
    """Dollars one ledger row committed to opening a position. Live rows count
    what filled; a live row with no fill count is charged in full, because an
    unknown fill must not reopen the budget."""
    if r.get("reduce_only"):
        return 0.0
    if r.get("status") == "placed":
        req, filled = r.get("count") or 0, r.get("fill_count")
        frac = (float(filled) / req) if (filled is not None and req) else 1.0
        return float(r.get("cost_usd") or 0) * min(max(frac, 0.0), 1.0)
    if r.get("status") == "paper":
        return float(r.get("cost_usd") or 0)
    return 0.0


def spent_today(rows=None, now=None):
    """Dollars committed to OPENING orders today (UTC)."""
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return round(sum(_opening_cost(r) for r in (ledger_rows() if rows is None else rows)
                     if str(r.get("at", ""))[:10] == day), 2)


def market_contracts(ticker, rows=None):
    """Contracts ever opened in one market (filled live, or paper), which is
    the most that market can pay us. Exits are not netted off: re-entering a
    market we exited still counts, which errs towards smaller positions."""
    n = 0.0
    for r in ledger_rows() if rows is None else rows:
        if r.get("ticker") != ticker or r.get("reduce_only"):
            continue
        if r.get("status") == "placed":
            f = kc.parse_size(r.get("fill_count"))
            n += float(r.get("count") or 0) if f is None else f
        elif r.get("status") == "paper":
            n += float(r.get("count") or 0)
    return n


def market_spent(ticker, rows=None):
    """Dollars ever committed to opening positions in one market. This is
    what per_trade_usd caps, so a leader order that fills in five pieces (or
    five leaders piling into one market) cannot stack five full-size copies."""
    return round(sum(_opening_cost(r) for r in (ledger_rows() if rows is None else rows)
                     if r.get("ticker") == ticker), 2)


# ------------------------------------------------------------------- loop --

def poll_once(cfg, state, now=None, fetch=fetch_trades, place=kc.place_order):
    """One pass over every leader. Mutates `state` (seen ids). Returns rows written."""
    written = []
    is_live, why_not = armed()
    for leader in cfg["leaders"]:
        try:
            d = fetch(leader)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10)
            continue
        except Exception:
            continue
        if d.get("visibility_state") == "hidden":
            continue
        trades = [t for t in d.get("trades") or [] if t.get("trade_id")]
        prior = list(state.setdefault("seen", {}).get(leader, []))
        seen = dict.fromkeys(prior)             # insertion-ordered set
        first = leader not in state.setdefault("initialised", [])
        if first:
            state["initialised"].append(leader)
        new = [t for t in trades if t["trade_id"] not in seen]
        for t in reversed(new):                  # API is newest-first
            seen[t["trade_id"]] = None
            if first:
                continue
            now_ = now or datetime.now(timezone.utc)
            rows_ = ledger_rows()
            dec = decide(t, leader, cfg, spent_today(rows_, now=now_), now=now_,
                         market_spent=market_spent(t.get("ticker"), rows_),
                         market_contracts=market_contracts(t.get("ticker"), rows_))
            row = {"at": now_.isoformat(), "leader": leader,
                   "leader_trade_id": t["trade_id"], "leader_ts": t.get("create_date"),
                   "leader_side": t.get("taker_side"), "leader_action": t.get("taker_action"),
                   "leader_count": t.get("count_fp"), **{k: v for k, v in dec.items()
                                                         if k != "action"}}
            if dec["action"] == "skip":
                row["status"] = "skipped"
            elif not is_live:
                row.update(status="paper", why_not_live=why_not)
            else:
                coid = f"apex-copy-{uuid.uuid4().hex[:16]}"
                row["client_order_id"] = coid
                try:
                    resp = place(ticker=dec["ticker"], side=dec["side"], count=dec["count"],
                                 price=dec["price"], time_in_force="immediate_or_cancel",
                                 reduce_only=dec["reduce_only"], client_order_id=coid)
                    row.update(status="placed", order_id=resp.get("order_id"),
                               fill_count=kc.parse_size(resp.get("fill_count")))
                    state["consecutive_errors"] = 0
                except Exception as e:
                    row.update(status="error", error=f"{type(e).__name__}: {str(e)[:200]}")
                    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
                    if state["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS:
                        _write_json(HALT_FILE, {"at": row["at"], "last_error": row["error"]})
                        is_live, why_not = False, "halted after repeated order errors"
            _append(row)
            written.append(row)
        # keep the newest 500 ids per leader (oldest first); the feed returns 100,
        # so anything trimmed is long past max_age_s and could not be copied anyway
        state["seen"][leader] = list(seen)[-500:]
    return written


def run_for(seconds):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another copier is running; exiting")
            return 0
        end = time.time() + seconds
        while time.time() < end:
            cfg = load_config()
            state = _read_json(STATE_FILE, {})
            if not isinstance(state, dict):
                state = {}
            for r in poll_once(cfg, state):
                print(f"{r['at'][11:19]} {r['leader']:<20} {r['status']:<8} "
                      f"{r.get('ticker')} {r.get('side', '')} x{r.get('count', '')} "
                      f"@{r.get('price', '')} {r.get('reason', '')}", flush=True)
            _write_json(STATE_FILE, state)
            time.sleep(cfg["poll_s"])
    return 0


# -------------------------------------------------------------------- cli --

def add_leader(nick):
    d = fetch_trades(nick)
    if d.get("visibility_state") == "hidden":
        print(f"REFUSED: {nick}'s trades are hidden on Kalshi, so there is nothing to copy.")
        return 1
    cfg = _read_json(CONFIG_FILE, {})
    cfg = cfg if isinstance(cfg, dict) else {}
    leaders = [x for x in cfg.get("leaders", []) if x.lower() != nick.lower()] + [nick]
    cfg["leaders"] = leaders
    _write_json(CONFIG_FILE, cfg)
    print(f"Following {nick} ({len(d.get('trades') or [])} recent trades visible). "
          f"Leaders: {', '.join(leaders)}")
    return 0


def remove_leader(nick):
    cfg = _read_json(CONFIG_FILE, {})
    cfg = cfg if isinstance(cfg, dict) else {}
    cfg["leaders"] = [x for x in cfg.get("leaders", []) if x.lower() != nick.lower()]
    _write_json(CONFIG_FILE, cfg)
    print(f"Leaders: {', '.join(cfg['leaders']) or '(none)'}")
    return 0


def check_key():
    try:
        b = kc.get_balance()
    except Exception as e:
        print(f"KEY CHECK FAILED: {e}")
        return 1
    print(f"Key works. Balance response: {json.dumps(b)[:200]}")
    return 0


def report():
    cfg = load_config()
    live, why = armed()
    rows = ledger_rows()
    print("KALSHI COPY-TRADER")
    print(f"  mode       : {'LIVE' if live else 'paper (' + why + ')'}")
    print(f"  leaders    : {', '.join(cfg['leaders']) or '(none — add with --add-leader)'}")
    print(f"  caps       : ${cfg['per_trade_usd']:.2f}/trade, ${cfg['per_day_usd']:.2f}/day")
    print(f"  spent today: ${spent_today(rows):.2f}")
    by = {}
    for r in rows:
        by[r.get("status")] = by.get(r.get("status"), 0) + 1
    print(f"  ledger     : {len(rows)} rows {by}")
    for r in rows[-10:]:
        print(f"    {r.get('at', '')[:19]} {r.get('leader')} {r.get('status')} "
              f"{r.get('ticker')} {r.get('side', '')} x{r.get('count', '')} "
              f"lag={r.get('lag_s')}s {r.get('reason', '')}")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--add-leader")
    ap.add_argument("--remove-leader")
    ap.add_argument("--check-key", action="store_true")
    ap.add_argument("--run-for", type=float)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv)
    if a.add_leader:
        return add_leader(a.add_leader)
    if a.remove_leader:
        return remove_leader(a.remove_leader)
    if a.check_key:
        return check_key()
    if a.run_for:
        return run_for(a.run_for)
    return report()


if __name__ == "__main__":
    sys.exit(main())
