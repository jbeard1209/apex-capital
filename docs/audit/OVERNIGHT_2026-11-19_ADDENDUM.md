# Overnight 2026-11-19, addendum: Jack's seven calls

Jack approved all seven open calls at 16:10Z ("complete them"). Each took the recommended option and was gated, merged, deployed and checked on the VM.

| Call | What was done | Proof |
|---|---|---|
| 13F scanner (#54) | Merged and deployed. `signal_engine.run_signal_scan()` returns 34 raw signals on the VM; it was 0 before. `pipeline_snapshots` should get its first live rows since 2026-05-15 on the next main.py run. | VM md5s match the Mac's. |
| Two Sigma CIK | `config.py` had Scion's CIK under the Two Sigma name. It is now `0001179392`. `migrate_two_sigma_cik.py --apply` ran on both hosts and relabelled 33 filings as Scion, with nothing deleted. Holdings: 477 on the Mac, 0 on the VM. Snapshots: 178 on the Mac, 310 on the VM. | Backups `data/historical_13f.before-two-sigma-cik.*.db` on each host. |
| PAUSE control (#69) | The emergency board's PAUSE_NEW_BUYS vote now writes `data/buy_pause.json` with a 24h expiry. While it is in force, `risk_gate` blocks new buys. Exits and genuine hedges still go through, and an unreadable file fails closed. Clear it with `python3 risk_gate.py --clear-pause`. | VM `risk_gate.py` shows "buy pause: none". 14 tests. |
| 20-position cap (#69) | ETF pod symbols get up to 4 slots above the cap. These are long only, and the cash floor still applies. reconciliation_check allows the same headroom. The cap is not binding right now, at 17 positions. | VM md5s match. |
| FE + ML research | Added to VM cron: `fe_research_scheduler.py` at 02:00 UTC and `ml_research_team_leader.py` at 07:00 UTC. Manual runs on the VM both returned rc=0. The crontab snapshot was refreshed. | Backup `~/crontab.before-research.20261119201346.txt`. |
| loss_attribution rows | 14 duplicate LO-*-unexplained_loss rows were superseded with board_dispatch `--supersede`. The canonical row LO-20261118201625 stays queued. logic_officer already dedupes, so no new copies have appeared since 11-18. | 14 `cit_implementation_queue.before-supersede.*` backups. |
| Kalshi copy-trading and shard-1 cash | Not changed. Moving the $20.44 shard-1 cash is a money movement, and only Jack can do it. Copy-trade stays inside the $1/$3 caps; it is at -$9.90 on $10.13 deployed. Arm and halt files were not touched, and the error counter stays at 3. | |

## New problems found while doing this (not fixed)

1. **FE valuation share-class bug.** BRK-B shows an IV of $144,538 against a $571.62 price, with an MoS of 99.6%. It uses `eps_used` = 70,997, which is Class A EPS applied to the B share. AEM's IV is $3.51 (MoS -4430%) and MCO's is $102.79, so per-share scaling is wrong more generally. No harm yet. `fe_pipeline` only takes BUY or "fresh" memos, and every memo lacks `synthesis_date`, so the deep-research path in `fe_pipeline.run` (lines 722-751) can never fire. That is a dead integration hiding a bad number. Fix both together, valuation first.
   **Fixed 2026-11-19 (branch `claude/project-thread-t0o9vi`).** Root causes, checked against live SEC data: (a) `fe_edgar_fundamentals` took the first XBRL tag with any data however old, so BRK-B used a 2011 Class-A-only share count (943,242) and 2012 EBIT next to 2025 earnings, AEM used 2013 us-gaap figures (IFRS since) on 2025 shares, and MCO's revenue stopped at 2017; (b) share counts were not in the units of the quoted class (1 BRK-A = 1,500 BRK-B). Now tags merge per year, fields that lag the filing year are dropped, stale EDGAR falls back to yfinance, and the share count is reconciled with marketCap/price. The deep-research valuation got the reliability gate, an FX guard and a 0.1x-5x plausibility guard. After the fix: BRK-B $195 (price $571), AEM $60 ($159), MCO $118 ($472), all OVERVALUED. The hand-off is fixed too: `run_pipeline` (the cron path) now uses `_check_research_library`, which reads the keys `synthesis_agent` really writes. Memos built on the old valuation (no `valuation_version` >= 2) are refused until they are re-researched. Tests: `TestFEValuationIsPerQuotedShare`, `TestFEDeepResearchReachesTheBoard`, and a `TestWiringContracts` key-contract case.
2. **ml_alt_data_agent FRED VIXCLS parse.** It fails with `Missing column provided to 'parse_dates': 'DATE'` because FRED's CSV header changed. The error is non-fatal, but VIX alt-data is missing.
3. **reconciliation HIGH LEDGER_ATTRIBUTION.** 72% of the book ($54,385 of $75,766) belongs to no pod, so no pod budget is charged for it. This was already true before these changes.
4. The Mac's `historical_13f.db` is from Jun 15. The VM's is from Nov 16. Mac-side 13F work runs on stale filings.

## Still Jack's

- Move the shard-1 Kalshi cash yourself, if you still want it moved.
- The Chairman decision queue has 22 items, 2 of them chairman-only (Kalshi arm gate). Run `python3 chairman_inbox.py --full`.
