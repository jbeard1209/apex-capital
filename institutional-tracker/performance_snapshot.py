"""Write the daily performance snapshot that nothing was writing.

`data/performance_history.json` describes itself as "Daily P&L snapshots with
loss attribution. Agents and the Chairman can read this to understand firm
performance history and what caused drawdowns." Three modules read it —
`logic_team.py` R9, `logic_officer.py::_rule_portfolio_unexplained_loss`, and
`dashboard_server.py:122` — and **no module in this repo ever wrote it**. It
contained exactly one hand-written row, dated 2026-06-09.

That is what disarmed the firm's only "why are we down?" alarm: both guards
asked whether *an* attribution existed, the June row satisfied that forever, and
so every drawdown after 2026-06-09 was reported as explained. See
`loss_attribution.py` for that fix. This module closes the other half: the
alarm now has a real, dated series to be measured against.

**What this deliberately does not do: write `loss_attribution`.**
The facts of a drawdown are mechanical and belong here. *Why* the firm is down
is a judgment, and it is precisely the judgment the alarm exists to demand from
the board. Auto-filling it with a generated sentence would re-disarm the alarm
while looking like a fix — the same failure, one layer up. `pnl_drivers` below
is the factual decomposition (which positions moved the number); it is stored
under its own key and no reader treats it as an attribution.

Each host keeps its own copy: `deploy.sh` allowlists only two files under
`data/`, so this file is never synced between the Mac and the VM.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

HISTORY_PATH = DATA_DIR / "performance_history.json"
# Both the account totals and the per-position rows come from this one file,
# written by review_agent.py on every pipeline run. The obvious alternative for
# the position rows, position_aging_report.json, was rejected: its producer
# (position_aging_agent.py) is scheduled on neither host and called by nothing,
# and the VM's copy is dated 2026-07-03. Reading it would have made this module
# a fresh instance of the exact bug it exists to close — a consumer of a file
# no live producer writes. One source also means the totals and the drivers can
# never disagree about which moment they describe.
REVIEW_PATH  = DATA_DIR / "review_recommendations.json"
REGISTRY_PATH = DATA_DIR / "agent_registry.json"

STARTING_CAPITAL = 100_000
ET = ZoneInfo("America/New_York")

#: Keep roughly two years of daily rows. Rows carrying a loss_attribution are
#: never trimmed — they are the ones the alarm actually reads, and a naive cap
#: would eventually delete the only records that matter.
MAX_ROWS = 520

#: How many top movers to record in each direction.
N_DRIVERS = 3


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _et_date(stamp):
    """ISO date (ET) of a producer's timestamp, or None if unparseable.

    Producers are inconsistent: `review_agent` writes a naive local isoformat,
    `position_aging` writes an aware UTC one. A naive stamp means "the clock of
    the host that wrote it", which is the same clock this process runs on, so
    attaching the local zone is the correct reading. Everything is then
    converted to ET, because the trading date is an ET fact and the VM runs in
    UTC — comparing raw local dates would label a late-day run as tomorrow.
    """
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # interpret in this host's local zone
    return dt.astimezone(ET).date().isoformat()


def _pnl_drivers(recommendations):
    """Biggest dollar movers among open positions, worst first then best.

    This is *unrealized* P&L on positions still open, so it explains the
    mark-to-market picture and nothing about losses already realised on closed
    trades. It is a starting point for an attribution, not the attribution —
    which is why it lives under its own key and no alarm reads it.

    Shorts are signed correctly off `side`, not off `qty`: the review agent
    reports short quantities as positive.
    """
    rows = []
    for r in recommendations or []:
        if not isinstance(r, dict):
            continue
        qty, entry, price = r.get("qty"), r.get("avg_entry"), r.get("current_price")
        if not all(isinstance(v, (int, float)) for v in (qty, entry, price)):
            continue
        direction = -1 if str(r.get("side", "long")).lower() == "short" else 1
        rows.append({
            "ticker":  r.get("ticker"),
            "pnl_usd": round((price - entry) * abs(qty) * direction, 2),
            "pnl_pct": r.get("unrealized_pnl_pct"),
        })
    if not rows:
        return []

    rows.sort(key=lambda x: x["pnl_usd"])
    losers  = [r for r in rows[:N_DRIVERS] if r["pnl_usd"] < 0]
    winners = [r for r in reversed(rows[-N_DRIVERS:]) if r["pnl_usd"] > 0]
    return losers + winners


def build_snapshot(today_et=None):
    """Build today's row, or return None if the inputs aren't today's.

    Returns None rather than a zero-filled row when `review_recommendations.json`
    is missing or stale. A row that says the firm is worth $0 because the
    pipeline failed is worse than an absent row: the absent row is visible to
    the freshness sentinel, the zero row silently becomes a -100% drawdown.
    """
    today_et = today_et or datetime.now(ET).date().isoformat()

    review = _load(REVIEW_PATH, {})
    account = review.get("account") or {}
    review_date = _et_date(review.get("generated_at"))
    if review_date != today_et:
        logger.info(
            "performance_snapshot: skipping — review_recommendations.json is "
            "dated %s, not %s (pipeline has not produced today's numbers).",
            review_date, today_et)
        return None

    pv = account.get("portfolio_value")
    if not isinstance(pv, (int, float)) or pv <= 0:
        logger.warning("performance_snapshot: skipping — no usable "
                       "portfolio_value in review_recommendations.json.")
        return None

    registry = _load(REGISTRY_PATH, {"agents": []})
    agents = registry.get("agents", []) if isinstance(registry, dict) else registry
    n_active = sum(1 for a in agents
                   if isinstance(a, dict) and a.get("status") == "active")

    return {
        "snapshot_date":     today_et,
        "snapshot_time":     datetime.now(ET).strftime("%H:%M ET"),
        "portfolio_value":   round(pv, 2),
        "cash":              round(account.get("cash") or 0, 2),
        "n_positions":       account.get("n_positions"),
        "pnl_today_usd":     round(account.get("pnl_today") or 0, 2),
        "pnl_today_pct":     round(account.get("pnl_today_pct") or 0, 3),
        "pnl_inception_usd": round(pv - STARTING_CAPITAL, 2),
        "pnl_inception_pct": round((pv - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2),
        "active_agents_at_snapshot": n_active,
        "pnl_drivers":       _pnl_drivers(review.get("recommendations")),
        "source":            "performance_snapshot.py",
    }


def write_snapshot(today_et=None):
    """Upsert today's row into performance_history.json. Returns the row or None.

    Idempotent by date: main.py runs four to six times a day, and each run
    refreshes today's row rather than appending a fifth. Any `loss_attribution`
    already on today's row is preserved — a human or the board writes that
    field, and a later pipeline run must not erase it.
    """
    row = build_snapshot(today_et)
    if row is None:
        return None

    history = _load(HISTORY_PATH, {})
    if not isinstance(history, dict):
        history = {}
    history.setdefault(
        "description",
        "Daily P&L snapshots with loss attribution. Agents and the Chairman "
        "can read this to understand firm performance history and what caused "
        "drawdowns.")
    history.setdefault("starting_capital", STARTING_CAPITAL)
    snapshots = history.get("snapshots")
    if not isinstance(snapshots, list):
        snapshots = []

    existing = next((s for s in snapshots
                     if isinstance(s, dict)
                     and s.get("snapshot_date") == row["snapshot_date"]), None)
    if existing is not None:
        attribution = existing.get("loss_attribution")
        existing.clear()
        existing.update(row)
        if attribution:
            existing["loss_attribution"] = attribution
    else:
        snapshots.append(row)

    snapshots.sort(key=lambda s: str(s.get("snapshot_date") or ""))
    if len(snapshots) > MAX_ROWS:
        keep = [s for s in snapshots if s.get("loss_attribution")]
        recent = [s for s in snapshots if not s.get("loss_attribution")][-MAX_ROWS:]
        snapshots = sorted(keep + recent,
                           key=lambda s: str(s.get("snapshot_date") or ""))

    history["snapshots"] = snapshots
    history["last_written"] = datetime.now(ET).isoformat()

    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(history, f, indent=2)
    tmp.replace(HISTORY_PATH)

    logger.info("performance_snapshot: %s  $%s  (%+.2f%% inception, %d drivers)",
                row["snapshot_date"], f"{row['portfolio_value']:,.2f}",
                row["pnl_inception_pct"], len(row["pnl_drivers"]))
    return row


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    row = write_snapshot()
    if row is None:
        print("No snapshot written — today's review_recommendations.json is "
              "missing or stale.")
        return
    print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
