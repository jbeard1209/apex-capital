# ML experiment ledger — what was tried, what it scored, what it was worth

The knowledge from `ml_experiment_chain.py` and `ml_chain_r2..r7.py`, kept here
after those files were deleted on 2026-08-22. They were dormant — on no
scheduler, imported by nothing, hardcoding `BASE = Path('/home/jackbeard/…')`
so they only ever ran on the VM — but `ml_chain_r6.py` and `ml_chain_r7.py`
ended their `promote()` with

```python
pickle.dump(d, open(PROD, 'wb'))                    # data/model_v2.pkl
shutil.copy(PROD, BASE / 'data' / 'model.pkl')      # ← the LIVE model
```

so `python3 ml_chain_r7.py` — one command, no flag, no confirmation — replaced
production with a July-3-shaped bundle. That bundle carries no `lt_model` or
`lt_scaler`, so the long-term model would have silently disappeared, and no
chain file writes `model_meta.json`, so `get_model_metrics()` would have kept
reporting the AUC of a model that was no longer on disk. **"Dormant" describes
the scheduler, not the file.** The record below is worth keeping; the loaded
gun was not. Everything is recoverable from git (`git log -- ml_chain_r7.py`).

## The ladder, as the chain recorded it

All figures are **val AUC on one 80/20 split** — that is what the chain
optimised and what it promoted on.

| Round | Variant | Idea | val AUC | Chain's verdict |
|------:|---------|------|--------:|-----------------|
| — | prod v2 | incumbent, XGBoost, 30 features | 0.6068 | benchmark |
| 1 | v2b | drop quarter_of_year + 17 zero-importance cols | 0.5589 | ✗ |
| 1 | v2c | " | 0.5613 | ✗ |
| 1 | v2d | " | 0.5590 | ✗ |
| 2 | v2e | RandomForest, full 30 features | 0.5805 | ✗ |
| 2 | v2g | Optuna optimising TS-CV instead of val | 0.5464 | ✗ |
| 2 | v2h | 5-day target instead of 20-day | 0.4389 | ✗ |
| 2 | v2i | + 5 interaction terms, Optuna→TS-CV | 0.5565 | ✗ |
| 3 | v2j | 100-trial Optuna on the production setup | 0.6009 | **promoted** |
| 4 | v2n | LightGBM, 60-trial Optuna | 0.6094 | promoted |
| 5 | v2p | LightGBM, 100-trial Optuna | 0.6097 | promoted |
| 6 | v2s | LightGBM + 6 hand-crafted interactions | 0.6229 | promoted |
| 7 | v2t / v2u | 15 interactions + squared terms; TS-CV objective | — | never recorded |

## What the ladder is actually worth: nothing

Three separate reasons, and they compound.

**1. Every increment is inside the noise.** The honest, embargoed walk-forward
estimate for this model family is **0.5477 with a standard deviation of
0.0727** (`data/model_meta.json`, 21,775 samples). The promotions above are
+0.0026, +0.0003 and +0.0132. The largest of them is one fifth of a standard
deviation. Running seven rounds and keeping the best val score is max-of-N
selection on a single split: the winner is whichever variant got the luckiest
draw, and the "improvement" does not survive to a new sample.

**2. The metric being optimised is the one the firm already knows is
misleading.** The same model reads 0.6615 on the single 80/20 holdout and
0.5477 walk-forward. `model_metrics.get_model_metrics()` deliberately headlines
the walk-forward number for exactly this reason. The chain promoted on the
holdout.

**3. The benchmark drifted, and once it moved backwards.** Round 3 promoted v2j
at **0.6009 — below the 0.6068 incumbent it was supposed to beat.** The chain's
own header says so: *"NOTE: val=0.6009 < original production 0.6068 — flag this
for chairman review."* It was never flagged to anyone. Round 4's fix was to
hardcode `PROD_VAL_AUC = 0.6068` as "the original production benchmark" so the
comparison could not slip again — which is why six files carried that literal.
By round 6 the pattern had returned as `CURRENT_BEST = 0.6097`, hand-edited
each round; by round 7, `0.6229`. A benchmark maintained by editing a constant
is a benchmark that is stale between edits, and nothing anywhere checks it.

## The rule that came out of this

A promotion decision compares the candidate against **the incumbent as the
single source of truth reports it**, measured **the same way on both sides**,
and refuses when either side is unreadable. Concretely:

- the incumbent's AUC comes from `model_metrics.get_model_metrics()`, never
  from a literal and never from whatever key happens to be inside a pickle;
- both sides are the **embargoed walk-forward** figure, never the holdout;
- an unreadable incumbent means *refuse to promote*, because a fallback number
  is a decision — `0.0` reads as "promote anything";
- an absolute floor applies regardless of the incumbent: nothing at or below
  0.50 reaches production, because "better than a bad model" is not a reason to
  ship an anti-predictive one;
- only a sanctioned trainer writes `data/model.pkl`, and it writes
  `model_meta.json` in the same operation so the artifact and the firm's
  description of it never separate.

Pinned by `test_pipeline_smoke.py::TestAPromotionGateThatNeverBlockedAnything`
and `::TestOnlyASanctionedTrainerWritesTheProductionModel`.

## Feature findings worth keeping

- **`quarter_of_year` carries real signal** on this dataset: removing it drops
  val AUC from 0.6068 to ~0.56 across three independent round-1 variants. It
  was suspected of being seasonal overfit (41% importance); the data says
  otherwise. Do not drop it without re-measuring.
- **Optimising Optuna against TS-CV rather than val produced worse results on
  both metrics** (v2g, v2i). Noted because it is the intuitive thing to try.
- **A 5-day target is much worse than 20-day** (v2h, 0.4389). Shorter horizon
  was not cleaner signal, it was noise.
- **LightGBM edged XGBoost** on val across rounds 4–6, but see the noise
  argument above before treating that as established.
- Two bugs that cost whole rounds: `study.best_params` returns Optuna shorthand
  keys (`'n'`, `'lr'`, `'md'`) which XGBoost silently ignores, so the "tuned"
  model trained on defaults; and refitting a booster that has
  `early_stopping_rounds` set crashes without an `eval_set`. Both are the same
  species — a parameter accepted and discarded without error.
