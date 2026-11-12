# Archive — Legacy Agents

## head_agent.py

**Archived:** 2026-05-01  
**Status:** SUPERSEDED — do not run

### What it was
The original single-agent orchestrator for Apex Capital. It held hardcoded allocations:

| Ticker | Amount | Thesis |
|--------|--------|--------|
| NVDA   | $4,000 | AI GPU monopoly ★★★★★ |
| AVGO   | $3,000 | Custom ASIC + networking ★★★★★ |
| AMD    | $2,000 | AI GPU challenger + EPYC ★★★★ |
| MRVL   | $1,000 | Custom ASIC / optical interconnects ★★★★ |

### Why it was retired
Replaced by the full multi-agent pipeline (`main.py`), which provides:
- Dynamic signal scanning (SEC EDGAR 13F filings)
- Research-driven picks (R&D Team, Energy Research Team)
- Risk management (CRO, IRM, Canary Agent)
- Capital efficiency gating (PDO)
- Governance oversight (Board Meeting, HR Team)

Running `head_agent.py` today would submit hardcoded orders that are:
1. Untracked by the current position ledger (`state.json`)
2. Not subject to any risk rules or circuit breakers
3. Based on a static thesis from early 2026 — stale

### If you need to reference it
Read this archived copy. **Do not execute the original** at
`institutional-tracker/head_agent.py` — it still exists for reference
but should be treated as dead code.

## pregenerate_lessons.py

**Archived:** 2026-11-12
**Status:** RETIRED one-shot — not scheduled, kept for reference

### What it was
A batch generator for `data/lessons_library.json`, written on a Chairman directive
of 2026-06-12 when Fable 5 access was due to expire on June 22. It ran nightly
from June 13 to June 21 (`0 2 13-21 6 *`, 12 lessons a night) and pre-wrote
lessons so teaching continued after expiry. It stored 18 of the 90 days on both
hosts.

### Why it was retired
That cron line was a June-only batch and was removed from the VM on 2026-11-12
(see docs/audit/INTENT_REGISTER.md). With nothing scheduling it, wiring_audit
correctly flagged it as an orphan agent, which blocked every push. Nothing is
lost by retiring it: `teaching_division.prepare_session()` reads the library
first and, for any day the library does not cover, writes the lesson live through
`apex_llm` (the "teaching" use). The library is a cache, not the only source.

It is archived rather than added to the shrink-only orphan baseline, because that
baseline must never grow to silence a finding.

### If you ever need it again
Move it back to `institutional-tracker/` (it resolves `data/` relative to its own
folder) and give it a scheduler entry, or the orphan check will fail again.
Running it spends LLM credits.
