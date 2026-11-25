# Kalshi weather-edge strategy (built 2026-11-25)

**Status: PAPER mode now; it goes LIVE BY ITSELF on 2026-12-02 at 4 PM ET
unless its last 7 days of settled paper buys lost 20% or more** (Jack,
2026-11-25: "in a week tho make it live", "unless it heinously loses money",
"make it automatic so i dont have to put in a command", "if it made fewer than
5 trades thats just fine", "only dont turn it on if it lost 20% or more").
The backtest found no proven edge (below), so expect roughly break-even to a
small loss, bounded by the shared $3/day cap.

## What it does
`institutional-tracker/kalshi_weather_edge.py`, run hourly at :20 on the VM,
scans every open Kalshi daily weather market (high temp, low temp, rain),
builds a forecast probability from three public sources (NBM, GFS MOS, the NWS
office grid), and buys when the forecast beats the price plus fee by 8+ points
**and** every source agrees. It re-reads the price right before ordering and
buys immediate-or-cancel at that ask. Temperature markets are priced and logged
but never bought (see the backtest below). Rain markets are the only kind it
may buy.

## Safety (the gates, and how each is proven)
| Gate | Rule | Proof |
|---|---|---|
| G1 | Unarmed → paper rows only, broker never called | `TestKalshiWeatherEdgeIsSafeAndShared.test_unarmed_it_records_paper_and_never_calls_the_broker` |
| G2 | Live needs `data/.kalshi_weather_armed` AND every copier gate (env, key, `.kalshi_armed`, `.kalshi_copytrade_armed`, no halt file) | `test_live_needs_its_own_arm_file_and_every_copier_gate`, `test_the_copiers_halt_file_stops_it` |
| G3 | Spends from the copier's caps ($1/market, $3/day, one budget), hard-clipped to $1/$3 even if the copier's are raised; one shared spend lock; a copied exit never sells weather-edge contracts | `test_the_daily_cap_is_shared_with_the_copier`, `test_caps_are_clipped_...`, `test_both_strategies_take_the_same_spend_lock`, `test_a_copier_exit_never_sells_...` |
| G4 | Edge ≥ margin (never < 5 pts), every source ≥ break-even, 15–85c band, spread ≤ 8c, one market per city-day, recheck before order | `test_every_source_must_agree`, `test_margin_band_and_spread`, `test_recheck_skips_when_the_price_moved_away`, `test_the_order_never_pays_more_than_the_rechecked_ask` |
| G5 | Probability math | bracket probabilities sum to 1, Kalshi strike wording, rain combination, day windows, boundary temps |
| G6 | Manager (every 15 min): WEATHER_NOT_RUNNING (>3h), WEATHER_ARMED_BUT_PAPER, WEATHER_FEED_DOWN; report section with paper P&L (so it reaches apex-reports hourly); live orders sit in the copier ledger, so the loss breaker and ORDER_ERRORS cover them | `test_manager_reports_not_running_and_armed_but_paper`, wiring contract |
| G7 | Liveness `kalshi_weather_edge`, 6h, beat = markets priced of examined | `test_a_run_beats_liveness_...`, `test_liveness` |
| G8 | Wiring contract + existing suites | `TestWiringContracts` (62/63 here; the 1 error is `lightgbm` missing in the cloud container), 235 Kalshi + liveness tests pass |
| G9 | Live dry run 2026-11-25 19:55Z | 148 series, 813 open markets, 379 in window, 376 priced, 0 unknown stations |
| G10 | Backtest shows an edge | **FAIL**, see below |

## Backtest (decision at D-1 20:00 UTC, archived NBM + GFS MOS, Kalshi bid/ask at that hour, Kalshi's settled result)
Files: `docs/audit/modules/weather_edge_backtest_*.txt`.

* **Temperature, Nov 18–25 (2,176 markets):** high-temp signals −18%, low-temp
  −27%. The market's favourite bracket beat NBM, whose own error that week was
  sd 2.3°F (within 1°F only 45–49% of the time). Turned off for buying.
* **Rain, Nov 4–25 (734 markets, 645 priced):** 53 signals, −0.4% (NO) / +1.0%
  (YES) per contract; under the caps 41 buys, 12 won, −$4.05 on $33.05.
  Break-even, not an edge. Most losers were NO bets on days with a 0.01–0.05 in
  shower, which suggests `combine_pops()` under-calls a day's rain.

## Run it
```
cd ~/institutional-tracker
~/venv/bin/python3 kalshi_weather_edge.py --dry-run     # price everything, write nothing
~/venv/bin/python3 kalshi_weather_edge.py --report
~/venv/bin/python3 kalshi_weather_edge.py --backtest 2026-11-04 2026-11-25 --kinds rain
```

## Deploy (Mac RC session, after the PR merges and Jack says yes)
1. `cd institutional-tracker && bash gcp_setup/deploy.sh`, then verify with md5sum of
   `kalshi_weather_edge.py kalshi_copytrade.py kalshi_copytrade_manager.py`.
2. Add to the VM crontab, then refresh `data/vm_crontab_snapshot.txt`:
   `20 * * * * cd /home/jackbeard/institutional-tracker && /home/jackbeard/venv/bin/python3 kalshi_weather_edge.py --run >> logs/kalshi_weather_edge.log 2>&1`
3. `instrumented_since` is set to 2026-11-25T22:00Z (the planned deploy); if the deploy slips past ~03:00Z, bump it.
4. Add the deny rules for `.kalshi_weather_armed` to the Mac's Claude settings
   (listed in `docs/audit/2026-11-11-permissions-setup.md`).

## Scheduled go-live (`auto_arm_check`)
* From `AUTO_ARM_AT` = 2026-12-02 21:00 UTC, every run checks once: paper buys
  opened in the 7 days before, settled by Kalshi. Loss ≥ `auto_arm_max_loss_pct`
  (20) of their cost → REFUSED (final; going live then needs Jack). Otherwise
  → ARMED (zero trades arms too; `auto_arm_min_settled` defaults to 0).
* The decision is written to `data/kalshi_weather_auto_arm.json` and emailed
  to Jack once (urgent), and shown on the `--report` "go-live" line. The
  Manager raises WEATHER_AUTO_ARM_MISSED if the check has not run 3h after.
* Every copier gate still applies (KALSHI_ENV=live, key, `.kalshi_armed`,
  `.kalshi_copytrade_armed`, no halt file). The bot never writes an arm file.
* Change the threshold: `{"auto_arm_max_loss_pct": 10}` in
  `data/kalshi_weather_edge_config.json` on the VM (clipped at 50).
* Stop it alone: `~/venv/bin/python3 kalshi_weather_edge.py --disarm` on the VM
  (writes `auto_arm_enabled: false`; paper from the next run).

## Arm / stop by hand (Chairman only)
Arm (VM): `echo '{"env": "live", "armed": true}' > ~/institutional-tracker/data/.kalshi_weather_armed`
Stop it alone: `rm ~/institutional-tracker/data/.kalshi_weather_armed` (back to paper at the next run).
Stop the whole Kalshi desk: the copier's halt file / arm file, as before.

## What would justify arming
Two to three weeks of paper results in `--report` with a positive return on
20+ settled paper buys, or a rain re-calibration (union-leaning
`combine_pops`) that wins on a backtest window it was not tuned on.
