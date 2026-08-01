#!/usr/bin/env python3
"""Mutation harness: break the code on purpose, confirm a test screams.

A guard test that has never been seen to fail is decoration. Each entry below
re-introduces a bug that actually happened (or that the design forbids), runs
the test that is supposed to catch it, and reports SURVIVED if the suite stayed
green — i.e. the guard is fake.
"""
import subprocess, sys
from pathlib import Path

BASE = Path("/Users/jackbeard/Claude investment strategy/institutional-tracker")

MUTANTS = [
    # (file, find, replace, test target, what bug this is)
    ("agent_roster.py",
     'EMP_RANK = {"employed": 0, "superseded": 1, "retired": 2}',
     'EMP_RANK = {"employed": 2, "superseded": 1, "retired": 0}',
     "TestRosterMergeLosesNobody.test_retired_outranks_employed_in_a_merge",
     "a host's silence re-hires everyone HR fired"),

    ("agent_roster.py",
     'name = (canon.get(r.get("file"), r["name"])\n                    if r["kind"] == "module" else r["name"])',
     'name = canon.get(r.get("file"), r["name"])',
     "TestRosterMergeLosesNobody.test_agents_sharing_one_file_are_not_collapsed",
     "collapsing by file path deletes 4 of the 5 CIT agents"),

    ("agent_roster.py",
     'name = (canon.get(r.get("file"), r["name"])\n                    if r["kind"] == "module" else r["name"])',
     'name = r["name"]',
     "TestRosterMergeLosesNobody.test_one_agent_named_twice_by_two_hosts_is_one_agent",
     "no canonicalization invents 24 phantom FE/FI agents"),

    ("agent_roster.py",
     'if d.get("retired_at") and d.get("retirement_reason"):',
     'if d.get("retired_at") or True:',
     "TestRosterMergeLosesNobody.test_ratchet_reds_on_a_vanished_agent_and_greens_on_a_recorded_one",
     "the ratchet waves through an undocumented disappearance"),

    ("agent_roster.py",
     '        parts.append((DATA / "vm_crontab_snapshot.txt").read_text())',
     "        pass",
     "TestRosterMergeLosesNobody.test_the_schedule_is_both_hosts_not_just_this_one",
     "this host's crontab becomes the firm's schedule -> 74 unscheduled vs 45"),

    ("chairman_decision_sla.py",
     "from chairman_queue_sync import group_key as _group_key",
     "_group_key = lambda i: str(i.get('id'))",
     "TestChairmanQueueCannotRefork.test_the_sla_ingest_uses_the_reconcilers_identity_function",
     "ingest owns a second identity function; every reconcile is undone"),

    ("chairman_decision_sla.py",
     "        akey = _group_key(a)\n        if akey in seen_keys:\n            continue",
     "        akey = _group_key(a)",
     "TestChairmanQueueCannotRefork.test_ingest_is_idempotent",
     "ingest de-dupes on raw id only -> 124 rows of one complaint"),

    ("chairman_decision_sla.py",
     'seen_ids.update(q.get("merged_ids") or [])',
     "pass",
     "TestChairmanQueueCannotRefork.test_an_already_merged_id_cannot_walk_back_in",
     "merged ids walk back in as new decisions"),

    ("hr_retirement_audit.py",
     '    return bool(a.get("status") == "retired"\n                or (a.get("retired_at") and a.get("retirement_reason")))',
     '    return a.get("status") == "retired"',
     "TestWrongfulRetirement.test_the_audit_knows_retired_has_two_spellings",
     "'retired' has two spellings -> retired list empty -> clean: true"),

    ("hr_retirement_audit.py",
     '            "check": "RETIREMENT_NEVER_ENFORCED",',
     '            "check": "SOMETHING_ELSE",',
     "TestWrongfulRetirement.test_a_recorded_but_unenforced_retirement_is_reported",
     "7 agents fired on paper and still trading go unreported"),

    ("hr_retirement_audit.py",
     '            "check": "RETIREMENT_CITES_VOIDED_CHANCE",',
     '            "check": "SOMETHING_ELSE",',
     "TestWrongfulRetirement.test_voiding_the_hollow_chance_does_not_switch_the_detector_off",
     "the C remediation erases C's own input; audit reports clean forever"),

    ("hr_retirement_audit.py",
     "        if not fresh or not covers:",
     "        if not fresh:",
     "TestWrongfulRetirement.test_a_memo_that_postdates_the_retirement_cannot_vouch_for_it",
     "a May firing asserted from a July memo — fresh, but not contemporaneous"),

    ("chairman_queue.py",
     "        ranked = sorted(own, key=lambda i: (SEVERITY_RANK.get(\n            str(combined[i].get(\"severity\", \"\")).upper(), 0), i))\n        drop = set(ranked[: len(own) - max_entries])",
     "        drop = set(own[: len(own) - max_entries])",
     "TestWrongfulRetirement.test_a_capped_queue_evicts_the_least_severe_and_says_so",
     "blind FIFO cap evicts a HIGH to keep a LOW"),

    ("chairman_queue.py",
     "    added = [e for e in added if id(e) not in dropped_ids]",
     "    pass",
     "TestWrongfulRetirement.test_a_capped_queue_evicts_the_least_severe_and_says_so",
     "reports evicted findings as raised; caller believes they reached someone"),

    ("hr_retirement_audit.py",
     '        groups.setdefault((f["check"], f["batch"]), []).append(f)',
     '        out.append(dict(f, subject=f["class_name"]))',
     "TestWrongfulRetirement.test_one_batch_retirement_is_one_decision_not_seven_rows",
     "21 queue rows + 21 board escalations for 3 decisions"),

    ("hr_agent.py",
     '                "status": "ATTENTION",\n                "checked": 0,',
     '                "status": "OK",\n                "checked": 0,',
     "TestHRSeesTheWholeFirm.test_an_unbuildable_roster_is_attention_never_ok",
     "a missing roster reads as a clean bill of health"),

    ("hr_agent.py",
     "    if not rel:\n        importlib.import_module(name)\n        return",
     "    importlib.import_module(name)\n    return",
     "TestHRSeesTheWholeFirm.test_attendance_does_not_invent_broken_agents",
     "dotted import invents a broken agent in a shadowed package dir"),

    ("hr_agent.py",
     "        via = runs_via.get(name)\n        if via is not None:",
     "        via = None\n        if via is not None:",
     "TestHRSeesTheWholeFirm.test_attendance_and_the_roster_agree_on_who_is_scheduled",
     "two definitions of 'scheduled' -> two headcounts for one firm"),

    ("hr_agent.py",
     '            if name in _GOVERNANCE_CHAIN:\n                governance.append(entry)',
     '            pass',
     "TestHRSeesTheWholeFirm.test_hr_reports_its_own_team_as_unreviewed",
     "HR stops naming its own chain of command as unreviewed"),
]


def run():
    backups, results = {}, []
    try:
        for fname, find, repl, target, why in MUTANTS:
            path = BASE / fname
            if fname not in backups:
                backups[fname] = path.read_text()
            src = backups[fname]
            if find not in src:
                results.append(("NO-ANCHOR", target, why))
                continue
            path.write_text(src.replace(find, repl, 1))
            try:
                p = subprocess.run([sys.executable, "-m", "unittest",
                                    f"test_pipeline_smoke.{target}"],
                                   cwd=BASE, capture_output=True, text=True, timeout=900)
                killed = p.returncode != 0
            finally:
                path.write_text(src)   # restore immediately, even on timeout
            results.append(("KILLED" if killed else "SURVIVED", target, why))
    finally:
        for fname, src in backups.items():
            (BASE / fname).write_text(src)

    w = max(len(r[1]) for r in results)
    for verdict, target, why in results:
        mark = "OK " if verdict == "KILLED" else "XX "
        print(f"{mark}{verdict:<9} {target:<{w}}  {why}")
    bad = [r for r in results if r[0] != "KILLED"]
    print(f"\n{len(results) - len(bad)}/{len(results)} mutants killed.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
