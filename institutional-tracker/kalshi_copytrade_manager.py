#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI COPY-TRADE MANAGER
========================================
The one agent responsible for the copy-trading desk (kalshi_copytrade.py +
kalshi_copytrade_scout.py) being UP and PROFITABLE. Part of the Kalshi sector,
but outside the board process: Chairman, 2026-11-04 — "it should function
independently ... shouldn't need to talk to the board ... make a manager agent
that's responsible for making sure this thing is profitable and actually
working ... send me a notification on email if anything crashes".

WHAT IT CHECKS (every 15 minutes)
---------------------------------
  COPIER_NOT_RUNNING  the copier's state file is >10 min old. Cron fires it
                      every minute, so 10 minutes is ten missed runs.
  HALTED              data/.kalshi_copytrade_halted exists (3 order errors in
                      a row, or this manager's circuit breaker below).
  ARMED_BUT_PAPER     the Chairman armed it but it is still trading on paper.
                      This is the failure that actually happened on launch day:
                      KALSHI_ENV=live sat in .env and the copier never read it.
                      An armed bot that silently isn't trading looks exactly
                      like a quiet day, so it is named, not inferred.
  KEY_FAILED          the signed balance read fails (key revoked, expired, or
                      wiped from .env by a deploy).
  LOW_BALANCE         Kalshi cash below one day's cap: orders will bounce.
  ORDER_ERRORS        any order error in the last 24h.
  NO_LEADERS          nobody to copy.
  LEADER_HIDDEN       a followed trader made their trades private. The manager
                      re-runs the scout immediately, which drops them and
                      promotes the next profitable visible trader (Chairman,
                      2026-11-04: "if one person goes private it should find
                      another profitable user").
  SCOUT_STALE         the daily scout has not run in 30h.
  LOSING              P&L on every still-open copy (marked at the last trade
                      price, whatever its age) plus copies settled in the last
                      7 days is below -LOSS_LIMIT_DAYS x the daily cap. Until
                      2026-11-09 only SETTLED losses counted, and most copies
                      sit in 2027-28 election markets, so the book was -84%
                      (-$7.28 of $8.66) while the breaker read -$2.88 and never
                      moved. `--reset-breaker` starts the window afresh (after
                      a leader change, say) and clears a breaker pause. The manager trips the
                      circuit breaker (writes the halt file) and emails. Paused,
                      not disarmed: deleting the halt file resumes it. A
                      losing streak is a reason to stop and look, and the cost
                      of a false pause is a day of missed copies; the cost of
                      not pausing has no ceiling.

URGENT findings email immediately (6h cooldown per kind). Everything else, plus
a once-a-day performance line, goes into the 6 PM digest via email_buffer.

PERFORMANCE
-----------
`--report` prints P&L for today, this week, this month, this year and all
time. Accounting is on the YES book, the way V2 orders are expressed: a bid of
n at p pays n*p, an ask of n at p receives n*p, and the net YES position
settles at $1 or $0 (or is marked at the last trade price while open).
Fees are Kalshi's published taker fee. The fill price is taken as the order's
limit, which IOC fills meet or beat, so the figure can only understate P&L.
Periods are by the date we OPENED the copy (UTC).

Usage:
    python3 kalshi_copytrade_manager.py --check     # cron, every 15 min
    python3 kalshi_copytrade_manager.py --report    # health + performance
    python3 kalshi_copytrade_manager.py --reset-breaker   # restart the loss window
"""

import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import kalshi_client as kc
import kalshi_copytrade as ct

REPORT_FILE = ct.DATA_DIR / "kalshi_copytrade_manager_report.json"
STATE_FILE = ct.DATA_DIR / "kalshi_copytrade_manager_state.json"

STALE_COPIER_S = 600
SCOUT_STALE_H = 30
LOSS_LIMIT_DAYS = 2.0          # 7-day loss (settled + open) > 2 days' cap trips the breaker
ALERT_COOLDOWN_H = 6
URGENT = {"COPIER_NOT_RUNNING", "HALTED", "ARMED_BUT_PAPER", "KEY_FAILED",
          "LOSING", "NO_LEADERS"}


# ------------------------------------------------------------ performance --

def _market(ticker, cache):
    if ticker not in cache:
        try:
            cache[ticker] = kc._get(f"/markets/{urllib.parse.quote(str(ticker))}"
                                    ).get("market") or {}
        except Exception:
            cache[ticker] = None
    return cache[ticker]


def books(rows):
    """{ticker: {"cash", "pos", "fees", "opened_at", "leader", "spent"}} from
    filled live rows, in ledger order."""
    out = {}
    for r in rows:
        if r.get("status") != "placed":
            continue
        n = kc.parse_size(r.get("fill_count")) or 0.0
        p = r.get("price")
        if not n or p is None:
            continue
        p = float(p)
        b = out.setdefault(r["ticker"], {"cash": 0.0, "pos": 0.0, "fees": 0.0,
                                         "spent": 0.0, "opened_at": r.get("at"),
                                         "leader": r.get("leader")})
        if r.get("side") == "bid":
            b["cash"] -= n * p
            b["pos"] += n
        else:
            b["cash"] += n * p
            b["pos"] -= n
        if not r.get("reduce_only"):
            b["spent"] += n * (p if r.get("side") == "bid" else 1.0 - p)
        b["fees"] += kc.fee_cents(p, max(1, int(round(n)))) / 100.0
    return out


def value_book(b, market):
    """(realised, unrealised, settled?) for one ticker's book."""
    if not market:
        return 0.0, 0.0, False
    res = str(market.get("result") or "").lower()
    if res in ("yes", "no"):
        return b["cash"] + (b["pos"] if res == "yes" else 0.0) - b["fees"], 0.0, True
    mark = kc.parse_price(market.get("last_price_dollars"))
    if mark is None:
        mark = 0.0 if b["pos"] > 0 else 1.0     # no price: assume the worst
    return 0.0, b["cash"] + b["pos"] * mark - b["fees"], False


PERIODS = ("today", "week", "month", "year", "all_time")


def period_start(name, now):
    if name == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "week":
        d = now - timedelta(days=now.weekday())
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if name == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def performance(rows=None, now=None, market=None):
    """P&L per period, by the date each ticker's copy was first opened."""
    now = now or datetime.now(timezone.utc)
    rows = ct.ledger_rows() if rows is None else rows
    cache = {}
    market = market or (lambda t: _market(t, cache))
    bk = books(rows)
    valued = {t: (b, *value_book(b, market(t))) for t, b in bk.items()}
    out = {}
    for name in PERIODS:
        start = period_start(name, now)
        sel = [v for v in valued.values()
               if start is None or (ct._parse_ts(v[0]["opened_at"]) or now) >= start]
        realised = sum(v[1] for v in sel)
        unrealised = sum(v[2] for v in sel)
        settled = [v for v in sel if v[3]]
        wins = sum(1 for v in settled if v[1] > 0)
        spent = sum(v[0]["spent"] for v in sel)
        out[name] = {"markets": len(sel), "settled": len(settled), "wins": wins,
                     "spent_usd": round(spent, 2),
                     "realised_usd": round(realised, 2),
                     "unrealised_usd": round(unrealised, 2),
                     "total_usd": round(realised + unrealised, 2),
                     "return_pct": round(100 * (realised + unrealised) / spent, 1)
                     if spent else None}
    by_leader = {}
    for b, real, unreal, _s in valued.values():
        L = by_leader.setdefault(b["leader"], {"markets": 0, "total_usd": 0.0})
        L["markets"] += 1
        L["total_usd"] = round(L["total_usd"] + real + unreal, 2)
    out["by_leader"] = by_leader
    return out


# ------------------------------------------------------------------ health --

def _age_s(path, now):
    try:
        return now.timestamp() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def breaker_reset_at():
    raw = ct._read_json(ct.CONFIG_FILE, {})
    return ct._parse_ts(raw.get("breaker_reset_at")) if isinstance(raw, dict) else None


def breaker_window_start(now):
    """Settled copies opened at or after this count toward the breaker: the
    last 7 days, or since `--reset-breaker` if that is more recent. Open
    positions count regardless of age (only a reset drops them)."""
    reset = breaker_reset_at()
    week_ago = now - timedelta(days=7)
    return max(week_ago, reset) if reset else week_ago


def reset_breaker(now=None):
    """Chairman action: start the loss window now and clear a breaker pause.
    A halt written by the copier for order errors is left alone."""
    now = now or datetime.now(timezone.utc)
    raw = ct._read_json(ct.CONFIG_FILE, {})
    raw = raw if isinstance(raw, dict) else {}
    raw["breaker_reset_at"] = now.isoformat()
    ct._write_json(ct.CONFIG_FILE, raw)
    h = ct._read_json(ct.HALT_FILE, None)
    cleared = (isinstance(h, dict) and
               str(h.get("reason", "")).startswith("manager circuit breaker"))
    if cleared:
        ct.HALT_FILE.unlink()
    return cleared


def check_health(now=None, rows=None, balance=None, market=None, fetch=None):
    """List of {"kind", "severity", "detail"}. Pure apart from the injected I/O."""
    now = now or datetime.now(timezone.utc)
    rows = ct.ledger_rows() if rows is None else rows
    cfg = ct.load_config()
    findings = []

    def add(kind, detail, severity="HIGH"):
        findings.append({"kind": kind, "severity": "CRITICAL" if kind in URGENT
                         else severity, "detail": detail})

    arm = ct._read_json(ct.ARM_FILE, {})
    armed_intent = isinstance(arm, dict) and arm.get("armed") is True
    live, why = ct.armed()

    age = _age_s(ct.STATE_FILE, now)
    if age is None or age > STALE_COPIER_S:
        add("COPIER_NOT_RUNNING", "the copier has not run for "
            f"{'ever' if age is None else f'{age / 60:.0f} min'}; check the VM crontab")
    if ct.HALT_FILE.exists():
        h = ct._read_json(ct.HALT_FILE, {})
        add("HALTED", f"copying is paused: {h.get('reason') or h.get('last_error') or 'see ledger'}."
            " Delete data/.kalshi_copytrade_halted to resume")
    elif armed_intent and not live:
        add("ARMED_BUT_PAPER", f"armed, but trading on paper because: {why}")

    if armed_intent:
        if balance is None:
            try:
                b = kc.get_balance()
                # The cash the copier can SPEND: the shards it trades on, not the
                # account total. 2026-11-11 the total was $16.13 (above the $3
                # cap, so this stayed silent) while the one shard every copied
                # market trades on held $0.57, and three orders bounced.
                per = ct.shard_cash(b)
                if per is not None:
                    balance = sum(per.get(i, 0.0) for i in cfg.get(
                        "exchange_indexes", ct.DEFAULTS["exchange_indexes"]))
                else:
                    balance = float(b.get("balance", 0)) / 100.0
            except Exception as e:
                add("KEY_FAILED", f"Kalshi key check failed: {str(e)[:160]}")
        if balance is not None and balance < cfg["per_day_usd"]:
            add("LOW_BALANCE", f"Kalshi cash on the shard(s) we trade is ${balance:.2f}, below the "
                f"${cfg['per_day_usd']:.2f} daily cap; orders will bounce", "MEDIUM")

    day_ago = now - timedelta(days=1)
    errs = [r for r in rows if r.get("status") == "error"
            and (ct._parse_ts(r.get("at")) or now) >= day_ago]
    if errs:
        add("ORDER_ERRORS", f"{len(errs)} order error(s) in 24h, latest: "
            f"{errs[-1].get('error', '')[:160]}")

    if not cfg["leaders"]:
        add("NO_LEADERS", "following nobody; the scout found no eligible trader")
    fetch = fetch or ct.fetch_trades
    hidden = []
    for L in cfg["leaders"]:
        try:
            if fetch(L).get("visibility_state") == "hidden":
                hidden.append(L)
        except Exception:
            pass
    if hidden:
        add("LEADER_HIDDEN", f"{', '.join(hidden)} made their trades private; "
            "re-running the scout to replace them", "MEDIUM")

    import kalshi_copytrade_scout as sc
    s_age = _age_s(sc.REPORT_FILE, now)
    if s_age is None or s_age > SCOUT_STALE_H * 3600:
        add("SCOUT_STALE", "the daily scout has not run in "
            f"{'ever' if s_age is None else f'{s_age / 3600:.0f}h'}", "MEDIUM")

    since = breaker_window_start(now)
    cache = {}
    market = market or (lambda t: _market(t, cache))
    real7 = 0.0
    reset = breaker_reset_at()
    unreadable = []
    for t, b in books(rows).items():
        opened = ct._parse_ts(b["opened_at"]) or now
        if reset and opened < reset:
            continue
        mk = market(t)
        if not mk:
            unreadable.append(t)
            continue
        realised, unrealised, settled = value_book(b, mk)
        # an OPEN position's loss counts for as long as it is open, however
        # old: a 2028 market must not age out of the breaker after a week
        # (the first version of this did; caught before deploy, 2026-11-09).
        # Settled losses count for 7 days, so one bad week does not pause
        # copying forever.
        if not settled or opened >= since:
            real7 += realised + unrealised
    if unreadable:
        add("MARKS_UNREADABLE", f"could not price {len(unreadable)} market(s) "
            f"({', '.join(unreadable[:3])}); the loss breaker cannot see them", "MEDIUM")
    limit = LOSS_LIMIT_DAYS * cfg["per_day_usd"]
    if real7 < -limit:
        add("LOSING", f"down ${-real7:.2f} on open copies plus copies settled "
            f"since {since:%b %d %H:%M} UTC (open marked at last price), past the "
            f"-${limit:.2f} limit; circuit breaker tripped, copying paused")
    return findings, hidden, real7


# ------------------------------------------------------------- escalation --

def _send(finding, state, now, send_urgent=None, send_digest=None):
    """Email one finding unless the same kind was sent within the cooldown."""
    sent = state.setdefault("last_sent", {})
    last = ct._parse_ts(sent.get(finding["kind"]))
    if last and now - last < timedelta(hours=ALERT_COOLDOWN_H):
        return False
    subject = f"Kalshi copy-trader: {finding['kind'].replace('_', ' ').lower()}"
    if finding["kind"] == "LOSING":
        subject = "Kalshi copy-trader circuit breaker tripped"
    body = f"<p>{finding['detail']}</p>"
    try:
        if finding["severity"] == "CRITICAL":
            if send_urgent is None:
                from email_sender import send_alert_email as send_urgent
            ok = send_urgent(subject, body, force=True)
        else:
            if send_digest is None:
                from email_buffer import buffer_email as send_digest
            ok = send_digest(subject=subject, html_body=body,
                             sender_name="Kalshi Copy-Trade Manager",
                             dedup_key=f"kalshi-copytrade-{finding['kind']}")
    except Exception:
        ok = False
    if ok is not False:
        sent[finding["kind"]] = now.isoformat()
    return ok


def _worst(findings):
    if not findings:
        return "OK"
    return max((f["severity"] for f in findings),
               key=lambda s: ["MEDIUM", "HIGH", "CRITICAL"].index(s))


def deployed_here():
    """True on the host that runs the desk. The Mac has the code but neither
    the arm file nor copier state, and must not email "copier not running"
    every 15 minutes about a bot it was never meant to run."""
    return ct.ARM_FILE.exists() or ct.STATE_FILE.exists()


def run_check(now=None, send_urgent=None, send_digest=None, rescout=None,
              notify=True):
    """`notify=False` (used by --report) computes and prints but never emails,
    trips the breaker, or re-scouts: asking how it is doing must not act."""
    now = now or datetime.now(timezone.utc)
    rows = ct.ledger_rows()
    if not deployed_here():
        rep = {"at": now.isoformat(), "findings": [], "status": "NOT_DEPLOYED_HERE",
               "performance": performance(rows, now)}
        return rep
    findings, hidden, real7 = check_health(now=now, rows=rows)
    if not notify:
        rep = {"at": now.isoformat(), "findings": findings,
               "status": _worst(findings), "performance": performance(rows, now)}
        return rep
    if any(f["kind"] == "LOSING" for f in findings) and not ct.HALT_FILE.exists():
        ct._write_json(ct.HALT_FILE, {"at": now.isoformat(), "reason":
                                      f"manager circuit breaker: recent copies ${real7:.2f} "
                                      "(settled + open)"})
    if hidden:
        try:
            (rescout or __import__("kalshi_copytrade_scout").scout)()
        except Exception:
            pass
    state = ct._read_json(STATE_FILE, {})
    state = state if isinstance(state, dict) else {}
    for f in findings:
        _send(f, state, now, send_urgent, send_digest)

    perf = performance(rows, now)
    day = now.strftime("%Y-%m-%d")
    if state.get("summary_day") != day and now.hour >= 21:
        a, t = perf["all_time"], perf["today"]
        try:
            buffer_email = send_digest
            if buffer_email is None:
                from email_buffer import buffer_email
            buffer_email(subject="Kalshi copy-trader daily summary",
                         html_body=f"<p>Today ${t['total_usd']:+.2f} on {t['markets']} "
                         f"market(s). All time ${a['total_usd']:+.2f} "
                         f"({a['settled']} settled, {a['wins']} won). Health: "
                         f"{', '.join(f['kind'] for f in findings) or 'OK'}.</p>",
                         sender_name="Kalshi Copy-Trade Manager",
                         dedup_key="kalshi-copytrade-daily")
            state["summary_day"] = day
        except Exception:
            pass
    ct._write_json(STATE_FILE, state)
    report = {"at": now.isoformat(), "findings": findings,
              "status": _worst(findings), "performance": perf}
    ct._write_json(REPORT_FILE, report)
    return report


# --------------------------------------------------------------------- cli --

def explain_copies(rows, limit=15):
    """One plain-English line per order we sent (newest last), so "why did it
    buy THAT?" is answerable from --report: who we copied, which side, what
    they paid, what we paid, how many, the cash at risk and the payout if it
    wins. Added 2026-11-06 after a $2.55 copy of a 6c longshot could not be
    explained without digging through the raw ledger."""
    out = []
    for r in [r for r in rows if r.get("status") in ("placed", "paper", "error")][-limit:]:
        side = r.get("side")
        what = ("sell " if r.get("reduce_only") else "buy ") + \
            (("YES" if side == "bid" else "NO") if not r.get("reduce_only")
             else ("YES" if side == "ask" else "NO"))
        px = r.get("price")
        our = None if px is None else (float(px) if side == "bid" else 1.0 - float(px))
        lp = r.get("leader_price")
        their = None if lp is None else (float(lp) if side == "bid" else 1.0 - float(lp))
        if r.get("reduce_only"):
            their, our = (None if their is None else 1.0 - their,
                          None if our is None else 1.0 - our)
        filled = r.get("fill_count") if r.get("status") == "placed" else r.get("count")
        n = kc.parse_size(filled) or 0
        cost = n * our if (our is not None and not r.get("reduce_only")) else 0.0
        bits = [str(r.get("at", ""))[:16].replace("T", " "), r.get("leader") or "?",
                str(r.get("ticker")), what,
                (f"they sold at {their:.2f}" if r.get("reduce_only") else
                 f"they paid {their:.2f}") if their is not None else "",
                (f"we sold at >= {our:.2f}" if r.get("reduce_only") else
                 f"we paid <= {our:.2f}") if our is not None else "",
                f"x{n:g}"]
        if not r.get("reduce_only") and n:
            bits.append(f"cost ${cost:.2f}, pays ${n:.0f} if right")
        st = r.get("status")
        if st == "paper":
            bits.append("[paper]")
        elif st == "error":
            bits.append(f"[ERROR: {str(r.get('error'))[:60]}]")
        elif n == 0:
            bits.append("[not filled]")
        out.append("  ".join(b for b in bits if b))
    return out


def skip_reasons(rows, since):
    """{reason-kind: count} of leader fills we chose NOT to copy since `since`."""
    out = {}
    for r in rows:
        if r.get("status") != "skipped" or str(r.get("at", "")) < since:
            continue
        k = str(r.get("reason") or "?").split(" (")[0].split(":")[0]
        k = "price outside band" if "outside" in k else k
        out[k] = out.get(k, 0) + 1
    return out


def print_report(rep):
    live, why = ct.armed()
    cfg = ct.load_config()
    print("KALSHI COPY-TRADE MANAGER")
    print(f"  mode    : {'LIVE' if live else 'paper (' + why + ')'}")
    print(f"  leaders : {', '.join(cfg['leaders']) or '(none)'}")
    print(f"  limits  : ${cfg['per_trade_usd']:.2f} per market, ${cfg['per_day_usd']:.2f} per day, "
          f"max ${ct.payout_cap(cfg):.0f} payout per market, only sides priced "
          f"{ct.MIN_PRICE*100:.0f}-{ct.MAX_PRICE*100:.0f}c")
    print(f"  health  : {rep['status']}")
    for f in rep["findings"]:
        print(f"    [{f['severity']}] {f['kind']}: {f['detail']}")
    print("  performance (by the day the copy was opened, UTC):")
    print(f"    {'period':<9} {'P&L':>9} {'return':>8} {'spent':>8} "
          f"{'markets':>8} {'settled':>8} {'won':>5}")
    for name in PERIODS:
        p = rep["performance"][name]
        ret = "" if p["return_pct"] is None else f"{p['return_pct']:+.1f}%"
        print(f"    {name:<9} {p['total_usd']:>+9.2f} {ret:>8} {p['spent_usd']:>8.2f} "
              f"{p['markets']:>8} {p['settled']:>8} {p['wins']:>5}")
    for L, v in rep["performance"]["by_leader"].items():
        print(f"    by leader: {L:<24} {v['total_usd']:+.2f} on {v['markets']} market(s)")
    rows = ct.ledger_rows()
    lines = explain_copies(rows)
    print("  orders sent (latest 15; price = cost of the side bought, $1 payout each):")
    for ln in lines or ["(none yet)"]:
        print(f"    {ln}")
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    skips = skip_reasons(rows, since)
    if skips:
        print("  leader fills NOT copied in the last 24h: " +
              ", ".join(f"{k} x{v}" for k, v in sorted(skips.items(), key=lambda x: -x[1])))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset-breaker", action="store_true",
                    help="count losses only from now on, and clear a breaker pause")
    a = ap.parse_args(argv)
    if a.reset_breaker:
        cleared = reset_breaker()
        print("breaker window restarted now"
              + ("; breaker pause cleared, copying resumes" if cleared else ""))
    elif a.check:
        run_check()
    else:
        print_report(run_check(notify=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
