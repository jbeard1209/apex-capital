# Security Policy

Apex Capital is a **paper-trading** firm. No real capital is ever at
risk in this repository, and no code path can submit a live order
without a per-host arm-gate file that is deliberately gitignored. That
constraint shapes the threat model.

## Reporting a vulnerability

Do **not** open a public GitHub issue for security bugs.

Email **jack.beard07@gmail.com** with:

- Affected file / commit
- Reproduction (or the invariant the bug lets you violate)
- What a malicious actor could do with it — the more concrete, the
  better. "This lets a caller bypass the CRO block on buys" is more
  useful than "this looks unsafe."

I will acknowledge within **72 hours** and aim to disposition within
**14 days**. If the report is credible, a fix will land on `main`
before the disclosure window closes.

## Scope

**In scope**

- The firm's controls: `coherence_laws.py`, `internal_audit_agent.py`,
  `wiring_audit.py`, `chairman_decision_sla.py`, the pre-push gate
  (`scripts/git-hooks/pre-push`), the two-host guard
  (`single_writer_detector`).
- The trading path: `manager_agent.py`, `alpaca_trader.py`,
  `risk_manager_agent.py`, `irm_review_agent.py`, `main.py`. These
  are the six **protected files** — edits require a Chairman
  decision recorded in `data/chairman_decision_queue.json`.
- Any path that could reach the broker (buy, sell, close, cancel).
- Any path that could bypass the arm-gate check for a live-trading
  transition (Kalshi, FI, futures).

**Out of scope**

- The paper Alpaca account balance (the account holds no capital
  worth protecting; the interesting property is that Apex never
  submits a real order, not the balance itself).
- Data-quality bugs in third-party feeds (EDGAR, FRED, yfinance,
  Alpaca). Reports of "AAPL P/E looks wrong" go to the ordinary
  issue tracker.
- Historical postmortems in `docs/history/` — those are archival.

## Threat model

The realistic attackers are:

1. **A contributor with commit access** who lands a change that
   bypasses a control. Guarded by the pre-push gate, the wiring
   contracts, and the third-line-of-defence audits.
2. **A dependency compromise** (a malicious version of a pinned
   package). Guarded by Dependabot weekly updates, CodeQL, and the
   Chairman-only arm-gate carve-out — even a compromised dependency
   cannot submit a live order without a gitignored file that only
   Jack can create.
3. **A prompt injection** in incoming data (an SEC filing, a news
   feed) attempting to steer a research agent. Guarded by the fact
   that no agent has direct order-submission authority — every
   pick flows through `manager_agent` and `risk_manager_agent`
   before the paper broker sees it.

Not in scope: nation-state actors, insider threats from Jack
himself. The firm is single-owner.

## Credentials and secrets

- `.env` is gitignored. `.env.example` documents the template.
- Git history was scrubbed **2026-09-29** using `git-filter-repo
  --replace-text` to remove pre-hardening dashboard passwords from
  earlier `START.md` commits.
- The Alpaca **paper** key is intentionally not rotated — a paper
  key controls nothing. Do not rotate; do not commit.
- Kalshi contracts are CFTC-regulated. This repo contains a
  *research mirror* only. No Apex code places a live bet or
  handles Jack's Kalshi credentials, and no session may create
  `data/.kalshi_armed` or `data/.kalshi_mirror_armed` — those are
  Chairman-only arm gates.

## Two-host safety

`main.py` runs on the Mac dev host and on a GCP VM
(`apex-capital-495523`) against the same paper Alpaca account.
There is **no state sync** between hosts. The broker is the only
shared medium. Arm-gate files are per-host by construction —
arming live trading on one host does not arm the other. That is
the design, not a bug.

The `single_writer_detector` counts how many hosts wrote paper
orders in a window and alarms when the count is not 1. If you
find a bypass, that is a security-scope report.
