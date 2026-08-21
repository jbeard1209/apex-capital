# Board Decision Memo — Lifting the 272-Day Buy Freeze

**To:** Chairman & Board, Apex Capital
**From:** CIT / Risk Oversight
**Date:** 2026-07-27
**Decision ID:** `chairman-decision-20260727-cro-unfreeze`
**Classification:** CRITICAL — capital deployment blocked firm-wide

---

## 1. The decision in one line

The CRO's firm-wide **buy freeze** was triggered by a now-fixed risk-calculation **bug**, not by real risk. The fix is live and risk currently reads **LOW / "standard deployment."** The Board is asked to **formally ratify lifting the freeze** so the firm can deploy capital when picks qualify.

## 2. What happened

On **2026-06-09**, the CRO blocked **all** BUY / ADD orders after the risk engine reported extreme portfolio risk. Per Chairman directive that same day, un-freezing was reserved for an explicit Board vote. The freeze has now stood for **272 days**, leaving the firm unable to buy regardless of pick quality.

## 3. Root cause — it was a bug, not risk

The risk engine weighted Value-at-Risk and the Herfindahl concentration index by **signed net exposure** instead of **gross exposure**. On a market-neutral (long ≈ short) book, netting collapses the denominator toward zero and **inflates every risk metric ~87×**:

| Metric | Bug reading | True value |
|---|---|---|
| Portfolio VaR | **44.9%** | ~0.5% |
| Herfindahl (HHI) | **240.87** | ~0.072 |

An HHI above 1.0 is mathematically impossible (it is a bounded 0–1 index) — a clear tell that the number was garbage, not a real exposure.

## 4. Current status — fix is live

- **Fix committed:** `149e513` — *"weight VaR + Herfindahl by GROSS exposure, not signed net."*
- **Deployed** to the always-on GCP VM (verified 2026-07-27).
- **Live risk bulletin now reads:** `risk_level: LOW` · `capital_guidance: STANDARD DEPLOYMENT` · `board_review_warranted: False` · `consecutive_high_cycles: 0`.
- Independent metric-sanity and data-integrity sweeps are **GREEN**; the impossible-HHI class is now auto-caught going forward.

The mechanical block no longer fires. Only the **governance ratification** remains outstanding.

## 5. Context

- **Firm equity:** ~$114.3k (paper account). The CRO note cites **~$96k idle/deployable** — confirm against live Alpaca buying power at execution.
- **Market backdrop:** Canary returned to **GREEN** (2026-07-24); risk-off ORANGE has lifted.
- **Pick pipeline:** FI / FE / ETF pods are running (reports 3–5h fresh). *Note: a same-day deep-dive found no pick currently clearing the bar — so lifting the freeze restores the **ability** to buy; it is not itself a buy instruction.*

## 6. Options

| Option | Effect | Trade-off |
|---|---|---|
| **A — Ratify unfreeze (recommended)** | Restores normal buying; standard position sizing & CRO circuit-breakers remain active | Firm can deploy when a pick qualifies |
| **B — Hold the freeze** | Firm stays 0-buy indefinitely | Idle capital; forgoes any future qualifying pick |
| **C — Partial / capped** | Unfreeze with a temporary per-trade or total-deployment cap | Extra caution; more moving parts |

## 7. Recommendation

**Option A.** The freeze was protecting the firm from a phantom number. With the calc fixed, verified, and reading LOW — and the ordinary CRO drawdown circuit-breakers (−2% intraday, −12% from peak) still in force — there is no risk basis to keep buys blocked. Lifting it simply lets the firm act when a genuinely attractive pick appears; nothing forces a trade before then.

---

## Chairman ratification

> **Decision:** ▢ A — Ratify unfreeze ▢ B — Hold ▢ C — Partial (cap: ______)
>
> **Chairman:** ______________________  **Date:** __________
>
> On ratification of A: freeze flag cleared; resume standard deployment under existing CRO/Canary controls.

*Paper-trading firm — simulation only. Prepared as an internal Apex Capital governance record.*
