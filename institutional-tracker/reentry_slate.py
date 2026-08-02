"""
reentry_slate.py — "what happened while I was gone", on one page.

Asked for 2026-07-27 alongside the session-start Chairman-queue reader
(chairman_inbox.py). That half shipped; this half did not, and the 7-day command
audit on 2026-08-01 caught it.

WHY IT IS GENERATED, NOT WRITTEN. The obvious version is an HTML file someone
composes once. That file is correct for a day and misleading forever after —
which is precisely the failure mode this firm keeps re-learning (a stale doc that
an auditor then measures reality against, see the orphan-baseline and
VM-freshness incidents). So every number below is read from live state at render
time, and the page states, per section, the file it came from and how old that
file is. A section whose source is missing says so in the page instead of
rendering as an empty, reassuring zero.

    python3 reentry_slate.py              # write data/reentry_slate.html
    python3 reentry_slate.py --stdout     # print it instead
    python3 reentry_slate.py --report     # numbers only, no HTML

Served live by dashboard_server.py at /reentry.
"""

import argparse
import datetime as dt
import html
import json
import logging
import os
from collections import OrderedDict

logger = logging.getLogger("reentry_slate")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT_PATH = os.path.join(DATA, "reentry_slate.html")

SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

#: A source older than this is called out on the page. The point is not that
#: stale data is useless — it is that a slate which renders a two-week-old
#: number identically to a fresh one is how you get confidently briefed on the
#: past.
STALE_HOURS = 36


def _load(path, default):
    """Read JSON, distinguishing 'absent' from 'empty' for the caller.

    Returns (value, note). `note` is None when the file was read cleanly and a
    human-readable reason otherwise — the page prints it rather than showing a
    section that looks healthy because nothing could be counted.
    """
    if not os.path.exists(path):
        return default, "%s does not exist on this host" % os.path.basename(path)
    try:
        with open(path) as f:
            return json.load(f), None
    except (ValueError, OSError) as e:
        return default, "%s could not be read: %s" % (os.path.basename(path), e)


def _age_hours(path):
    if not os.path.exists(path):
        return None
    return (dt.datetime.now()
            - dt.datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600.0


def _parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00").split("+")[0])
    except (TypeError, ValueError):
        return None


def _days_since(s):
    t = _parse_ts(s)
    return None if t is None else (dt.datetime.now() - t).days


# ── sections ──────────────────────────────────────────────────────────────────

def decisions_owed():
    """Rows waiting on the Chairman, worst first, with the escalation clock.

    Reads through chairman_inbox.load_pending() rather than re-implementing the
    filter, so the page and the session-start banner can never disagree about
    what "pending" means. It deliberately does NOT call chairman_inbox.reconcile()
    — that reaches the VM over ssh, and a page render must not depend on the
    network. The staleness of the file is printed instead.
    """
    path = os.path.join(DATA, "chairman_decision_queue.json")
    note = None
    try:
        import chairman_inbox
        rows = chairman_inbox.load_pending()
    except ImportError as e:                       # pragma: no cover - import guard
        raw, note = _load(path, [])
        rows = [e2 for e2 in (raw or [])
                if e2.get("status") == "awaiting_chairman"]
        note = note or "chairman_inbox unavailable (%s); read the file directly" % e
    if not rows and note is None and not os.path.exists(path):
        note = "chairman_decision_queue.json does not exist on this host"
    out = []
    today = dt.datetime.now()
    for e in rows:
        deadline = _parse_ts(e.get("deadline"))
        out.append({
            "id": e.get("id") or "(unidentified)",
            "severity": (e.get("severity") or "MEDIUM").upper(),
            "source": e.get("source") or "",
            "age_days": _days_since(e.get("first_surfaced")),
            "escalates_in": None if deadline is None else (deadline - today).days,
            "description": (e.get("description") or "").strip(),
        })
    out.sort(key=lambda r: (SEV_RANK.get(r["severity"], 9),
                            r["escalates_in"] if r["escalates_in"] is not None else 999))
    return {"rows": out, "note": note, "age_hours": _age_hours(path),
            "source": "data/chairman_decision_queue.json"}


def firm_is_handling():
    """Open sweep findings — the work the firm assigned itself, by owner.

    Deliberately separate from the section above. The whole design of the weekly
    sweep is that most findings are NOT the Chairman's to answer; showing them in
    one undifferentiated list is what took his queue to 88 rows.
    """
    path = os.path.join(DATA, "hr_action_queue.json")
    raw, note = _load(path, [])
    rows = [e for e in (raw or []) if str(e.get("status", "open")) == "open"]
    by_owner, overdue = OrderedDict(), []
    for e in rows:
        by_owner.setdefault(e.get("owner") or "(unassigned)", []).append(e)
        age = _days_since(e.get("first_seen"))
        sla = e.get("sla_days")
        if age is not None and sla and age > sla:
            overdue.append({"key": e.get("key"), "owner": e.get("owner"),
                            "age_days": age, "sla_days": sla,
                            "severity": (e.get("severity") or "MEDIUM").upper()})
    overdue.sort(key=lambda r: r["age_days"] - r["sla_days"], reverse=True)
    kinds = OrderedDict()
    for e in rows:
        kinds[e.get("kind") or "?"] = kinds.get(e.get("kind") or "?", 0) + 1
    return {"open": len(rows), "by_owner": {k: len(v) for k, v in by_owner.items()},
            "kinds": OrderedDict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "overdue": overdue, "note": note, "age_hours": _age_hours(path),
            "source": "data/hr_action_queue.json"}


def agents_on_the_clock():
    """Reinstated agents under monitoring and the date a verdict is owed.

    This is the section that exists because seven agents were retired on paper
    and kept trading for eleven weeks with nothing watching. Imported live from
    hr_retirement_audit so the page cannot disagree with the audit.
    """
    try:
        import hr_retirement_audit as A
    except ImportError as e:                       # pragma: no cover - import guard
        return {"rows": [], "note": "hr_retirement_audit unavailable: %s" % e,
                "age_hours": None, "source": "data/hr_history.json"}
    hist, note = _load(os.path.join(DATA, "hr_history.json"), {})
    today = dt.date.today()
    rows = []
    for cn, (due, ev, deferrals) in A._monitored(hist, today).items():
        rows.append({"agent": cn, "due": due.isoformat(),
                     "days_left": (due - today).days,
                     "deferrals": deferrals,
                     "reinstated": ev.get("date", "?")})
    rows.sort(key=lambda r: r["days_left"])
    return {"rows": rows, "note": note,
            "age_hours": _age_hours(os.path.join(DATA, "hr_history.json")),
            "source": "data/hr_history.json"}


def hosts():
    """Is the code you last shipped the code that is running?

    Only the local half can be answered from here — reaching the VM needs ssh,
    which a page render must not depend on. So this reports what THIS host knows
    and says plainly that the other host is unverified from here, rather than
    implying both are fine. See the VM-freshness incident: a check that cannot
    run is worse than no check, because it reads as a passing one.
    """
    stamp, note = _load(os.path.join(HERE, "deploy_stamp.json"), {})
    beat_path = os.path.join(DATA, "pipeline_heartbeat.json")
    beat, beat_note = _load(beat_path, {})
    return {"stamp": stamp, "note": note,
            "heartbeat_age_hours": _age_hours(beat_path),
            "heartbeat_note": beat_note,
            "heartbeat": beat if isinstance(beat, dict) else {}}


def collect():
    """Everything the slate shows, as data. --report prints this."""
    return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "decisions": decisions_owed(),
            "firm": firm_is_handling(),
            "monitored": agents_on_the_clock(),
            "hosts": hosts()}


# ── rendering ─────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#faf9f7;--fg:#1c1a17;--dim:#6b665e;--line:#e2ded7;--card:#fff;
--crit:#8c1d18;--high:#a5450b;--med:#7a6420;--low:#4a5a4a;--ok:#2f5d3a}
@media(prefers-color-scheme:dark){:root{--bg:#161513;--fg:#eceae6;--dim:#9b958b;
--line:#2e2b27;--card:#1e1c1a;--crit:#f2837c;--high:#e8a463;--med:#d6c384;
--low:#a8bda8;--ok:#8fc79c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:2.5rem 1.25rem 4rem}
h1{font-size:1.6rem;margin:0 0 .2rem;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:.85rem;margin:0 0 2rem}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;
color:var(--dim);margin:2.4rem 0 .7rem;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:.85rem 1rem;margin-bottom:.5rem}
.card.crit{border-left:3px solid var(--crit)}.card.high{border-left:3px solid var(--high)}
.card.med{border-left:3px solid var(--med)}.card.low{border-left:3px solid var(--low)}
.top{display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap}
.sev{font-size:.66rem;font-weight:700;letter-spacing:.07em;padding:.1rem .4rem;
border-radius:3px;border:1px solid currentColor}
.sev.CRITICAL{color:var(--crit)}.sev.HIGH{color:var(--high)}
.sev.MEDIUM{color:var(--med)}.sev.LOW{color:var(--low)}
.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem}
.age{margin-left:auto;color:var(--dim);font-size:.76rem;
font-variant-numeric:tabular-nums}
.age.warn{color:var(--high);font-weight:600}
.desc{color:var(--dim);font-size:.83rem;margin-top:.3rem}
table{width:100%;border-collapse:collapse;font-size:.85rem}
.scroll{overflow-x:auto}
th{text-align:left;font-weight:600;color:var(--dim);font-size:.72rem;
text-transform:uppercase;letter-spacing:.06em;padding:.4rem .6rem;
border-bottom:1px solid var(--line)}
td{padding:.42rem .6rem;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.none{color:var(--ok);font-size:.87rem;padding:.3rem 0}
.warnbox{border:1px solid var(--high);color:var(--high);border-radius:6px;
padding:.6rem .8rem;font-size:.82rem;margin-bottom:.6rem}
.src{color:var(--dim);font-size:.72rem;margin-top:.45rem}
"""


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _sev_class(s):
    return {"CRITICAL": "crit", "HIGH": "high", "MEDIUM": "med"}.get(s, "low")


def _source_line(sec):
    """Every section states where its numbers came from and how old they are."""
    age = sec.get("age_hours")
    if age is None:
        when = "never written"
    elif age < 1:
        when = "%d min old" % int(age * 60)
    else:
        when = "%.1f h old" % age
    flag = " — STALE" if (age is not None and age > STALE_HOURS) else ""
    return '<div class="src">source: %s (%s%s)</div>' % (
        _e(sec.get("source", "?")), _e(when), flag)


def _note(sec):
    n = sec.get("note")
    return '<div class="warnbox">%s — this section is not empty, it is '\
           'UNREADABLE. Do not read it as "nothing to report".</div>' % _e(n) if n else ""


def render(d=None):
    d = d or collect()
    p = ['<div class="wrap">',
         "<h1>Re-entry slate</h1>",
         '<p class="sub">Apex Capital &middot; generated %s &middot; every number '
         'read from live state at render time</p>' % _e(d["generated_at"])]

    # 1. Yours to answer.
    dec = d["decisions"]
    p.append("<h2>Waiting on you (%d)</h2>" % len(dec["rows"]))
    p.append(_note(dec))
    if not dec["rows"] and not dec["note"]:
        p.append('<p class="none">Nothing is blocked on a decision from you.</p>')
    for r in dec["rows"][:12]:
        soon = r["escalates_in"] is not None and r["escalates_in"] <= 14
        p.append(
            '<div class="card %s"><div class="top">'
            '<span class="sev %s">%s</span><span class="id">%s</span>'
            '<span class="age%s">%s</span></div><div class="desc">%s</div></div>'
            % (_sev_class(r["severity"]), _e(r["severity"]), _e(r["severity"]),
               _e(r["id"]), " warn" if soon else "",
               _e("escalates to the board in %sd" % r["escalates_in"]
                  if r["escalates_in"] is not None else "age unknown"),
               _e(r["description"][:230])))
    if len(dec["rows"]) > 12:
        p.append('<p class="sub">…and %d more. The queue is long because routine '
                 'work was filed here; the weekly sweep now routes most findings '
                 'to their owner instead.</p>' % (len(dec["rows"]) - 12))
    p.append(_source_line(dec))

    # 2. Not yours — the firm's own work.
    f = d["firm"]
    p.append("<h2>The firm is handling (%d open)</h2>" % f["open"])
    p.append(_note(f))
    if f["by_owner"]:
        p.append('<div class="scroll"><table><tr><th>Owner</th><th>Open</th></tr>')
        for owner, n in sorted(f["by_owner"].items(), key=lambda kv: -kv[1]):
            p.append("<tr><td>%s</td><td>%d</td></tr>" % (_e(owner), n))
        p.append("</table></div>")
    if f["overdue"]:
        p.append("<h2>Past its SLA (%d)</h2>" % len(f["overdue"]))
        p.append('<div class="scroll"><table>'
                 "<tr><th>Finding</th><th>Owner</th><th>Age</th><th>SLA</th></tr>")
        for r in f["overdue"][:10]:
            p.append("<tr><td>%s</td><td>%s</td><td>%dd</td><td>%dd</td></tr>"
                     % (_e(r["key"]), _e(r["owner"]), r["age_days"], r["sla_days"]))
        p.append("</table></div>")
    elif not f["note"]:
        p.append('<p class="none">Nothing is past its SLA.</p>')
    p.append(_source_line(f))

    # 3. The agents this whole apparatus exists for.
    m = d["monitored"]
    p.append("<h2>Agents under monitoring (%d)</h2>" % len(m["rows"]))
    p.append(_note(m))
    if m["rows"]:
        p.append('<div class="scroll"><table><tr><th>Agent</th><th>Reinstated</th>'
                 "<th>Verdict due</th><th>Days</th><th>Deferrals</th></tr>")
        for r in m["rows"]:
            p.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>"
                     % (_e(r["agent"]), _e(r["reinstated"]), _e(r["due"]),
                        r["days_left"],
                        "%d of 2" % r["deferrals"] if r["deferrals"] else "—"))
        p.append("</table></div>")
    elif not m["note"]:
        p.append('<p class="none">No agent is currently under monitoring.</p>')
    p.append(_source_line(m))

    # 4. Hosts.
    h = d["hosts"]
    p.append("<h2>This host</h2>")
    stamp = h.get("stamp") or {}
    beat = h.get("heartbeat_age_hours")
    p.append('<div class="card"><div class="desc">deployed sha <b>%s</b> at %s'
             "<br>pipeline heartbeat: %s"
             "<br><i>The second host (GCP VM) cannot be checked from a page "
             "render — verify it with <code>ssh apex-capital 'cat "
             "~/institutional-tracker/deploy_stamp.json'</code>. A check that "
             "cannot run must not be shown as one that passed.</i>"
             "</div></div>"
             % (_e(stamp.get("sha", "unknown")), _e(stamp.get("deployed_at", "?")),
                _e("%.1f h old" % beat if beat is not None else
                   (h.get("heartbeat_note") or "never written"))))
    p.append("</div>")
    return "<style>%s</style>\n%s" % (CSS, "\n".join(p))


def render_page(d=None):
    """Standalone document, for writing to disk or serving directly."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Apex Capital — Re-entry slate</title></head><body>%s</body></html>"
            % render(d))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print HTML, do not write")
    ap.add_argument("--report", action="store_true", help="print the numbers only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")

    d = collect()
    if args.report:
        def _flat(v):
            if not isinstance(v, dict):
                return v
            out = {kk: vv for kk, vv in v.items() if kk != "rows"}
            if "rows" in v:
                out["rows"] = len(v["rows"])
            return out
        print(json.dumps({k: _flat(v) for k, v in d.items()}, indent=2, default=str))
        return 0
    page = render_page(d)
    if args.stdout:
        print(page)
        return 0
    with open(OUT_PATH, "w") as f:
        f.write(page)
    logger.info("Wrote %s — %d decision(s) owed, %d open finding(s), %d agent(s) "
                "under monitoring.", OUT_PATH, len(d["decisions"]["rows"]),
                d["firm"]["open"], len(d["monitored"]["rows"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
