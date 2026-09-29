# Apex Capital — Improvement Ideas
*Compiled 2026-05-26 by Claude after full codebase scan*

---

## ML MODELS

### High Priority
**1. Regime-conditioned feature weighting**
The current LT model uses a flat 8-feature set regardless of market regime. SHAP analysis shows `rd_vix` and `rd_spy_rsi` matter more in stressed markets. Build a regime-router: one LightGBM trained on bull data, one on bear/correction data; a meta-classifier picks which to use at inference time. Expected lift: +0.01-0.02 AUC.

**2. Earnings-surprise lag features**
`edgar_net_margin` / `edgar_fcf_margin` are in the ST queue but raw. Earnings *surprise* (actual vs consensus estimate) is a stronger predictor than raw margin. If FactSet/consensus data is unavailable, proxy with QoQ margin change z-scored vs sector. Insider buying + earnings surprise together is the most predictive known combo for short-term outperformance.

**3. Walk-forward expanding window for LT**
Current LT walk-forward uses 6 fixed folds. For 365-day labels the test window is very short per fold. Use an expanding-window scheme (train on all history, test on next 90 days) with at least 8 folds. More data in training, less variance in AUC estimates.

**4. Calibrated probability output**
Models currently output raw scores. Adding Platt scaling or isotonic regression calibration would let the system express "this pick has 62% probability of beating SPX in 20 days" rather than just a rank. CoS/Board can then apply a probability threshold rather than a percentile threshold.

**5. Alpha-decay tracking per feature**
`alpha_decay_report.json` exists but shows 0 features analyzed. Reactivating this agent and feeding its output back into feature selection would auto-retire features that have stopped predicting. Should run weekly.

### Medium Priority
**6. Multi-horizon ensemble**
Combine ST (20d) and LT (365d) signals into a single blended score. A stock scoring high on both is a stronger conviction pick than one scoring on only one horizon. The blend weight can be tuned per portfolio objective.

**7. Sector-relative features**
Most features are absolute. Adding sector-z-scored versions (e.g. `price_momentum_20d` minus sector median `price_momentum_20d`) typically adds 0.005-0.01 AUC for equity selection models.

**8. Institution cluster quality**
`herding_flag` is binary. Replace with a continuous "herding quality" score: herding by Renaissance/Two Sigma is very different from herding by generic pension funds. Weighted herding by filer alpha historically would be more predictive.

---

## AGENT COMMUNICATION

### Fixes / Completions
**9. Wake-log data file**
`data/wake_log.json` is MISSING (the full system scan showed it). `wake_agent.py` should create this file on first run. Low effort, adds visibility into agent wakeup history.

**10. Concurrent write safety**
The `_save()` function uses a `.tmp` → `os.replace()` pattern, which is good, but under heavy simultaneous writes (multiple agents running at once) there's a race on the temp filename since all processes use `path + ".tmp"`. Use `tempfile.NamedTemporaryFile` with `delete=False` in the same directory instead.

**11. Inbox summary digest**
CoS currently runs escalation check + cleanup on every run but doesn't broadcast a daily digest of "inbox health" to the board (how many messages processed, how many escalated, any stale threads). A daily 9am digest to the board_meeting channel would give leadership visibility.

### New Communication Patterns
**12. Agent-to-agent direct message (DM)**
Currently all communication goes through shared JSON files (which any agent can read). A true DM pattern would be a record with `private: true` that only appears in the recipient's inbox. Useful for HR passing sensitive PIP details, or board passing confidential votes.

**13. Structured "ask for data" protocol**
Right now, if MLShortTermBoard needs fresh signals from RD Team Leader, it files an INFO_REQUEST and waits. A `request_compute(from, to, computation_spec, callback_meeting_id)` pattern would let one agent ask another to run a specific computation and post results back into a designated meeting — more structured than free-text requests.

**14. Agent health heartbeat to board**
Each agent already calls `update_agent_status()` via inbox. But the Board never sees a rollup. Add a weekly "agent health report" meeting auto-created by CoS that includes: agents OK/DEGRADED/OFFLINE, average inbox processing time, top escalation reasons. Board can act on DEGRADED agents proactively.

---

## RISK MANAGEMENT

**15. Position-level stop-loss tracking**
The canary agent monitors portfolio-level VIX/SPY drawdown. There's no per-position stop-loss tracker. A `position_risk_monitor.py` that flags positions that have dropped >X% since entry (adjustable by sector volatility) and alerts Manager Agent would reduce tail risk at the position level.

**16. Correlation clustering before rebalancing**
Before Manager Agent executes a rebalance, check whether the new proposed portfolio has correlation clusters (e.g. 5 positions all highly correlated with NVDA). An IRM-owned "correlation audit" step that can veto or trim positions in a cluster above a threshold (e.g. 3+ positions with pairwise r > 0.8).

**17. Drawdown circuit breaker**
If portfolio drawdown exceeds 8% in a rolling 30-day window, automatically freeze new position entries and require a Board vote to resume. Right now the canary flags it but there's no hard stop. This is the difference between a warning and a circuit breaker.

**18. Macro regime model**
`rd_regime_bull` is a binary feature from the R&D bridge. A dedicated macro regime model (recession probability, credit spread trend, yield curve shape) would give a richer multi-class regime label (bull / late-cycle / contraction / recovery) that all other models and agents could consume.

---

## NEW AGENTS WORTH BUILDING

**19. Pre-trade compliance agent**
Before any trade is placed, checks: position size limits, sector concentration limits, blackout periods, liquidity (can the full position be filled in <3 days' volume). Sits between Manager Agent and Alpaca executor. Current gap: there's no guardrail between "model recommends buy" and "order sent."

**20. EDGAR earnings call transcript agent**
SEC EDGAR has 8-K filings that include earnings call transcripts. An agent that NLP-parses these for management tone (uncertainty keywords, guidance cuts, "headwinds") and adds a `mgmt_tone_score` feature would complement the quantitative filing signals. Zero-cost data source, high-value signal.

**21. Short-interest change monitor**
`short_interest_pct` is a static snapshot. A monitor that tracks *change* in short interest (e.g. short interest doubled in 2 weeks) is a much stronger signal — either a squeeze setup or fundamental deterioration. This agent would update the feature daily and alert CROAgent on extreme moves.

**22. Portfolio attribution agent**
Weekly, decompose portfolio returns into: which picks from which signal (ML, R&D, FI, FE) generated alpha, which lost money. Feed this back to each board as a performance review input. Currently there's no systematic attribution — it's hard to know if the ML model is actually adding value vs random picks.

**23. Liquidity stress test agent**
Monthly, run a hypothetical: "if we needed to liquidate 50% of the portfolio in 3 days, what's the expected slippage?" Report to IRM. Flag any positions where estimated slippage > 1.5%. Especially relevant for smaller/mid-cap positions.

**24. News catalyst agent**
Currently `alt_data_signals.json` shows all-zero news sentiment (the scraper isn't running). Reactivating news sentiment with a proper source (e.g. Benzinga, NewsAPI, or even Reddit r/investing) and feeding it as a feature or alert signal would add a meaningful short-term catalyst layer missing from the current system.

---

## INFRASTRUCTURE / OPERATIONS

**25. Disk space monitor**
`/dev/sda1` is at 82% used (7.4GB / 9.7GB). At current growth rate (logs + data + model checkpoints) this could fill within weeks. Options: (a) add log rotation to crontab — delete logs > 30 days old; (b) upgrade GCP disk from 10GB to 30GB ($1.50/month); (c) both. The VM going down from a full disk would be worse than either.

**26. Cron heartbeat monitoring**
CIT Director already flagged this: there's no external health check ping. If the GCP VM is up but a cron job silently fails, nothing alerts. Add a `curl healthchecks.io/ping/<id>` call at the end of `main.py`. Free tier on healthchecks.io covers this. Would have caught the OOM issue tonight hours earlier.

**27. GCP VM right-sizing**
The OOM crash tonight happened because two ML jobs ran simultaneously on a 4GB RAM instance. Two options: (a) add a lock file in `ml_loop_runner.sh` so only one ML job can run at a time (already in the runner but verify); (b) upgrade to n1-standard-2 (7.5GB RAM, ~$50/month). The lock file is free; the upgrade reduces risk.

**28. Model versioning / registry**
Trained models are saved as flat `.pkl` files. There's no registry of "which model is currently live, what AUC did it have, when was it promoted." A lightweight `data/model_registry.json` with `{model_name, version, auc, promoted_at, feature_list, baseline_auc}` would make it auditable. Currently hard to answer "is the live model better than 3 months ago?"

**29. Automated backtester on promotions**
When a model is PROMOTED (AUC > threshold), automatically run the backtester on the last 6 months of data and attach the result to the promotion log. Right now promotion is based purely on walk-forward AUC — backtested returns are a separate manual step.

---

## STRATEGIC / LONGER-TERM

**30. Multi-asset expansion**
The current system is equity-only (13F filings). Extending to crypto signals (on-chain flows, funding rates) and commodity futures (COT reports) would add diversification and uncorrelated alpha sources. The agent architecture already supports this — it's a data + feature engineering problem.

**31. Real-time signal layer**
Current architecture is batch (cron jobs every few hours). Adding a WebSocket feed for intraday price data (Alpaca's free tier supports this) and a "signal refresh" that re-scores positions intraday would let the system respond to intraday catalysts rather than waiting for the next cron run.

**32. Institutional flow velocity**
13F filings are quarterly — a 90-day lag. Alternative data providers (e.g. Quiver Quantitative, which aggregates Congressional trades, Senate disclosures, and alternative 13F proxies) have near-real-time institutional flow data at low cost. This would significantly close the data latency gap.

**33. Counterparty / venue analysis**
Currently all trades go through Alpaca. Adding a simple TCA (transaction cost analysis) layer — comparing Alpaca fill prices against NBBO midpoint — would measure execution quality and flag if slippage is consistently high on certain position sizes.

---

## QUICK WINS (< 1 day each)

- **Log rotation crontab entry** — delete logs older than 30 days (5 min to add)
- **Wake-log file initialization** — create `data/wake_log.json` as `[]` on startup (10 min)
- **Alt data sentiment reactivation** — the agent exists, just needs an API key plugged in
- **Board health digest** — CoS posts a daily 9am summary of agent status registry to firm_channel (1 hour)
- **Model registry JSON** — flat file tracking live model versions (1 hour)

---

*Total: 33 ideas across ML, comms, risk, new agents, infrastructure, and strategy.*
*Top 3 highest ROI: #25 (disk space — urgent), #26 (cron heartbeat), #15 (position stop-loss).*
