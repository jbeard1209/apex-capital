# Kalshi BUILD — Research Plan

**Chairman decision, 2026-09-30**: BUILD, not DROP. The
BUILD-OR-DROP verdict of 2026-09-08 read the mirror-only backtest
and concluded the premise was dead. That verdict was too narrow.
Kalshi participants are demonstrably profitable. Before Apex
concludes the venue is unharvestable, it has to identify **which**
lane those participants are trading and **which** of those lanes
is accessible to a single-owner firm with $0 committed capital.

This is a research plan. Nothing here authorizes live trading.
`data/.kalshi_armed` and `data/.kalshi_mirror_armed` remain OFF,
and only the Chairman can create them.

---

## Guardrails (do not cross)

- Kalshi contracts are CFTC-regulated. Apex code **does not**
  place a live bet, connect to Jack's Kalshi account, or handle
  his Kalshi API keys.
- The research mirror stays paper. Every strategy this plan
  identifies is evaluated in shadow mode against the same public
  price and volume feed the mirror already ingests.
- Any transition from research to armed requires a Chairman
  decision recorded in `data/chairman_decision_queue.json`.

---

## What we already know

`kalshi_mirror_edge` (memory) proved a small, real edge in the
**98-100¢ × 1-7d** slice: `n=2695`, `EV_net = +1.016¢`. Fee
cliffs and slippage kill everything wider. The 2026-09-08 verdict
read that as "the harvestable edge is a rounding error" and
recommended DROP.

The verdict measured **calibration arbitrage on the retail-side
mid-price** and nothing else. Kalshi is a two-sided venue; there
are at least five other economically-plausible ways to make money
on it, and each one has to be independently tested before "DROP"
is a supportable verdict.

---

## The six lanes

For each lane: the archetype, the observable, the disqualifier,
the go/no-go call.

### 1. Calibration arbitrage (already tested)

**Archetype.** Buy YES on markets the crowd underpriced or the
retail-side mid-price is stale.

**Observable.** `EV_net` on close-out over N days by
(price-band × time-to-resolution × market-category).

**Status.** Positive in the 98-100¢ × 1-7d slice. Kill-fee
elsewhere. **Verdict: harvestable in a narrow slice; not the
main lane.**

### 2. Market-making rebate capture

**Archetype.** Post two-sided quotes; earn maker rebate; exit
inventory at even or better. This is how CFTC-regulated venue
market makers actually make money — the P&L is fees, not
directional risk.

**Observable.** For each market: (maker-fee rebate structure)
minus (inventory carry cost) minus (adverse-selection cost).

**Research task.** Rebuild the Kalshi fee schedule from
`kalshi_fee_schedule.py` (if it exists) or scrape the public
docs. Simulate a two-sided quoting agent against the mirror's
order-book snapshots. Measure rebate P&L before touching
directional P&L.

**Disqualifier.** Kalshi's rebate schedule is unfavorable to
sub-institutional volume, OR the venue's minimum-post-time /
minimum-quote-life rules make micro-quoting infeasible for a
single-account setup.

**Go/no-go date.** 2 weeks (2026-10-14). Ship a
`fi_kalshi_mm_backtest.py` that reports rebate P&L per market
per day. If the median day-P&L is negative post-adverse-selection,
this lane is dead.

### 3. Information-edge trading

**Archetype.** A participant with a better forecast than the
market. Weather derivatives desks trade weather-outcome markets
against private radar + surface-station networks. Election desks
trade against private polling. Sports desks trade against
proprietary injury feeds. In each case the *edge* is the private
signal, and the venue is where the edge cashes out.

**Observable.** For each Kalshi category, is there a public
signal that beats the market's implied probability by more than
fees + adverse-selection?

**Research task.** Build `kalshi_edge_scan.py` — for each of
Kalshi's ~30 category groups, pair the category with a public
signal source (FRED for macro, EDGAR for corporate, NOAA for
weather, FiveThirtyEight-style aggregators for elections).
Compute the calibration of the public signal against the market's
implied probability *at contract close*. Categories where the
public signal is well-calibrated but the market is not — the
market pays a premium for the public signal that Apex can
capture — are candidates.

**Disqualifier.** If no category shows a persistent
market-vs-public-signal spread, this lane is not accessible to a
firm without proprietary data.

**Go/no-go date.** 3 weeks (2026-10-21).

### 4. Cross-venue arbitrage

**Archetype.** Same or nearly-same contract trades at different
prices on Kalshi vs. Polymarket vs. Predictit vs. offshore
betting exchanges. Buy the cheap venue, sell the expensive one,
carry to resolution.

**Observable.** For each Kalshi event, does an equivalent
Polymarket / Predictit / offshore contract exist? If yes, what
is the spread net of fees on both sides and the reg-cost of
running the two accounts?

**Research task.** Extend the existing `polymarket_scanner`
(shadow mode) to emit paired Kalshi ↔ Polymarket contract IDs
and their live spreads. Compute the daily distribution of
spreads. A persistent 3-5¢ spread (well above fee + carry) is
the go signal.

**Disqualifier.** Kalshi's CFTC-regulated status vs. Polymarket's
non-US status means running both accounts as a US person may not
be legal. Confirm regulatory posture before allocating any real
capital.

**Go/no-go date.** 2 weeks (2026-10-14).

### 5. Event-catalyst directional trades

**Archetype.** Fed meetings, Jobs Reports, CPI releases,
earnings, elections. The market's implied probability shifts
predictably around known catalysts. A trader with a good
pre-catalyst probability model and a good post-catalyst reaction
model captures the drift.

**Observable.** For each catalyst class (FOMC, CPI, NFP, Fed
speeches, earnings, IPO pricing, election milestones), does the
market's implied probability trace a predictable path relative to
the catalyst?

**Research task.** Take the existing Apex `earnings_predictions`
and `fi_credit_conditions` outputs; join them to Kalshi
event-catalyst markets by date; measure whether Apex's own
forecasts beat the market's implied probability at close. This
uses signal Apex is already producing.

**Disqualifier.** Apex's earnings and credit forecasts do not
outperform the crowd on Kalshi's event markets. Since the
walk-forward AUC is ~0.58, the honest expectation is that they
do not, but the test is cheap and the null result is worth
having.

**Go/no-go date.** 2 weeks (2026-10-14).

### 6. Retail-flow bias exploitation

**Archetype.** Kalshi is a retail venue. Retail traders exhibit
well-documented biases: home-team bias in sports,
recency-weighted bias in elections, favorite-longshot bias in
sports-adjacent markets. A trader who systematically takes the
other side captures the bias premium.

**Observable.** For each market category, does the closing price
show a persistent bias vs. the empirical resolution rate?

**Research task.** Build `kalshi_bias_scan.py` — for each
category, compute the empirical bias (mean deviation of
implied-probability-at-close from actual-resolution-rate) over
the mirror's history. Categories with a bias >2¢ net of fees are
candidates.

**Disqualifier.** No category shows a persistent bias > fee.
This is likely the outcome, since venue market-makers arbitrage
the largest biases away; but the residual bias in low-volume
markets is often unarbitraged and detectable.

**Go/no-go date.** 3 weeks (2026-10-21).

---

## Deliverable structure

Each lane produces:

1. A `fi_kalshi_{lane}_backtest.py` script that runs against the
   existing mirror ledger and emits a report to
   `data/kalshi_{lane}_report.json`.
2. A one-page markdown writeup: what the archetype is, what the
   test measured, what the number was, and the go/no-go call.
3. A `TestKalshiWiringContracts` case (per CONTRIBUTING.md) if
   the lane adds a new routed item.

If *any* lane clears its go signal, that lane graduates to a
Chairman decision: "arm the mirror for this lane, in this slice,
at this size cap." The default cap is `$50 notional per day` for
the first 30 days of any armed lane.

If *no* lane clears, the BUILD verdict is revisited on
**2026-11-15** and DROP becomes the supportable call. Not before.

---

## Why this is the right framing

Jack's directive was specific: winners on Kalshi exist; the firm
must identify why and how before it concludes the market is
unharvestable. The DROP verdict of 2026-09-08 answered "does the
narrow calibration-arb lane clear the fee cliff?" (no) and
generalized that to "the venue is unharvestable." Those are
different claims.

Six lanes, six tests, three weeks. The firm either finds a
harvestable lane and moves to a Chairman-armed pilot, or it
generates six negative results that make DROP the honest
verdict. Either outcome is the point.

---

## Rollback

If any lane's test produces a strong positive that turns out to
be a leakage artifact (walk-forward-vs-holdout divergence, or a
label-in-features bug), the lane is disqualified. The
`SUSPICIOUS_HIGH` heuristic from `model_test_harness.py` (see
[QUANTITATIVE_METHODS.md](QUANTITATIVE_METHODS.md)) applies
verbatim here: a Kalshi-lane P&L that jumps by more than a
threshold between runs is treated as a bug, not a discovery,
until proven otherwise.
