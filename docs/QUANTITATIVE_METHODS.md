# Quantitative Methods

This document describes how Apex Capital measures its own
predictive model, why the reported AUC is honest, and what
guardrails exist against the single failure mode most likely to
mislead a reader: **remembering a good number and quoting it
after the code has moved on**.

## The one source of truth

Every caller that displays a model AUC reads it through
`model_metrics.get_model_metrics()`, which in turn reads
`data/model_meta.json`. There are no hardcoded numbers.

The function returns:

```python
{
  "trained": bool,      # False if no model exists yet
  "auc": float | None,  # None when trained=False
  ...
}
```

When `trained=False`, callers **must render "N/A"** and never a
number. This is enforced by
`test_pipeline_smoke.py::TestModelMetricsSingleSource`, which walks
the codebase for any string that looks like a hardcoded AUC and
fails the suite if it finds one outside of doctest / comment
blocks.

Why: an earlier version of the firm confidently reported `AUC =
0.67` for months after that number's underlying model had been
retrained twice. The 0.67 was a stale holdout number; the current
walk-forward AUC is approximately **0.58**.

## Walk-forward with embargo

`model_test_harness.py` is the reproducible evaluation. Its
shape:

```python
from sklearn.model_selection import TimeSeriesSplit

splitter = TimeSeriesSplit(
    n_splits=N_SPLITS,
    gap=EMBARGO_DAYS,           # 20 trading days
    test_size=TEST_WINDOW_DAYS, # fixed test window
)
```

The **20-day embargo** is the critical detail. Without it, the
label window (which looks ahead 20 trading days for the return
that becomes `y`) leaks across the fold boundary — the last day
of the train set knows what its `y` looks like partway through
the test set. Plain `KFold` on this time-ordered data leaks the
future by default and inflates AUC by several points.

Any code that computes an AUC and calls `KFold` on the raw
timeseries is a bug. The harness is the only supported evaluator.

## Beat a naive baseline, or it doesn't count

The harness always reports:

- `walk_forward_auc_mean` — the model.
- `baseline_auc_mean` — the same evaluation on a naive momentum
  baseline (`sign(return_20d)`).
- `edge_over_baseline` — the difference.

A model whose AUC is 0.58 but whose baseline is 0.58 has added
nothing. The reported metric is the **edge**, not the level.

## Brier score alongside AUC

AUC ranks; it does not calibrate. A model that outputs `0.99` for
every positive and `0.01` for every negative has a perfect AUC and
useless calibration. `data/model_meta.json` therefore carries a
**Brier score** as well:

```json
{
  "walk_forward_auc_mean": 0.5842,
  "baseline_auc_mean": 0.5691,
  "edge_over_baseline": 0.0151,
  "brier_score": 0.2372,
  "n_test_examples": 1247,
  "sample_confidence": "OK"
}
```

Brier is bounded in `[0, 1]`; lower is better; `0.25` is the
constant-`0.5` predictor. The firm's calibration is only slightly
better than that, and the report says so.

## Sample-size confidence

Small samples make AUC swing run-to-run in ways that look like
signal. The harness attaches one of three flags:

- `OK` — the walk-forward run had enough test examples that
  standard-error math permits the conclusion.
- `LOW_SAMPLE` — the run finished but the confidence interval on
  the AUC crosses the baseline. Report the number but do not act
  on it.
- `SUSPICIOUS_HIGH` — the AUC exceeded a threshold that historically
  correlates with a leakage bug. Investigate before believing.

`SUSPICIOUS_HIGH` has fired for real, twice: once when a feature
used a `.shift(-1)` where a `.shift(1)` was intended, and once
when a merge order left today's label joined to today's features.
Both were caught by the flag, not by the AUC.

## Feature engineering discipline

The firm has a strong prior against adding features:

1. **More labeled data beats more features.** Small samples make
   AUC swing run-to-run. Backfill history (the EDGAR scraper, the
   label builder) before trusting any new feature's number.
2. **Never add a feature straight to `FEATURE_COLS` and assume it
   helps.** Add the column, then run `model_test_harness.py`, and
   compare `walk_forward_auc_mean` + `edge_over_baseline` against
   the prior report at `data/model_test_report.json`.
3. **Keep the feature only if it improves embargoed walk-forward
   AUC and beats baseline.** Aesthetic appeal is not a
   justification.
4. **Record the result in a code comment next to the feature.**
   `feature_engineer.py::FEATURE_COLS` carries empirical notes:
   commodity momentum and bridge features were tried and
   regressed AUC (0.6057 → 0.5842); they are intentionally
   excluded.

The comment discipline exists so that the next contributor who
has the same feature idea can see the previous run's number
attached and decide whether their variant is meaningfully
different, or whether they are about to repeat the same
experiment.

## The `SUSPICIOUS_HIGH` fail-loud

`model_test_harness.py` compares each new AUC against the previous
report at `data/model_test_report.json`. If the new number jumps
by more than a fixed threshold, or crosses `SUSPICIOUS_HIGH`, the
harness raises rather than writing the file. This has caught two
real leakage bugs (above); it has never falsely alarmed on a real
improvement, because real improvements arrive in tenths of a
point.

## What Apex does not claim

- The firm does not claim to beat the market. The walk-forward
  edge over a naive baseline is small.
- The AUC is not an alpha estimate. AUC is a ranking metric; the
  firm's returns depend on execution, position sizing, and
  regime — none of which the harness measures.
- The 0.58 is not a promise. It is a report. If the next feature
  regresses it, the reported number will fall, and the code and
  the docs will move together.

The point of this document is not to make Apex look impressive.
It is to make it verifiable.
