# Kalshi Sector — Goals

**Written 2026-08-17. Every number here was measured, not estimated.** Where a
figure is a projection it says so and shows the arithmetic. Re-measure before
citing: this file is point-in-time, and the last version of it was wrong because
it was written before anybody looked.

---

## 0. Why this file exists

The previous goals were written first and measured never. Two of the four were
unreachable by construction, and neither said so — the sector could have run for
a year "making progress" toward a target that no amount of work reaches. The
first step of any goal is finding out what is actually on the table.

So: what the exchange actually contains, then what we can actually do about it.

---

## 1. What was measured (2026-08-17, `--pages 25`, shadow env)

### The feed

| Quantity | Value |
|---|---|
| Events walked | 5,000 |
| Genuine markets (shards excluded) | **48,199** |
| Markets with a real ask | 46,720 |
| Markets with a **two-sided** book | 24,881 |
| Scan runtime | **10 seconds** |

The same walk one week earlier returned 12,000 markets and **zero** two-sided
books. Nothing about the exchange changed. `/markets` cursor-walks return
Kalshi's auto-generated parlay inventory almost exclusively — 59,989 shards in
60,000 rows — and the sector had been measuring that inventory and reporting it
as "the exchange". Every conclusion drawn before this date was drawn from a
population that does not trade.

### Structural arbitrage: **there is none, and that is now proven**

| Quantity | Value |
|---|---|
| Pair locks (YES+NO on one market under $1) | **0** |
| Mutex fields fully quoted | 1,591 |
| Median cost to buy a whole mutex field | **110.0c** |
| Fields costing over 105c | 1,129 |
| Fields costing under 95c | 40 |
| Fields **certified** as locks | **0** |

The 40 cheap fields are not mispricings. `mutually_exclusive` promises *at most*
one leg resolves YES; a lock needs *exactly* one, and Kalshi exposes no flag for
that. `KXLAPRIMARY-01R26` quotes two legs at 12.9c under the exchange's own
clause *"all markets in this event will resolve to No"* — Louisiana moved to a
jungle primary, so there may be no nominee at all. `KXSTATE51-29` prices eight
candidate 51st states at 14.5c total because there will not be a 51st state.
Buying those fields loses 100% of stake. **Structural arbitrage is closed as a
line of business** until a series is *proven* exhaustive from its rulebook.

### The near-certainty band (90–99c), which is the actual business

| Quantity | Value |
|---|---|
| Coherent candidates | **1,920** |
| Dropped: quotes admit no probabilities | **2** |
| Uncheckable: in no testable ladder | 1,057 |
| Closing within 30 days | **0** |
| Closing in 31–90 days | 66 |
| Closing in 91–365 days | 899 |
| Closing beyond a year | 527 |

**The drop count was 441 until the same afternoon, and 439 of those were the
bid-ask spread.** The coherence test compared one rung's ask against another's,
which compares the tops of two brackets of different widths. A quote brackets a
probability; it is not one. Nested pricing is contradicted only when the
brackets cannot be ordered at all — `ask(wide) < bid(narrow)`, which is also the
arbitrage and therefore the proof. Every one of those 439 rows had already
cleared the 10c spread gate, so the sector was discarding **23% of its own
intake** and writing "the quote is not a probability" into the ledger beside
each one. That claim was false 99.5% of the time.

The tell was the 19 ladders quoting an identical ask on every rung. Their *bids*
were 0.80, 0.65, 0.54, 0.48, 0.22, 0.18 — flawlessly monotone. The market was
coherent; the ask was a resting ceiling offer, and `kc.two_sided`'s 10c cap had
already refused every one. Nothing was loosened to get from 1,492 to 1,920: the
empty-book rejection, the spread cap, the arm file and `is_edge_proven` are
untouched. A false charge was dropped.

Breakeven, corrected 2026-08-17 for order size (Kalshi rounds its fee up to a
whole cent **per order**, so per-contract cost falls steeply with size):

| Ask | Breakeven at x1 | Breakeven at x100 | Margin at x100 |
|---|---|---|---|
| 96c | 97.00% | 96.27% | 0.73 pts |
| 98c | 99.00% | 98.14% | 0.86 pts |
| 99c | **100.00%** | **99.07%** | 0.93 pts |

The x1 column is why the scanner used to print *"need >100.0% to break even"*
against every 99c market on the exchange. That is not a fact about Kalshi. It is
a rounding artifact, and it declared the sector's entire reason for existing
mathematically impossible without ever stating a reason.

**The business, stated honestly: a margin under one percentage point wide,
on events that take three months to a year to settle.** That is the whole thing.
It is not obviously worth doing, and the goals below are designed to find out
cheaply rather than to assume.

---

## 2. The goals

Each goal names what would falsify it. A goal you cannot fail is not a goal.

### G1 — Classify everything we surface. *(reachable now)*

**Target:** every candidate the scanner surfaces carries one of three verdicts —
`coherent`, `incoherent`, `uncheckable` — and the `uncheckable` count falls
below **10%** of the band (today: 1,057 of 2,979, **35%**).

**Why this replaces the old G1.** The previous goal was "zero candidates fail
structural coherence". We do not control the exchange's quotes. Rows fail
because the market really does quote them that way; the only lever that drives
that number to zero is loosening the detector, so the old goal could be
*achieved only by corrupting the thing that made it worth achieving*. A goal you
can only hit by breaking your own detector is a bad goal.

Note what happened on the day this was written: the failure count fell 441 → 2
without the detector being loosened at all, because the detector had been
measuring the wrong thing. That is the difference between hitting a target and
correcting a measurement, and it is why the target is now the *uncheckable*
fraction — a number about our own reading, which no amount of exchange
behaviour can move.

`uncheckable`, by contrast, is entirely ours. A rung sits in no ladder because we
could not parse its strike, or because its siblings were not in the page we
walked. That is a gap in our reading, and closing it is work we can do.

**Falsified by:** the uncheckable fraction not falling after a reading fix, which
would mean the rows are genuinely unpartnered and 10% was the wrong target.

### G2 — Accumulate a settled sample honestly. *(long-horizon, and now correctly scaled)*

**Target:** `data/kalshi_predictions.json` reaches **100 settled rows**, at which
point `kalshi_calibration.is_edge_proven()` returns a definite verdict.

**What changed:** this was the old stop condition. It is not reachable in a work
session and pretending otherwise is how a sector ends up reporting progress it
does not have. **Zero markets in the band close within 30 days; 66 close within
90.** Recording every one of those 66 and waiting still leaves us 34 short at the
three-month mark. Realistic first definite verdict: **6–12 months**, i.e. roughly
2027-02 at the earliest.

So the goal splits:

- **G2a (now):** the recorder runs unattended, records every coherent candidate
  with its close date, and settles rows automatically as they resolve. The ledger
  is *correct and growing*.
- **G2b (a projection, restated every month):** at the current rate, n=100
  arrives on *<date>*. Until then `is_edge_proven()` returns NOT YET, **and that
  is the right answer, not a failure to report.**

The one existing prediction row is a `KXMVESPORTS…` parlay shard recorded before
the feed was fixed. It must be voided with a reason, not deleted — a shard's
settlement teaches nothing about our calibration, and silently dropping it would
hide that the ledger was contaminated.

**Falsified by:** the projected date not moving closer month over month, which
would mean the recorder is not actually accumulating.

### G3 — The whole cycle runs without me. *(reachable now — the 10s scan is what unlocked it)*

**Target:** scan → record → settle → verdict runs on the VM cron, and the verdict
reaches the Chairman by email **through `mail_guard`**, never raw `smtplib`.

**Why it was blocked:** the scan took over ten minutes because it re-fetched the
`mutually_exclusive` flag once per event — 3,500 HTTP calls for data already in
hand. At 10 seconds it fits any cron slot. Runtime was not a performance detail;
it was the difference between a sector that runs and one that runs when a human
remembers, and an unrunnable scan produces exactly the same evidence as no scan.

**Falsified by:** a day passing with no scan row and no alert about the absence.

### G4 — The coherence layer stays honest. *(built; now under continuous proof)*

**Target:** `coherence_laws.py` wired into the daily pipeline and `wiring_audit`,
every law carrying a poison pill that proves it can fail, every HOLDS carrying
`n_examined`, deployed to both hosts md5-verified.

**What was added since the original:** `internal_audit_agent.py`, the third line.
The original goal said the laws must exist and be tested. That is not enough — a
law can pass every test and still be incapable of failing. Each law now runs
against an input built to break it; HOLDS on that input means the law is BLIND
and every HOLDS it ever reported was worthless.

**Falsified by:** any law reporting BLIND or UNPROVEN, or any HOLDS with no
denominator.

---

## 3. What would make this sector not worth running

Stated in advance, so the answer is not negotiated after the fact:

1. **Calibration refutes the edge.** If at n≥100 our estimates are no better than
   the market price, there is no business here and the sector closes. This is the
   most likely outcome and the goals are built to reach it cheaply.
2. **The margin closes.** Under 1 point at 99c, a fee change or a wider spread
   erases it entirely. Re-measure the table in §1 monthly.
3. **Settlement risk exceeds the margin.** Kalshi and Polymarket resolved the
   *same* 2024 shutdown event in opposite directions on differing rule wording.
   One adverse resolution at 99c costs 100 winners. This is why
   `MAX_SLEEVE_FRACTION_PER_EVENT` is 5% and why "riskless" is never the word.

None of these is a reason to stop now. All three are reasons the answer must
come from settled outcomes rather than from how good the arithmetic looks.

---

## 4. Standing constraints

Not goals — boundaries, and they do not relax to hit a goal:

- **No bet is placed by Claude.** `data/.kalshi_armed` is a file the Chairman
  creates. `kalshi_client.place_order` raises without it.
- **The empty-book rejection stays.** 94.4% of open markets quote `0.0000` on no
  book. Zero is not a price.
- **`two_sided()` stays.** An ask with no bid has one participant, and their
  asking price is an opinion nobody has met.
- **`is_edge_proven()` never "gets signal".** It returns NOT YET until the sample
  supports a verdict, however long that takes.
- **Widening the aperture is not loosening a gate.** Everything above was won by
  fixing what we *looked at*. Not one filter was relaxed, and the count of
  certified locks went 14 → 0 in the process.
