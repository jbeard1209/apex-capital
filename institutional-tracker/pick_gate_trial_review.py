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
    return _upsert_chairman_row(
        "pick-gate-trial-verdict", "HIGH",
        "Pick-gate trial verdict: %s. %s Scoreboard — trial %s (n=%d), "
        "incumbents %s (n=%d of %d still held), SPY %s. %s Trial %s."
        % (verdict, why,
           _fmt(o["trial_avg_return_pct"]), o["n_trial_picks"],
           _fmt(o["incumbent_avg_return_pct"]), o["n_incumbents"],
           len(o.get("incumbents_at_start") or []),
           _fmt(o["spy_return_since_start_pct"]), action, disposition))


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
    }
    return out


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


def _verdict(o) -> tuple:
    """Return (verdict, one-line rationale) comparing trial picks to incumbents.

    Verdicts: EXTEND / REVERT / HOLD / INCONCLUSIVE / UNMEASURABLE.
    Only HOLD is non-terminal — everything else wants a decision, now.
    """
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
                f"only ever shrinks, so waiting longer cannot restore it. The cap "
                f"needs a judgement call, not another window.")

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
    picks = "".join(
        f"<li>{r['ticker']}: {_fmt(r['unrealized_pnl_pct'])} (wt {_fmt(r['weight_pct'])})</li>"
        for r in o["trial_picks"]) or "<li>none admitted</li>"
    body = (
        f"<h2>Pick-gate trial review — verdict: {v}</h2>"
        f"<p>{why}</p>"
        f"<p>Trial ran {o['started']} → {o['review_on']}, cap {o['prior_cap']}→{o['trial_cap']}.</p>"
        f"<p><b>Disposition:</b> {disposition}</p>"
        f"<h3>Scoreboard (avg unrealized return)</h3><ul>"
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
    print("\n--- scoreboard (avg unrealized return) ---")
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
