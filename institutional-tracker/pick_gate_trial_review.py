#!/usr/bin/env python3
"""
Pick-gate trial review — measures the 2026-07-27 "raise position cap 15→20" trial.

Question the trial answers: when we let a few more of the already-vetted picks in
(the cap, not the quality bar, was rejecting them), do those extra picks earn their
seat? We compare:

  • TRIAL picks   — open positions whose ticker is NOT in the original 15 incumbents
  • INCUMBENT book — the 15 names held when the trial started
  • SPY           — passive benchmark over the same window

Return is measured from each position's avg entry to current price (unrealized),
which is the honest "how are these picks doing" number for names still held. Run:

    python3 pick_gate_trial_review.py             # human summary, no side effects
    python3 pick_gate_trial_review.py --json      # machine-readable
    python3 pick_gate_trial_review.py --notify    # the scheduled entrypoint

`--notify` is safe to run DAILY. It reads the manifest, returns 0 without side
effects until `review_on` arrives, and on the due date settles the manifest —
either extending it (HOLD, at most MAX_EXTENSIONS times) or closing it with a
verdict. It used to be scheduled as a single yearly cron fire on the review
date; that fire raised FileNotFoundError on 2026-08-17 and the trial's only
scheduled attempt was gone. See _load_manifest.

Revert the trial by setting MAX_OPEN_POSITIONS back to 15 in config.py. This
script never edits config.py itself — that is a live risk limit and the change
is the Chairman's, so terminal verdicts land in the decision queue instead.
"""
import json, sys, os, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(BASE, "data", "pick_gate_trial.json")
CHAIRMAN_QUEUE = os.path.join(BASE, "data", "chairman_action_required.json")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _load_manifest():
    """(manifest, error). A one-shot review must never die into a log file.

    2026-08-17 13:00 UTC: this fired on the VM, where the cron lives, and raised
    FileNotFoundError — the manifest is written by the Mac and there is no state
    sync between hosts, so the trial's own record was on the other machine. The
    traceback went to logs/pick_gate_trial.log and nowhere else. The review was
    scheduled ONCE, for a date that has now passed, so the failure consumed the
    only attempt: the cap sat at 20 past its review date with nobody measuring
    it, and every surface said the trial was ACTIVE and on schedule.

    The manifest is now in deploy.sh's data/ allowlist so both hosts carry it.
    This function is the second half — if it is ever missing again, the run says
    so where a person will see it instead of raising into a file nobody reads.
    """
    try:
        with open(MANIFEST) as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, (f"{MANIFEST} does not exist on this host. The trial "
                      f"manifest is written by the Mac; if this is the VM, the "
                      f"deploy allowlist in gcp_setup/deploy.sh no longer "
                      f"carries data/pick_gate_trial.json.")
    except (ValueError, OSError) as exc:
        return None, f"{MANIFEST} is unreadable ({type(exc).__name__}: {exc})."


def _upsert_chairman_row(rid: str, severity: str, description: str) -> bool:
    """One keyed row in the Chairman's queue. Upserts; never appends a second.

    Keyed by `id` so a daily cron re-raising the same unresolved thing updates
    one row instead of growing the queue by one per day — the queue is already
    128 items deep and a detector that floods it is a detector people stop
    reading.
    """
    from datetime import datetime as _dt
    try:
        with open(CHAIRMAN_QUEUE) as fh:
            rows = json.load(fh)
        if not isinstance(rows, list):
            return False
    except (ValueError, OSError):
        return False
    row = {"id": rid, "severity": severity, "source": "pick_gate_trial_review",
           "ts": _dt.now().isoformat(), "description": description}
    for i, existing in enumerate(rows):
        if isinstance(existing, dict) and existing.get("id") == rid:
            rows[i] = {**existing, **row}
            break
    else:
        rows.append(row)
    try:
        with open(CHAIRMAN_QUEUE, "w") as fh:
            json.dump(rows, fh, indent=2)
        return True
    except OSError:
        return False


def _escalate_unreviewable(why: str) -> bool:
    return _upsert_chairman_row(
        "pick-gate-trial-unreviewable", "HIGH",
        "Pick-gate trial cannot be reviewed — " + why +
        " The cap is live at MAX_OPEN_POSITIONS and its review date has "
        "passed unmeasured.")


def _queue_decision(verdict: str, why: str, o: dict, disposition: str) -> bool:
    """The verdict wants a change to a live risk limit. Put it where it sticks."""
    action = {
        "REVERT": "Set MAX_OPEN_POSITIONS back to 15 in config.py (does not force-sell).",
        "EXTEND": "Keep MAX_OPEN_POSITIONS at 20, or raise it further.",
    }.get(verdict,
          "Decide whether MAX_OPEN_POSITIONS stays at 20 or returns to 15. "
          "The trial cannot answer it.")
    w = o.get("window") or {}
    if w.get("usable"):
        # The reconciled full-cohort numbers ARE the scoreboard. The
        # survivors-only line rides along because every earlier queue row and
        # email quoted it, and a reader comparing the two needs both to see
        # that they measure different things rather than that one is "newer".
        board = ("Full-cohort window — trial %+.2f%% ($%+.0f, %d names, %d "
                 "closed) vs incumbents %+.2f%% ($%+.0f, %d names); residual "
                 "%.2f%% of equity. Survivors-only (the number earlier reports "
                 "quoted) — trial %s (n=%d), incumbents %s (n=%d of %d still "
                 "held). SPY %s."
                 % (w["trial"]["return_pct"] if w["trial"]["return_pct"] is not None else float("nan"),
                    w["trial"]["pnl"], w["trial"]["n"], w["trial"]["n_closed"],
                    w["incumbent"]["return_pct"] if w["incumbent"]["return_pct"] is not None else float("nan"),
                    w["incumbent"]["pnl"], w["incumbent"]["n"], w["residual_pct"],
                    _fmt(o["trial_avg_return_pct"]), o["n_trial_picks"],
                    _fmt(o["incumbent_avg_return_pct"]), o["n_incumbents"],
                    len(o.get("incumbents_at_start") or []),
                    _fmt(o["spy_return_since_start_pct"])))
    else:
        board = ("Scoreboard — trial %s (n=%d), incumbents %s (n=%d of %d still "
                 "held), SPY %s. The full-cohort measurement was unavailable: %s"
                 % (_fmt(o["trial_avg_return_pct"]), o["n_trial_picks"],
                    _fmt(o["incumbent_avg_return_pct"]), o["n_incumbents"],
                    len(o.get("incumbents_at_start") or []),
                    _fmt(o["spy_return_since_start_pct"]),
                    w.get("why") or "not attempted."))
    return _upsert_chairman_row(
        "pick-gate-trial-verdict", "HIGH",
        "Pick-gate trial verdict: %s. %s %s %s Trial %s."
        % (verdict, why, board, action, disposition))


def _escalate_pre_held(o: dict) -> bool:
    """A name in the book before the trial is not evidence about the trial.

    Kept separate from the verdict row because it is a defect in the trial's own
    record, not an input to the cap decision: the manifest declared its
    incumbents while the book held one more name, so the extra one was scored as
    a cap admission. Worth fixing before the next trial is designed, and it
    outlives whatever the Chairman decides about MAX_OPEN_POSITIONS.
    """
    w = o.get("window") or {}
    pre = w.get("pre_held") or {}
    if not w.get("usable") or not pre.get("n"):
        return False
    declared = len(o.get("incumbents_at_start") or [])
    return _upsert_chairman_row(
        "pick-gate-trial-manifest-incomplete", "MEDIUM",
        "Pick-gate trial manifest listed %d incumbents but the book held %d "
        "names when the trial opened. %s was scored as a cap admission and "
        "contributed $%+.0f to the trial arm despite predating the cap change. "
        "Excluded from both arms now; the manifest's `incumbents` list is still "
        "wrong and any future trial built the same way will repeat this."
        % (declared, declared + pre["n"], ", ".join(pre["tickers"]), pre["pnl"]))


def _spy_return(start):
    """Return % change of SPY since `start`, or None. Never NaN.

    2026-08-19: this benchmark had been reporting `nan` and the summary printed
    it as "+nan%", because two things were true at once and neither raised:

      • `spy["Close"]` is a one-column DataFrame under yfinance's multi-index
        columns, not a Series, so `.iloc[-1]` is a row and not a price;
      • the last row was TODAY's not-yet-closed bar, whose Close is NaN.

    `float(NaN)` succeeds, `NaN is not None` is True, and `isinstance(NaN,
    float)` is True — so the value sailed through the guard, the formatter and
    the email. The benchmark arm of the experiment silently stopped existing
    while still occupying its line in the scoreboard. Drop the empty bars,
    then refuse to return a NaN at all (see memory `nan-is-not-none`).
    """
    try:
        import math
        import yfinance as yf
        spy = yf.download("SPY", start=start, progress=False, auto_adjust=True)
        if spy is None or spy.empty:
            return None
        close = spy["Close"]
        if hasattr(close, "columns"):            # DataFrame → take the one column
            close = close.iloc[:, 0]
        close = close.dropna()                   # an unfilled bar is not a price
        if len(close) < 2:
            return None
        pct = float((close.iloc[-1] / close.iloc[0] - 1) * 100)
        return None if math.isnan(pct) or math.isinf(pct) else pct
    except Exception:
        return None


#: A HOLD may buy more time, but not forever — see _settle().
HOLD_EXTENSION_DAYS = 21
MAX_EXTENSIONS = 2

#: Residual tolerance for _window_pnl's reconciliation, as % of starting equity.
#: Sized against the bug it exists to catch: seeding SH's start position at zero
#: left a residual of 5.8% of equity, so 1.0% catches that with room to spare
#: while absorbing dividends, fees and the yfinance-close-vs-Alpaca-mark gap
#: (measured 0.16% on 111 fills across 50 tickers).
RECON_TOLERANCE_PCT = 1.0


def _capital_at_risk(fills, start_qty, start_mv, end_mv, tickers, t0, t1) -> dict:
    """AVERAGE capital each ticker tied up — the only denominator that compares.

    Two wrong answers were tried first, and both bent the result the same way:

      • "start value + everything bought" double-charges a covered short, because
        covering IS a buy. Six covered incumbent shorts put $199k of basis under
        a book that opened at $88k, halving the control arm's return.
      • "capital ever committed" charges a name again every time it is re-opened.
        The trial arm turned over 17 of 34 names in four weeks and the control
        arm barely traded, so cumulative commitment penalises exactly the arm
        that traded — which is not a property of the picks, only of the churn.

    Both mistakes flattered the trial arm. The honest denominator is average
    capital at risk: integrate |position value| over the window and divide by its
    length. A name held at $10k for half the window costs $5k of average capital,
    a name round-tripped in a day costs almost nothing, and covering a short
    releases capital instead of consuming it. Then "return" means return per
    dollar-year of exposure, which is the same question for both arms however
    differently they traded.

    Prices between fills are carried forward from the last observed price (start
    close, then each fill, then the current mark). That is a step function where
    the truth is a continuous path, so a name that moved hard mid-segment is
    slightly mis-weighted — it cannot change a sign, only a decimal.
    """
    span = max((t1 - t0).total_seconds(), 1.0)
    dollar_secs = {t: 0.0 for t in tickers}
    def _start_px(t):
        # A quantity too small to have been priced above must not index into a
        # dict that skipped it — that KeyError would be swallowed by the caller
        # and reported as "the whole measurement is unavailable".
        q = start_qty.get(t, 0.0)
        return abs(start_mv[t] / q) if (q and t in start_mv) else 0.0

    state = {t: (start_qty.get(t, 0.0), _start_px(t), t0) for t in tickers}
    for o in sorted(fills, key=lambda f: (f.filled_at is None, f.filled_at)):
        t = o.symbol
        if t not in state or o.filled_at is None:
            continue
        qty, px, since = state[t]
        ts = o.filled_at if o.filled_at.tzinfo else o.filled_at.replace(tzinfo=t0.tzinfo)
        ts = min(max(ts, t0), t1)
        dollar_secs[t] += abs(qty) * px * (ts - since).total_seconds()
        q, p = float(o.filled_qty), float(o.filled_avg_price)
        state[t] = (qty + (q if "buy" in str(o.side).lower() else -q), p, ts)
    for t in tickers:
        qty, px, since = state[t]
        # close the final segment at the current mark, not the last fill price
        final = abs(end_mv.get(t, 0.0)) or abs(qty) * px
        dollar_secs[t] += final * max((t1 - since).total_seconds(), 0.0)
    return {t: dollar_secs[t] / span for t in tickers}


def _window_pnl(started: str) -> dict:
    """Dollar P&L per ticker over [started, now], sourced entirely from the broker.

    Why this exists: the scoreboard above measures *percent return since each
    position's own avg entry, for positions still open*. That is three different
    windows and a survivorship filter wearing one label.

      • Incumbents' avg_entry predates the trial, so their number covers a longer
        and unknown period than the trial picks' does.
      • A name opened and closed inside the window vanishes from both arms. 17 of
        the 34 cap-admitted names had already closed by 2026-08-24 — the trial arm
        was dropping half its own cohort, the same decay it condemns in the control.
      • USB was added to after the trial started, so the broker's avg entry sits
        15.06% above the frozen one and the two sources disagree by 14.65pp.

    The identity below has none of those problems::

        P&L = end_MV - start_MV + (sell proceeds - buy costs)

    It is exact for shorts, adds, partial exits and re-entries, and it needs no
    FIFO lot matching. Both arms get the same window because the window is the
    only thing that defines it.

    `start_qty` is DERIVED, never read from the manifest: end_qty minus the net
    quantity the fills moved IS the starting quantity, by conservation. That is
    what caught the trial's largest single "winner" — SH, +$4,830, 54% of the
    trial arm's P&L — as a position held before the trial began. The manifest
    declared 15 incumbents; the book held 16 names. A control arm assembled from
    the manifest could never have noticed, because the manifest was the thing
    that was wrong.

    Returns a dict with `error` set when the measurement is not trustworthy. It
    never returns a partial number with the error suppressed: a degraded read
    that substitutes a plausible constant is a decision to act on data we do not
    have (memory: `fallback-that-means-act`).
    """
    from collections import defaultdict
    out = {"error": None, "pnl": {}, "start_qty": {}, "capital": {},
           "closed": {}, "n_fills": 0, "residual": None, "residual_pct": None,
           "equity_start": None, "equity_now": None}
    try:
        from alpaca_trader import AlpacaTrader
        from alpaca.trading.requests import (GetOrdersRequest,
                                             GetPortfolioHistoryRequest)
        from alpaca.trading.enums import QueryOrderStatus
        client = AlpacaTrader().client

        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=datetime.fromisoformat(started + "T00:00:00"), limit=500))
        fills = [o for o in orders
                 if str(o.status).endswith("FILLED") and o.filled_qty
                 and o.filled_avg_price]
        if not fills:
            out["error"] = ("the broker returned no filled orders since %s, so "
                            "the window cannot be reconstructed" % started)
            return out

        dq, cash = defaultdict(float), defaultdict(float)
        for o in fills:
            q, p = float(o.filled_qty), float(o.filled_avg_price)
            buy = "buy" in str(o.side).lower()
            dq[o.symbol] += q if buy else -q
            cash[o.symbol] += -(q * p) if buy else (q * p)
        out["n_fills"] = len(fills)

        end_qty, end_mv = defaultdict(float), defaultdict(float)
        for p in client.get_all_positions():
            end_qty[p.symbol] = float(p.qty)
            end_mv[p.symbol] = float(p.market_value)

        tickers = sorted(set(dq) | set(end_qty))
        start_qty = {t: end_qty[t] - dq[t] for t in tickers}
        held_at_start = [t for t in tickers if abs(start_qty[t]) > 1e-6]

        # Price the opening book at the last close STRICTLY BEFORE the window,
        # so the whole of the first day belongs to the window — its fills
        # already do.
        start_mv = {}
        if held_at_start:
            import math
            import yfinance as yf
            hist = yf.download(held_at_start, start="2026-01-01", end=started,
                               progress=False, auto_adjust=True)["Close"]
            for t in held_at_start:
                s = (hist[t] if hasattr(hist, "columns") else hist).dropna()
                if not len(s):
                    out["error"] = ("no pre-window close for %s, which was held "
                                    "at the start — its P&L would be credited to "
                                    "whichever arm holds it now" % t)
                    return out
                px = float(s.iloc[-1])
                if math.isnan(px) or math.isinf(px):
                    out["error"] = "pre-window close for %s is not a number" % t
                    return out
                start_mv[t] = start_qty[t] * px

        out["start_qty"] = {t: start_qty[t] for t in held_at_start}
        out["pnl"] = {t: end_mv[t] - start_mv.get(t, 0.0) + cash[t] for t in tickers}
        out["closed"] = {t: abs(end_qty[t]) < 1e-6 for t in tickers}
        from datetime import timezone
        t0 = datetime.fromisoformat(started + "T00:00:00").replace(tzinfo=timezone.utc)
        out["capital"] = _capital_at_risk(fills, start_qty, start_mv, end_mv,
                                          tickers, t0,
                                          datetime.now(timezone.utc))
        out["window_days"] = round(
            (datetime.now(timezone.utc) - t0).total_seconds() / 86400, 2)

        # ---- the check that can fail -------------------------------------
        acct = client.get_account()
        ph = client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
        eq = [e for e in (ph.equity or []) if e]
        if not eq or not acct.equity:
            out["error"] = ("no equity series to reconcile against — the "
                            "per-ticker sum would be unchecked arithmetic")
            return out
        out["equity_start"], out["equity_now"] = float(eq[0]), float(acct.equity)
        out["residual"] = ((out["equity_now"] - out["equity_start"])
                           - sum(out["pnl"].values()))
        out["residual_pct"] = abs(out["residual"]) / out["equity_start"] * 100
        if out["residual_pct"] > RECON_TOLERANCE_PCT:
            out["error"] = (
                "reconciliation failed: per-ticker P&L sums to %+.2f but equity "
                "moved %+.2f, a residual of %+.2f (%.2f%% of starting equity, "
                "tolerance %.2f%%). Some position's start or end value is wrong, "
                "so no arm total below can be believed."
                % (sum(out["pnl"].values()),
                   out["equity_now"] - out["equity_start"],
                   out["residual"], out["residual_pct"], RECON_TOLERANCE_PCT))
        return out
    except Exception as exc:                      # noqa: BLE001 — see docstring
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out


def _settle(m, verdict: str, why: str) -> str:
    """Advance the manifest past this review. Returns a one-line disposition.

    A trial that stays ACTIVE after its review date is indistinguishable from a
    trial that is still running, which is how the raised cap sat live and
    unmeasured past 2026-08-17. Every review therefore MOVES the manifest:
    either to a new dated window (HOLD, at most MAX_EXTENSIONS times) or to a
    terminal REVIEWED state carrying the verdict.

    Terminal never changes config.py. REVERT means lowering a live risk limit,
    which is the Chairman's call and not this script's — it escalates instead.
    """
    from datetime import date, timedelta
    m["last_reviewed"] = datetime.now().isoformat()
    m["verdict"] = verdict
    m["verdict_why"] = why

    if verdict == "HOLD" and m.get("extensions", 0) < MAX_EXTENSIONS:
        m["extensions"] = m.get("extensions", 0) + 1
        try:
            base = date.fromisoformat(m.get("review_on", ""))
        except ValueError:
            base = date.today()
        base = max(base, date.today())
        m["review_on"] = (base + timedelta(days=HOLD_EXTENSION_DAYS)).isoformat()
        disposition = (f"extended to {m['review_on']} "
                       f"({m['extensions']}/{MAX_EXTENSIONS} extensions used)")
    else:
        m["status"] = "REVIEWED"
        m["closed_on"] = datetime.now().date().isoformat()
        disposition = ("closed — no further reviews will run" if verdict != "HOLD"
                       else f"closed — {MAX_EXTENSIONS} extensions used without a "
                            f"signal; the cap needs a decision either way")
    try:
        with open(MANIFEST, "w") as fh:
            json.dump(m, fh, indent=2)
    except OSError as exc:
        return f"{disposition} (BUT THE MANIFEST COULD NOT BE WRITTEN: {exc})"
    return disposition


def review() -> dict:
    m, err = _load_manifest()
    if err:
        raise FileNotFoundError(err)
    incumbents = set(m.get("incumbents", []))

    from alpaca_trader import AlpacaTrader
    trader = AlpacaTrader()
    pos = trader.get_positions()
    # `weight_pct` is not a column get_positions() builds, so the old
    # `r.get("weight_pct", 0)` printed every trial pick as "(wt +0.0%)".
    # None, not 0.0, when there is no denominator — a weight of zero is a
    # claim, and "we could not measure it" is not that claim.
    portfolio_value = float(trader.get_account().get("portfolio_value", 0) or 0)

    trial_rows, incumbent_rows = [], []
    if not pos.empty:
        for _, r in pos.iterrows():
            tkr = r["ticker"]
            pnl = r.get("unrealized_pct")
            pnl = float(pnl) if pnl is not None else None
            wt  = (abs(float(r["market_value"])) / portfolio_value * 100
                   if portfolio_value > 0 else None)
            row = {"ticker": tkr, "unrealized_pnl_pct": pnl,
                   "weight_pct": wt}
            (incumbent_rows if tkr in incumbents else trial_rows).append(row)

    spy_ret = _spy_return(m.get("started"))

    # The full-cohort, single-window measurement. Everything above this line is
    # survivors-only percent-since-entry and is kept because it is what every
    # earlier report said — where the two disagree, the disagreement is the
    # finding, not something to average away.
    w = _window_pnl(m.get("started") or "2026-07-27")
    arms = _classify_arms(w, incumbents)

    out = {
        "trial": m.get("trial"), "started": m.get("started"),
        "review_on": m.get("review_on"), "as_of": datetime.now().isoformat(),
        "prior_cap": m.get("prior_cap"), "trial_cap": m.get("trial_cap"),
        "n_trial_picks": len(trial_rows), "n_incumbents": len(incumbent_rows),
        # The declared control arm, so a reader can see how much of it is left.
        # `n_incumbents` counts only those STILL HELD; the two diverge as the
        # book turns over, and that divergence is itself the finding.
        "incumbents_at_start": sorted(incumbents),
        "extensions": m.get("extensions", 0),
        "trial_picks": sorted(trial_rows, key=lambda x: x["ticker"]),
        "trial_avg_return_pct": _mean([r["unrealized_pnl_pct"] for r in trial_rows]),
        "incumbent_avg_return_pct": _mean([r["unrealized_pnl_pct"] for r in incumbent_rows]),
        "spy_return_since_start_pct": spy_ret,
        "window": arms,
    }
    return out


#: An arm's return needs enough capital behind it to mean anything.
MIN_ARM_CAPITAL = 1000.0


def _classify_arms(w: dict, incumbents: set) -> dict:
    """Split the window P&L into TRIAL / INCUMBENT / PRE_HELD.

    PRE_HELD is the arm that should always be empty and was not. A ticker with a
    non-zero starting quantity was in the book before the cap was raised, so the
    cap did not admit it and it is not evidence about the cap — whatever the
    manifest's `incumbents` list says. On 2026-08-24 that arm held SH, whose
    +$4,830 had been counted as the trial's biggest win.
    """
    if w.get("error"):
        return {"usable": False, "why": w["error"], "n_fills": w.get("n_fills", 0),
                "residual": w.get("residual"), "residual_pct": w.get("residual_pct")}
    pre = set(w["start_qty"]) - set(incumbents)
    arms = {}
    for name, members in (
            ("trial", [t for t in w["pnl"] if t not in incumbents and t not in pre]),
            ("incumbent", [t for t in w["pnl"] if t in incumbents]),
            ("pre_held", sorted(pre))):
        pnl = sum(w["pnl"][t] for t in members)
        cap = sum(w["capital"][t] for t in members)
        arms[name] = {
            "n": len(members),
            "n_closed": sum(1 for t in members if w["closed"].get(t)),
            "pnl": round(pnl, 2),
            "capital": round(cap, 2),
            # No denominator, no percentage. A return computed on $0 of capital
            # is not 0.0%, it is absent (memory: `nan-is-not-none`).
            "return_pct": (round(pnl / cap * 100, 2)
                           if cap >= MIN_ARM_CAPITAL else None),
            "tickers": sorted(members),
        }
    arms.update(usable=True, why=None, n_fills=w["n_fills"],
                window_days=w.get("window_days"),
                residual=round(w["residual"], 2),
                residual_pct=round(w["residual_pct"], 3),
                equity_start=round(w["equity_start"], 2),
                equity_now=round(w["equity_now"], 2))
    return arms


def _fmt(x):
    # `isinstance(nan, float)` is True and f"{nan:+.2f}%" renders "+nan%", which
    # reads as a measurement. Belt to _spy_return's braces: a missing number is
    # spelled "n/a" here no matter which upstream let one through.
    import math
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return "n/a"
    return "n/a" if math.isnan(x) or math.isinf(x) else f"{x:+.2f}%"


#: Below this many names still held, the control arm is not a control arm.
MIN_CONTROL_ARM = 5


#: What the comparison can and cannot support, appended to every window verdict.
#: Both arms are now measured over one window with no survivorship, which fixes
#: the arithmetic — it does not make this a randomised experiment. Names entered
#: the trial arm by being bought during the window and the control arm by having
#: been held before it, so the arms differ in vintage and in turnover, and the
#: firm chose which incumbents to sell. The number below is "new money vs the
#: legacy book", which is worth knowing; it is not the shadow value of seats
#: 16-20. That would need the admission ranking of each pick, so the picks the
#: cap actually decided can be separated from the ones any cap would have taken.
_NOT_IDENTIFIED = (" Identification caveat: arms differ by vintage and turnover "
                   "and assignment was not random, so this measures new money "
                   "against the legacy book, not the cap's marginal effect.")


def _verdict_from_window(w: dict) -> tuple:
    """Verdict from the full-cohort, single-window, reconciled measurement."""
    tr, ic, pre = w["trial"], w["incumbent"], w["pre_held"]
    if tr["n"] == 0:
        return ("INCONCLUSIVE",
                "No pick was admitted during the trial window at all — the cap "
                "was not the binding constraint. Investigate the pod→manager "
                "path before extending.")
    if tr["return_pct"] is None or ic["return_pct"] is None:
        return ("INCONCLUSIVE",
                "One arm has under $%.0f of capital behind it (trial $%.0f, "
                "incumbent $%.0f), so its return has no denominator worth "
                "dividing by." % (MIN_ARM_CAPITAL, tr["capital"], ic["capital"]))

    edge = tr["return_pct"] - ic["return_pct"]
    scope = ("Measured over one window on the full cohort: trial %+.2f%% "
             "($%+.0f on $%.0f committed, %d names, %d already closed) vs "
             "incumbents %+.2f%% ($%+.0f on $%.0f, %d names)."
             % (tr["return_pct"], tr["pnl"], tr["capital"], tr["n"], tr["n_closed"],
                ic["return_pct"], ic["pnl"], ic["capital"], ic["n"]))
    if pre["n"]:
        scope += (" %d name(s) held before the trial (%s) were excluded from "
                  "both arms; the manifest had listed neither them nor a reason."
                  % (pre["n"], ", ".join(pre["tickers"])))

    if edge >= 1.0:
        return ("EXTEND", "Trial picks beat incumbents by %+.1fpp. %s%s"
                % (edge, scope, _NOT_IDENTIFIED))
    if edge <= -1.0:
        return ("REVERT", "Trial picks lag incumbents by %+.1fpp. %s%s"
                % (edge, scope, _NOT_IDENTIFIED))
    return ("HOLD", "Trial picks ≈ incumbents (%+.1fpp). %s%s"
            % (edge, scope, _NOT_IDENTIFIED))


def _verdict(o) -> tuple:
    """Return (verdict, one-line rationale) comparing trial picks to incumbents.

    Verdicts: EXTEND / REVERT / HOLD / INCONCLUSIVE / UNMEASURABLE.
    Only HOLD is non-terminal — everything else wants a decision, now.

    Prefers the reconciled full-cohort window measurement. The survivors-only
    path below it is the fallback for when the broker cannot be reached, and it
    keeps the decayed-arm guard: a 2-name control arm must not produce a verdict
    just because the richer measurement was unavailable.
    """
    w = o.get("window") or {}
    if w.get("usable"):
        return _verdict_from_window(w)

    t, inc = o["trial_avg_return_pct"], o["incumbent_avg_return_pct"]
    n_ctrl, n_start = o["n_incumbents"], len(o.get("incumbents_at_start") or [])

    if o["n_trial_picks"] == 0:
        return ("INCONCLUSIVE",
                "No trial pick ever filled a 16th+ slot — the cap wasn't the "
                "only thing gating buys, or no new pick qualified. Investigate the "
                "pod→manager path before extending.")

    # The control arm decays. Incumbents get sold; nothing ever adds one back,
    # because membership was fixed at 2026-07-27 by definition. So "extend the
    # window and look again" — the old HOLD advice — makes this comparison
    # strictly WORSE with every passing day, not better. By 2026-08-19 the arm
    # was 3 of the original 15, and a 3-name average against an 18-name average
    # is not an experiment; it is two numbers. Time cannot repair it, so this
    # branch never extends — it hands the decision back. (memory:
    # `sample-outlived-its-rule` — a verdict about a rule whose sample is gone.)
    if n_ctrl < MIN_CONTROL_ARM:
        return ("UNMEASURABLE",
                f"The control arm has decayed to {n_ctrl} of the original "
                f"{n_start or '?'} incumbents — below the {MIN_CONTROL_ARM}-name "
                f"floor. Incumbent membership was frozen at the trial's start and "
                f"only ever shrinks, so waiting longer cannot restore it. The "
                f"full-cohort window measurement, which does not decay, was "
                f"unavailable this run ({w.get('why') or 'not attempted'}), so "
                f"there is nothing left to compare. The cap needs a judgement "
                f"call, not another window.")

    if t is None or inc is None:
        return ("INCONCLUSIVE", "Not enough return data yet to judge.")
    edge = t - inc
    if edge >= 1.0:
        return ("EXTEND", f"Trial picks beat incumbents by {edge:+.1f}pp — the extra "
                          "seats are earning their place. Consider bumping the cap further.")
    if edge <= -1.0:
        return ("REVERT", f"Trial picks lag incumbents by {edge:+.1f}pp — the extra "
                          "seats diluted quality. Set MAX_OPEN_POSITIONS back to 15.")
    return ("HOLD", f"Trial picks ≈ incumbents ({edge:+.1f}pp) — no clear signal "
                    f"yet across {o['n_trial_picks']} trial vs {n_ctrl} control names.")


def _pct(x):
    """Percent or 'n/a' — never a bare number that might be None."""
    return "n/a" if x is None else "%+.2f%%" % x


def _window_html(o) -> str:
    """The full-cohort block, or an honest explanation of why there isn't one."""
    w = o.get("window") or {}
    if not w.get("usable"):
        return ("<h3>Full-cohort window measurement</h3>"
                f"<p style='color:#b00'>Unavailable: {w.get('why') or 'not attempted.'} "
                "The survivors-only scoreboard below is all there is this run, and "
                "it measures three different windows with closed names dropped.</p>")
    tr, ic, pre = w["trial"], w["incumbent"], w["pre_held"]
    extra = ""
    if pre["n"]:
        extra = (f"<li style='color:#b00'>Excluded — held before the trial, so the "
                 f"cap did not admit them: {', '.join(pre['tickers'])} "
                 f"(${pre['pnl']:+,.0f})</li>")
    # Built outside the f-string: py3.9 cannot nest the same quote character
    # inside one, and the Mac is py3.9 (memory: host_divergence_is_a_bug_class).
    span = " , %.0f days" % w["window_days"] if w.get("window_days") else ""
    span = span.replace(" ,", ",")
    return (
        f"<h3>Full-cohort window ({o['started']} → today{span}, "
        f"same window both arms)</h3>"
        f"<p style='color:#888'>Return is P&amp;L per dollar of AVERAGE capital at "
        f"risk over the window — not annualised, and not comparable to a "
        f"since-entry number.</p>"
        f"<ul><li>Trial picks: {_pct(tr['return_pct'])} "
        f"(${tr['pnl']:+,.0f} on ${tr['capital']:,.0f} committed; {tr['n']} names, "
        f"{tr['n_closed']} already closed)</li>"
        f"<li>Incumbents: {_pct(ic['return_pct'])} "
        f"(${ic['pnl']:+,.0f} on ${ic['capital']:,.0f}; {ic['n']} names, "
        f"{ic['n_closed']} closed — no decay, all are counted)</li>{extra}</ul>"
        f"<p style='color:#888'>Reconciled against the account: {w['n_fills']} fills, "
        f"residual ${w['residual']:+,.2f} ({w['residual_pct']:.2f}% of starting "
        f"equity, tolerance {RECON_TOLERANCE_PCT:.2f}%).</p>")


def notify():
    """Compose the review + verdict and email it through the guarded mail path.

    Safe to run daily: returns 0 without side effects until the manifest says
    the review is due, and settles the manifest once it is.
    """
    m, err = _load_manifest()
    if err:
        queued = _escalate_unreviewable(err)
        print("CANNOT REVIEW: %s%s" % (err, "" if queued else
              "  (and the Chairman queue could not be written either)"))
        return 2
    if m.get("status") != "ACTIVE":
        print("Trial no longer ACTIVE (%s) — nothing to review." % m.get("status"))
        return 0

    # Due-date gate. The cron is DAILY now, not the single yearly fire that
    # 2026-08-17 spent on a traceback: a job that gets one attempt has no
    # margin for a bad day. Running daily and returning early is idempotent
    # and costs a file read, and the review date becomes a property of the
    # manifest rather than of a crontab line nobody re-reads.
    from datetime import date
    try:
        due = date.fromisoformat(m.get("review_on", ""))
    except ValueError:
        due = date.today()          # an unparseable date is due, not deferred
    if date.today() < due:
        print("Trial review not due until %s — no action." % due.isoformat())
        return 0

    o = review()
    v, why = _verdict(o)
    disposition = _settle(m, v, why)
    print("Verdict %s — %s" % (v, disposition))
    # EXTEND, REVERT and UNMEASURABLE all mean "a person must now change, or
    # deliberately keep, a live risk limit". The email can be missed; the queue
    # cannot. HOLD-with-extensions-left is the one case that needs no decision.
    if v != "HOLD" or m.get("status") == "REVIEWED":
        _queue_decision(v, why, o, disposition)
    # Fires regardless of verdict: a mis-declared control arm is a defect in the
    # trial's record and stays true whichever way the cap decision goes.
    _escalate_pre_held(o)
    picks = "".join(
        f"<li>{r['ticker']}: {_fmt(r['unrealized_pnl_pct'])} (wt {_fmt(r['weight_pct'])})</li>"
        for r in o["trial_picks"]) or "<li>none admitted</li>"
    body = (
        f"<h2>Pick-gate trial review — verdict: {v}</h2>"
        f"<p>{why}</p>"
        f"<p>Trial ran {o['started']} → {o['review_on']}, cap {o['prior_cap']}→{o['trial_cap']}.</p>"
        f"<p><b>Disposition:</b> {disposition}</p>"
        f"{_window_html(o)}"
        f"<h3>Survivors-only (percent since each position's own entry)</h3><ul>"
        f"<li>Trial picks: {_fmt(o['trial_avg_return_pct'])} "
        f"(n={o['n_trial_picks']})</li>"
        f"<li>Incumbents: {_fmt(o['incumbent_avg_return_pct'])} "
        f"(n={o['n_incumbents']} of {len(o.get('incumbents_at_start') or [])} "
        f"still held — the control arm only shrinks)</li>"
        f"<li>SPY: {_fmt(o['spy_return_since_start_pct'])}</li></ul>"
        f"<h3>Trial picks ({o['n_trial_picks']})</h3><ul>{picks}</ul>"
        f"<p style='color:#888'>Revert = set MAX_OPEN_POSITIONS back to 15 in config.py "
        f"(does not force-sell). Auto-sent by pick_gate_trial_review.py.</p>")
    try:
        from email_sender import send_alert_email
        send_alert_email(f"[APEX TRIAL] Pick-gate review — {v} ({o['n_trial_picks']} trial picks)", body)
        print(f"Emailed review — verdict {v}.")
        return 0
    except Exception as e:
        # _settle() has already closed or extended the manifest by this point,
        # so a failed send is a verdict that will not be recomputed — not a
        # formatting inconvenience. The Chairman queue is the durable channel
        # and email is only the notification, so leave a row behind here too
        # and let RC=1 keep this out of the "ran fine" bucket.
        print(f"Email failed ({e}); printing instead.")
        main()
        _upsert_chairman_row(
            "pick-gate-trial-email-failed", "HIGH",
            f"Pick-gate trial reached verdict {v} but the email failed ({e}). "
            f"{why} Disposition: {disposition}. The manifest has already been "
            f"settled, so this verdict will not be recomputed.")
        return 1


def main():
    o = review()
    if "--json" in sys.argv:
        print(json.dumps(o, indent=2))
        return
    print(f"\n=== Pick-gate trial review — {o['trial']} ===")
    print(f"Started {o['started']}  ·  review target {o['review_on']}  ·  cap {o['prior_cap']}→{o['trial_cap']}")
    print(f"Trial picks admitted so far: {o['n_trial_picks']}")
    if o["trial_picks"]:
        for r in o["trial_picks"]:
            print(f"   {r['ticker']:6} {_fmt(r['unrealized_pnl_pct']):>9}  (wt {_fmt(r['weight_pct'])})")
    else:
        print("   (none yet — no new pick has cleared into a 16th+ slot)")
    w = o.get("window") or {}
    _wd = (w.get("window_days") if w.get("usable") else None)
    span = (", %.0fd" % _wd) if _wd else ""
    print(f"\n--- full-cohort window ({o['started']} → today{span}, both arms; "
          f"return is per dollar of AVERAGE exposure, not annualised) ---")
    if not w.get("usable"):
        print(f"   UNAVAILABLE: {w.get('why') or 'not attempted.'}")
    else:
        for label, a in (("Trial picks", w["trial"]), ("Incumbents", w["incumbent"]),
                         ("Pre-held*  ", w["pre_held"])):
            if not a["n"]:
                continue
            print(f"   {label:12}: {_pct(a['return_pct']):>9}  "
                  f"${a['pnl']:+10,.0f} on ${a['capital']:>10,.0f}  "
                  f"n={a['n']} ({a['n_closed']} closed)")
        if w["pre_held"]["n"]:
            print(f"   * {', '.join(w['pre_held']['tickers'])} — held before the "
                  f"trial, so the cap never admitted them; in neither arm.")
        print(f"   reconciled: {w['n_fills']} fills, residual ${w['residual']:+,.2f} "
              f"({w['residual_pct']:.2f}% of equity, tol {RECON_TOLERANCE_PCT:.2f}%)")

    print("\n--- survivors-only (percent since each position's own entry) ---")
    print(f"   Trial picks : {_fmt(o['trial_avg_return_pct'])}")
    print(f"   Incumbents  : {_fmt(o['incumbent_avg_return_pct'])}")
    print(f"   SPY         : {_fmt(o['spy_return_since_start_pct'])}")
    print()


if __name__ == "__main__":
    if "--notify" in sys.argv:
        # Non-zero when the verdict did not reach anyone. The 2026-08-17 fire
        # exited 1 by tracebacking, which is the same code an unhandled bug
        # gives — so nothing could tell "the review says REVERT and could not
        # send it" from "the interpreter fell over". Now the RC says which.
        sys.exit(notify() or 0)
    else:
        main()
