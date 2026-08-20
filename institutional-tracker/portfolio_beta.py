"""
APEX CAPITAL — PORTFOLIO BETA
=============================
What the firm's book is actually exposed to, so the risk limits can be written
on that instead of on an accounting proxy for it.

WHY THIS EXISTS (2026-08-19)
----------------------------
On 2026-08-19 `risk_gate` blocked a buy of SH — an inverse S&P ETF, bought to
hedge — because cash was 13.4% against a 20% floor and 20 of 20 slots were
full. Both of those tests are true statements about the order: it spends cash
and it occupies a slot. Neither is a statement about risk. The firm was long
$90,607 against $99,271 of equity, and the trade that would have *reduced* the
exposure the limits exist to bound was refused by the limits.

That is a misspecified constraint, not a bug in the arithmetic. Cash % and slot
count are proxies for risk capacity, and a proxy fails exactly at the boundary
where it binds: the one trade whose economics differ most from the proxy is a
hedge, and a hedge is precisely what you want to be able to do when the proxy
is screaming. So the gate needs the real quantity, which is net beta.

WHAT IS MEASURED AND WHAT IS ASSUMED
------------------------------------
Two different kinds of number live here and they are kept apart on purpose:

  * STRUCTURAL_BETA — the inverse and leveraged funds. -1x SPY is not an
    estimate of SH's behaviour, it is SH's mandate. These are facts about the
    instrument, quoted against its own index, then converted to an SPY beta by
    an index factor that IS an estimate (and is labelled as one).
  * measured_beta() — everything else, OLS slope of daily returns against SPY
    over a lookback. Real, noisy, and cached.

Anything neither structural nor measurable returns **None**, never 1.0. A
defaulted beta is a decision disguised as a datum: assuming 1.0 for an unknown
symbol makes the book look more long, which makes a hedge look more justified,
which is the direction that grants permission. Coverage is reported alongside
every net-beta number so a caller can refuse to act on a thin one.
See memory `fallback-that-means-act`.
"""

import json
import logging
import math
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("portfolio_beta")

_CACHE_PATH = Path(__file__).parent / "data" / "beta_cache.json"
CACHE_TTL_DAYS = 14          # betas move slowly; a fortnight is not stale
LOOKBACK_DAYS = 252          # ~1 trading year
MIN_OBSERVATIONS = 60        # below this the slope is noise, not a beta

# Minimum share of the book (by absolute market value) that must have a beta
# before `net_beta` is willing to state one. A net beta computed from a third
# of the book is a number about a third of the book.
MIN_COVERAGE = 0.80


# ---------------------------------------------------------------------------
# Structural betas — the instrument's own mandate, not an estimate of it
# ---------------------------------------------------------------------------
# Quoted against the fund's OWN index. `_INDEX_TO_SPY` converts. Deliberately a
# short, explicit, hand-reviewed table: inferring "this looks like an inverse
# ETF" from the ticker or the name would turn the hedge exemption below into a
# naming convention, and a limit you can escape by naming something differently
# is not a limit.
_INDEX_TO_SPY = {
    "SPX": 1.00,    # by definition
    "NDX": 1.10,    # Nasdaq-100 runs hotter than the S&P; an ESTIMATE
    "DJI": 0.95,    # an ESTIMATE
    "RUT": 1.15,    # small caps; an ESTIMATE
}

STRUCTURAL_BETA: Dict[str, Tuple[float, str]] = {
    # S&P 500
    "SH":    (-1.0, "SPX"), "SDS":  (-2.0, "SPX"),
    "SPXU":  (-3.0, "SPX"), "SPXS": (-3.0, "SPX"),
    "SPY":   (1.0, "SPX"),  "SSO":  (2.0, "SPX"),  "UPRO": (3.0, "SPX"),
    "VOO":   (1.0, "SPX"),  "IVV":  (1.0, "SPX"),
    # Nasdaq-100
    "PSQ":   (-1.0, "NDX"), "QID":  (-2.0, "NDX"), "SQQQ": (-3.0, "NDX"),
    "QQQ":   (1.0, "NDX"),  "QLD":  (2.0, "NDX"),  "TQQQ": (3.0, "NDX"),
    # Dow
    "DOG":   (-1.0, "DJI"), "DXD":  (-2.0, "DJI"), "SDOW": (-3.0, "DJI"),
    "DIA":   (1.0, "DJI"),
    # Russell 2000
    "RWM":   (-1.0, "RUT"), "TWM":  (-2.0, "RUT"), "SRTY": (-3.0, "RUT"),
    "IWM":   (1.0, "RUT"),
}


def structural_beta(symbol: str) -> Optional[float]:
    """SPY-beta of an instrument whose exposure is defined by its mandate."""
    row = STRUCTURAL_BETA.get(str(symbol).upper().strip())
    if not row:
        return None
    mult, index = row
    return round(mult * _INDEX_TO_SPY.get(index, 1.0), 4)


def is_structural_hedge(symbol: str) -> bool:
    """True only for instruments whose NEGATIVE exposure is by construction.

    Not "has a negative measured beta". A stock that happened to fall while the
    market rose prints a negative beta for a lookback and is not a hedge; the
    exemption below must not be reachable by a noisy regression.
    """
    b = structural_beta(symbol)
    return b is not None and b < 0


# ---------------------------------------------------------------------------
# Measured beta
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        if _CACHE_PATH.exists():
            with open(_CACHE_PATH) as fh:
                d = json.load(fh)
                return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.warning(f"  [beta] cache unreadable ({e}) — starting empty")
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(cache, fh, indent=2)
        tmp.replace(_CACHE_PATH)
    except Exception as e:                                    # pragma: no cover
        logger.warning(f"  [beta] could not write cache: {e}")


def _fresh(entry: dict) -> bool:
    try:
        ts = datetime.fromisoformat(str(entry.get("at")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) < timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return False


def _ols_beta(sym_returns: List[float], mkt_returns: List[float]) -> Optional[float]:
    """Slope of sym on mkt. None when the market has no variance to regress on."""
    n = min(len(sym_returns), len(mkt_returns))
    if n < MIN_OBSERVATIONS:
        return None
    x = mkt_returns[-n:]
    y = sym_returns[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    var = sum((v - mx) ** 2 for v in x)
    if var <= 0 or not math.isfinite(var):
        return None
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    b = cov / var
    return round(b, 4) if math.isfinite(b) else None


def measured_beta(symbol: str, use_cache: bool = True) -> Optional[float]:
    """OLS beta vs SPY from daily closes. None when it cannot be measured.

    None is the honest answer for a symbol with no history, a data outage, or
    too few observations, and it must stay None: a caller that wants a number
    can decide to assume one, but it will have to write that decision down.
    """
    sym = str(symbol).upper().strip()
    if not sym:
        return None

    cache = _load_cache() if use_cache else {}
    hit = cache.get(sym)
    if use_cache and isinstance(hit, dict) and _fresh(hit):
        v = hit.get("beta")
        return float(v) if isinstance(v, (int, float)) else None

    try:
        import yfinance as yf
        data = yf.download([sym, "SPY"], period=f"{LOOKBACK_DAYS}d",
                           auto_adjust=True, progress=False)
        closes = data["Close"] if "Close" in data else None
        if closes is None or sym not in closes or "SPY" not in closes:
            return None
        s = closes[sym].dropna()
        m = closes["SPY"].dropna()
        joined = s.to_frame("s").join(m.to_frame("m"), how="inner").dropna()
        if len(joined) < MIN_OBSERVATIONS + 1:
            return None
        sr = joined["s"].pct_change().dropna().tolist()
        mr = joined["m"].pct_change().dropna().tolist()
        beta = _ols_beta(sr, mr)
    except Exception as e:
        logger.warning(f"  [beta] could not measure {sym}: {e}")
        return None

    if beta is not None and use_cache:
        cache[sym] = {"beta": beta, "at": datetime.now(timezone.utc).isoformat(),
                      "n": len(sr), "source": "ols_vs_spy"}
        _save_cache(cache)
    return beta


def beta_of(symbol: str, use_cache: bool = True) -> Tuple[Optional[float], str]:
    """(beta, source). source is 'structural' | 'measured' | 'unknown'."""
    b = structural_beta(symbol)
    if b is not None:
        return b, "structural"
    b = measured_beta(symbol, use_cache=use_cache)
    if b is not None:
        return b, "measured"
    return None, "unknown"


# ---------------------------------------------------------------------------
# Net exposure
# ---------------------------------------------------------------------------

def net_beta(positions: Dict[str, dict], equity: float,
             use_cache: bool = True) -> dict:
    """Beta-weighted net exposure as a multiple of equity.

    `positions` is {SYMBOL: {"qty": float, "price": float}}. A short carries a
    negative qty and therefore a negative market value, so shorts subtract
    without any special case — which is the point of measuring exposure rather
    than counting positions.

    Returns `beta=None` when coverage is below MIN_COVERAGE. A net beta computed
    from part of the book is a fact about part of the book, and reporting it as
    the firm's would be the same error as the old pod ledger reporting a total
    it could not see most of.
    """
    try:
        eq = float(equity)
        if not math.isfinite(eq) or eq <= 0:
            return {"beta": None, "reason": f"equity is not usable ({equity!r})",
                    "coverage": 0.0, "covered_value": 0.0, "gross_value": 0.0,
                    "unknown_symbols": [], "long_beta_dollars": 0.0,
                    "short_beta_dollars": 0.0}
    except (TypeError, ValueError):
        return {"beta": None, "reason": f"equity is not usable ({equity!r})",
                "coverage": 0.0, "covered_value": 0.0, "gross_value": 0.0,
                "unknown_symbols": [], "long_beta_dollars": 0.0,
                "short_beta_dollars": 0.0}

    gross = 0.0
    covered = 0.0
    beta_dollars = 0.0
    long_bd = 0.0
    short_bd = 0.0
    unknown: List[str] = []

    for sym, pos in (positions or {}).items():
        try:
            qty = float(pos.get("qty") or 0.0)
            price = float(pos.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(qty) and math.isfinite(price)) or qty == 0:
            continue
        mv = qty * price
        gross += abs(mv)
        b, _src = beta_of(sym, use_cache=use_cache)
        if b is None:
            unknown.append(str(sym).upper())
            continue
        covered += abs(mv)
        bd = mv * b
        beta_dollars += bd
        if bd >= 0:
            long_bd += bd
        else:
            short_bd += bd

    coverage = (covered / gross) if gross > 0 else 1.0
    out = {
        "gross_value": round(gross, 2),
        "covered_value": round(covered, 2),
        "coverage": round(coverage, 4),
        "unknown_symbols": sorted(unknown),
        "beta_dollars": round(beta_dollars, 2),
        "long_beta_dollars": round(long_bd, 2),
        "short_beta_dollars": round(short_bd, 2),
        "equity": round(eq, 2),
    }
    if coverage < MIN_COVERAGE:
        out["beta"] = None
        out["reason"] = (f"only {coverage*100:.0f}% of the book has a beta "
                         f"(need {MIN_COVERAGE*100:.0f}%); unpriced or "
                         f"unmeasurable: {', '.join(unknown[:8])}")
    else:
        out["beta"] = round(beta_dollars / eq, 4)
        out["reason"] = ""
    return out


def beta_effect_of(symbol: str, qty: float, price: float,
                   positions: Dict[str, dict], equity: float,
                   use_cache: bool = True) -> dict:
    """Would this order move |net beta| toward zero, and by how much?

    Returns `verdict` in {REDUCES, INCREASES, OVERSHOOTS, CANNOT_TELL}.

    OVERSHOOTS is its own verdict on purpose. A "hedge" large enough to carry
    the book from net long through flat to net short is not a hedge, it is a
    directional bet in the other direction, and it must not inherit a hedge's
    exemption. Where a hedge stops being a hedge is exactly at zero, and that
    is a line worth naming rather than a size the sizer happens to pick.
    """
    before = net_beta(positions, equity, use_cache=use_cache)
    if before["beta"] is None:
        return {"verdict": "CANNOT_TELL", "before": before, "after": None,
                "reason": before.get("reason") or "net beta is unavailable"}

    b, src = beta_of(symbol, use_cache=use_cache)
    if b is None:
        return {"verdict": "CANNOT_TELL", "before": before, "after": None,
                "reason": f"no beta for {str(symbol).upper()} — it is neither a "
                          f"structural product nor measurable right now"}

    try:
        q = float(qty)
        p = float(price)
    except (TypeError, ValueError):
        return {"verdict": "CANNOT_TELL", "before": before, "after": None,
                "reason": f"order is not priceable (qty={qty!r}, price={price!r})"}
    if not (math.isfinite(q) and math.isfinite(p)) or q == 0 or p <= 0:
        return {"verdict": "CANNOT_TELL", "before": before, "after": None,
                "reason": f"order is not priceable (qty={qty!r}, price={price!r})"}

    sym = str(symbol).upper()
    after_positions = dict(positions or {})
    existing = after_positions.get(sym, {})
    try:
        held = float(existing.get("qty") or 0.0)
    except (TypeError, ValueError):
        held = 0.0
    after_positions[sym] = {"qty": held + q, "price": p}
    after = net_beta(after_positions, equity, use_cache=use_cache)
    if after["beta"] is None:
        return {"verdict": "CANNOT_TELL", "before": before, "after": after,
                "reason": after.get("reason") or "post-trade net beta unavailable"}

    b0, b1 = before["beta"], after["beta"]
    # Sign cross is tested FIRST. An order that carries the book from +0.6 to
    # -0.9 both crosses flat and enlarges the exposure; calling that INCREASES
    # is true but says the wrong thing, because the fact that decides the case
    # is that it went through zero. Landing exactly at flat is not a cross
    # (b0 * 0 == 0), and is the best outcome a hedge can have.
    if b0 * b1 < 0:
        verdict = "OVERSHOOTS"
    elif abs(b1) >= abs(b0) - 1e-9:
        verdict = "INCREASES"
    else:
        verdict = "REDUCES"

    return {
        "verdict": verdict,
        "symbol": sym,
        "instrument_beta": b,
        "instrument_beta_source": src,
        "before": before,
        "after": after,
        "delta_abs_beta": round(abs(b1) - abs(b0), 4),
        "reason": (f"net beta {b0:+.2f} -> {b1:+.2f} "
                   f"({sym} beta {b:+.2f}, {src})"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _live_positions() -> Tuple[Dict[str, dict], float]:
    """Read the broker. Returns ({}, 0.0) rather than guessing."""
    try:
        import risk_gate
        snap = risk_gate.snapshot()
        if not snap:
            return {}, 0.0
        pos = {s: {"qty": v.get("qty"), "price": v.get("price")}
               for s, v in snap.get("positions", {}).items()}
        return pos, float(snap.get("portfolio_value") or 0.0)
    except Exception as e:
        logger.warning(f"  [beta] could not read the account: {e}")
        return {}, 0.0


if __name__ == "__main__":
    import argparse
    # The library path is imported by callers that have already loaded the env;
    # the CLI has not, and without this every `--report` dies on KeyError:
    # 'ALPACA_API_KEY' inside `snapshot()` — which `_live_positions` catches and
    # reports as an empty book. A missing credential must not present as "the
    # firm holds nothing" (memory `fallback-that-means-act`).
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--symbol", help="beta of one symbol")
    ap.add_argument("--hedge", help="what would buying this symbol do")
    ap.add_argument("--qty", type=float, default=0.0)
    args = ap.parse_args()

    if args.symbol:
        b, src = beta_of(args.symbol)
        print(json.dumps({"symbol": args.symbol.upper(), "beta": b,
                          "source": src}, indent=2))
    else:
        positions, equity = _live_positions()
        nb = net_beta(positions, equity)
        print(json.dumps(nb, indent=2))
        if args.hedge and args.qty:
            price = None
            try:
                import risk_gate
                price = risk_gate._quote(args.hedge)
            except Exception:
                pass
            if price:
                print(json.dumps(beta_effect_of(args.hedge, args.qty, price,
                                                positions, equity), indent=2))
            else:
                print(f"could not price {args.hedge}")
