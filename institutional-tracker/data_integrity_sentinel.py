#!/usr/bin/env python3
"""
Data Integrity Sentinel  (Department: CIT — code/quality oversight)
===================================================================
The umbrella watchdog for the firm's SILENT-BAD-DATA bug class: a value gets
fetched or computed, looks plausibly numeric, is stored, and flows into a trading
decision — but nothing checks it is physically sane, correctly scaled, fresh, or
reconciled with the broker. That class produced the 2026-07 fundamentals mess
(ABBV P/E 256, INCY debt/assets 2.74) and had no single owner. This is that owner.

It does NOT re-implement the specialists — it COMPOSES them and adds the gaps, so
there is one scheduled sweep and one health verdict instead of scattered checks:

  1. fundamentals   → fundamentals_sanity.run()        (period/alignment/currency)
  2. risk & model   → metric_sanity_sentinel.run()     (VaR, Herfindahl, AUC bounds)
  3. freshness      → key artifacts younger than their cadence (stale data = wrong data)
  4. json health    → every data/*.json parses and holds only FINITE numbers
                      (NaN/Infinity are valid Python-json output but invalid JSON and
                      silently break downstream consumers)

DESIGN NOTE — extensibility is the point. New invariants are added as one entry in
CHECKS below; each returns (section_name, status, findings). Adding a check must be
a one-line change so the firm's self-inspection keeps pace with the firm. When you
add a producer of a NEW numeric artifact, add its invariant here.

Read-only with --report. `run(write=True)` writes data/data_integrity_report.json
and lets the specialist sub-agents file their own board proposals (they dedupe).
Freshness/json findings are filed here as a single consolidated Chairman item when
they are genuinely blocking, never one-per-file (queue-noise is its own anti-pattern).
"""
from __future__ import annotations
import json, math, re, argparse, datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
REPORT_PATH = DATA / "data_integrity_report.json"
CHAIR_ACTION = DATA / "chairman_action_required.json"

# Artifact -> max age in hours before it is considered stale. Only files that are
# supposed to refresh on a cadence are listed; static registries are omitted.
FRESHNESS = {
    "model_meta.json": 24 * 14,          # retrained ~weekly; 2wk = stale
    "pipeline_heartbeat.json": 26,       # weekday pipeline; mirrors deadman's switch
    "fundamentals_sanity_report.json": 24 * 3,
    "metric_sanity_report.json": 24,
    "canary_state.json": 24,
}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _iter_numbers(obj):
    """Yield every numeric leaf in a nested JSON structure."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_numbers(v)
    elif isinstance(obj, (int, float)):
        yield obj


# ---------------------------------------------------------------------------
# Individual invariant checks. Each returns (section, status, findings[list]).
# status in {"GREEN","YELLOW","RED"}.
# ---------------------------------------------------------------------------

def check_fundamentals():
    try:
        import fundamentals_sanity as fs
        rep = fs.run(write=True)  # dedupes its own CIT proposals
        bad = rep.get("impossible_tickers", [])
        status = "RED" if bad else "GREEN"
        findings = [{"note": f"{len(bad)} ticker(s) with physically-impossible "
                     f"fundamentals: {', '.join(bad[:20])}"}] if bad else []
        return ("fundamentals", status, findings)
    except Exception as e:
        return ("fundamentals", "YELLOW", [{"note": f"validator error: {e}"}])


def check_risk_and_model():
    try:
        import metric_sanity_sentinel as ms
        rep = ms.run(write=True)
        viols = rep.get("violations", [])
        impossible = [v for v in viols if v.get("impossible")]
        status = "RED" if impossible else ("YELLOW" if viols else "GREEN")
        return ("risk_and_model", status,
                [{"note": f"{v['metric']}={v['value']} ({v['note']})"} for v in viols])
    except Exception as e:
        return ("risk_and_model", "YELLOW", [{"note": f"sentinel error: {e}"}])


def check_freshness():
    findings, worst = [], "GREEN"
    now = datetime.datetime.now()
    for fname, max_h in FRESHNESS.items():
        p = DATA / fname
        if not p.exists():
            continue  # absent is handled by wiring_audit / canary, not here
        age_h = (now - datetime.datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 3600
        if age_h > max_h:
            findings.append({"file": fname, "age_hours": round(age_h, 1),
                             "limit_hours": max_h,
                             "note": f"{fname} is {age_h/24:.1f}d old (> {max_h/24:.1f}d limit) "
                                     "— downstream may be trading on stale inputs."})
            worst = "YELLOW"
    return ("freshness", worst, findings)


def check_json_health():
    findings, worst = [], "GREEN"
    for p in sorted(DATA.glob("*.json")):
        raw = p.read_text() if p.exists() else ""
        # 1) BARE NaN/Infinity tokens that Python's json writes but standards forbid.
        # Must use the token regex, not a substring scan: the words "NaN"/"Infinity"
        # legitimately appear inside string values (e.g. "pe_ratio": "Infinity") and in
        # prose (this sentinel's own report). Only unquoted structural tokens are corrupt.
        if _NONFINITE.search(raw):
            findings.append({"file": p.name, "note": "contains NaN/Infinity — invalid JSON; "
                             "downstream json.loads in other languages/tools will fail."})
            worst = "RED"
            continue
        # 2) parseability + non-finite floats (belt and suspenders)
        try:
            obj = json.loads(raw) if raw.strip() else None
        except Exception as e:
            findings.append({"file": p.name, "note": f"does not parse: {e}"})
            worst = "RED"
            continue
        if obj is not None and any(isinstance(n, float) and not math.isfinite(n)
                                   for n in _iter_numbers(obj)):
            findings.append({"file": p.name, "note": "holds a non-finite float"})
            worst = "RED"
    return ("json_health", worst, findings)


CHECKS = [check_fundamentals, check_risk_and_model, check_freshness, check_json_health]

_RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2}

# Match the invalid JSON tokens as standalone values (after ':' or in an array),
# never inside a string. json.dump writes them bare, so they always sit between
# structural chars.
_NONFINITE = re.compile(r'(?<=[:\[,\s])(-?Infinity|NaN)(?=[\s,\]\}])')


def heal_json_health() -> list[str]:
    """Rewrite any data/*.json containing NaN/Infinity, replacing those invalid
    tokens with null (valid JSON; downstream .get()/None-handling already tolerates
    it). This is a safe, universal net that cleans corruption regardless of which
    producer emitted the non-finite value. Only top-level data/ files, not backups."""
    healed = []
    for p in sorted(DATA.glob("*.json")):
        raw = p.read_text()
        if not _NONFINITE.search(raw):
            continue
        fixed = _NONFINITE.sub("null", raw)
        try:
            json.loads(fixed)  # only write if the result is now valid JSON
        except Exception:
            continue           # couldn't safely heal — leave it for the RED flag
        p.write_text(fixed)
        healed.append(p.name)
    return healed


def _file_chairman(sections: dict):
    """File ONE consolidated Chairman item if any section is RED. Deduped by day."""
    red = {name: s for name, s in sections.items() if s["status"] == "RED"}
    if not red:
        return 0
    actions = _load(CHAIR_ACTION, [])
    if not isinstance(actions, list):
        actions = []
    aid = "data-integrity-" + datetime.date.today().isoformat()
    if any(isinstance(a, dict) and a.get("id") == aid for a in actions):
        return 0
    summary = "; ".join(f"{name}: {len(s['findings'])} issue(s)" for name, s in red.items())
    actions.insert(0, {
        "id": aid,
        "source": "Data Integrity Sentinel (CIT)",
        "severity": "HIGH",
        "description": f"Firm data-integrity sweep is RED — {summary}. See "
                       "data/data_integrity_report.json. Impossible values are quarantined "
                       "from trading; decide on root-cause fixes.",
        "status": "awaiting_chairman",
        "ts": datetime.datetime.utcnow().isoformat(),
    })
    CHAIR_ACTION.write_text(json.dumps(actions, indent=2))
    return 1


def run(write: bool = True) -> dict:
    healed = heal_json_health() if write else []
    sections = {}
    overall = "GREEN"
    for chk in CHECKS:
        name, status, findings = chk()
        sections[name] = {"status": status, "findings": findings}
        if _RANK[status] > _RANK[overall]:
            overall = status
    report = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "overall": overall,
        "healed_files": healed,
        "sections": sections,
    }
    if write:
        # Written twice on purpose — the first write is crash insurance for the
        # sections, the second is what makes the receipt readable. Whether this
        # sweep escalated to the Chairman is exactly the fact an auditor needs,
        # and it was being set after the only write. See the same fix in
        # metric_sanity_sentinel, fundamentals_sanity, hr_agent, irm_review_agent.
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        report["chairman_filed"] = _file_chairman(sections)
        REPORT_PATH.write_text(json.dumps(report, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser(description="Firm-wide data integrity sweep.")
    ap.add_argument("--report", action="store_true", help="print, don't write/file")
    args = ap.parse_args()
    rep = run(write=not args.report)
    print(f"DATA INTEGRITY: {rep['overall']}")
    for name, s in rep["sections"].items():
        mark = {"GREEN": "✅", "YELLOW": "⚠️ ", "RED": "🛑"}[s["status"]]
        print(f"  {mark} {name:16} {s['status']:6} ({len(s['findings'])} finding(s))")
        for f in s["findings"][:5]:
            print(f"        - {f.get('note') or f}")
    if rep.get("healed_files"):
        print(f"  → healed {len(rep['healed_files'])} corrupt file(s): "
              f"{', '.join(rep['healed_files'])}")
    if not args.report and rep.get("chairman_filed"):
        print("  → filed a consolidated Chairman decision item.")


if __name__ == "__main__":
    main()
