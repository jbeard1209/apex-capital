#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI WEATHER-EDGE STRATEGY
===========================================
Buys a Kalshi weather contract on its own when the weather forecast says the
price is wrong by more than a safety margin, after fees. It is the second
automatic strategy on the Kalshi desk, next to the copier (kalshi_copytrade.py),
and it shares the copier's caps, arm checks, halt file, ledger and Manager.

Chairman, 2026-11-25: "make sure its filed under the automatic so it can make
automatic trades and not rely on me — also make sure someone is checking it".
It grew out of three hand-researched $1 picks on 2026-11-24
(/mnt/project-files/kalshi-copytrade/2026-11-24-*.md): the Atlanta
Thanksgiving-rain YES at 81c was a real edge the market had missed (NWS text
said 25% for the daytime only; the hourly grid, NBM, HRRR and four ensembles
all had rain after midnight). Those notes also found that high-temperature
markets are usually sharper than the NWS public forecast, which is why this
needs every source to agree and a wide margin, and why it ships on paper.

WHAT IT TRADES
--------------
Daily US-city weather markets that settle on one station's climate report:
  high temperature  (KXHIGH*)   brackets like "44-45°", "50° or above"
  low temperature   (KXLOWT*)
  rain              (KXRAIN*)   "total precipitation > 0 in" (trace counts 0)
The station is read from the market's own rules ("Chicago (CLIMDW)" -> KMDW,
"Boston (KBOS)") and checked against api.weather.gov; a station NWS does not
know is skipped, never guessed. Only target days that have NOT started yet
(local standard time, as the climate report uses) and start within
max_lead_h: once the day is under way, part of the answer is already observed
and a forecast-only model would be trading against people watching the
thermometer.

WHERE THE PROBABILITY COMES FROM (no API keys; all public)
-----------------------------------------------------------
  NBM   National Blend of Models, NBS text bulletin via IEM
        (mesonet.agron.iastate.edu/api/1/mos.json?model=NBS). Gives the max/min
        AND its own standard deviation (TXN/XND), plus 6-hour PoPs. Required:
        no NBM, no trade.
  GFS   GFS MOS (MAV) via IEM. Max/min and 6-hour PoPs.
  NWS   the forecast office's gridded point forecast (api.weather.gov
        gridpoints): maxTemperature, minTemperature, probabilityOfPrecipitation.
Temperature: each source is a normal distribution (NBM's own spread; fixed
spreads for GFS and NWS), blended by weight with the disagreement between
sources added to the spread. A bracket's probability uses a half-degree
continuity correction, because the climate report is whole degrees.
Rain: each source's 6-hour PoPs for the four blocks covering the day are
combined halfway between the largest block (rain fully correlated) and the
union (independent), see combine_pops().

THE RULE (all must hold, or nothing is bought)
----------------------------------------------
  1. blend probability >= break-even (price + fee per contract) + margin
     (default 8 points, never below HARD_MIN_MARGIN)
  2. EVERY source on its own is at or above break-even (no source says -EV)
  3. NBM present and fresh, plus at least one other source
  4. the side we buy costs 15c-85c (the copier's band: no lottery tickets)
  5. bid/ask spread <= max_spread, so the price is a real market
  6. at most one market per event (one city-day) and no ticker the copier
     already holds
  7. RECHECK right before the order: the market is re-read, the edge is
     recomputed on the fresh price, and the order is immediate-or-cancel at
     that ask, so it never pays more than the price that passed the rule
Then the caps decide how many contracts: the copier's per_trade_usd (per
market, fees in) and per_day_usd (UTC day, shared with the copier) from
data/kalshi_copytrade_config.json, plus the copier's payout cap.

ARMING (Chairman only — Claude never writes these files)
--------------------------------------------------------
Since 2026-11-25 the Chairman's standing order is a SCHEDULED go-live: from
AUTO_ARM_AT (2026-12-02 4 PM ET) the bot arms itself unless its last 7 days
of settled paper buys lost 20% or more (see auto_arm_check). The arm file
below still works and still wins.
Live orders need EVERYTHING the copier needs (KALSHI_ENV=live, the key,
data/.kalshi_armed, data/.kalshi_copytrade_armed, no halt file) AND its own
    data/.kalshi_weather_armed      {"env": "live", "armed": true}
Missing any one, it runs on paper: what it WOULD have bought goes to
data/kalshi_weather_edge_paper.jsonl, never to the broker. The copier's halt
file stops it too, and three order errors in a row write that halt file.

OVERSIGHT
---------
  * Live orders go into the copier's ledger as leader "weather-edge", so the
    Manager's loss breaker, ORDER_ERRORS check, P&L report and the hourly
    apex-reports publish all see them.
  * The Manager (kalshi_copytrade_manager.py --check, every 15 min) alarms on
    WEATHER_NOT_RUNNING, WEATHER_ARMED_BUT_PAPER and WEATHER_FEED_DOWN, and its
    --report has a weather-edge section with paper P&L.
  * liveness.beat("kalshi_weather_edge", markets priced, markets examined)
    every run: STALE if it stops, NO_OUTCOME if it runs but prices nothing.

Usage:
    python3 kalshi_weather_edge.py --run          # cron: hourly at :20
    python3 kalshi_weather_edge.py --dry-run      # price everything, print, write nothing
    python3 kalshi_weather_edge.py --report
    python3 kalshi_weather_edge.py --backtest 2026-11-18 2026-11-24
"""

import fcntl
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import kalshi_client as kc
import kalshi_copytrade as ct

DATA_DIR = ct.DATA_DIR
ARM_FILE = DATA_DIR / ".kalshi_weather_armed"
CONFIG_FILE = DATA_DIR / "kalshi_weather_edge_config.json"
STATE_FILE = DATA_DIR / "kalshi_weather_edge_state.json"
PAPER_LEDGER = DATA_DIR / "kalshi_weather_edge_paper.jsonl"
EVAL_LOG = DATA_DIR / "kalshi_weather_edge_log.jsonl"
STATION_CACHE = DATA_DIR / "kalshi_weather_stations.json"
LOCK_FILE = DATA_DIR / ".kalshi_weather_edge.lock"
BACKTEST_FILE = DATA_DIR / "kalshi_weather_edge_backtest.json"
AUTO_ARM_FILE = DATA_DIR / "kalshi_weather_auto_arm.json"   # the go-live decision, once made

# SCHEDULED GO-LIVE (Chairman, project thread 2026-11-25 21:31-21:33Z):
#   "in a week tho make it live" / "unless it heinously loses money" /
#   "no make it automatic so i dont have to put in a command".
# So the bot arms ITSELF: from AUTO_ARM_AT it judges its own paper record
# over the previous AUTO_ARM_WINDOW_DAYS and goes live unless the paper buys
# that have SETTLED lost auto_arm_max_loss_pct (20%) or more of what they
# cost. (auto_arm_min_settled can demand a sample; it defaults to 0.) Either
# way it emails the Chairman once with the numbers. A loss refusal is final
# (going live then needs Jack); too few settled buys, if a minimum is set, is
# re-checked every 6 hours. Nothing here writes an arm
# file, and every copier gate (KALSHI_ENV=live, the key, data/.kalshi_armed,
# data/.kalshi_copytrade_armed, no halt file) still has to pass, so the
# copier's halt or the Manager's loss breaker still stops it.
# Stop it, alone, at any time: {"auto_arm_enabled": false} in
# data/kalshi_weather_edge_config.json, or `kalshi_weather_edge.py --disarm`.
AUTO_ARM_AT = "2026-12-02T21:00:00Z"        # 4 PM ET
AUTO_ARM_WINDOW_DAYS = 7
AUTO_ARM_RECHECK_H = 6.0

LEADER = "weather-edge"          # the ledger's "leader" column for our rows
LIVENESS_FEATURE = "kalshi_weather_edge"
USER_AGENT = "apex-weather-edge/1.0"
IEM_MOS = "https://mesonet.agron.iastate.edu/api/1/mos.json"
NWS = "https://api.weather.gov"
SERIES_PREFIXES = ("KXHIGH", "KXLOWT", "KXRAIN")
# Which market kinds it may BUY. Backtest 2026-11-25 (target days Nov 18-25,
# 2,176 settled markets, decisions at D-1 20:00Z on archived NBM + GFS MOS):
#   high-temp signals  199, return -18%;  low-temp 99, -27%.
# The market's favourite bracket beat the public NBM forecast (NBM's own
# error that week: sd 2.3F, within 1F only 45-49% of the time), so "the
# forecast says this bracket is overpriced" was mostly wrong. Temperature is
# still PRICED and logged every run (that is the evidence for turning it back
# on); it is not bought. Changing this set needs a new backtest that wins.
# Rain, same method, target days Nov 4-25 (734 markets, 645 priced): 53
# signals, return -0.4% (NO) / +1.0% (YES) per contract, and -$4.05 on $33
# under the $1/$3 caps. Break-even, NOT a proven edge: most losers were NO
# bets on days with a trace-to-0.05in shower, so combine_pops() probably
# under-calls a day's rain. Rain stays the only buyable kind so paper mode
# measures it forward; arming it is the Chairman's call on that paper record.
BUY_KINDS = frozenset({"rain"})

DEFAULTS = {
    "margin": 0.08,            # probability points the blend must clear break-even by
    "max_lead_h": 42.0,        # target day must START within this many hours
    "max_spread": 0.08,        # YES ask - YES bid
    "max_orders_per_run": 3,
    "nbm_max_age_h": 9.0,      # NBM runs hourly; IEM carries 4 a day
    "gfs_max_age_h": 15.0,     # GFS MOS runs 4x a day
    "pause_s": 0.4,            # between Kalshi list calls; the copier shares the IP
    # Chairman 2026-11-25 21:34Z: "if it made fewer than 5 trades thats just
    # fine" / "only dont turn it on if it lost 20% or more". So no minimum by
    # default: zero settled paper buys arms it. Raise this to demand a sample.
    "auto_arm_min_settled": 0,     # settled paper buys needed before it may arm itself
    "auto_arm_max_loss_pct": 20.0,  # refuse to arm if the paper week lost this much or more
}
HARD_MIN_AUTO_ARM_SETTLED = 0
HARD_MAX_AUTO_ARM_LOSS_PCT = 50.0
HARD_MIN_MARGIN = 0.05
# Chairman's Kalshi caps (2026-11): $1 per market, $3 per day. This strategy
# shares the copier's config, and is ALSO clipped here, so raising the
# copier's caps does not silently raise this strategy's.
HARD_MAX_PER_MARKET = 1.0
HARD_MAX_PER_DAY = 3.0
HARD_MAX_LEAD_H = 72.0
HARD_MAX_ORDERS = 10
MAX_CONSECUTIVE_ERRORS = ct.MAX_CONSECUTIVE_ERRORS

# Blend weights. NBM is itself a calibrated blend of ~30 models, so it leads.
TEMP_WEIGHTS = {"nbm": 0.5, "nws": 0.25, "gfs": 0.25}
RAIN_WEIGHTS = {"nbm": 0.5, "nws": 0.25, "gfs": 0.25}
# Spread (deg F) for sources that do not publish one. GFS MOS day-1 max/min
# mean absolute error is ~2-3F (NWS MDL verification), sd ~1.25 x MAE.
GFS_SIGMA = 3.0
NWS_SIGMA = 2.5
# Added in quadrature to NBM's own XND: the climate report is one station's
# whole-degree reading, and NBM's max/min window (7am-7pm / 7pm-8am) is not
# the calendar day.
STATION_SIGMA = 1.0
MIN_SIGMA = 1.5


# ------------------------------------------------------------------ config --

def load_config():
    cfg = dict(DEFAULTS)
    raw = ct._read_json(CONFIG_FILE, {})
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for k in DEFAULTS:
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
        if not cfg[k] > 0:                  # also rejects NaN
            cfg[k] = DEFAULTS[k]
    cfg["margin"] = max(cfg["margin"], HARD_MIN_MARGIN)
    cfg["max_lead_h"] = min(cfg["max_lead_h"], HARD_MAX_LEAD_H)
    cfg["max_orders_per_run"] = int(min(cfg["max_orders_per_run"], HARD_MAX_ORDERS))
    cfg["auto_arm_min_settled"] = int(max(cfg["auto_arm_min_settled"], HARD_MIN_AUTO_ARM_SETTLED))
    cfg["auto_arm_max_loss_pct"] = min(cfg["auto_arm_max_loss_pct"], HARD_MAX_AUTO_ARM_LOSS_PCT)
    # only an explicit false switches the scheduled go-live off
    cfg["auto_arm_enabled"] = not (isinstance(raw, dict) and raw.get("auto_arm_enabled") is False)
    return cfg


def armed():
    """(True, "") only when the copier's every live gate passes AND this
    strategy's own arm file is live; else (False, why). The copier's halt
    file is one of the copier's gates, so a breaker pause stops both."""
    live, why = ct.armed()
    if not live:
        return False, why
    d = ct._read_json(ARM_FILE, {})
    if isinstance(d, dict) and d.get("env") == "live" and d.get("armed") is True:
        return True, ""
    a = ct._read_json(AUTO_ARM_FILE, {})
    if isinstance(a, dict) and a.get("decision") == "armed":
        if not load_config()["auto_arm_enabled"]:
            return False, "scheduled go-live switched off (auto_arm_enabled: false)"
        return True, ""
    return False, ("paper until the scheduled go-live check at "
                   f"{AUTO_ARM_AT} ({(a or {}).get('decision') or 'not yet run'})")


# ------------------------------------------------------------------- http --

def _json(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/geo+json, application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 400):
                raise
            time.sleep(2 ** attempt)
        except Exception as e:
            last = e
            time.sleep(1 + attempt)
    raise last


# --------------------------------------------------------------- markets --

_STATION_RES = (re.compile(r"\(([KP][A-Z0-9]{3})\)"),   # "Boston (KBOS)", "(PHNL)"
                re.compile(r"\bCLI([A-Z]{3})\b"))        # "Chicago (CLIMDW)" -> KMDW
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def parse_station(rules):
    """ICAO station a market settles on, from its rules text, or None."""
    rules = rules or ""
    m = _STATION_RES[0].search(rules)
    if m:
        return m.group(1)
    m = _STATION_RES[1].search(rules)
    if m:
        return "K" + m.group(1)
    return None


def parse_target_date(ticker):
    """The weather day a market is about: from the event token of its ticker,
    '26NOV26' (YYMONDD) or '261231' (YYMMDD). None if neither reads."""
    parts = str(ticker or "").split("-")
    if len(parts) < 2:
        return None
    tok = parts[1]
    m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{2})", tok)
    try:
        if m and m.group(2) in _MONTHS:
            return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
        m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", tok)
        if m:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


def parse_market(m):
    """A Kalshi market record -> {"kind", "station", "day", "lo", "hi"} or
    (None, reason). lo/hi are the whole-degree outcomes that pay YES,
    inclusive (None = unbounded). Kalshi's own wording, verified on live
    records 2026-11-25: "greater" X pays at X+1 and up ("T49" = "50° or
    above"), "less" X pays at X-1 and down ("T42" = "41° or below"),
    "between" a b pays at a or b ("B44.5" = 44-45°)."""
    t = str(m.get("ticker") or "")
    rules = str(m.get("rules_primary") or "")
    low = rules.lower()
    if "maximum temperature" in low:
        kind = "high"
    elif "minimum temperature" in low:
        kind = "low"
    elif "total precipitation" in low:
        kind = "rain"
    else:
        return None, "not a temperature or rain market"
    station = parse_station(rules)
    if not station:
        return None, "no station in rules"
    day = parse_target_date(t)
    if day is None:
        return None, "no date in ticker"
    st = m.get("strike_type")
    fl, cp = m.get("floor_strike"), m.get("cap_strike")
    if kind == "rain":
        if not (st == "greater" and fl is not None and float(fl) == 0
                and "strictly greater than 0" in low):
            return None, "rain market is not 'any measurable rain'"
        return {"kind": kind, "station": station, "day": day, "lo": None, "hi": None}, ""
    try:
        if st == "greater" and fl is not None:
            lo, hi = int(round(float(fl))) + 1, None
        elif st == "less" and cp is not None:
            lo, hi = None, int(round(float(cp))) - 1
        elif st == "between" and fl is not None and cp is not None:
            lo, hi = int(round(float(fl))), int(round(float(cp)))
        else:
            return None, f"unknown strike {st}"
    except (TypeError, ValueError):
        return None, "bad strike"
    return {"kind": kind, "station": station, "day": day, "lo": lo, "hi": hi}, ""


def weather_series(fetch=None):
    """Daily weather series tickers Kalshi lists (one catalogue call)."""
    fetch = fetch or (lambda: kc._get("/series", {"category": "Climate and Weather"}))
    out = []
    for s in (fetch() or {}).get("series") or []:
        t = str(s.get("ticker") or "")
        if t.startswith(SERIES_PREFIXES) and s.get("frequency") in ("daily", "custom"):
            out.append(t)
    return sorted(set(out))


def open_markets(series, cfg, fetch=None):
    """{series: [market, ...]} of open markets; a failed series is left out
    and counted, never read as 'no markets'."""
    fetch = fetch or (lambda s: kc._get("/markets", {"series_ticker": s, "status": "open",
                                                      "limit": 200}))
    out, failed = {}, 0
    for i, s in enumerate(series):
        try:
            out[s] = (fetch(s) or {}).get("markets") or []
        except Exception:
            failed += 1
        if i + 1 < len(series) and cfg["pause_s"]:
            time.sleep(cfg["pause_s"])
    return out, failed


# --------------------------------------------------------------- stations --

def std_offset_hours(tz_name):
    """Local STANDARD time offset (the climate day ignores daylight time)."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz_name)
    jan = datetime(2026, 1, 15, 12, tzinfo=z).utcoffset()
    jul = datetime(2026, 7, 15, 12, tzinfo=z).utcoffset()
    return min(jan, jul).total_seconds() / 3600.0


def station_info(icao, cache, fetch=_json):
    """{"lat", "lon", "tz", "std_offset_h", "grid"} for a station, cached in
    data/kalshi_weather_stations.json (station metadata does not move), or
    None if NWS does not know it."""
    if icao in cache:
        return cache[icao]
    try:
        d = fetch(f"{NWS}/stations/{icao}")
        lon, lat = d["geometry"]["coordinates"][:2]
        tz = d["properties"]["timeZone"]
        p = fetch(f"{NWS}/points/{lat:.4f},{lon:.4f}")["properties"]
        info = {"lat": lat, "lon": lon, "tz": tz, "std_offset_h": std_offset_hours(tz),
                "grid": p.get("forecastGridData")}
    except Exception:
        return None
    cache[icao] = info
    return info


def day_window(day, std_offset_h):
    """(start, end) in UTC of a local-standard-time calendar day."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(
        hours=std_offset_h)
    return start, start + timedelta(days=1)


# --------------------------------------------------------------- the math --

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_temp(mu, sigma, lo, hi):
    """P(whole-degree reading in [lo, hi]) for a normal(mu, sigma) forecast,
    with the half-degree continuity correction (a reading of 44 is any true
    value in [43.5, 44.5))."""
    sigma = max(float(sigma), 0.1)
    a = 0.0 if lo is None else norm_cdf((lo - 0.5 - mu) / sigma)
    b = 1.0 if hi is None else norm_cdf((hi + 0.5 - mu) / sigma)
    return min(max(b - a, 0.0), 1.0)


def combine_pops(ps):
    """Chance of measurable rain over a day from its sub-period PoPs.

    The truth lies between the largest block (rain in every block is one
    event: fully correlated) and the union 1 - prod(1 - p) (independent).
    Rain from one weather system spans adjacent blocks, so neither end is
    right; this takes the midpoint. It is an assumption, recorded so the
    settled paper log can test it: if paper trades that lean on it lose,
    this is the first number to revisit."""
    ps = [min(max(float(p), 0.0), 1.0) for p in ps if p is not None]
    if not ps:
        return None
    mx = max(ps)
    union = 1.0 - math.prod(1.0 - p for p in ps)
    return mx + 0.5 * (union - mx)


def _blocks_for_day(start, end):
    """End times (UTC) of the 6-hour synoptic blocks (ending 00/06/12/18Z)
    whose majority lies inside [start, end): four for any US time zone."""
    out = []
    t = start.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    while t <= end + timedelta(hours=6):
        bs = t - timedelta(hours=6)
        overlap = (min(t, end) - max(bs, start)).total_seconds()
        if overlap > 3 * 3600:
            out.append(t)
        t += timedelta(hours=6)
    return out


def _iem_rows(payload):
    """IEM MOS json -> (runtime, [rows of the newest run]) with parsed times."""
    rows = (payload or {}).get("data") or []
    if not rows:
        return None, []
    rt = max(str(r.get("runtime")) for r in rows)
    out = []
    for r in rows:
        if str(r.get("runtime")) != rt:
            continue
        try:
            ft = datetime.strptime(str(r["ftime"])[:16], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append(dict(r, _ft=ft))
    try:
        rtd = datetime.strptime(rt[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        rtd = None
    return rtd, out


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def mos_source(payload, kind, start, end, maxtemp_field, sigma_fn, now, max_age_h):
    """One MOS source (NBM or GFS) for one station-day -> {"mu","sigma"} for
    temperature, {"p"} for rain, or None if the run is stale or the day is
    not fully covered."""
    rt, rows = _iem_rows(payload)
    if rt is None or not rows or (now - rt).total_seconds() > max_age_h * 3600:
        return None
    if kind == "rain":
        want = _blocks_for_day(start, end)
        got = {r["_ft"]: _num(r.get("p06")) for r in rows if _num(r.get("p06")) is not None}
        ps = [got.get(t) for t in want]
        if len(want) < 4 or any(p is None for p in ps):
            return None
        return {"p": combine_pops([p / 100.0 for p in ps]), "run": rt.isoformat(),
                "blocks": [round(p) for p in ps]}
    # max: the daytime period ending ~00Z; min: the overnight period ending ~12Z
    if kind == "high":
        lo_t, hi_t = start + timedelta(hours=15), start + timedelta(hours=27)
    else:
        lo_t, hi_t = start, start + timedelta(hours=12)
    cand = [r for r in rows if lo_t < r["_ft"] <= hi_t and _num(r.get(maxtemp_field)) is not None]
    if not cand:
        return None
    r = cand[0]
    mu = _num(r.get(maxtemp_field))
    # the calendar day can peak outside the model's max/min window (a warm
    # night, a cold front late in the day): the day's own 3-hourly temps bound it
    hourly = _day_temps([(x["_ft"], _num(x.get("tmp"))) for x in rows], start, end)
    if hourly:
        mu = max(mu, max(hourly)) if kind == "high" else min(mu, min(hourly))
    return {"mu": mu, "sigma": sigma_fn(r, (start - rt).total_seconds() / 3600.0),
            "run": rt.isoformat()}


def _day_temps(series, start, end):
    """Temperatures inside [start, end] from a (time, deg) series, plus the
    values interpolated AT the two boundaries. 2026-11-25: Atlanta's low for
    Nov 26 comes at midnight behind a cold front; the 3-hourly NBM points
    stopped at 10pm (43F) and the next one was 1am the next day (41F), so
    ignoring the boundary put the day's low 1-2F too warm."""
    pts = sorted((t, v) for t, v in series if v is not None)
    out = [v for t, v in pts if start <= t <= end]
    for edge in (start, end):
        before = [(t, v) for t, v in pts if t <= edge]
        after = [(t, v) for t, v in pts if t >= edge]
        if before and after:
            (t0, v0), (t1, v1) = before[-1], after[0]
            span = (t1 - t0).total_seconds()
            out.append(v0 if span <= 0 else v0 + (v1 - v0) * (edge - t0).total_seconds() / span)
    return out


def _nbm_sigma(row, lead_h):
    x = _num(row.get("xnd"))
    if x is None:
        x = 2.5
    return max(MIN_SIGMA, math.sqrt(x * x + STATION_SIGMA ** 2))


def _gfs_sigma(row, lead_h):
    return GFS_SIGMA + 0.5 * max(0.0, lead_h / 24.0)


def _nws_intervals(layer):
    """api.weather.gov gridpoint layer -> [(start, end, value)] in UTC."""
    out = []
    for v in (layer or {}).get("values") or []:
        try:
            s, dur = str(v["validTime"]).split("/")
            st = datetime.fromisoformat(s).astimezone(timezone.utc)
            m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?", dur)
            hours = (int(m.group(1) or 0) * 24 + int(m.group(2) or 0)) if m else 1
            out.append((st, st + timedelta(hours=max(hours, 1)), _num(v.get("value"))))
        except Exception:
            continue
    return out


def nws_source(grid, kind, start, end, now, max_age_h=12.0):
    """The NWS office's gridded forecast for one station-day, or None."""
    p = (grid or {}).get("properties") or {}
    try:
        upd = datetime.fromisoformat(str(p.get("updateTime"))).astimezone(timezone.utc)
        if (now - upd).total_seconds() > max_age_h * 3600:
            return None
    except Exception:
        return None
    if kind == "rain":
        iv = _nws_intervals(p.get("probabilityOfPrecipitation"))
        want = _blocks_for_day(start, end)
        ps = []
        for t in want:
            bs = t - timedelta(hours=6)
            vals = [v for a, b, v in iv if v is not None and a < t and b > bs]
            if not vals:
                return None
            ps.append(max(vals))
        if len(ps) < 4:
            return None
        return {"p": combine_pops([x / 100.0 for x in ps]), "run": upd.isoformat(),
                "blocks": [round(x) for x in ps]}
    layer = "maxTemperature" if kind == "high" else "minTemperature"
    iv = _nws_intervals(p.get(layer))
    if kind == "high":   # the daytime interval that STARTS on this day
        cand = [(a, b, v) for a, b, v in iv if start <= a < start + timedelta(hours=15)]
    else:                # the overnight interval that ENDS on this day's morning
        cand = [(a, b, v) for a, b, v in iv if start < b <= start + timedelta(hours=15)]
    cand = [c for c in cand if c[2] is not None]
    if not cand:
        return None
    c = cand[0][2] * 9.0 / 5.0 + 32.0
    # the office's hourly temperatures bound the calendar day the same way
    # (see _day_temps): a max/min period is 7am-7pm / 7pm-8am, not the day
    hourly = _day_temps([(a, None if v is None else v * 9.0 / 5.0 + 32.0)
                         for a, _b, v in _nws_intervals(p.get("temperature"))], start, end)
    if hourly:
        c = max(c, max(hourly)) if kind == "high" else min(c, min(hourly))
    return {"mu": c, "sigma": NWS_SIGMA, "run": upd.isoformat()}


def blend(kind, sources, lo=None, hi=None):
    """{"p", "by_source": {name: p}, ...} for one market, or None when NBM is
    missing or fewer than two sources answered."""
    srcs = {k: v for k, v in sources.items() if v}
    if "nbm" not in srcs or len(srcs) < 2:
        return None
    w = TEMP_WEIGHTS if kind != "rain" else RAIN_WEIGHTS
    tot = sum(w[k] for k in srcs)
    if kind == "rain":
        by = {k: v["p"] for k, v in srcs.items()}
        p = sum(w[k] * by[k] for k in srcs) / tot
        return {"p": p, "by_source": by}
    mu = sum(w[k] * srcs[k]["mu"] for k in srcs) / tot
    var = sum(w[k] * (srcs[k]["sigma"] ** 2 + (srcs[k]["mu"] - mu) ** 2) for k in srcs) / tot
    sigma = math.sqrt(var)
    by = {k: prob_temp(v["mu"], v["sigma"], lo, hi) for k, v in srcs.items()}
    return {"p": prob_temp(mu, sigma, lo, hi), "mu": round(mu, 1), "sigma": round(sigma, 2),
            "by_source": by}


# --------------------------------------------------------------- the rule --

def evaluate(market, model, cfg, kind=None):
    """One market + its model probability -> a candidate dict with
    "action": "buy" (and side/limit/edge) or "pass" (and why). Pure."""
    t = market.get("ticker")
    ask = kc.parse_price(market.get("yes_ask_dollars"))
    bid = kc.parse_price(market.get("yes_bid_dollars"))
    base = {"ticker": t, "event": market.get("event_ticker") or t.rsplit("-", 1)[0]}
    if model is None:
        return dict(base, action="pass", why="no forecast (NBM missing or one source only)")
    p_only = kind is not None and kind not in BUY_KINDS
    p = model["p"]
    base.update(p=round(p, 4), by_source={k: round(v, 4) for k, v in model["by_source"].items()})
    if ask is None or bid is None:
        return dict(base, action="pass", why="one-sided book")
    if ask - bid > cfg["max_spread"] + 1e-9:
        return dict(base, action="pass", why=f"spread {ask - bid:.2f} > {cfg['max_spread']:.2f}")
    best = None
    for side, cost, q, by in (("bid", ask, p, model["by_source"]),
                              ("ask", round(1.0 - bid, 4), 1.0 - p,
                               {k: 1.0 - v for k, v in model["by_source"].items()})):
        be = cost + (kc.fee_cents(cost, 1) or 0) / 100.0
        edge = q - be
        row = {"side": side, "buy": "yes" if side == "bid" else "no", "cost": round(cost, 2),
               "breakeven": round(be, 4), "q": round(q, 4), "edge": round(edge, 4),
               "limit": round(ask if side == "bid" else bid, 2)}
        if not (ct.MIN_PRICE - 1e-9 <= cost <= ct.MAX_PRICE + 1e-9):
            row["why"] = f"{row['buy'].upper()} at {cost:.2f} outside the {ct.MIN_PRICE:.2f}-{ct.MAX_PRICE:.2f} band"
        elif edge < cfg["margin"] - 1e-9:
            row["why"] = f"edge {edge:+.3f} below margin {cfg['margin']:.2f}"
        elif min(by.values()) < be - 1e-9:
            worst = min(by, key=by.get)
            row["why"] = f"{worst} says {by[worst]:.2f} < break-even {be:.2f}"
        if best is None or ("why" not in row and "why" in best) or (
                ("why" in row) == ("why" in best) and row["edge"] > best["edge"]):
            best = row
    if "why" in best:
        return dict(base, action="pass", **best)
    if p_only:
        return dict(base, action="pass", **dict(best, why=f"{kind} markets are priced, not "
                                                "bought (backtest 2026-11-25 lost)"))
    return dict(base, action="buy", **best)


def size(cost, cfg, spent_day, spent_market, held_contracts):
    """Contracts the caps allow at `cost` per contract, fees in (copier rules)."""
    budget = min(cfg["per_trade_usd"] - spent_market, cfg["per_day_usd"] - spent_day)
    payout_left = ct.payout_cap(cfg) - held_contracts
    n = min(math.floor(budget / cost + 1e-9) if cost > 0 else 0, math.floor(payout_left + 1e-9))
    while n >= 1 and n * cost + (kc.fee_cents(cost, n) or 0) / 100.0 > budget + 1e-9:
        n -= 1
    return max(n, 0), round(budget, 2)


def shared_caps():
    """The copier's caps (one budget for both strategies), clipped to ours."""
    c = ct.load_config()
    return {"per_trade_usd": min(c["per_trade_usd"], HARD_MAX_PER_MARKET),
            "per_day_usd": min(c["per_day_usd"], HARD_MAX_PER_DAY),
            "max_payout_usd": c["max_payout_usd"]}


# ------------------------------------------------------------------ scan ---

def _mos_fetch(station, model):
    return _json(f"{IEM_MOS}?station={station}&model={model}")


def scan(now=None, cfg=None, series_fetch=None, markets_fetch=None, mos_fetch=None,
         station_fetch=None, grid_fetch=None):
    """Price every eligible open weather market. Returns
    (evaluations, stats). All I/O is injectable for tests."""
    now = now or datetime.now(timezone.utc)
    cfg = cfg or load_config()
    mos_fetch = mos_fetch or _mos_fetch
    grid_fetch = grid_fetch or _json
    stats = {"series": 0, "series_failed": 0, "markets": 0, "eligible": 0, "priced": 0,
             "stations": 0, "station_unknown": 0}
    try:
        series = weather_series(series_fetch)
    except Exception as e:
        stats["error"] = f"series list failed: {str(e)[:120]}"
        return [], stats
    stats["series"] = len(series)
    by_series, stats["series_failed"] = open_markets(series, cfg, markets_fetch)
    cache = ct._read_json(STATION_CACHE, {})
    cache = cache if isinstance(cache, dict) else {}
    n_cache = len(cache)
    groups = {}            # (station, day, kind) -> [(market, parsed)]
    evals = []
    for s, ms in by_series.items():
        for m in ms:
            stats["markets"] += 1
            parsed, why = parse_market(m)
            if parsed is None:
                continue
            info = station_info(parsed["station"], cache, station_fetch or _json)
            if info is None:
                stats["station_unknown"] += 1
                continue
            start, end = day_window(parsed["day"], info["std_offset_h"])
            lead_h = (start - now).total_seconds() / 3600.0
            if lead_h <= 0 or lead_h > cfg["max_lead_h"]:
                continue
            stats["eligible"] += 1
            groups.setdefault((parsed["station"], parsed["day"], parsed["kind"]), []).append(
                (m, parsed, info, start, end))
    if len(cache) != n_cache:
        try:
            ct._write_json(STATION_CACHE, cache)
        except Exception:
            pass
    mos_cache, grid_cache = {}, {}
    for (station, day, kind), items in sorted(groups.items(), key=lambda x: str(x[0])):
        _, _, info, start, end = items[0]
        stats["stations"] += 1
        srcs = {}
        for name, model, field, sig, age in (("nbm", "NBS", "txn", _nbm_sigma, cfg["nbm_max_age_h"]),
                                             ("gfs", "GFS", "n_x", _gfs_sigma, cfg["gfs_max_age_h"])):
            key = (station, model)
            if key not in mos_cache:
                try:
                    mos_cache[key] = mos_fetch(station, model)
                except Exception:
                    mos_cache[key] = None
            srcs[name] = mos_source(mos_cache[key], kind, start, end, field, sig, now, age)
        g = info.get("grid")
        if g and g not in grid_cache:
            try:
                grid_cache[g] = grid_fetch(g)
            except Exception:
                grid_cache[g] = None
        srcs["nws"] = nws_source(grid_cache.get(g), kind, start, end, now) if g else None
        for m, parsed, _i, _s, _e in items:
            model = blend(kind, srcs, parsed["lo"], parsed["hi"])
            ev = evaluate(m, model, cfg, kind)
            ev.update(kind=kind, station=station, day=day.isoformat(),
                      lead_h=round((start - now).total_seconds() / 3600.0, 1),
                      sources={k: v for k, v in srcs.items() if v})
            if model is not None:
                stats["priced"] += 1
            evals.append(ev)
    return evals, stats


# ----------------------------------------------------------------- ledger --

def paper_rows():
    try:
        lines = PAPER_LEDGER.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _append_paper(row):
    PAPER_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with PAPER_LEDGER.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _ours_today(rows, now):
    day = now.strftime("%Y-%m-%d")
    return [r for r in rows if r.get("leader") == LEADER and str(r.get("at", ""))[:10] == day
            and r.get("status") in ("placed", "paper")]


def _log_evals(evals, now):
    """Append this run's evaluations (one line per market) and keep the file
    bounded, so 'why did it not buy X' is answerable after the fact."""
    try:
        EVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        old = EVAL_LOG.read_text().splitlines() if EVAL_LOG.exists() else []
        new = [json.dumps(dict(e, at=now.isoformat()), sort_keys=True, default=str)
               for e in evals]
        keep = (old + new)[-20000:]
        tmp = EVAL_LOG.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + ("\n" if keep else ""))
        os.replace(tmp, EVAL_LOG)
    except Exception:
        pass


# ------------------------------------------------------- scheduled go-live --

def paper_week(now, rows=None, market=None):
    """Settled paper buys opened in the AUTO_ARM_WINDOW_DAYS before `now`:
    {"settled", "open", "spent_usd", "pnl_usd", "loss_pct"}. Settled =
    Kalshi has a result; an open position is not judged yet."""
    import kalshi_copytrade_manager as mgr
    rows = paper_rows() if rows is None else rows
    since = now - timedelta(days=AUTO_ARM_WINDOW_DAYS)
    fake = [dict(r, status="placed", fill_count=r.get("count")) for r in rows
            if r.get("status") == "paper" and since <= (ct._parse_ts(r.get("at")) or now) <= now]
    cache = {}
    market = market or (lambda t: mgr._market(t, cache))
    settled = open_ = 0
    spent = pnl = 0.0
    for t, b in mgr.books(fake).items():
        realised, _u, done = mgr.value_book(b, market(t))
        if not done:
            open_ += 1
            continue
        settled += 1
        spent += b["spent"] + b["fees"]
        pnl += realised
    return {"settled": settled, "open": open_, "spent_usd": round(spent, 2),
            "pnl_usd": round(pnl, 2),
            "loss_pct": round(-100.0 * pnl / spent, 1) if spent else None}


def _notify(subject, body):
    try:
        from email_sender import send_alert_email
        return send_alert_email(subject, body, force=True) is not False
    except Exception:
        return False


def auto_arm_check(now=None, market=None, notify=_notify, cfg=None):
    """Make the scheduled go-live decision once it is due. Returns the
    decision record (also written to AUTO_ARM_FILE)."""
    now = now or datetime.now(timezone.utc)
    cfg = cfg or load_config()
    rec = ct._read_json(AUTO_ARM_FILE, {})
    rec = rec if isinstance(rec, dict) else {}
    if rec.get("decision") in ("armed", "refused"):
        return rec                                   # final either way
    due = ct._parse_ts(AUTO_ARM_AT)
    if not cfg["auto_arm_enabled"] or due is None or now < due:
        return rec
    last = ct._parse_ts(rec.get("checked_at"))
    if last and (now - last).total_seconds() < AUTO_ARM_RECHECK_H * 3600:
        return rec
    wk = paper_week(now, market=market)
    need, cap = cfg["auto_arm_min_settled"], cfg["auto_arm_max_loss_pct"]
    rule = (f"settled paper buys in the last {AUTO_ARM_WINDOW_DAYS} days lost less than "
            f"{cap:.0f}% of their cost" + (f", with at least {need} of them" if need else ""))
    stats = (f"{wk['settled']} settled ({wk['open']} still open), cost ${wk['spent_usd']:.2f}, "
             f"P&L ${wk['pnl_usd']:+.2f}"
             + ("" if wk["loss_pct"] is None else f" ({-wk['loss_pct']:+.1f}%)"))
    if wk["settled"] < need:
        first = rec.get("decision") != "waiting"
        rec = {"decision": "waiting", "checked_at": now.isoformat(), "rule": rule,
               "paper_week": wk, "why": f"only {wk['settled']} settled paper buys; need {need}"}
        if first:
            notify("Kalshi weather-edge: NOT live yet (too few paper trades)",
                   f"<p>The scheduled go-live check ran. Rule: {rule}.</p><p>Paper week: "
                   f"{stats}.</p><p>It stays on paper and re-checks every "
                   f"{AUTO_ARM_RECHECK_H:.0f} hours until it has {need}.</p>")
    elif wk["loss_pct"] is not None and wk["loss_pct"] >= cap:
        rec = {"decision": "refused", "checked_at": now.isoformat(), "rule": rule,
               "paper_week": wk, "why": f"paper week lost {wk['loss_pct']:.1f}% (limit {cap:.0f}%)"}
        notify("Kalshi weather-edge: REFUSED to go live (paper losses)",
               f"<p>The scheduled go-live check ran. Rule: {rule}.</p><p>Paper week: "
               f"{stats}.</p><p>It stays on paper. Going live now needs you.</p>")
    else:
        rec = {"decision": "armed", "checked_at": now.isoformat(), "armed_at": now.isoformat(),
               "rule": rule, "paper_week": wk, "why": "paper week passed the rule"}
        notify("Kalshi weather-edge is now LIVE",
               f"<p>The scheduled go-live check passed. Rule: {rule}.</p><p>Paper week: "
               f"{stats}.</p><p>It now buys real rain contracts by itself, inside the shared "
               "$1 per market / $3 per day caps. Stop it alone: ask Claude to run "
               "<code>kalshi_weather_edge.py --disarm</code> on the VM; the copier's halt file "
               "stops everything.</p>")
    ct._write_json(AUTO_ARM_FILE, rec)
    return rec


def disarm():
    """Switch the scheduled go-live off on this host (a disarm, never an arm)."""
    raw = ct._read_json(CONFIG_FILE, {})
    raw = raw if isinstance(raw, dict) else {}
    raw["auto_arm_enabled"] = False
    ct._write_json(CONFIG_FILE, raw)
    return armed()


# ------------------------------------------------------------------- run ---

def run(now=None, dry_run=False, place=None, market_fetch=None, scan_fn=None, cfg=None):
    """One pass: price, pick, recheck, buy (live) or record (paper).
    Returns the state dict it writes. `dry_run` writes nothing at all."""
    now = now or datetime.now(timezone.utc)
    cfg = dict(cfg or load_config())
    cfg.update(shared_caps())
    place = place or kc.place_order
    market_fetch = market_fetch or (lambda t: kc._get(
        f"/markets/{urllib.parse.quote(str(t))}").get("market"))
    if not dry_run and not ARM_FILE.exists():
        try:
            auto_arm_check(now=now, cfg=cfg)
        except Exception:
            pass                                  # the Manager reports a missed check
    is_live, why_not = armed()
    if dry_run:
        is_live, why_not = False, "dry run"
    evals, stats = (scan_fn or scan)(now=now, cfg=cfg)
    buys = sorted([e for e in evals if e["action"] == "buy"],
                  key=lambda e: -e["edge"] / max(e["breakeven"], 0.01))
    state = ct._read_json(STATE_FILE, {})
    state = state if isinstance(state, dict) else {}
    orders, events_done = [], set()
    for e in buys:
        if len(orders) >= cfg["max_orders_per_run"]:
            break
        if e["event"] in events_done:
            continue
        with ct.spend_lock():
            rows = ct.ledger_rows()
            prow = paper_rows()
            if any(r.get("ticker", "").rsplit("-", 1)[0] == e["event"]
                   for r in _ours_today(rows + ([] if is_live else prow), now)):
                continue
            if abs(ct.net_position(e["ticker"], rows)) > 1e-9 and not any(
                    r.get("leader") == LEADER for r in rows if r.get("ticker") == e["ticker"]):
                continue          # the copier holds it; never trade against its book
            # the shared caps: live mode counts the copier's live spend; paper
            # mode counts that too plus its own paper rows, so paper shows
            # what the shared $3/day would really have allowed
            mine = rows if is_live else rows + [dict(r, status="paper") for r in prow]
            spent_day = ct.spent_today(rows, now=now, live=True) + (
                0.0 if is_live else ct.spent_today(prow, now=now, live=False))
            spent_mkt = ct.market_spent(e["ticker"], mine, live=None)
            held = ct.market_contracts(e["ticker"], mine, live=None)
            # RECHECK: the price right now, against the same forecast
            try:
                fresh = market_fetch(e["ticker"]) or {}
            except Exception as ex:
                orders.append(dict(e, status="recheck_failed", error=str(ex)[:120]))
                continue
            model = {"p": e["p"], "by_source": e["by_source"]}
            re_ev = evaluate(dict(fresh, ticker=e["ticker"]), model, cfg, e.get("kind"))
            if re_ev["action"] != "buy" or re_ev["side"] != e["side"]:
                orders.append(dict(e, status="moved", why=re_ev.get("why", "side flipped")))
                continue
            if str(fresh.get("status", "active")) not in ("active", "open"):
                orders.append(dict(e, status="moved", why=f"market {fresh.get('status')}"))
                continue
            n, budget = size(re_ev["cost"], cfg, spent_day, spent_mkt, held)
            if n < 1:
                orders.append(dict(e, status="capped", why=f"${max(budget, 0):.2f} left under "
                                   "the shared caps buys no contract"))
                continue
            fee = (kc.fee_cents(re_ev["cost"], n) or 0) / 100.0
            row = {"at": now.isoformat(), "leader": LEADER, "ticker": e["ticker"],
                   "side": re_ev["side"], "buy": re_ev["buy"], "count": n,
                   "price": re_ev["limit"], "leader_price": re_ev["limit"],
                   "cost_usd": round(n * re_ev["cost"] + fee, 2), "reduce_only": False,
                   "model_p": e["p"], "by_source": e["by_source"], "edge": re_ev["edge"],
                   "breakeven": re_ev["breakeven"], "kind": e.get("kind"),
                   "station": e.get("station"), "day": e.get("day"),
                   "why": f"forecast {re_ev['q']:.0%} vs break-even {re_ev['breakeven']:.0%}"}
            if dry_run:
                orders.append(dict(row, status="dry_run"))
                events_done.add(e["event"])
                continue
            if not is_live:
                row.update(status="paper", why_not_live=why_not)
                _append_paper(row)
            else:
                coid = f"apex-wx-{uuid.uuid4().hex[:16]}"
                row["client_order_id"] = coid
                try:
                    resp = place(ticker=row["ticker"], side=row["side"], count=n,
                                 price=row["price"], time_in_force="immediate_or_cancel",
                                 reduce_only=False, client_order_id=coid)
                    row.update(status="placed", order_id=resp.get("order_id"),
                               fill_count=kc.parse_size(resp.get("fill_count")))
                    state["consecutive_errors"] = 0
                except Exception as ex:
                    row.update(status="error", error=f"{type(ex).__name__}: {str(ex)[:200]}")
                    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
                    if state["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS:
                        ct._write_json(ct.HALT_FILE, {"at": row["at"], "reason":
                                                      "weather-edge: repeated order errors",
                                                      "last_error": row["error"]})
                        is_live, why_not = False, "halted after repeated order errors"
                ct._append(row)
            orders.append(row)
            events_done.add(e["event"])
    state.update({"last_run": now.isoformat(), "mode": "live" if is_live else "paper",
                  "why_not_live": why_not, "stats": stats,
                  "n_buy_signals": len(buys),
                  "orders": [{k: o.get(k) for k in ("ticker", "status", "buy", "count", "price",
                                                    "cost_usd", "edge", "why")} for o in orders],
                  "top": [{k: e.get(k) for k in ("ticker", "action", "p", "buy", "cost",
                                                 "edge", "why")}
                          for e in sorted([x for x in evals if "edge" in x],
                                          key=lambda x: -x["edge"])[:8]]})
    if not dry_run:
        ct._write_json(STATE_FILE, state)
        _log_evals(evals, now)
        try:
            import liveness
            # done = markets we could put a forecast probability on; examined =
            # markets in our window. A dead forecast feed is NO_OUTCOME, a dead
            # Kalshi list (nothing examined at all) is forced to (0, 1) so it
            # alarms too: there are always weather markets listed.
            liveness.beat("kalshi_weather_edge", stats["priced"], max(stats["eligible"], 1))
        except Exception:
            pass
    return dict(state, evaluations=evals)


def run_locked(dry_run=False):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another weather-edge run is going; exiting")
            return None
        return run(dry_run=dry_run)


# ----------------------------------------------------------------- report --

def paper_performance(rows=None, market=None, now=None):
    """The Manager's P&L math run on paper rows, as if every one had filled."""
    import kalshi_copytrade_manager as mgr
    rows = paper_rows() if rows is None else rows
    fake = [dict(r, status="placed", fill_count=r.get("count")) for r in rows
            if r.get("status") == "paper"]
    return mgr.performance(fake, now=now, market=market)


def health(now=None):
    """Findings for the Manager: [] when fine or not deployed on this host."""
    now = now or datetime.now(timezone.utc)
    arm = ct._read_json(ARM_FILE, {})
    auto = ct._read_json(AUTO_ARM_FILE, {})
    armed_intent = (isinstance(arm, dict) and arm.get("armed") is True) or (
        isinstance(auto, dict) and auto.get("decision") == "armed"
        and load_config()["auto_arm_enabled"])
    if not (STATE_FILE.exists() or ARM_FILE.exists()):
        return []
    out = []
    st = ct._read_json(STATE_FILE, {})
    st = st if isinstance(st, dict) else {}
    last = ct._parse_ts(st.get("last_run"))
    if last is None or (now - last).total_seconds() > 3 * 3600:
        out.append({"kind": "WEATHER_NOT_RUNNING",
                    "severity": "CRITICAL" if armed_intent else "HIGH",
                    "detail": "the weather-edge strategy has not run for "
                    f"{'ever' if last is None else f'{(now - last).total_seconds() / 3600:.0f}h'}"
                    " (cron: hourly at :20); check the VM crontab"})
    live, why = armed()
    if armed_intent and not live:
        out.append({"kind": "WEATHER_ARMED_BUT_PAPER", "severity": "CRITICAL",
                    "detail": f"weather-edge is armed but trading on paper because: {why}"})
    due = ct._parse_ts(AUTO_ARM_AT)
    a = ct._read_json(AUTO_ARM_FILE, {})
    if (due and now > due + timedelta(hours=3) and load_config()["auto_arm_enabled"]
            and not (isinstance(a, dict) and a.get("decision"))):
        out.append({"kind": "WEATHER_AUTO_ARM_MISSED", "severity": "HIGH",
                    "detail": f"the scheduled go-live check due {AUTO_ARM_AT} has not run"})
    s = st.get("stats") or {}
    if last is not None and s and (s.get("error") or (s.get("eligible", 0) > 0
                                                       and s.get("priced", 0) == 0)):
        out.append({"kind": "WEATHER_FEED_DOWN", "severity": "MEDIUM",
                    "detail": s.get("error") or f"priced 0 of {s.get('eligible')} weather "
                    "markets: the NBM forecast feed (IEM) is down or stale"})
    return out


def _auto_arm_line(cfg):
    a = ct._read_json(AUTO_ARM_FILE, {})
    a = a if isinstance(a, dict) else {}
    if not cfg["auto_arm_enabled"]:
        return "scheduled go-live switched OFF (auto_arm_enabled: false)"
    if not a.get("decision"):
        return (f"automatic at {AUTO_ARM_AT[:16]}Z unless the last {AUTO_ARM_WINDOW_DAYS}d of "
                f"settled paper buys lost {cfg['auto_arm_max_loss_pct']:.0f}% or more")
    return f"{a['decision'].upper()} at {str(a.get('checked_at'))[:16]}: {a.get('why')}"


def report_lines(now=None):
    now = now or datetime.now(timezone.utc)
    cfg = load_config()
    live, why = armed()
    st = ct._read_json(STATE_FILE, {})
    st = st if isinstance(st, dict) else {}
    s = st.get("stats") or {}
    out = ["  WEATHER-EDGE (auto, forecast vs price)",
           f"    mode      : {'LIVE' if live else 'paper (' + why + ')'}",
           f"    rule      : forecast >= break-even + {cfg['margin']:.2f}, every source agrees, "
           f"target day within {cfg['max_lead_h']:.0f}h, shared copier caps",
           "    go-live   : " + _auto_arm_line(cfg),
           f"    last run  : {st.get('last_run', 'never')[:16]}  examined {s.get('eligible', 0)} "
           f"markets, priced {s.get('priced', 0)}, buy signals {st.get('n_buy_signals', 0)}"]
    for o in st.get("orders") or []:
        out.append(f"      {o.get('status'):<8} {o.get('ticker')} {str(o.get('buy') or '').upper()} "
                   f"x{o.get('count') or ''} @{o.get('price') or ''} edge {o.get('edge')} "
                   f"{o.get('why') or ''}")
    prow = paper_rows()
    if prow:
        try:
            perf = paper_performance(prow, now=now)
            a, w = perf["all_time"], perf["week"]
            out.append(f"    paper P&L : all time {a['total_usd']:+.2f} on {a['markets']} market(s), "
                       f"{a['settled']} settled, {a['wins']} won; this week {w['total_usd']:+.2f}")
        except Exception as e:
            out.append(f"    paper P&L : unavailable ({str(e)[:80]})")
        for r in prow[-5:]:
            out.append(f"      {str(r.get('at', ''))[:16]} paper {r.get('ticker')} "
                       f"{str(r.get('buy', '')).upper()} x{r.get('count')} @{r.get('price')} "
                       f"{r.get('why', '')}")
    return out


# --------------------------------------------------------------- backtest --

def _event_candles(series, event, ts):
    """{ticker: (yes_bid, yes_ask)} at the hour ending `ts`, from Kalshi's
    event candlesticks (one call per event)."""
    d = kc._get(f"/series/{series}/events/{event}/candlesticks",
                {"start_ts": ts - 3600, "end_ts": ts, "period_interval": 60})
    out = {}
    for t, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
        if not cs:
            continue
        c = cs[-1]
        out[t] = (kc.parse_price((c.get("yes_bid") or {}).get("close_dollars")),
                  kc.parse_price((c.get("yes_ask") or {}).get("close_dollars")))
    return out


def _error_stats(errors):
    """Bias and spread (deg F) of each source's max/min against the settled
    reading: the evidence for the sigmas this model assumes."""
    out = {}
    for src in ("nbm", "gfs"):
        for kind in ("high", "low"):
            e = [x[src] for x in errors if x["kind"] == kind and src in x]
            if len(e) < 2:
                continue
            mean = sum(e) / len(e)
            sd = math.sqrt(sum((v - mean) ** 2 for v in e) / (len(e) - 1))
            out[f"{src}_{kind}"] = {"n": len(e), "bias": round(mean, 2), "sd": round(sd, 2),
                                    "rmse": round(math.sqrt(sum(v * v for v in e) / len(e)), 2),
                                    "within_1F": round(sum(1 for v in e if abs(v) <= 1) / len(e), 2)}
    return out


def backtest(first, last, decide_hour_utc=20, cfg=None, verbose=True, kinds=None):
    """What the rule would have bought for target days first..last.

    For each target day D the decision is made at D-1 {decide_hour_utc}:00
    UTC, using only what was public then: the NBM and GFS MOS runs IEM had
    archived by that hour, and Kalshi's YES bid/ask in the hour ending then.
    NWS gridded forecasts are not archived, so the backtest runs on two
    sources (NBM + GFS), which the live rule also accepts. The caps are
    applied per UTC decision day. Outcomes are Kalshi's own settled result."""
    cfg = cfg or load_config()
    ccfg = dict(cfg, **shared_caps())
    series = weather_series()
    cache = ct._read_json(STATION_CACHE, {})
    cache = cache if isinstance(cache, dict) else {}
    picks, examined, priced, errors = [], 0, 0, []
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    lo_ts = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp())
    hi_ts = int((datetime(d1.year, d1.month, d1.day, tzinfo=timezone.utc)
                 + timedelta(days=2)).timestamp())
    events = {}
    for s in series:
        ms, cursor = [], None
        for _page in range(30):          # KXRAIN alone lists ~70 cities a day
            try:
                d = kc._get("/markets", {"series_ticker": s, "status": "settled", "limit": 1000,
                                         "min_close_ts": lo_ts, "max_close_ts": hi_ts,
                                         "cursor": cursor})
            except Exception:
                break
            ms += d.get("markets") or []
            cursor = d.get("cursor")
            time.sleep(cfg["pause_s"])
            if not cursor or not d.get("markets"):
                break
        for m in ms:
            p, _ = parse_market(m)
            if p is None or not (d0 <= p["day"] <= d1) or (kinds and p["kind"] not in kinds):
                continue
            # KXRAIN lists many cities in ONE event: group by station too
            events.setdefault((s, m.get("event_ticker"), p["station"]), []).append((m, p))
    mos = {}
    for (s, ev, _stn), items in sorted(events.items()):
        p0 = items[0][1]
        info = station_info(p0["station"], cache)
        if info is None:
            continue
        start, end = day_window(p0["day"], info["std_offset_h"])
        dec = datetime(p0["day"].year, p0["day"].month, p0["day"].day, decide_hour_utc,
                       tzinfo=timezone.utc) - timedelta(days=1)
        try:
            quotes = _event_candles(s, ev, int(dec.timestamp()))
        except Exception:
            continue
        time.sleep(cfg["pause_s"])
        srcs = {}
        for name, model, lag_h, field, sig, age in (("nbm", "NBS", 8, "txn", _nbm_sigma, 14),
                                                    ("gfs", "GFS", 5, "n_x", _gfs_sigma, 12)):
            # the newest 6-hourly run that was out by the decision hour: NBM
            # text is up ~1.5h after its cycle, GFS MOS ~4.5h; the lags are
            # rounded up so the backtest never reads a run that was not public
            r0 = dec - timedelta(hours=lag_h)
            run = r0.replace(hour=(r0.hour // 6) * 6, minute=0, second=0, microsecond=0)
            key = (p0["station"], model, run)
            if key not in mos:
                try:
                    mos[key] = _json(f"{IEM_MOS}?station={p0['station']}&model={model}"
                                     f"&runtime={run.strftime('%Y-%m-%dT%H:%MZ')}")
                except Exception:
                    mos[key] = None
                time.sleep(0.2)
            srcs[name] = mos_source(mos[key], p0["kind"], start, end, field, sig, dec, age)
        if p0["kind"] != "rain":
            actual = _num(items[0][0].get("expiration_value"))
            if actual is not None:
                errors.append({"event": ev, "kind": p0["kind"], "actual": actual,
                               **{k: round(v["mu"] - actual, 1) for k, v in srcs.items() if v}})
        for m, p in items:
            examined += 1
            bid, ask = quotes.get(m["ticker"], (None, None))
            mk = dict(m, yes_bid_dollars=bid, yes_ask_dollars=ask)
            model = blend(p["kind"], srcs, p["lo"], p["hi"])
            if model is not None:
                priced += 1
            e = evaluate(mk, model, cfg)      # every kind, so the backtest can judge each
            e["event"] = f"{ev}:{p0['station']}"
            if e["action"] == "buy":
                won = (str(m.get("result")).lower() == e["buy"])
                picks.append(dict(e, decided_at=dec.isoformat(), result=m.get("result"),
                                  won=won, kind=p["kind"], day=p["day"].isoformat(),
                                  station=p0["station"],
                                  expiration_value=m.get("expiration_value")))
    # apply the shared caps, best edge first within each decision day, one per event
    taken, by_day = [], {}
    for e in sorted(picks, key=lambda x: (x["decided_at"], -x["edge"] / max(x["breakeven"], .01))):
        if e["kind"] not in BUY_KINDS:
            continue                 # judged in by_kind, never "bought"
        d = by_day.setdefault(e["decided_at"][:10], {"spent": 0.0, "events": set()})
        if e["event"] in d["events"]:
            continue
        n, _b = size(e["cost"], ccfg, d["spent"], 0.0, 0.0)
        if n < 1:
            continue
        fee = (kc.fee_cents(e["cost"], n) or 0) / 100.0
        cost = n * e["cost"] + fee
        pnl = (n if e["won"] else 0.0) - cost
        d["spent"] += cost
        d["events"].add(e["event"])
        taken.append(dict(e, count=n, cost_usd=round(cost, 2), pnl_usd=round(pnl, 2)))
    all_pnl = sum(((1.0 if e["won"] else 0.0) - e["breakeven"]) for e in picks)
    by_kind = {}
    for e in picks:
        k = by_kind.setdefault(f"{e['kind']}_{e['buy']}", {"n": 0, "won": 0, "cost": 0.0, "pnl": 0.0})
        k["n"] += 1
        k["won"] += int(e["won"])
        k["cost"] += e["breakeven"]
        k["pnl"] += (1.0 if e["won"] else 0.0) - e["breakeven"]
    for k in by_kind.values():
        k["return_pct"] = round(100 * k["pnl"] / k["cost"], 1) if k["cost"] else None
        k["cost"], k["pnl"] = round(k["cost"], 2), round(k["pnl"], 2)
    out = {"first": first, "last": last, "decide_hour_utc": decide_hour_utc,
           "markets_examined": examined, "markets_priced": priced,
           "signals": len(picks), "signals_won": sum(1 for e in picks if e["won"]),
           "signals_pnl_per_contract": round(all_pnl, 2),
           "taken": taken, "taken_n": len(taken),
           "taken_won": sum(1 for e in taken if e["won"]),
           "taken_cost_usd": round(sum(e["cost_usd"] for e in taken), 2),
           "taken_pnl_usd": round(sum(e["pnl_usd"] for e in taken), 2),
           "by_kind": by_kind, "buy_kinds": sorted(BUY_KINDS),
           "signals_detail": picks,
           "forecast_error": _error_stats(errors), "errors_detail": errors}
    try:
        ct._write_json(STATION_CACHE, cache)
    except Exception:
        pass
    return out


# -------------------------------------------------------------------- cli --

def _print_run(st):
    s = st.get("stats") or {}
    print(f"WEATHER-EDGE {st.get('mode')} ({st.get('why_not_live') or 'armed'})  "
          f"{st.get('last_run', '')[:16]}")
    print(f"  series {s.get('series')} (failed {s.get('series_failed')}), open markets "
          f"{s.get('markets')}, in window {s.get('eligible')}, priced {s.get('priced')}, "
          f"station-days {s.get('stations')}, unknown stations {s.get('station_unknown')}"
          + (f", ERROR {s['error']}" if s.get("error") else ""))
    ev = [e for e in st.get("evaluations") or [] if "edge" in e]
    print(f"  buy signals: {st.get('n_buy_signals')}")
    print("  closest to the bar (edge = forecast - break-even, after fee):")
    for e in sorted(ev, key=lambda x: -x["edge"])[:15]:
        print(f"    {e['action']:<4} {e['ticker']:<32} {e['buy'].upper():<3} cost {e['cost']:.2f} "
              f"forecast {e['q']:.2f} edge {e['edge']:+.3f}  srcs "
              + " ".join(f"{k}={v:.2f}" for k, v in sorted(e['by_source'].items()))
              + (f"  ({e['why']})" if e.get("why") else ""))
    for o in st.get("orders") or []:
        print(f"  ORDER {o.get('status')}: {o.get('ticker')} {str(o.get('buy') or '').upper()} "
              f"x{o.get('count')} @{o.get('price')} ${o.get('cost_usd')} {o.get('why', '')}")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--backtest", nargs=2, metavar=("FIRST", "LAST"))
    ap.add_argument("--decide-hour", type=int, default=20)
    ap.add_argument("--kinds", help="comma list: rain,high,low (default all)")
    ap.add_argument("--disarm", action="store_true",
                    help="switch the scheduled go-live off on this host")
    a = ap.parse_args(argv)
    if a.disarm:
        live, why = disarm()
        print(f"scheduled go-live OFF on this host; now {'LIVE (own arm file)' if live else 'paper: ' + why}")
        return 0
    if a.backtest:
        r = backtest(a.backtest[0], a.backtest[1], a.decide_hour,
                     kinds=set(a.kinds.split(",")) if a.kinds else None)
        ct._write_json(BACKTEST_FILE, {k: v for k, v in r.items()})
        print(f"BACKTEST {r['first']}..{r['last']} decide D-1 {r['decide_hour_utc']}:00Z: "
              f"examined {r['markets_examined']}, priced {r['markets_priced']}, signals "
              f"{r['signals']} ({r['signals_won']} won, {r['signals_pnl_per_contract']:+.2f} per "
              f"1 contract each), within caps {r['taken_n']} bought ({r['taken_won']} won) "
              f"cost ${r['taken_cost_usd']:.2f} P&L ${r['taken_pnl_usd']:+.2f}")
        for k, v in sorted(r["by_kind"].items()):
            print(f"  signals {k:<9} n={v['n']:<4} won {v['won']:<4} return {v['return_pct']}% "
                  f"(1 contract each, fee in)")
        for k, v in (r.get("forecast_error") or {}).items():
            print(f"  forecast error {k:<9} n={v['n']:<4} bias {v['bias']:+.2f}F  sd {v['sd']:.2f}F  "
                  f"rmse {v['rmse']:.2f}F  within 1F {v['within_1F']:.0%}")
        for e in r["taken"]:
            print(f"  {e['decided_at'][:13]} {e['ticker']:<32} {e['buy'].upper():<3} x{e['count']} "
                  f"@{e['cost']:.2f} forecast {e['q']:.2f} -> {e['result']} "
                  f"(actual {e.get('expiration_value')}) P&L {e['pnl_usd']:+.2f}")
        return 0
    if a.run or a.dry_run:
        st = run_locked(dry_run=a.dry_run) if a.run else run(dry_run=True)
        if st:
            _print_run(st)
        return 0
    for ln in report_lines():
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
