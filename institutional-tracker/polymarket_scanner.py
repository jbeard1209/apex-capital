#!/usr/bin/env python3
"""
APEX CAPITAL — POLYMARKET SCANNER (shadow mode, measurement only)
==================================================================
Polymarket is a Polygon-based prediction market that had, before mid-2025,
been unavailable to US users. In late 2025 they acquired QCX (a CFTC-registered
DCM) and re-opened US access under `polymarket.com` (KYC via ID.me). This
scanner does NOT authenticate, place, sign or send any transaction; it uses
the public gamma-api endpoints (which return JSON without an API key) to
answer a single question:

  "Is there a real, actionable population of near-certainty markets on
   Polymarket the way kalshi_mirror proved there is on Kalshi?"

The gamma-api answers with market metadata (prices, outcomes, closing time,
volume). If the population exists, the mirror strategy from `kalshi_mirror.py`
is portable — buying NO in the 98-100c YES tier — with two frictions that
Kalshi does not have:

  1. Crypto rail. All Polymarket trades settle in USDC on Polygon; entering the
     market means opening a self-custodial wallet and moving USDC in. That is
     a REAL operational load, not a paperwork detail, and it is why this file
     is shadow-only for now.

  2. Fee structure. Polymarket historically ran zero-fee for takers with a 2%
     maker rebate; the DCM re-launch is expected to introduce fees but they
     are not yet published. Any live sizing MUST re-derive the fee model from
     an on-chain settlement, not from Polymarket's marketing page.

WHAT THIS FILE PRODUCES
-----------------------
`data/polymarket_scan.json`, a snapshot with:
  - total_markets: how many markets returned
  - active_markets: how many are still open
  - band_histogram: distribution of YES prices at scan time, in 10c buckets
  - tight_tier_sample: up to 25 markets in the 98-100c YES × 1-7d horizon slice
    with their (title, yes_price, no_price, closing_time, volume, url) — this
    is Kalshi's PROVEN mirror bucket, transplanted to check whether Polymarket
    has an analog population at all.

The scanner is NOT written to trade. It is written to prove or disprove that
there is anything to measure. If the tight-tier population is empty or too
small (n<100/scan), the strategy does not apply and we stop; if it is
comparable to Kalshi (n>=200/scan), the next step is a settlement ledger, and
after 30 days of settlements the same `is_edge_proven` gate that governs
kalshi_mirror.

WHAT THIS FILE IS NOT
---------------------
It is not a trader. It does not import a wallet library or a signer. It does
not know an account key. It emits no orders. Any code that adds those things
must first ask [[the Chairman]] and second write a mirror-of-the-mirror gate:
`is_polymarket_edge_proven()` with its own ledger, its own settled sample,
its own fee model, and its own arming file.
"""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
SCAN_FILE = DATA_DIR / "polymarket_scan.json"

# Public gamma-api, no auth required, rate-limited. Documented at
# https://docs.polymarket.com/#gamma-markets-api (subject to change).
GAMMA_ENDPOINT = "https://gamma-api.polymarket.com/markets"

# The tight tier we are asking about — same as `kalshi_mirror.py`.
YES_TIER_LO = 0.98
YES_TIER_HI = 1.00
HORIZON_LO_DAYS = 1.0
HORIZON_HI_DAYS = 7.0


class _EndOfUniverse(Exception):
    """Raised when gamma-api returns 422 — its way of saying offset overflow."""


def _fetch_markets(limit: int = 100, offset: int = 0, active: bool = True) -> list:
    """Public gamma-api call. Returns a list of market dicts.

    The endpoint requires a non-generic User-Agent (a bare urllib UA gets
    a 403), and returns HTTP 422 when the offset walks past the end of
    the active universe (empirically somewhere between 2000 and 3000).
    We translate 422 into _EndOfUniverse rather than an anonymous crash;
    everything else is raised as-is so a real outage doesn't look like
    "there are no markets".
    """
    params = {
        "limit": str(limit),
        "offset": str(offset),
        "active": "true" if active else "false",
    }
    url = f"{GAMMA_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "apex-capital-shadow/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"gamma-api HTTP {resp.status}")
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 422:
            raise _EndOfUniverse() from None
        raise
    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError(f"gamma-api returned non-list: {type(data).__name__}")
    return data


def _extract_yes_price(m: dict) -> Optional[float]:
    """Polymarket markets are two-outcome (YES/NO); the YES price is
    `outcomePrices[0]` per their schema. `lastTradePrice` is a fallback.
    Returns a float in [0, 1] or None if the market is not priced.
    """
    ops = m.get("outcomePrices")
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except Exception:
            ops = None
    if isinstance(ops, list) and ops:
        try:
            p = float(ops[0])
            if 0.0 <= p <= 1.0:
                return p
        except (TypeError, ValueError):
            pass
    ltp = m.get("lastTradePrice")
    if ltp is not None:
        try:
            p = float(ltp)
            if 0.0 <= p <= 1.0:
                return p
        except (TypeError, ValueError):
            pass
    return None


def _days_to_close(m: dict) -> Optional[float]:
    end = m.get("endDate") or m.get("end_date_iso") or m.get("closeTime")
    if not end:
        return None
    try:
        # Polymarket uses ISO-8601 with Z. Some endpoints return unix seconds.
        if isinstance(end, (int, float)):
            end_dt = datetime.fromtimestamp(end, tz=timezone.utc)
        else:
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (end_dt - now).total_seconds() / 86400.0
    except Exception:
        return None


def scan(max_markets: int = 2000) -> dict:
    """Walk the gamma-api and build a shadow-mode snapshot.

    NB: gamma-api caps `limit` at 100 regardless of what we ask for; we
    request 100 and paginate. Stopping when a page returns fewer than 100
    would misread that cap as end-of-data on the first page and give up
    at 100 markets — which is what the first draft of this scanner did.
    """
    fetched: list = []
    offset = 0
    page_size = 100
    empty_pages = 0
    while len(fetched) < max_markets:
        try:
            page = _fetch_markets(limit=page_size, offset=offset, active=True)
        except _EndOfUniverse:
            break
        if not page:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
            fetched.extend(page)
        offset += page_size
        # Be nice to gamma-api.
        time.sleep(0.2)

    band = {f"{lo:.1f}-{lo+0.1:.1f}": 0 for lo in [i / 10 for i in range(10)]}
    tight_tier = []
    total_priced = 0
    total_two_sided = 0
    for m in fetched:
        yp = _extract_yes_price(m)
        if yp is None:
            continue
        total_priced += 1
        # Two-sided requires both outcome prices populated
        ops = m.get("outcomePrices")
        if isinstance(ops, str):
            try:
                ops = json.loads(ops)
            except Exception:
                ops = None
        if isinstance(ops, list) and len(ops) >= 2:
            total_two_sided += 1
        # Bucket
        key = None
        for lo_b in [i / 10 for i in range(10)]:
            if lo_b <= yp < lo_b + 0.1 + (0.001 if lo_b == 0.9 else 0):
                key = f"{lo_b:.1f}-{lo_b+0.1:.1f}"
                break
        if key:
            band[key] += 1
        # Tight tier
        dtc = _days_to_close(m)
        if (YES_TIER_LO <= yp < YES_TIER_HI + 0.001
                and dtc is not None
                and HORIZON_LO_DAYS <= dtc < HORIZON_HI_DAYS):
            if len(tight_tier) < 25:
                tight_tier.append({
                    "question": m.get("question") or m.get("title") or "?",
                    "yes_price": yp,
                    "no_price": 1.0 - yp,
                    "end_date": m.get("endDate") or m.get("end_date_iso"),
                    "days_to_close": round(dtc, 2),
                    "volume_usd": m.get("volume") or m.get("volumeNum"),
                    "slug": m.get("slug"),
                })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": GAMMA_ENDPOINT,
        "total_markets_walked": len(fetched),
        "total_priced": total_priced,
        "total_two_sided": total_two_sided,
        "band_histogram": band,
        "tight_tier_slice": {
            "yes_lo": YES_TIER_LO,
            "yes_hi": YES_TIER_HI,
            "horizon_days": [HORIZON_LO_DAYS, HORIZON_HI_DAYS],
            "sample_size": sum(1 for _ in tight_tier),
            "samples": tight_tier,
        },
    }


def _print_report(snap: dict) -> None:
    print("=" * 76)
    print("POLYMARKET SHADOW SCAN — no orders, measurement only")
    print("=" * 76)
    print(f"Generated: {snap['generated_at']}")
    print(f"Endpoint:  {snap['endpoint']}")
    print(f"Walked:    {snap['total_markets_walked']} markets")
    print(f"Priced:    {snap['total_priced']}  Two-sided: {snap['total_two_sided']}")
    print()
    print("Yes-price histogram (10c buckets):")
    for k, v in snap["band_histogram"].items():
        bar = "#" * min(60, v // max(1, snap["total_priced"] // 60))
        print(f"  {k}  {v:>6d}  {bar}")
    print()
    tt = snap["tight_tier_slice"]
    print(f"TIGHT TIER ({tt['yes_lo']}-{tt['yes_hi']} × "
          f"{tt['horizon_days'][0]:.0f}-{tt['horizon_days'][1]:.0f}d):")
    print(f"  sample_size: {tt['sample_size']}")
    for s in tt["samples"][:10]:
        vol = s.get("volume_usd") or 0
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            vol = 0
        print(f"    NO={s['no_price']*100:.2f}c  "
              f"in {s['days_to_close']:.1f}d  "
              f"vol=${vol:>10,.0f}  "
              f"— {(s['question'] or '')[:80]}")
    if tt["sample_size"] == 0:
        print("  (empty — either the endpoint changed, the market shape is "
              "different from Kalshi, or there is no tight-tier population)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--max-markets", type=int, default=2000)
    args = ap.parse_args()
    try:
        snap = scan(max_markets=args.max_markets)
    except Exception as e:
        # Print the failure literally; the next Chairman-facing decision is
        # "is Polymarket reachable at all?"
        print(f"POLYMARKET SCAN FAILED: {type(e).__name__}: {e}")
        raise
    _print_report(snap)
    SCAN_FILE.write_text(json.dumps(snap, indent=2))


if __name__ == "__main__":
    main()
