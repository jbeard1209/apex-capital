#!/usr/bin/env python3
"""
Fundamentals Sanity Validator  (Department: CIT — code/quality oversight)
========================================================================
The companion to metric_sanity_sentinel.py, one layer UPSTREAM. The sentinel
bounds-checks *portfolio/risk* outputs; this bounds-checks the *fundamentals cache*
(data/fe_fundamentals_cache/*.json) that feeds every valuation.

WHY THIS EXISTS
---------------
The 2026-07-27 deep dive produced garbage ratios (ABBV P/E 256, INCY debt/assets
2.74, SNBR price $0.00) and nobody caught it until the numbers were eyeballed.
Every prior guard checked PLUMBING (does code run, do files exist, are queues wired)
— none checked whether the NUMBERS THEMSELVES were physically possible. Two root
causes were found and fixed in fe_edgar_fundamentals.py:
  1. period-mixing: a 10-K's XBRL carries quarterly facts tagged form="10-K"; the
     parser filtered by form but not period length, so a 3-month value could
     overwrite the 12-month one. (Fixed: require ~annual span for flow concepts.)
  2. positional misalignment: unlabeled series of DIFFERENT lengths meant `[-1]`
     of two metrics could be different fiscal years. (Fixed: `by_year` keyed output.)
But a third cause can't be fixed by parsing at all: EDGAR sometimes carries a
BAD SOURCE VALUE (INCY 2018 LongTermDebt tagged $19.09B on $2.3B of assets). Only
a PLAUSIBILITY check catches that. This validator is that check, and it is meant to
be a GATE: FE / deep-dive code should call `is_reliable()` and refuse to value a
name whose fundamentals are impossible, rather than trade on garbage.

Read-only by default. `--report` prints findings; `run(write=True)` files a CIT
proposal per structurally-broken ticker (deduped).
"""
from __future__ import annotations
import json, argparse, datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CACHE_DIR = DATA / "fe_fundamentals_cache"
CIT_PENDING = DATA / "cit_pending_proposals.json"
REPORT_PATH = DATA / "fundamentals_sanity_report.json"

STALE_DAYS = 120  # fundamentals refresh cadence; older with no fetched_at = unknown-age


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _latest(series):
    """Last numeric element of a list, or None."""
    if isinstance(series, list):
        for v in reversed(series):
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _aligned_latest(by_year, field):
    """Most-recent year's value for a by_year field, or None."""
    d = (by_year or {}).get(field) or {}
    if not isinstance(d, dict) or not d:
        return None
    y = max(d)
    v = d.get(y)
    return (float(v), y) if isinstance(v, (int, float)) else None


ETF_HINT = {  # instruments with no company fundamentals; never board-escalate these
    "GDX", "GDXJ", "ETHB", "SPY", "QQQ", "IEF", "SHY", "VCIT", "VCSH", "IGSB",
    "IGIB", "USIG", "SLQD", "XLE", "XLF", "XLK", "GLD", "SLV",
}


def check_ticker(ticker: str, data: dict) -> list[dict]:
    """Return a list of finding dicts for one ticker's fundamentals.

    Two orthogonal flags per finding:
      • gate       — True means 'do not value/trade this name' (data unusable).
      • impossible — True means 'physically-impossible number' → board-worthy bug.
    A missing share count GATES valuation but is NOT impossible (it's a data gap,
    often an ETF or a renamed ticker), so it must not spam LOGIC_ERROR proposals.
    A debt>assets value is BOTH — it blocks valuation AND signals bad source data."""
    v = []
    if not isinstance(data, dict):
        return [{"ticker": ticker, "metric": "cache", "value": "unreadable",
                 "gate": True, "impossible": True, "severity": "HIGH", "note": "cache file not a dict"}]

    by_year = data.get("by_year") or {}

    def add(metric, value, note, gate=True, impossible=True, severity="HIGH"):
        v.append({"ticker": ticker, "metric": metric, "value": value, "gate": gate,
                  "impossible": impossible, "severity": severity, "note": note})

    # --- shares outstanding must be positive (gates valuation; not a 'bug') ---
    sh = _latest(data.get("shares_outstanding"))
    if sh is None or sh <= 0:
        add("shares_outstanding", sh, "Shares outstanding missing or <= 0 — per-share "
            "metrics undefined; likely an ETF, foreign issuer, or unmapped share tag.",
            impossible=False, severity="MEDIUM")

    # --- debt cannot exceed assets (year-ALIGNED; this is what caught INCY) ---
    la = _aligned_latest(by_year, "long_term_debt")
    ta = _aligned_latest(by_year, "total_assets")
    if la and ta and la[1] == ta[1] and ta[0] > 0 and la[0] > ta[0] * 1.05:
        add("long_term_debt/total_assets", round(la[0] / ta[0], 2),
            f"FY{la[1]}: long-term debt ${la[0]/1e9:.2f}B exceeds total assets "
            f"${ta[0]/1e9:.2f}B — physically impossible; likely a bad EDGAR source value.")
    elif la is None and by_year.get("long_term_debt") == {} and ta:
        pass  # zero-debt company; fine

    # --- liabilities shouldn't wildly exceed assets (implausible, not blocking) ---
    ll = _aligned_latest(by_year, "total_liabilities")
    if ll and ta and ll[1] == ta[1] and ta[0] > 0 and ll[0] > ta[0] * 2.0:
        add("total_liabilities/total_assets", round(ll[0] / ta[0], 2),
            f"FY{ll[1]}: liabilities are >2x assets — deep insolvency or bad data.",
            gate=False, impossible=False, severity="MEDIUM")

    # --- core flow series must exist (gates valuation; a data gap, not a bug) ---
    for field in ("revenue", "net_income"):
        if not (data.get(field) or []):
            add(field, "empty", f"{field} series empty — cannot value the company.",
                gate=True, impossible=False, severity="MEDIUM")

    # --- stale SUB-SERIES: a field that stopped updating is as dangerous as a
    #     missing one. EOG's shares stop at 2012 (P/E computed on a 13yr-old share
    #     count); INCY's long_term_debt stops at 2018 (leverage ratio unreliable).
    #     Anchor to the record's newest fiscal year (from the reliably-populated
    #     series) and gate any per-share / leverage input that lags it by >2 years. ---
    def _max_year(field):
        ys = [int(y) for y in (by_year.get(field) or {}).keys() if str(y).isdigit()]
        return max(ys) if ys else None
    anchor = max([y for y in (_max_year("net_income"), _max_year("revenue"),
                              _max_year("total_assets")) if y is not None], default=None)
    if anchor is not None:
        for field, label in (("shares_outstanding", "per-share metrics"),
                             ("long_term_debt", "leverage ratio")):
            fy = _max_year(field)
            if fy is not None and anchor - fy > 2:
                add(field, f"latest FY{fy} vs record FY{anchor}",
                    f"{field} series stops at FY{fy} but the company reports through "
                    f"FY{anchor} — {label} would use a {anchor - fy}yr-stale value. "
                    "Likely a taxonomy/tag change EDGAR parsing missed.",
                    gate=True, impossible=False, severity="MEDIUM")

    # --- freshness: unknown age is a soft flag only ---
    fetched = data.get("fetched_at")
    if not fetched:
        add("fetched_at", "missing", "No fetch timestamp — cache age unknown; refetch "
            "to confirm currency.", gate=False, impossible=False, severity="LOW")
    else:
        try:
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(fetched)
            if age.days > STALE_DAYS:
                add("fetched_at", fetched, f"Fundamentals cache is {age.days}d old "
                    f"(> {STALE_DAYS}d) — may predate the latest 10-K.",
                    gate=False, impossible=False, severity="LOW")
        except Exception:
            pass
    return v


def is_reliable(data: dict) -> tuple[bool, list[str]]:
    """GATE for FE / deep-dive code: True only if nothing BLOCKS valuation.
    Gating findings (missing shares/revenue, impossible ratios) block; soft flags
    (staleness) don't. This is the guard a pod calls before valuing a name."""
    viols = check_ticker(data.get("_ticker", "?"), data)
    hard = [x for x in viols if x.get("gate")]
    return (not hard, [x["note"] for x in hard])


def scan_cache() -> list[dict]:
    out = []
    if not CACHE_DIR.exists():
        return out
    for f in sorted(CACHE_DIR.glob("*.json")):
        out.extend(check_ticker(f.stem, _load(f, {})))
    return out


def _file_bug_proposals(violations) -> int:
    """One deduped CIT proposal per ticker with an IMPOSSIBLE fundamental.
    ETFs / instruments with no fundamentals are never board-escalated."""
    impossible = [v for v in violations if v.get("impossible") and v["ticker"] not in ETF_HINT]
    if not impossible:
        return 0
    pending = _load(CIT_PENDING, [])
    if not isinstance(pending, list):
        pending = []
    existing = {p.get("id") for p in pending if isinstance(p, dict)}
    now = datetime.datetime.utcnow().isoformat()
    added = 0
    by_ticker: dict[str, list] = {}
    for v in impossible:
        by_ticker.setdefault(v["ticker"], []).append(v)
    for ticker, vs in by_ticker.items():
        pid = f"fund-sanity-{ticker}"
        if pid in existing:
            continue
        notes = "; ".join(f"{x['metric']}={x['value']} ({x['note']})" for x in vs)
        pending.append({
            "id": pid,
            "source": "Fundamentals Sanity Validator (CIT)",
            "category": "LOGIC_ERROR",
            "severity": max(x["severity"] for x in vs),
            "confidence": 88,
            "title": f"Impossible fundamentals for {ticker} — quarantine from valuation",
            "description": notes,
            "evidence": f"fundamentals_sanity: {ticker} fails plausibility: {notes}",
            "proposed_fix": "Refetch on fixed fe_edgar_fundamentals.py; if the value persists "
                            "it is a bad EDGAR source fact — quarantine the ticker via "
                            "is_reliable() so no pod values or trades it until corrected.",
            "auto_implementable": False,
            "ts": now,
        })
        added += 1
    if added:
        CIT_PENDING.write_text(json.dumps(pending, indent=2))
    return added


def run(write: bool = True) -> dict:
    viols = scan_cache()
    impossible_tickers = sorted({v["ticker"] for v in viols
                                 if v.get("impossible") and v["ticker"] not in ETF_HINT})
    gated_tickers = sorted({v["ticker"] for v in viols if v.get("gate")})
    report = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "tickers_scanned": len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.exists() else 0,
        "impossible_tickers": impossible_tickers,   # board-worthy bad-source values
        "gated_tickers": gated_tickers,             # quarantined from valuation (incl. data gaps)
        "violations": viols,
        "clean": not impossible_tickers,
    }
    if write:
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        report["bugs_filed"] = _file_bug_proposals(viols)
    return report


def main():
    ap = argparse.ArgumentParser(description="Bounds-check the fundamentals cache.")
    ap.add_argument("--report", action="store_true", help="print findings, don't write")
    ap.add_argument("--ticker", help="check a single ticker")
    args = ap.parse_args()

    if args.ticker:
        data = _load(CACHE_DIR / f"{args.ticker.upper()}.json", {})
        for v in check_ticker(args.ticker.upper(), data):
            flag = "IMPOSSIBLE" if v["impossible"] else ("GATE" if v["gate"] else "flag")
            print(f"[{flag:10}] {v['metric']} = {v['value']} — {v['note']}")
        ok, reasons = is_reliable({**data, "_ticker": args.ticker.upper()})
        print(f"\nreliable_for_valuation: {ok}" + ("" if ok else f"  ({'; '.join(reasons)})"))
        return

    rep = run(write=not args.report)
    print(f"Scanned {rep['tickers_scanned']} tickers · "
          f"{len(rep['impossible_tickers'])} IMPOSSIBLE (board) · "
          f"{len(rep['gated_tickers'])} gated from valuation")
    if rep["impossible_tickers"]:
        print("  impossible:", ", ".join(rep["impossible_tickers"][:40]))
    if not args.report:
        print(f"  CIT proposals filed: {rep.get('bugs_filed', 0)}")


if __name__ == "__main__":
    main()
