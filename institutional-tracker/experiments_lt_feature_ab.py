"""A/B the LT model's feature set on the firm's own embargoed walk-forward CV.

The bug: feature_engineer.py selected LT_FEATURE_COLS only when
target == "outcome_365d", a column that was never built. The live LT target is
outcome_252d, so the LT model has always trained on all 47 short-term features
instead of its curated 9.

Fixing the identifier is unambiguous. Which feature set is BETTER is not — so
measure it, exactly the way ml_model.py does (TimeSeriesSplit + 252-day
embargo), rather than assuming the curated set wins.

RESULT (2026-08-02, n=79,676, 58.2% positive, 5 folds):
    curated 9  = 0.5205 ± 0.0283
    all 47     = 0.5174 ± 0.0269      delta +0.0031

A null result, and worth stating plainly: the identifier fix was right on its
own terms — the LT model should train on the features chosen for it — but it
bought no measurable accuracy. Both sets sit ~0.52 against a 58.2% base rate,
far below the 0.57 LT_LIVE_AUC_GATE, so nothing goes live either way. Kept in
the repo so the number can be reproduced instead of re-argued; the conclusion
lives next to LT_FEATURE_COLS in feature_engineer.py.

Run:  python3 experiments_lt_feature_ab.py     (from institutional-tracker/)
"""
import os
import sys
import numpy as np
import pandas as pd

# Resolve from __file__, never a host literal: this script was written with the
# Mac's absolute path baked in, which imports nothing on the VM. See memory
# host_divergence_is_a_bug_class.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score

import feature_engineer as fe
from feature_engineer import build_feature_matrix

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    from sklearn.ensemble import RandomForestClassifier
    HAS_LGB = False


def _make():
    if HAS_LGB:
        return lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=4,
                                  max_depth=2, min_child_samples=150, reg_lambda=3.0,
                                  class_weight="balanced", random_state=42, verbosity=-1)
    return RandomForestClassifier(n_estimators=300, max_depth=4, min_samples_leaf=100,
                                  class_weight="balanced", random_state=42, n_jobs=-1)


def walk_forward(X, y, dates):
    """ml_model.py's exact LT procedure: time-ordered TimeSeriesSplit with a
    252-day embargo so the 1-year label window cannot leak across the fold."""
    o = np.argsort(dates, kind="mergesort")
    Xo, yo, do = X[o], y[o], dates[o]
    Xs = StandardScaler().fit_transform(Xo)
    n_splits = min(5, max(2, len(Xo) // 30))
    aucs = []
    for tr, te in TimeSeriesSplit(n_splits=n_splits, gap=0).split(Xs):
        cut = do[te].min() - np.timedelta64(252, "D")
        tr = tr[do[tr] < cut]
        if len(set(yo[te])) < 2 or len(tr) == 0 or len(set(yo[tr])) < 2:
            continue
        m = _make()
        m.fit(Xs[tr], yo[tr])
        aucs.append(roc_auc_score(yo[te], m.predict_proba(Xs[te])[:, 1]))
    return aucs


def momentum_baseline(snaps_meta, y, dates, mom):
    """CLAUDE.md's rule: a model must beat naive momentum to add value."""
    o = np.argsort(dates, kind="mergesort")
    yo, do, mo = y[o], dates[o], mom[o]
    n_splits = min(5, max(2, len(yo) // 30))
    aucs = []
    for tr, te in TimeSeriesSplit(n_splits=n_splits, gap=0).split(yo):
        if len(set(yo[te])) < 2:
            continue
        aucs.append(roc_auc_score(yo[te], mo[te]))
    return aucs


print("Building LT matrix (target=outcome_252d)...")
# use_cache=False: the cache key now includes the feature set, but an A/B that
# silently compares two cache hits from before the fix would prove nothing.
X_all, y_raw, meta = build_feature_matrix(target="outcome_252d", use_cache=False)
y = (np.asarray(y_raw) > 0).astype(int)
dates = pd.to_datetime(meta["snapshot_date"]).values
print(f"  n={len(X_all)}, pos_rate={y.mean():.1%}, cols={X_all.shape[1]}")
print()

results = {}
for label, cols in (("LT_FEATURE_COLS (curated, 9)", fe.LT_FEATURE_COLS),
                    ("FEATURE_COLS (short-term, 47) — the buggy status quo", fe.FEATURE_COLS)):
    # Rebuild the raw frame once per set by re-selecting from the full matrix is
    # not possible (build_feature_matrix already narrowed it), so ask for each
    # set explicitly by patching the resolver.
    real = fe.LT_TARGETS
    fe.LT_TARGETS = frozenset({"outcome_252d"}) if cols is fe.LT_FEATURE_COLS else frozenset()
    try:
        X, yr, m = build_feature_matrix(target="outcome_252d", use_cache=False)
    finally:
        fe.LT_TARGETS = real
    yy = (np.asarray(yr) > 0).astype(int)
    dd = pd.to_datetime(m["snapshot_date"]).values
    aucs = walk_forward(np.asarray(X, dtype=float), yy, dd)
    mean = float(np.mean(aucs)) if aucs else float("nan")
    std = float(np.std(aucs)) if aucs else float("nan")
    results[label] = (mean, std, len(aucs), X.shape[1])
    print(f"{label}")
    print(f"   walk-forward AUC = {mean:.4f} ± {std:.4f}  ({len(aucs)} folds, {X.shape[1]} cols)")
    print(f"   per-fold: {[round(a, 4) for a in aucs]}")
    print()

print("=" * 72)
cur = results["LT_FEATURE_COLS (curated, 9)"][0]
buggy = results["FEATURE_COLS (short-term, 47) — the buggy status quo"][0]
print(f"curated(9) {cur:.4f}  vs  status-quo(47) {buggy:.4f}   delta = {cur - buggy:+.4f}")
print(f"LT live gate = 0.57 — curated {'CLEARS' if cur >= 0.57 else 'does NOT clear'} it")
