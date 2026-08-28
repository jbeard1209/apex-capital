#!/usr/bin/env python3
"""
APEX CAPITAL — AGENT LIFECYCLE (what a status MEANS, in one place)
=================================================================
The Chief of Staff runs a four-stage "dignified performance" ladder. Its own
docstring states the intent plainly:

    Stage 1 — Performance Notice   status → "noticed"      "Agent remains fully active."
    Stage 2 — PIP                  status → "pip"          "intensive support phase, NOT
                                                            a prelude to termination"
    Stage 3 — Reassignment         status → "reassigned"   "Original slot coverage is
                                                            maintained."
    Stage 4 — Replacement          status → "retired"      terminal.

    "Recovery at any stage: one approved pick resets the counter to zero and
     restores the agent to active status."

`research_team.py` loaded `status in ("active", "reassigned")`. `noticed` and
`pip` were not in that tuple. So the two stages the ladder describes as coaching
were, in fact, removal from service — and because recovery requires *an approved
pick*, an agent that cannot run can never earn the thing that would save it. The
counter kept climbing: notice (8) → PIP (14) → reassignment (20) → retirement
(30), with no branch anywhere able to interrupt it. The sentence removed the
means of appeal.

WHAT IT COST (measured 2026-08-28, the GCP VM, the always-on trading host)
-------------------------------------------------------------------------
  05:00:49  Loading 50 CoS-spawned registry agent(s)   ← every run for weeks
  08:00:48  Loading  8 CoS-spawned registry agent(s)   ← after one CoS pass

43 agents crossed czc=8 in a single pass and went to `noticed` — 64% of the
roster, on the same day, at the same count. Research coverage on the host that
does most of the trading fell 84% in one cycle, taking Technology, Financials,
Industrials, all ten First-Eagle value agents, Fixed Income, Options-Flow and
Quant-Factor with it. Nothing alarmed. The Mac was two cycles behind the same
cliff: 45 of its agents sat at czc=6 against a threshold of 8.

Simultaneity is the tell. 43 independent performance judgements do not land on
one day at one identical count. That is one systemic condition being counted 43
times — and the firm's own code knew it: every one of the 43 carried
`exoneration_count = 7`, meaning CoS had already ruled seven times that the
missing picks were not their fault (buys blocked system-wide), then charged them
anyway on the other eight.

THE DEEPER BUG, WHICH WIDENING THE TUPLE DOES NOT FIX
-----------------------------------------------------
`consecutive_zero_cycles` is a proxy. The real quantity is "this agent had a
fair opportunity and wasted it." The proxy is "no approved pick appeared." Those
differ whenever the agent never got to run, and there were three such paths:

  1. status ∈ {noticed, pip} → not loaded          (this bug; self-reinforcing)
  2. the decision-#79 skip    → constructed, then skipped for missing context.
     research_team.py's own comment says these agents "abstained 6 times a day,
     into HR's zero-pick streak counter, where CIT then 'exonerated' all 7 by
     blaming market conditions for what was a structural impossibility." The
     fix documented the contamination and left it running. All 8 FI agents were
     in the 43.
  3. buys blocked system-wide → the `cro_blocking_all` exoneration branch, the
     one contaminant that was actually patched.

Widening the status tuple closes (1) only. So the counter now requires
POSITIVE EVIDENCE that the agent ran: `record_cycle()` is written by whoever
actually constructs and calls the agents, and CoS charges a zero-pick cycle only
against an agent the manifest says ran. No evidence is not evidence of failure —
a missing manifest means "cannot tell", and a cycle nobody can vouch for is not
chargeable (memory `fallback-that-means-act`: the value a degraded read
substitutes is a decision, and "charge it" is the wrong default here).

The belt, so a broken manifest cannot freeze the ladder silently: every refusal
to charge is counted on the agent (`unrun_cycles`) and returned by
`refusals_this_cycle()` for the daily brief, and `law_lifecycle_statuses_are_runnable`
in coherence_laws.py compares the statuses the ladder can ASSIGN against the
statuses a loader will RUN — two independently-sourced sets, so the next status
someone invents cannot silently become a removal.

NO STATE SYNC: this manifest is host-local by design, like the registry it
grades. The VM's CoS must judge the VM's runs; a Mac that has not run main.py in
a week has no business charging the VM's agents (memory `two_brains_one_account`).
"""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MANIFEST = DATA_DIR / "agent_run_manifest.json"

# Every status the performance ladder can put an agent into, and what it means
# for whether that agent still works here. Terminal is terminal; everything else
# is somewhere on the coaching ladder, and the ladder's whole design — coaching,
# support, recovery-by-performing — is incoherent if the agent cannot perform.
#
# These are frozensets and not tuples so that a caller cannot append to the
# firm's definition of "employed" by accident.
RUNNABLE_STATUSES = frozenset({"active", "noticed", "pip", "reassigned"})
TERMINAL_STATUSES = frozenset({"retired"})

# The rungs of the ladder — everything runnable that is not the healthy state.
# Derived, never re-spelled: the recovery branch in chief_of_staff_agent.py used
# to carry its own ("noticed", "pip", "reassigned") literal, so the next stage
# anyone adds would be a stage no approved pick could restore an agent from.
COACHING_STATUSES = RUNNABLE_STATUSES - {"active"}

# Present in live registries on both hosts (16 rows each) but assigned by no
# branch of the ladder — a pre-ladder deactivation. Not runnable, not a stage,
# and NOT retired: agent_roster.py's header warns in capitals that "inactive"
# does not mean retired. Named here so it is part of the vocabulary rather than
# an unknown string every consumer guesses at.
DEACTIVATED_STATUSES = frozenset({"inactive"})

# Everything the firm has a defined meaning for. A status outside this set is
# one nobody designed.
KNOWN_STATUSES = RUNNABLE_STATUSES | TERMINAL_STATUSES | DEACTIVATED_STATUSES

# Legacy/unset rows. hr_retirement_audit.py:1257 already normalises a missing
# status to "active", so an absent field is runnable, not a fourth category.
DEFAULT_STATUS = "active"

# How long a run record stays good. The ladder ticks at most once per calendar
# day (`last_checked`), so one full day of evidence is the natural unit; 2 gives
# a weekend or a single missed cron room without letting a genuinely idle agent
# coast. This is deliberately NOT tuned to the cron cadence: if the pipeline
# stops running entirely, no agent is charged, which is correct — that is a
# pipeline outage, and law_runs_match_schedule is the detector for it.
RUN_EVIDENCE_DAYS = 2


def is_runnable(status: Optional[str]) -> bool:
    """Does this status mean 'the research cycle should still construct it'?"""
    return (status or DEFAULT_STATUS) in RUNNABLE_STATUSES


def runnable(agents: Iterable[dict]) -> List[dict]:
    """Filter a registry agent list down to the ones that should run."""
    return [a for a in agents if is_runnable(a.get("status"))]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load(path: Optional[Path] = None) -> dict:
    path = path or MANIFEST
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write(path: Path, payload: dict) -> None:
    """Write via a temp file in the same directory, then rename.

    `open(path, "w")` truncates before anything is written, so a crash or a
    concurrent reader mid-write sees an empty file — and an empty manifest reads
    as "nobody ran", which is precisely the input that must never be fabricated
    (memory `truncate-before-lock`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_cycle(ran: Iterable[str],
                 skipped: Optional[Dict[str, str]] = None,
                 path: Optional[Path] = None) -> dict:
    """Record which agents actually executed this research cycle.

    `ran` — class names whose `.run()` was called (whatever it returned; an
    agent that ran and found nothing HAS had its fair cycle, and should be
    charged for it).
    `skipped` — class name → why, for agents the loader declined to construct.
    A skip is the opposite: it must never be chargeable, and keeping the reason
    means the next person can see *why* an agent stopped being measurable
    instead of rediscovering it from a 50→8 cliff in a log.
    """
    path = path or MANIFEST
    data = _load(path)
    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    today = _today()
    now = datetime.now().isoformat(timespec="seconds")

    for cn in ran:
        rec = agents.setdefault(str(cn), {})
        rec["last_ran"] = today
        rec.pop("skip_reason", None)
    for cn, why in (skipped or {}).items():
        rec = agents.setdefault(str(cn), {})
        rec["last_skipped"] = today
        rec["skip_reason"] = str(why)

    payload = {
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "updated_at": now,
        "last_cycle_date": today,
        "last_cycle_ran": sorted(str(c) for c in ran),
        "last_cycle_skipped": {str(k): str(v) for k, v in (skipped or {}).items()},
        "agents": agents,
    }
    _atomic_write(path, payload)
    return payload


def had_a_fair_cycle(class_name: str,
                     days: int = RUN_EVIDENCE_DAYS,
                     path: Optional[Path] = None,
                     now: Optional[datetime] = None) -> Optional[bool]:
    """Did this agent actually run recently enough to be judged?

    Three-valued on purpose:
      True   — it ran; a zero-pick cycle is chargeable.
      False  — the manifest is fresh and this agent is not in it (loaded-and-
               skipped, or never constructed). Not chargeable.
      None   — no manifest, or the manifest itself is stale. CANNOT TELL. Also
               not chargeable, but for a different reason, and the caller must
               be able to say which — one is a fact about the agent, the other
               is a fact about the instrument (memory `verdict-with-no-consequence`).
    """
    data = _load(path)
    agents = data.get("agents")
    if not isinstance(agents, dict) or not data.get("last_cycle_date"):
        return None

    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    if str(data.get("last_cycle_date")) < cutoff:
        return None                      # the instrument is stale, not the agent

    rec = agents.get(str(class_name))
    if not isinstance(rec, dict):
        return False
    return str(rec.get("last_ran") or "") >= cutoff


def refusals_this_cycle(refusals: Dict[str, str]) -> str:
    """One line for the daily brief when the ladder declined to charge anyone.

    A ladder that quietly stops advancing is indistinguishable from a ladder
    with nothing to do. This makes the freeze self-announcing.
    """
    if not refusals:
        return ""
    by_reason: Dict[str, int] = {}
    for why in refusals.values():
        by_reason[why] = by_reason.get(why, 0) + 1
    parts = ", ".join(f"{n} {why}" for why, n in sorted(by_reason.items()))
    return (f"performance ladder charged no zero-pick cycle to {len(refusals)} "
            f"agent(s) ({parts}) — they did not get a cycle to fail")


def manifest_age_days(path: Optional[Path] = None,
                      now: Optional[datetime] = None) -> Optional[float]:
    """Age of the newest recorded cycle, or None if there is no manifest."""
    data = _load(path)
    stamp = data.get("last_cycle_date")
    if not stamp:
        return None
    try:
        d = datetime.strptime(str(stamp), "%Y-%m-%d")
    except ValueError:
        return None
    return ((now or datetime.now()) - d).total_seconds() / 86400.0


if __name__ == "__main__":
    data = _load()
    age = manifest_age_days()
    print(f"manifest: {MANIFEST}")
    print(f"  host           : {data.get('host', 'n/a')}")
    print(f"  last cycle     : {data.get('last_cycle_date', 'never')}"
          + (f" ({age:.1f}d ago)" if age is not None else ""))
    print(f"  ran last cycle : {len(data.get('last_cycle_ran') or [])}")
    print(f"  skipped        : {len(data.get('last_cycle_skipped') or {})}")
    print(f"  agents tracked : {len(data.get('agents') or {})}")
    print(f"  runnable       : {', '.join(sorted(RUNNABLE_STATUSES))}")
    print(f"  terminal       : {', '.join(sorted(TERMINAL_STATUSES))}")
