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
