#!/usr/bin/env python3
"""
Agent Roster  (Department: HR / Governance)
===========================================
WHY THIS EXISTS
---------------
"How many agents does this firm employ?" had three different answers on
2026-08-01, and every one of them was wrong.

  agent_registry.json (Mac)   37     agent_registry.json (VM)   66
  union by class_name         67     agents actually running    ~100

Three separate defects produced that spread:

  1. THE REGISTRY IS FORKED. deploy.sh rsyncs `data/` through a two-file
     allowlist, so the moment the VM's Chief-of-Staff factory spawned an agent,
     that agent existed on exactly one host forever. 30 agents — the whole FE
     and FI pods — lived only on the VM.

  2. "inactive" DOES NOT MEAN RETIRED. Sixteen VM rows read `status: inactive`
     with `deactivation_reason: "Non-research agent — should not run in research
     cycle"`. That flag exists for research_team.py, which loads only
     active/reassigned. But the rows carrying it are the CRO, the Canary, the
     Manager, the five CIT agents — tier-0 infrastructure, `permanent: true`,
     all of them running on cron right now. Anything that reads `status` as
     employment concludes the Risk Manager quit.

  3. MOST AGENTS ARE NOT IN THE REGISTRY AT ALL. The registry only holds what
     the Chief-of-Staff factory spawned. Thirty-five agent modules run on cron
     or inside main.py — logic_team, the boards, the sentinels, HR's own
     retirement audit — and no registry has ever contained them. HR audits the
     registry, so HR has never been able to see its own team.

WHAT A ROSTER ROW IS
--------------------
Employment and research-cycle participation are split, because conflating them
is defect #2:

  employment       employed | superseded | retired   ← is this agent still ours
  research_cycle   bool                              ← does research_team load it
  runs_via         cron / main / called_by:<mod> / UNSCHEDULED

`superseded` is for a row whose file was renamed out from under it —
`RiskManager -> risk_manager.py` still sits in the registry pointing at a path
deleted months ago, while the agent itself runs from risk_manager_agent.py. The
agent was never lost; the pointer was. A "cleanup" that deleted rows with
missing files would have fired the CRO, the IRM, and the Chief of Staff.

THE RATCHET
-----------
data/agent_roster_manifest.json is the DECLARED roster and is tracked in git.
`--check` compares discovery against it: growth is absorbed silently, but an
agent that disappears is a RED finding unless the manifest records a
`retired_at` and a `retirement_reason`. Losing an agent must take a decision,
never a rename or a bad merge.

USAGE
    python3 agent_roster.py                # build data/agent_roster.json (this host)
    python3 agent_roster.py --report       # print, write nothing
    python3 agent_roster.py --check        # ratchet against the manifest (exit 1 on RED)
    python3 agent_roster.py --merge-remote # fold the VM's roster + registry in
    python3 agent_roster.py --adopt        # accept the current roster as the manifest
"""
from __future__ import annotations

import argparse
import ast
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

REGISTRY_PATH = DATA / "agent_registry.json"
ROSTER_PATH = DATA / "agent_roster.json"
MANIFEST_PATH = DATA / "agent_roster_manifest.json"

VM_HOST = "apex-capital"
VM_BASE = "~/institutional-tracker"

# A module is agent-shaped if its name says so, or if it is one of the named
# core modules that predate the naming convention.
#
# _tracker/_runner/_analyst/_sizer were added 2026-08-01 after HR's attendance
# audit — which scans for those suffixes — found seven agents the roster did not
# hold, including international_analyst (the module that tracked the KOSPI,
# crashed on import for a month, and started this entire line of work) and the
# IRM's four-analyst board. A roster that omits agents because of a suffix it
# forgot to list is the same failure as the registry it replaces, one level up.
MODULE_HINT = re.compile(
    r"(_agent|_sentinel|_monitor|_officer|_board|_team|_pod|_director|"
    r"_watchdog|_auditor|_checker|_guard|_council|_sla|_committee|"
    r"_tracker|_runner|_analyst|_sizer)\.py$"
    r"|^(canary|hr|logic|board_meeting|deputy_board|research_team|manager|"
    r"strategy_council|chairman_)")

# Modules that match the name pattern but are libraries, not agents: they hold
# no schedule and make no decisions. Listed explicitly so the exclusion is a
# recorded judgement rather than a silent regex accident.
NOT_AGENTS = {
    "mail_guard.py",            # smtp wrapper used by everyone
    "manager_knowledge_base.py",  # lookup tables for manager_agent
    "chairman_inbox.py",        # read-only printer for the SLA queue
    "chairman_queue_sync.py",   # reconciler, not a decision-maker
    "agent_roster.py",          # this file
    "wake_agent.py",            # shell shim, no logic
}

# Superseded pointers: a registry row's `file` was renamed. Recorded here, not
# repaired by guesswork, so the succession is auditable.
SUPERSEDED = {
    "risk_manager.py": "risk_manager_agent.py",
    "irm_agent.py": "irm_review_agent.py",
    "chief_of_staff.py": "chief_of_staff_agent.py",
}


def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _registry_rows(reg) -> list:
    if isinstance(reg, dict):
        return reg.get("agents", [])
    return reg if isinstance(reg, list) else []


def _py_files(base: Path) -> list:
    out = []
    for p in base.rglob("*.py"):
        s = str(p.relative_to(base))
        if s.startswith((".git/", "venv/", "__pycache__/")) or "/__pycache__/" in s:
            continue
        out.append(s)
    return sorted(out)


def _crontab() -> str:
    """This host's crontab UNIONED with the VM's deployed snapshot.

    The local crontab alone is not the firm's schedule. The Mac has a handful of
    jobs; the VM has 75, and it is the VM that runs the pipeline. Reading only
    `crontab -l` made a Mac-built roster report 74 agents as running nowhere
    against the merged view's 45 — the same agent "employed but unscheduled" or
    not depending on which host last wrote the file. That is the host-divergence
    class this firm keeps paying for, and here it would have fed HR's audit and
    the shrink ratchet two different firms.

    data/vm_crontab_snapshot.txt is refreshed daily on the VM, travels in
    deploy.sh's allowlist, and is already the ground truth wiring_audit.py and
    hr_agent.py measure against. One schedule, both hosts.
    """
    parts = [local_crontab()]
    try:
        parts.append((DATA / "vm_crontab_snapshot.txt").read_text())
    except Exception:
        pass
    return "\n".join(parts)


def local_crontab() -> str:
    """This host's OWN crontab, with no VM snapshot mixed in.

    _crontab() above deliberately unions both hosts so the roster and the shrink
    ratchet see one firm — "is this agent employed anywhere?" is a firm-wide
    question. Attendance is the opposite kind of question. An agent's output
    freshness is evidence about the AGENT only on the host that actually runs
    it; on any other box a perfectly healthy agent's files sit untouched
    forever. The Mac has no crontab at all, so measuring VM-scheduled agents
    against Mac mtimes nominated six working agents for a PIP.
    """
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""


def schedule_pattern(stem: str):
    """The one matcher for "does this schedule text invoke this module".

    Word-boundary, because the substring test calls `risk_manager` scheduled on
    the strength of `risk_manager_agent.py`. Exported so callers import it
    rather than re-typing the regex — a second copy of this exact pattern in
    the sweep's isolation graph is what produced six false reports.
    """
    return re.compile(rf"\b{re.escape(stem)}\.py\b")


def runs_here(agents: list, crontab_text: str = None) -> set:
    """Stems whose invocation chain is rooted in THIS host's crontab.

    Being on the firm's schedule is not the same as running on this box. An
    agent qualifies if its own module is in the local crontab, or if whatever
    invokes it is — main.py's agents, research_team's registry agents and
    called_by: chains all inherit the host of the entry that starts them.
    Returns stems, matching the sweep's identity for an agent.
    """
    txt = local_crontab() if crontab_text is None else crontab_text
    direct = set()
    for a in agents:
        stem = os.path.basename(a.get("file") or "")[:-3]
        if stem and schedule_pattern(stem).search(txt):
            direct.add(stem)
    if not direct:
        # Nothing on this host's crontab: nothing here is attendance-assessable.
        # Returning early keeps the empty case explicit rather than letting the
        # closure below quietly conclude the same thing.
        return set()

    here = set(direct)
    main_here = bool(schedule_pattern("main").search(txt))
    for a in agents:
        stem = os.path.basename(a.get("file") or "")[:-3]
        if not stem or stem in here:
            continue
        for via in a.get("runs_via") or []:
            if via in ("main", "research_team") and main_here:
                here.add(stem)
            elif via.startswith("called_by:") and via.split(":", 1)[1] in direct:
                here.add(stem)
    return here


def _imports_of(base: Path, rel: str) -> set:
    """Module names this file imports, first level only."""
    try:
        tree = ast.parse((base / rel).read_text())
    except Exception:
        return set()
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods.add(n.module.split(".")[0])
    return mods


# ──────────────────────────────────────────────────────────────────────────
def build(base: Path = BASE, host: str = "local") -> dict:
    """Discover every agent this host employs, from every source available."""
    files = _py_files(base)
    cron = _crontab()
    main_src = ""
    try:
        main_src = (base / "main.py").read_text()
    except Exception:
        pass

    stems = {os.path.basename(f)[:-3]: f for f in files}

    def scheduled_in(stem):
        pat = schedule_pattern(stem)
        via = []
        if pat.search(cron):
            via.append("cron")
        if pat.search(main_src) or re.search(rf"\b{re.escape(stem)}\b", main_src):
            via.append("main")
        return via

    # Everything a scheduled module imports is itself running, one level deep.
    directly_scheduled = {s for s in stems if scheduled_in(s)}
    called_by = {}
    for s in sorted(directly_scheduled):
        for imported in _imports_of(base, stems[s]):
            if imported in stems and imported not in directly_scheduled:
                called_by.setdefault(imported, set()).add(s)

    rows = {}

    # ── source 1: the Chief-of-Staff registry ────────────────────────────
    for a in _registry_rows(_load(base / "data" / "agent_registry.json", [])):
        name = a.get("class_name")
        if not name:
            continue
        f = a.get("file") or ""
        exists = bool(f) and (base / f).exists()
        employment = "employed"
        superseded_by = None
        if a.get("retired_at") or a.get("status") == "retired":
            employment = "retired"
        elif not exists:
            superseded_by = SUPERSEDED.get(os.path.basename(f))
            employment = "superseded" if superseded_by else "employed"
        rows[name] = {
            "name": name,
            "kind": "registry",
            "file": f,
            "file_exists": exists,
            "tier": a.get("tier"),
            "team": a.get("team") or a.get("division"),
            "lane": a.get("lane"),
            "specialization": a.get("specialization"),
            "employment": employment,
            "superseded_by": superseded_by,
            # research_cycle is what `status` actually controls (research_team.py
            # loads active + reassigned). It is NOT employment.
            "research_cycle": a.get("status") in ("active", "reassigned"),
            "registry_status": a.get("status"),
            "permanent": bool(a.get("permanent")),
            "retirement_reason": a.get("retirement_reason") or a.get("deactivation_reason"),
            "runs_via": [],
            "hosts": [host],
        }

    known_files = {r["file"] for r in rows.values() if r["file"]}
    known_files.update(SUPERSEDED.values())

    # ── source 2: agent modules no registry has ever held ────────────────
    for f in files:
        b = os.path.basename(f)
        if b.startswith("test_") or b in NOT_AGENTS or not MODULE_HINT.search(b):
            continue
        if f in known_files:
            continue
        stem = b[:-3]
        via = scheduled_in(stem)
        if not via and stem in called_by:
            via = [f"called_by:{s}" for s in sorted(called_by[stem])]
        rows[stem] = {
            "name": stem,
            "kind": "module",
            "file": f,
            "file_exists": True,
            "tier": 0,
            "team": None, "lane": None, "specialization": None,
            "employment": "employed",
            "superseded_by": None,
            "research_cycle": False,
            "registry_status": None,
            "permanent": False,
            "retirement_reason": None,
            "runs_via": via or ["UNSCHEDULED"],
            "hosts": [host],
        }

    # ── fill runs_via for registry rows (their file may be renamed) ───────
    for r in rows.values():
        if r["kind"] != "registry":
            continue
        f = r["superseded_by"] or r["file"]
        stem = os.path.basename(f)[:-3] if f.endswith(".py") else ""
        via = scheduled_in(stem) if stem else []
        if not via and stem in called_by:
            via = [f"called_by:{s}" for s in sorted(called_by[stem])]
        # A research agent with no module schedule still runs: research_team.py
        # instantiates it from the registry every cycle.
        if not via and r["research_cycle"]:
            via = ["research_team"]
        r["runs_via"] = via or ["UNSCHEDULED"]

    return {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "host": host,
        "counts": _counts(rows.values()),
        "agents": [rows[k] for k in sorted(rows)],
    }


def _counts(agents) -> dict:
    agents = list(agents)
    return {
        "total": len(agents),
        "registry": sum(1 for a in agents if a["kind"] == "registry"),
        "module": sum(1 for a in agents if a["kind"] == "module"),
        "employed": sum(1 for a in agents if a["employment"] == "employed"),
        "superseded": sum(1 for a in agents if a["employment"] == "superseded"),
        "retired": sum(1 for a in agents if a["employment"] == "retired"),
        "research_cycle": sum(1 for a in agents if a["research_cycle"]),
        "unscheduled": sum(1 for a in agents if a["runs_via"] == ["UNSCHEDULED"]),
        "zombie": sum(1 for a in agents if a.get("zombie")),
    }


# ──────────────────────────────────────────────────────────────────────────
def _canonical_names(*rosters) -> dict:
    """file path -> the registry class_name that owns it, across every input.

    One agent must not appear twice because the hosts disagree about what to
    call it. The Mac's registry has 37 rows and the VM's has 66, so
    `agents/cos_fe_hardmoney_agent.py` is the registry row `FEHardMoneyAgent`
    on the VM and an anonymous module row `cos_fe_hardmoney_agent` on the Mac.
    Same file, same agent — the class_name wins, and 24 phantom FE/FI agents
    stop being double-counted.
    """
    m = {}
    for r in rosters:
        for a in r.get("agents", []):
            if a["kind"] != "registry":
                continue
            for f in (a.get("file"), a.get("superseded_by")):
                if f:
                    m.setdefault(f, a["name"])
    return m


def merge_rosters(a: dict, b: dict) -> dict:
    """Union two hosts' rosters. An agent employed on either host is employed.

    Merging may only ADD. There is no rule here under which an agent present in
    one input is absent from the output — losing an agent to a merge is the
    accident this whole module exists to make impossible.
    """
    # NOT a ladder. "employed" is the default state — the absence of news — so
    # it must never outrank a recorded decision. Only one host holds HR's
    # retirement records; if the merge let the other host's silence win, every
    # sync would quietly re-hire everyone HR has ever retired.
    EMP_RANK = {"employed": 0, "superseded": 1, "retired": 2}
    canon = _canonical_names(a, b)
    out = {}
    for src in (a, b):
        for r in src.get("agents", []):
            # Only a nameless MODULE row gets folded onto a registry row by
            # file. Two registry rows may legitimately share a file — the five
            # CIT agents all live in cit_director.py — and collapsing those by
            # path would delete four agents in the name of de-duplication.
            name = (canon.get(r.get("file"), r["name"])
                    if r["kind"] == "module" else r["name"])
            r = {**r, "name": name}
            cur = out.get(name)
            if cur is None:
                out[name] = dict(r)
                continue
            if EMP_RANK.get(r["employment"], 0) > EMP_RANK.get(cur["employment"], 0):
                cur["employment"] = r["employment"]
            cur["research_cycle"] = cur["research_cycle"] or r["research_cycle"]
            cur["permanent"] = cur["permanent"] or r["permanent"]
            cur["file_exists"] = cur["file_exists"] or r["file_exists"]
            for k in ("file", "tier", "team", "lane", "specialization",
                      "superseded_by", "registry_status", "retirement_reason"):
                if not cur.get(k) and r.get(k):
                    cur[k] = r[k]
            if cur["kind"] == "module" and r["kind"] == "registry":
                cur["kind"] = "registry"
            live = [v for v in cur["runs_via"] + r["runs_via"] if v != "UNSCHEDULED"]
            cur["runs_via"] = sorted(set(live)) or ["UNSCHEDULED"]
            cur["hosts"] = sorted(set(cur["hosts"]) | set(r["hosts"]))

    # A retirement one host never heard about leaves the agent still on cron
    # somewhere. That is the reverse of a lost agent and just as invisible.
    for r in out.values():
        r["zombie"] = (r["employment"] == "retired"
                       and r["runs_via"] != ["UNSCHEDULED"])
    return {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "host": "merged",
        "counts": _counts(out.values()),
        "agents": [out[k] for k in sorted(out)],
    }


def merge_registries(mac: dict, vm: dict) -> dict:
    """Reconcile the two hosts' agent_registry.json into one.

    The VM's row wins on OPERATIONAL fields, because the VM is production: its
    `status` is what research_team.py actually loads there, and its
    "inactive / Non-research agent" flags on the CRO, Canary, Manager and the
    five CIT agents are deliberate and correct — those are tier-0 infrastructure
    that must not be instantiated as research agents. Taking the Mac's `active`
    for them would push eight infrastructure classes into the research cycle.

    The Mac's row wins on HR HISTORY, because only the Mac has any: retirement
    records, performance notices, the voided 2026-05-06 reassignments. The VM
    schema has none of those fields, so "VM wins" alone would erase the entire
    disciplinary record of the firm.

    Mac-only agents are added, never dropped. MissedWinnersAgent exists on
    exactly one host and a naive `deploy.sh` allowlist entry would have deleted
    the other 30.
    """
    HR_FIELDS = ("division", "retired_at", "retirement_reason",
                 "performance_notice", "performance_notice_sent",
                 "voided_reassignment", "reinstated_at", "reinstatement_reason",
                 "whole_picture_exonerated")

    mac_rows = {a.get("class_name"): a for a in _registry_rows(mac) if a.get("class_name")}
    vm_rows = {a.get("class_name"): a for a in _registry_rows(vm) if a.get("class_name")}

    out = []
    for name in sorted(set(mac_rows) | set(vm_rows)):
        m, v = mac_rows.get(name), vm_rows.get(name)
        row = dict(v or m)
        if m and v:
            for f in HR_FIELDS:
                if m.get(f) is not None and row.get(f) is None:
                    row[f] = m[f]
        f = row.get("file") or ""
        successor = SUPERSEDED.get(os.path.basename(f))
        if successor:
            # Record the rename instead of leaving a dangling path. A tidy-up
            # that deleted rows whose file was missing would have fired the CRO,
            # the IRM and the Chief of Staff in one commit.
            row["superseded_by"] = successor
            row["file_note"] = (f"{f} was renamed to {successor}; the agent runs "
                                f"from the new module. Row kept for continuity.")
        out.append(row)

    return {
        "agents": out,
        "last_updated": datetime.datetime.utcnow().isoformat(),
        "total_spawned": max(int((mac or {}).get("total_spawned") or 0),
                             int((vm or {}).get("total_spawned") or 0),
                             len(out)),
        "merged_by": "agent_roster.py --sync-registry",
        "merged_at": datetime.datetime.utcnow().isoformat(),
    }


def sync_registry(write: bool = True) -> dict:
    """One canonical registry on both hosts."""
    mac = _load(REGISTRY_PATH, {"agents": []})
    r = subprocess.run(["ssh", VM_HOST, f"cat {VM_BASE}/data/agent_registry.json"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read the VM registry: {r.stderr.strip()[:200]}")
    vm = json.loads(r.stdout)

    merged = merge_registries(mac, vm)
    names_in = {a.get("class_name") for a in _registry_rows(mac)} | \
               {a.get("class_name") for a in _registry_rows(vm)}
    names_out = {a.get("class_name") for a in merged["agents"]}
    lost = sorted(n for n in names_in - names_out if n)
    if lost:
        raise RuntimeError(f"registry merge would lose {len(lost)} agent(s): {lost}")

    rep = {"mac": len(_registry_rows(mac)), "vm": len(_registry_rows(vm)),
           "merged": len(merged["agents"]), "lost": lost, "wrote": False}
    if write:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        arch = DATA / "agent_roster_archive" / stamp
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "agent_registry.mac.json").write_text(json.dumps(mac, indent=2))
        (arch / "agent_registry.vm.json").write_text(json.dumps(vm, indent=2))
        payload = json.dumps(merged, indent=2)
        tmp = REGISTRY_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload)
        tmp.replace(REGISTRY_PATH)
        w = subprocess.run(
            ["ssh", VM_HOST,
             f"cat > {VM_BASE}/data/agent_registry.json.tmp && "
             f"mv {VM_BASE}/data/agent_registry.json.tmp {VM_BASE}/data/agent_registry.json"],
            input=payload, capture_output=True, text=True, timeout=120)
        if w.returncode != 0:
            raise RuntimeError(f"cannot write the VM registry: {w.stderr.strip()[:200]}")
        rep["wrote"] = True
        rep["archive"] = str(arch)
    return rep


def fetch_remote() -> dict:
    """Build the roster on the VM and bring it back."""
    cmd = (f"cd {VM_BASE} && ~/venv/bin/python3 agent_roster.py --report --json --host vm")
    r = subprocess.run(["ssh", VM_HOST, cmd], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"remote roster build failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


# ──────────────────────────────────────────────────────────────────────────
def check(roster: dict, manifest: dict) -> dict:
    """Ratchet: the roster may grow freely; it may only shrink with a reason."""
    have = {a["name"]: a for a in roster.get("agents", [])}
    declared = {a["name"]: a for a in manifest.get("agents", [])}

    vanished = []
    for name, d in declared.items():
        if name in have:
            continue
        if d.get("retired_at") and d.get("retirement_reason"):
            continue          # a recorded decision, not a loss
        vanished.append({"name": name, "file": d.get("file"),
                         "last_seen": d.get("last_seen"),
                         "hosts": d.get("hosts")})

    added = sorted(set(have) - set(declared))
    unscheduled = sorted(n for n, a in have.items()
                         if a["runs_via"] == ["UNSCHEDULED"] and a["employment"] == "employed")
    return {
        "declared": len(declared),
        "discovered": len(have),
        "vanished": vanished,
        "added": added,
        "unscheduled": unscheduled,
        "status": "RED" if vanished else ("GREEN" if not added else "GREEN_GREW"),
    }


CHAIR_ACTION_PATH = DATA / "chairman_action_required.json"


def escalate(rep: dict) -> bool:
    """A RED ratchet has to reach a person, not an exit code.

    This runs from cron. `sys.exit(1)` into a log file is precisely the shape of
    failure this module was written to end — the orphaned KOSPI agent, the
    272-day buy freeze, the forked queue: all of them were *detected* somewhere
    nobody was reading. So a vanished agent is filed as an awaiting_chairman
    action, which chairman_decision_sla ingests into the app inbox and escalates
    to the board on its own if it goes 20 days undecided.

    The dedup_key is stable across runs (it names the agents, not the date), so
    a daily cron re-stating the same loss folds into one decision rather than
    filing thirty.
    """
    names = sorted(v["name"] for v in rep.get("vanished", []))
    if not names:
        return False
    key = "agent_roster:vanished:" + ",".join(names)
    try:
        actions = _load(CHAIR_ACTION_PATH, [])
        if not isinstance(actions, list):
            return False
        if any(isinstance(a, dict) and a.get("dedup_key") == key for a in actions):
            return False
        actions.append({
            "id": f"roster-vanished-{datetime.date.today().isoformat()}",
            "severity": "CRITICAL",
            "category": "GOVERNANCE",
            "title": f"{len(names)} agent(s) vanished from the roster",
            "description": (
                f"agent_roster --check found {len(names)} agent(s) declared in "
                f"agent_roster_manifest.json but discovered on NO host, with no "
                f"retired_at/retirement_reason recorded: {', '.join(names)}.\n\n"
                "An agent leaving the firm must be a decision, never a rename, a "
                "bad merge, or a deploy that overwrote a host's registry. Either "
                "recover it, or record the retirement and its reason so the "
                "manifest reflects a choice that was actually made."),
            "source": "agent_roster",
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "status": "awaiting_chairman",
            "dedup_key": key,
        })
        CHAIR_ACTION_PATH.write_text(json.dumps(actions, indent=2))
        return True
    except Exception:
        return False


def adopt(roster: dict) -> dict:
    """Freeze the current roster as the declared manifest."""
    prev = {a["name"]: a for a in _load(MANIFEST_PATH, {}).get("agents", [])}
    today = datetime.date.today().isoformat()
    agents = []
    for a in roster["agents"]:
        p = prev.get(a["name"], {})
        agents.append({
            "name": a["name"], "kind": a["kind"], "file": a["file"],
            "tier": a["tier"], "team": a["team"],
            "employment": a["employment"],
            "hosts": a["hosts"],
            "first_declared": p.get("first_declared", today),
            "last_seen": today,
            "retired_at": p.get("retired_at"),
            "retirement_reason": p.get("retirement_reason"),
        })
    # Anything already declared but not discovered stays in the manifest so the
    # ratchet keeps complaining about it. Dropping it here would let a vanished
    # agent be laundered out of existence by one --adopt.
    for name, p in prev.items():
        if name not in {a["name"] for a in agents}:
            agents.append(p)
    return {"generated_at": datetime.datetime.utcnow().isoformat(),
            "count": len(agents),
            "agents": sorted(agents, key=lambda a: a["name"])}


# ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Build and ratchet the firm's canonical agent roster.")
    ap.add_argument("--report", action="store_true", help="print only, write nothing")
    ap.add_argument("--json", action="store_true", help="emit raw JSON (for --merge-remote over ssh)")
    ap.add_argument("--host", default="local", help="label this host in the roster")
    ap.add_argument("--merge-remote", action="store_true", help="fold the VM's roster in")
    ap.add_argument("--check", action="store_true", help="ratchet against the manifest; exit 1 on RED")
    ap.add_argument("--adopt", action="store_true", help="accept the current roster as declared")
    ap.add_argument("--sync-registry", action="store_true",
                    help="reconcile agent_registry.json across both hosts first")
    args = ap.parse_args()

    if args.sync_registry:
        rep = sync_registry(write=not args.report)
        print(f"  registry: mac {rep['mac']} + vm {rep['vm']} -> {rep['merged']} "
              f"(lost {len(rep['lost'])}){'  [written to both hosts]' if rep['wrote'] else '  [dry run]'}")

    roster = build(host=args.host)
    if args.merge_remote:
        roster = merge_rosters(roster, fetch_remote())

    if args.json:
        print(json.dumps(roster))
        return 0

    if not args.report:
        ROSTER_PATH.write_text(json.dumps(roster, indent=2))

    c = roster["counts"]
    print("=" * 68)
    print(f"  APEX CAPITAL — AGENT ROSTER  ({roster['host']})")
    print("=" * 68)
    print(f"  {c['total']} agents on the books "
          f"({c['registry']} in the CoS registry, {c['module']} module agents)")
    print(f"    employed {c['employed']} · superseded {c['superseded']} · retired {c['retired']}")
    print(f"    {c['research_cycle']} loaded into the research cycle · "
          f"{c['unscheduled']} employed but running nowhere")

    if args.adopt:
        MANIFEST_PATH.write_text(json.dumps(adopt(roster), indent=2))
        print(f"\n  adopted: {MANIFEST_PATH.name} now declares {c['total']} agents.")

    if args.check:
        rep = check(roster, _load(MANIFEST_PATH, {"agents": []}))
        print(f"\n  RATCHET: {rep['status']}  "
              f"(declared {rep['declared']} · discovered {rep['discovered']})")
        if rep["added"]:
            print(f"    +{len(rep['added'])} new since the manifest: "
                  f"{', '.join(rep['added'][:8])}{'…' if len(rep['added']) > 8 else ''}")
        if rep["vanished"]:
            print(f"\n  ⛔ {len(rep['vanished'])} DECLARED AGENT(S) HAVE VANISHED "
                  f"with no recorded retirement:")
            for v in rep["vanished"]:
                print(f"       • {v['name']}  ({v['file']}, last seen {v['last_seen']})")
            if not args.report:
                filed = escalate(rep)
                print(f"    {'filed for the Chairman' if filed else 'already filed'} "
                      f"— chairman_action_required.json")
            print()
            return 1
        if rep["unscheduled"]:
            print(f"    {len(rep['unscheduled'])} employed agent(s) run nowhere: "
                  f"{', '.join(rep['unscheduled'][:6])}"
                  f"{'…' if len(rep['unscheduled']) > 6 else ''}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
