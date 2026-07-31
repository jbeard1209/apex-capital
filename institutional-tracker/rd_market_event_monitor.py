#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — R&D MARKET EVENT MONITOR
# =============================================================================
# rd_geopolitical_team.py maps CURRENT conditions onto a static list of 7
# historical precedents — it never detects that something new is happening
# right now. It imports yfinance but never calls it. This agent is the live
# front end that was missing: it watches market-derived signals (no paid news
# API — the market itself is the fastest, most liquid signal for "something
# just happened") and, when a genuine event fires, escalates to the board FAST
# and triggers an immediate R&D cycle so rd_geopolitical_team's precedent
# mapping runs against fresh conditions instead of waiting for the next
# scheduled slot.
#
# Detection rules (all market-derived, see _RULES below):
#   INDEX_GAP            — SPY or QQQ moves beyond threshold in one session
#   VIX_SPIKE            — VIX level or 1-day % change breaches a threshold
#                           (same thresholds canary_agent.py uses, for
#                           firm-wide consistency: 28 / +25%)
#   SECTOR_DIVERGENCE     — one sector ETF moves hard while SPY is flat —
#                           an isolated shock (e.g. energy spike, bank stress)
#   CROSS_ASSET_STRESS     — equities down + bonds up + gold up together —
#                           the classic "flight to safety" signature
#   OIL_SHOCK             — USO (oil) moves beyond threshold — proxy for
#                           energy/geopolitical supply shocks
#
# Escalation path (producer -> queue -> consumer, verified before wiring):
#   cit_director.submit_to_pod_queue() with pod="main", category="STRATEGY"
#   -> data/cit_pending_proposals.json (FULL_BOARD_ONLY_CATEGORIES forces
#      board attention regardless of severity)
#   -> read by deputy_board.py and board_meeting.py (confirmed consumers —
#      see CLAUDE.md's producer/queue/consumer contract section)
#   cit_director.trigger_rd_research() -> data/rd_trigger.json -> read by
#      `rd_team_leader.py --triggered`, which the VM crontab runs every 30 min
#      on weekdays 13:00-20:00 UTC (verified in data/vm_crontab_snapshot.txt).
#      That is the fast path: an event escalates into a real R&D cycle within
#      ~30 min instead of waiting for the next scheduled slot, so
#      rd_geopolitical_team.py's precedent mapping runs while it's still fresh.
#      NOTE: main.py does NOT read rd_trigger.json — an earlier version of this
#      header said it did. rd_team_leader.py is the only consumer; it also
#      ignores a trigger file older than 4h, so a stale event cannot fire a
#      cycle days later.
#
# A history file (EVENT_HISTORY_PATH) enforces a cooldown per (rule, ticker)
# so an ongoing shock doesn't re-fire a board proposal every run.
#
# Usage:
#   python3 rd_market_event_monitor.py            # run + escalate if warranted
#   python3 rd_market_event_monitor.py --report    # run + print JSON summary
# =============================================================================

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("rd_market_event_monitor")

# yfinance logs a lot of noise on transient failures — same suppression
# canary_agent.py uses, for a clean cron log.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("yfinance.base").setLevel(logging.CRITICAL)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

EVENT_REPORT_PATH  = os.path.join(DATA_DIR, "rd_market_event_report.json")
EVENT_HISTORY_PATH = os.path.join(DATA_DIR, "rd_market_event_history.json")

COOLDOWN_HOURS = 12.0  # don't re-escalate the same (rule, ticker) within this window

# Sector ETFs watched for isolated (single-sector) shocks
SECTOR_ETFS = ["XLE", "XLF", "XLK", "XLI", "XLU", "XLV", "XLB", "XLC", "XLY"]

# Thresholds — VIX levels/spike match canary_agent.py's ORANGE tier for
# firm-wide consistency; the rest are new, calibrated to flag genuinely rare
# single-session moves rather than ordinary daily noise.
INDEX_GAP_PCT        = 3.0    # SPY/QQQ 1-day % move
VIX_LEVEL_THRESHOLD  = 28.0
VIX_SPIKE_PCT        = 25.0   # 1-day % change in VIX
SECTOR_DIVERGENCE_PCT = 4.0   # sector ETF move ...
SECTOR_QUIET_SPY_PCT  = 1.0   # ... while SPY moves less than this
CROSS_ASSET_SPY_PCT  = -2.0   # SPY down at least this much
CROSS_ASSET_TLT_PCT  = 1.0    # AND TLT up at least this much
CROSS_ASSET_GLD_PCT  = 1.0    # AND GLD up at least this much
OIL_SHOCK_PCT        = 5.0    # USO 1-day % move


def _flatten_yf(df) -> "pd.DataFrame":
    """yfinance >=0.2.x returns MultiIndex columns for single-ticker downloads."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    return df


def _fetch_close_pair(ticker: str, attempts: int = 2) -> tuple:
    """Returns (latest close, prior close, reason_unreadable).

    Retries once on failure. This run makes ~15 sequential yfinance calls and
    the tail of that burst draws rate-limiting: on 2026-07-31 TLT came back
    empty here while succeeding on its own seconds later. A transient miss is
    not cosmetic — CROSS_ASSET_STRESS needs SPY *and* TLT *and* GLD, so one
    dropped ticker silently switches the entire flight-to-safety detector off
    while the report still reads "market quiet".

    NaN is checked explicitly. `float('nan')` passes `is not None` and fails
    every threshold comparison, so an unchecked NaN turns bad data into silence
    — and json.dump writes it as bare `NaN`, which is not valid JSON.
    """
    last_reason = "unknown"
    for i in range(attempts):
        try:
            df = _flatten_yf(yf.download(ticker, period="5d", progress=False, auto_adjust=True))
            if df.empty or len(df) < 2:
                last_reason = f"only {0 if df.empty else len(df)} session(s) returned"
            else:
                close = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2])
                if math.isnan(close) or math.isnan(prev):
                    last_reason = "close price was NaN"
                elif prev == 0:
                    last_reason = "prior close was 0 — cannot compute a % change"
                else:
                    return close, prev, None
        except Exception as e:
            last_reason = f"{type(e).__name__}: {e}"
        if i + 1 < attempts:
            time.sleep(1.5)
    return None, None, last_reason


def _pct_change_1d(ticker: str, unreadable: list = None) -> Optional[float]:
    """% change of the newest bar vs the prior session's close.

    Intraday this compares the live (partial) bar against yesterday's close —
    deliberate, since the point is to catch a move while it is happening.
    """
    close, prev, why = _fetch_close_pair(ticker)
    if why is not None:
        logger.debug(f"  _pct_change_1d({ticker}) unreadable: {why}")
        if unreadable is not None:
            unreadable.append({"ticker": ticker, "reason": why})
        return None
    return (close - prev) / prev * 100


def _get_vix(unreadable: list = None) -> tuple:
    """Returns (current VIX level, VIX 1-day % change)."""
    level, prev, why = _fetch_close_pair("^VIX")
    if why is not None:
        logger.debug(f"  _get_vix() unreadable: {why}")
        if unreadable is not None:
            unreadable.append({"ticker": "^VIX", "reason": why})
        return None, None
    return level, (level - prev) / prev * 100


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Catch the whole NaN class at the boundary, whichever upstream field went
    # bad: json.dump emits bare `NaN`, which most strict parsers reject, so a
    # single NaN can make the entire report unreadable to its consumers.
    blob = json.dumps(data, indent=2, default=str, allow_nan=False)
    with open(path, "w") as f:
        f.write(blob)


def _on_cooldown(history: dict, key: str) -> bool:
    rec = history.get(key)
    if not rec:
        return False
    try:
        last = datetime.fromisoformat(rec["last_fired_at"])
    except Exception:
        return False
    return (datetime.now() - last).total_seconds() / 3600.0 < COOLDOWN_HOURS


# Which tickers each rule needs in order to be able to fire at all. Used to
# report a rule as BLIND when an input could not be read, rather than letting
# a failed fetch masquerade as "no event".
_RULE_INPUTS = {
    "INDEX_GAP":          {"SPY", "QQQ"},
    "VIX_SPIKE":          {"^VIX"},
    "SECTOR_DIVERGENCE":  {"SPY", *SECTOR_ETFS},
    "CROSS_ASSET_STRESS": {"SPY", "TLT", "GLD"},
    "OIL_SHOCK":          {"USO"},
}


def _finite(value, ticker: str, unreadable: list):
    """Defence in depth: a non-finite number that reaches this layer becomes an
    explicit `unreadable`, never a silent number.

    _fetch_close_pair already rejects NaN closes, so this should be unreachable
    today. It is here because the guard above is one line in one function, and
    the failure mode it prevents is invisible: NaN passes `is not None`, fails
    every threshold, and the rule just never fires. Cheap insurance against a
    future edit computing a percentage somewhere else.
    """
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        if not any(u["ticker"] == ticker for u in unreadable):
            unreadable.append({"ticker": ticker,
                               "reason": f"computed value was not finite ({value})"})
        return None
    return value


def detect_events() -> dict:
    """Pull live cross-asset data and evaluate every rule. No news API calls —
    every signal here comes from price/vol data yfinance already gives us."""
    unreadable = []
    spy_pct = _pct_change_1d("SPY", unreadable)
    qqq_pct = _pct_change_1d("QQQ", unreadable)
    vix_level, vix_pct = _get_vix(unreadable)
    tlt_pct = _pct_change_1d("TLT", unreadable)
    gld_pct = _pct_change_1d("GLD", unreadable)
    uso_pct = _pct_change_1d("USO", unreadable)
    sector_pct = {t: _pct_change_1d(t, unreadable) for t in SECTOR_ETFS}

    # Sanitise BEFORE any rule sees these values, so a bad number can never be
    # compared against a threshold.
    spy_pct   = _finite(spy_pct, "SPY", unreadable)
    qqq_pct   = _finite(qqq_pct, "QQQ", unreadable)
    vix_level = _finite(vix_level, "^VIX", unreadable)
    vix_pct   = _finite(vix_pct, "^VIX", unreadable)
    tlt_pct   = _finite(tlt_pct, "TLT", unreadable)
    gld_pct   = _finite(gld_pct, "GLD", unreadable)
    uso_pct   = _finite(uso_pct, "USO", unreadable)
    sector_pct = {t: _finite(v, t, unreadable) for t, v in sector_pct.items()}

    events = []

    if spy_pct is not None and abs(spy_pct) >= INDEX_GAP_PCT:
        events.append({
            "rule": "INDEX_GAP", "ticker": "SPY", "value_pct": round(spy_pct, 2),
            "detail": f"SPY moved {spy_pct:+.2f}% in one session (threshold ±{INDEX_GAP_PCT}%)",
        })
    if qqq_pct is not None and abs(qqq_pct) >= INDEX_GAP_PCT:
        events.append({
            "rule": "INDEX_GAP", "ticker": "QQQ", "value_pct": round(qqq_pct, 2),
            "detail": f"QQQ moved {qqq_pct:+.2f}% in one session (threshold ±{INDEX_GAP_PCT}%)",
        })

    if vix_level is not None and (vix_level >= VIX_LEVEL_THRESHOLD or
                                   (vix_pct is not None and vix_pct >= VIX_SPIKE_PCT)):
        events.append({
            "rule": "VIX_SPIKE", "ticker": "^VIX",
            "value_pct": round(vix_pct, 2) if vix_pct is not None else None,
            "vix_level": round(vix_level, 2),
            "detail": f"VIX at {vix_level:.1f} ({vix_pct:+.1f}% 1-day) — "
                      f"threshold level={VIX_LEVEL_THRESHOLD} or spike={VIX_SPIKE_PCT}%",
        })

    if spy_pct is not None and abs(spy_pct) < SECTOR_QUIET_SPY_PCT:
        for etf, pct in sector_pct.items():
            if pct is not None and abs(pct) >= SECTOR_DIVERGENCE_PCT:
                events.append({
                    "rule": "SECTOR_DIVERGENCE", "ticker": etf, "value_pct": round(pct, 2),
                    "detail": f"{etf} moved {pct:+.2f}% while SPY was flat "
                              f"({spy_pct:+.2f}%) — isolated sector shock",
                })

    if (spy_pct is not None and spy_pct <= CROSS_ASSET_SPY_PCT and
            tlt_pct is not None and tlt_pct >= CROSS_ASSET_TLT_PCT and
            gld_pct is not None and gld_pct >= CROSS_ASSET_GLD_PCT):
        events.append({
            "rule": "CROSS_ASSET_STRESS", "ticker": "SPY/TLT/GLD",
            "value_pct": round(spy_pct, 2),
            "detail": f"Flight-to-safety signature: SPY {spy_pct:+.2f}%, "
                      f"TLT {tlt_pct:+.2f}%, GLD {gld_pct:+.2f}% same session",
        })

    if uso_pct is not None and abs(uso_pct) >= OIL_SHOCK_PCT:
        events.append({
            "rule": "OIL_SHOCK", "ticker": "USO", "value_pct": round(uso_pct, 2),
            "detail": f"USO (oil) moved {uso_pct:+.2f}% in one session "
                      f"(threshold ±{OIL_SHOCK_PCT}%) — energy/geopolitical signal",
        })

    # A rule whose inputs failed to load is BLIND, not quiet. Name it, loudly.
    # Without this an empty `events` list has two very different meanings —
    # "nothing happened" and "we could not look" — and they are indistinguishable
    # to every downstream reader. Absence must never be the encoding for failure.
    missing = {u["ticker"] for u in unreadable}
    degraded = [{"rule": rule, "blind_on": sorted(needs & missing)}
                for rule, needs in _RULE_INPUTS.items() if needs & missing]

    return {
        "generated_at": datetime.now().isoformat(),
        "inputs": {
            "spy_pct": spy_pct, "qqq_pct": qqq_pct,
            "vix_level": vix_level, "vix_pct": vix_pct,
            "tlt_pct": tlt_pct, "gld_pct": gld_pct, "uso_pct": uso_pct,
            "sector_pct": sector_pct,
        },
        "unreadable": unreadable,
        "degraded_rules": degraded,
        "events": events,
    }


def escalate(events: list, history: dict) -> list:
    """Submit board proposals + trigger an immediate R&D cycle for any event
    not currently on cooldown. Returns the list of events actually escalated."""
    if not events:
        return []

    escalated = []
    try:
        import sys
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        from cit_director import submit_to_pod_queue, trigger_rd_research
    except Exception as e:
        logger.warning(f"  Could not import cit_director — escalation path unavailable: {e}")
        return []

    for ev in events:
        key = f"{ev['rule']}:{ev['ticker']}"
        if _on_cooldown(history, key):
            logger.info(f"  [Cooldown] {key} fired within {COOLDOWN_HOURS}h — not re-escalating")
            continue

        proposal = {
            "id":           f"RDEVT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{ev['rule']}",
            "source":       "R&D Market Event Monitor",
            "agent":        "RDMarketEventMonitor",
            "category":     "STRATEGY",   # FULL_BOARD_ONLY_CATEGORIES — forces board attention
            "severity":     "HIGH",
            "confidence":   85,
            "pod":          "main",       # explicit — bypasses default rd-pod routing
            "title":        f"Live market event: {ev['rule']} on {ev['ticker']}",
            "description":  ev["detail"],
            "evidence":     ev["detail"],
            "proposed_fix": ("R&D to run an immediate cycle and assess sector/position "
                              "exposure against this event; Geopolitical/Macro Policy "
                              "analysts to map historical precedent once the R&D cycle "
                              "picks this up."),
            "why":          "A live cross-asset signal breached a calibrated threshold — "
                             "this needs board visibility before the next scheduled briefing.",
            "created_at":   datetime.now().isoformat(),
            "status":       "pending",
        }
        try:
            pod_dest = submit_to_pod_queue(proposal)
            logger.info(f"  Escalated {key} -> pod={pod_dest}: {proposal['title']}")
        except Exception as e:
            logger.warning(f"  submit_to_pod_queue failed for {key}: {e}")
            continue

        try:
            trigger_rd_research(reason=f"Market event: {ev['detail']}",
                                 source="RDMarketEventMonitor")
        except Exception as e:
            logger.debug(f"  trigger_rd_research failed for {key}: {e}")

        history[key] = {"last_fired_at": datetime.now().isoformat(), "detail": ev["detail"]}
        escalated.append(ev)

    return escalated


def run() -> dict:
    logger.info("  R&D Market Event Monitor — scanning cross-asset signals...")
    result = detect_events()
    history = _load_json(EVENT_HISTORY_PATH, {})
    if not isinstance(history, dict):
        history = {}

    escalated = escalate(result["events"], history)
    _save_json(EVENT_HISTORY_PATH, history)

    result["escalated"] = escalated
    if result["events"]:
        result["status"] = "EVENTS_DETECTED"
    elif result["degraded_rules"]:
        # NOT "QUIET" — we did not see a quiet market, we failed to look at
        # part of it. A monitor that reports all-clear while half-blind is
        # worse than one that reports nothing.
        result["status"] = "DEGRADED"
    else:
        result["status"] = "QUIET"
    _save_json(EVENT_REPORT_PATH, result)

    if result["events"]:
        logger.info(f"  {len(result['events'])} event(s) detected, "
                    f"{len(escalated)} escalated (rest on cooldown)")
        for ev in result["events"]:
            logger.info(f"    [{ev['rule']}] {ev['detail']}")
    elif result["degraded_rules"]:
        logger.warning("  No breaches seen, but the scan was DEGRADED — "
                       "these rules could not run:")
    else:
        logger.info("  No threshold breaches — market quiet")

    for d in result["degraded_rules"]:
        logger.warning(f"    [BLIND] {d['rule']} — no data for {', '.join(d['blind_on'])}")

    return result


if __name__ == "__main__":
    import sys
    report = run()
    if "--report" in sys.argv:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"\nStatus: {report['status']}")
        print(f"Events: {len(report['events'])} detected, {len(report['escalated'])} escalated")
