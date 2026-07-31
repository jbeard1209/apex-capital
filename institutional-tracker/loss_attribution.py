"""Is the firm's loss attribution CURRENT? — one answer, for every caller.

Two agents guard the same promise: if the firm is down, Jack gets told *why*.
Both asked the question as a presence check.

    logic_officer._rule_unexplained_loss:
        has_attribution = bool(snapshots and snapshots[-1].get("loss_attribution"))
    logic_team R9:
        has_attribution = any(s.get("loss_attribution") for s in snapshots)

`data/performance_history.json` holds exactly ONE snapshot, dated 2026-06-09,
and nothing in this repo writes it — three modules read it, no module produces
it. So from 2026-06-09 onward both alarms answered "attribution provided" for
every drawdown that came afterwards, permanently. On 2026-07-07 the Logic
Officer told the board "Portfolio is -10.7% below starting capital — attribution
provided", pointing at a document a month older than the loss it claimed to
explain, and the board approved it (LO-20260707160101-unexplained_loss).

An attribution written before the loss answers a different question. Presence is
therefore the wrong test — currency is the test. That is this module.

Note the underlying gap this does NOT fix: nothing writes the snapshots. Until a
producer exists, `status()` will keep returning STALE, which is the honest
answer and the point — a silent alarm looked like good news for seven weeks.
"""

from datetime import date, datetime

#: performance_history.json bills itself as "Daily P&L snapshots", so a week is
#: already generous. Past this, an attribution is about a different drawdown.
MAX_ATTRIBUTION_AGE_DAYS = 7


def _snapshot_date(snap):
    raw = (snap or {}).get("snapshot_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except Exception:
        return None


def status(perf_history, today=None):
    """Describe the newest loss attribution.

    Returns a dict:
      current   -- bool, safe to treat the loss as explained
      label     -- "provided" / "STALE" / "MISSING", for alert titles
      detail    -- human sentence naming the date and age, or why it's missing
      as_of     -- ISO date of the newest attributed snapshot, or None
      age_days  -- int age of that snapshot, or None

    Accepts whatever the callers already hold: the parsed file, or {} / None.
    """
    today = today or date.today()
    snapshots = []
    if isinstance(perf_history, dict):
        snapshots = perf_history.get("snapshots") or []
    if not isinstance(snapshots, list):
        snapshots = []

    attributed = [s for s in snapshots
                  if isinstance(s, dict) and s.get("loss_attribution")]
    if not attributed:
        return {
            "current": False, "label": "MISSING", "as_of": None,
            "age_days": None,
            "detail": ("No loss attribution has been written. Jack does not "
                       "know why the firm is down."),
        }

    # Newest by date, not by list order: the file is hand-maintained and there
    # is nothing to guarantee append order.
    dated = [(d, s) for s in attributed if (d := _snapshot_date(s))]
    if not dated:
        return {
            "current": False, "label": "STALE", "as_of": None, "age_days": None,
            "detail": ("An attribution exists but carries no snapshot_date, so "
                       "there is no way to tell which loss it explains."),
        }

    as_of, _ = max(dated, key=lambda pair: pair[0])
    age = (today - as_of).days
    if age <= MAX_ATTRIBUTION_AGE_DAYS:
        return {
            "current": True, "label": "provided", "as_of": as_of.isoformat(),
            "age_days": age,
            "detail": ("Loss attribution is documented in "
                       "performance_history.json (dated %s)." % as_of.isoformat()),
        }
    return {
        "current": False, "label": "STALE", "as_of": as_of.isoformat(),
        "age_days": age,
        "detail": ("The newest loss attribution is dated %s — %d days ago — so "
                   "it cannot explain the current drawdown. Nothing in the repo "
                   "writes performance_history.json; the snapshots have to be "
                   "produced before this alarm can clear."
                   % (as_of.isoformat(), age)),
    }
