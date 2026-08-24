#!/usr/bin/env python3
"""
APEX CAPITAL — RECONCILIATION ENGINE
=====================================
Compares what agents promised against Alpaca ground truth. Does not trust any
agent output — goes straight to the source.

The canonical list of checks is `_CHECKS` below, not this docstring. It used to
be enumerated here as seven items, the firm-channel all-clear hardcoded "All 7
reconciliation checks passed", and neither number was ever recomputed as checks
were added — so both were wrong by two.

SCHEDULING (2026-08-24). This file's header said "Runs every 30 minutes" for the
firm's entire history and it has never been true: `run_reconciliation()` is
called from exactly one place, the `__main__` block at the bottom, and this
module appears in NO scheduler on either host — not the VM's 68-job crontab, not
the Mac's two launchd agents. It looked employed to wiring_audit only because
risk_gate.py imports `_mirror_check` from it, and reachability over the import
graph cannot tell "someone imports a helper from this file" from "this file's
work gets done". data/firm_value.json — whose sole writer is this module and
which performance_forecaster reads as the firm's equity — was five days stale
when that was found. Scheduling it is a Chairman decision; see the Chairman
queue. Until then, treat every finding below as a check that CAN fire, not one
that DOES.

Any flag goes to firm channel. CRITICAL flags email Jack immediately.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reconciliation")

REPORT_PATH = "data/reconciliation_report.json"

# Mirror config.py guardrails — deliberately hardcoded here so the
# reconciler doesn't silently pass if config.py has a wrong value.
_MIN_CASH_FLOOR_PCT    = 0.20
_MAX_POSITIONS         = 20     # 2026-07-27 pick-gate trial raised it 15 -> 20;
                                # reverts to 15 if the trial is not extended, and
                                # _mirror_check below will say so when it does
_STOP_LOSS_PCT         = 0.07   # 7%
_MAX_POSITION_SIZE_PCT = 0.07   # 7% (10% grace before flagging)
_IMMEDIATE_TIMEOUT_H   = 2      # IMMEDIATE board actions must execute within 2h
_PNL_DRIFT_THRESHOLD   = 1000   # flag if pipeline P&L tracking drifts > $1000 from Alpaca

# ── The mirror has to be checked, or it is just a second wrong number ────────
# The hardcoding above is right: an independent copy is what lets this file
# disagree with config.py, and disagreement is the whole point. What was missing
# is that nothing ever COMPARED them, so a legitimate config change looked
# identical to a tampered one — and the disagreement surfaced disguised as
# something else.
#
# Measured 2026-08-19: the board raised MAX_OPEN_POSITIONS 15 -> 20 for the
# pick-gate trial on 2026-07-27. This mirror stayed at 15 for 23 days and
# reported "21 positions open — exceeds 15 maximum" on every run. The count was
# over the real cap too, so the flag was not wrong — but it named the wrong
# number, and a HIGH that quotes a stale limit is how a reader learns to skim
# the HIGHs. Nobody was told the guardrail itself had moved.
#
# So: compare, flag the divergence as its OWN finding, and keep evaluating
# against whichever bound is stricter. Stricter preserves the tamper guard — a
# config loosened to 999 cannot widen this check — while the flag makes sure a
# deliberate change is noticed rather than absorbed.
#: mirror name -> (local value, config attribute, "cap" | "floor")
_MIRRORED = {
    "MIN_CASH_FLOOR_PCT":    (_MIN_CASH_FLOOR_PCT,    "MIN_CASH_FLOOR_PCT",    "floor"),
    "MAX_OPEN_POSITIONS":    (_MAX_POSITIONS,         "MAX_OPEN_POSITIONS",    "cap"),
    "STOP_LOSS_PCT":         (_STOP_LOSS_PCT,         "STOP_LOSS_PCT",         "cap"),
    "MAX_POSITION_SIZE_PCT": (_MAX_POSITION_SIZE_PCT, "MAX_POSITION_SIZE_PCT", "cap"),
}


def _mirror_check():
    """(effective bounds, divergences). Never raises — a broken config is itself
    a finding, not a reason for the reconciler to stop reconciling."""
    try:
        import config
    except Exception as exc:                     # pragma: no cover — import guard
        return ({k: v[0] for k, v in _MIRRORED.items()},
                [("config", None, None, f"config.py will not import ({exc})")])

    effective, drift = {}, []
    for name, (local, attr, kind) in _MIRRORED.items():
        theirs = getattr(config, attr, None)
        if theirs is None:
            effective[name] = local
            drift.append((name, local, None,
                          f"config.{attr} no longer exists — this mirror is now "
                          f"the only copy and nothing can contradict it"))
            continue
        effective[name] = (min(local, theirs) if kind == "cap"
                           else max(local, theirs))
        if abs(float(theirs) - float(local)) > 1e-9:
            drift.append((name, local, theirs,
                          f"reconciler mirrors {local}, config.{attr} is {theirs} — "
                          f"checking against the stricter {effective[name]}"))
    return effective, drift


# The checks this module actually performs, in the order it performs them.
#
# It exists because the firm-channel all-clear used to read "All 7 reconciliation
# checks passed." There were never 7: the progress labels alone run [0/7] through
# [7/7], which is eight, and the pod-ledger attribution check ahead of them is
# unnumbered, which makes nine. A hardcoded count is a claim about coverage that
# nothing recomputes, so it was wrong the day a check was added and stayed wrong.
#
# Worse than the arithmetic: that sentence counted a check that COULD NOT RUN as
# a check that passed. Four of the nine sit inside `except Exception` handlers
# that log a warning and continue, so a CRO bulletin that fails to parse and a
# CRO bulletin that is clean produced the identical NORMAL-priority "all passed"
# post. This is the same rule the coherence laws now carry: a pass has to say how
# many things it looked at, and a pass on something you could not examine is not
# a pass.
_CHECKS = ("LEDGER_ATTRIBUTION", "GUARDRAIL_MIRROR", "CASH_FLOOR", "MAX_POSITIONS",
           "STOP_LOSS", "POSITION_SIZE", "CRO_BUY_FREEZE", "BOARD_EXECUTION",
           "PNL_DRIFT")

# Severity order for the prefix-slicing consumers below. Ranked once, here,
# because three separate readers take a `[:N]` of the flag list.
_SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}


def _flag(flags: list, severity: str, check: str, detail: str):
    logger.warning(f"  [{severity}] {check}: {detail}")
    flags.append({"severity": severity, "check": check, "detail": detail, "ts": datetime.now().isoformat()})


def _load_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def run_reconciliation() -> dict:
    flags: list[dict] = []
    # Checks that raised and were swallowed by a section-level handler. An entry
    # here means "unknown", never "clean" — see _CHECKS.
    skipped: list = []
    now = datetime.now()

    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — RECONCILIATION ENGINE")
    logger.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ── Connect to Alpaca (ground truth) ──────────────────────────────────
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        account   = trader.get_account()
        positions = trader.get_positions()
    except Exception as e:
        logger.error(f"  Alpaca connection failed — reconciliation aborted: {e}")
        return {"error": str(e), "flags": [], "generated_at": now.isoformat()}

    portfolio_value = account.get("portfolio_value", 0) or 1
    cash            = account.get("cash", 0) or 0
    cash_pct        = cash / portfolio_value
    n_positions     = len(positions) if positions is not None and not positions.empty else 0

    logger.info(f"  Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f} ({cash_pct*100:.1f}%) | Positions: {n_positions}")

    # ── AUTHORITATIVE FIRM VALUE (2026-07-23, rewritten 2026-08-19) ────────
    # The Alpaca account is ground truth. It always was; the pod ledger is an
    # ATTRIBUTION of that equity, not a second account holding its own money.
    #
    # This block used to compare `pod_ledger`'s total against broker equity and
    # flag the difference as LEDGER_DRIFT. It fired every run for weeks and was
    # never closed, for three reasons worth keeping written down:
    #
    #   1. The two sides were different kinds of thing. The pod total was
    #      budget headroom PLUS market value — an entitlement added to a price
    #      — so part of the "drift" was the units, not an error.
    #   2. One number covered three unrelated defects (a mark-to-market job with
    #      no caller, unattributed positions, a book reserve with no broker
    #      counterpart). Nobody can close a sum.
    #   3. It named no owner.
    #
    # What is compared now is a conservation identity between two measurements
    # of the SAME quantity — the market value of the firm's positions, once as
    # the broker reports it and once as the pods claim it — and the residual is
    # decomposed into individually closable causes. The headline number is the
    # share of the book that belongs to no pod, because that is the one with a
    # decision attached: exposure nobody's budget is charged for is exposure
    # nobody is sizing.
    try:
        import pod_ledger
        broker_pos = {}
        if positions is not None and not positions.empty:
            for _, _p in positions.iterrows():
                broker_pos[str(_p.get("ticker", "")).upper()] = {
                    "qty": float(_p.get("qty", 0) or 0),
                    "price": float(_p.get("current_price", 0) or 0),
                }
        recon = pod_ledger.reconcile_to_broker(broker_pos, portfolio_value, cash)
        deployed = recon["pod_deployed_total"]
        book = recon["broker_positions_value"]
        unattr_pct = recon["unattributed_pct_of_book"]

        with open("data/firm_value.json", "w") as fh:
            json.dump({
                "equity": round(portfolio_value, 2),
                "cash": round(cash, 2),
                "as_of": now.isoformat(),
                "source": "alpaca_paper_account (ground truth)",
                "pod_deployed_total": deployed,
                "broker_positions_value": book,
                "attribution_residual": recon["residual"],
                "unattributed_value": recon["unattributed_value"],
                "unattributed_pct_of_book": unattr_pct,
                "reconciliation": recon,
            }, fh, indent=2)
        logger.info(f"  Authoritative firm value written: ${portfolio_value:,.2f} "
                    f"| book ${book:,.0f}, attributed to pods ${deployed:,.0f} "
                    f"({100.0 - unattr_pct:.0f}% covered)")
        for _c in recon["causes"]:
            logger.info(f"    unreconciled: {_c}")

        # Threshold is on ATTRIBUTION COVERAGE, not on a dollar difference.
        # 10% of the book unowned is the point at which pod-level risk numbers
        # stop describing the firm.
        if unattr_pct > 10.0:
            lead = recon["causes"][0] if recon["causes"] else (
                "no known cause accounts for it — the gap is unexplained, which "
                "is a stronger finding than the residual itself")
            _flag(flags, "HIGH", "LEDGER_ATTRIBUTION",
                  f"{unattr_pct:.0f}% of the book (${recon['unattributed_value']:,.0f} "
                  f"of ${book:,.0f}) belongs to no pod, so no pod's budget is "
                  f"charged for it and pod-level exposure numbers understate the "
                  f"firm. Largest gap: {lead}. "
                  f"Full split in data/firm_value.json.reconciliation.")
        elif recon["causes"] and abs(recon["residual"]) > 0.02 * (book or 1.0):
            _flag(flags, "MEDIUM", "LEDGER_ATTRIBUTION",
                  f"Pod claims and the broker disagree by ${recon['residual']:,.0f} "
                  f"on a ${book:,.0f} book. Largest cause: {recon['causes'][0]}.")
    except Exception as _fve:
        # This used to swallow the whole block. A reconciliation that fails
        # silently is indistinguishable from one that passes, so say so loudly
        # and make the silence itself a flag.
        logger.warning(f"  Could not reconcile the pod ledger: {_fve}")
        skipped.append("LEDGER_ATTRIBUTION")
        _flag(flags, "MEDIUM", "LEDGER_ATTRIBUTION_UNAVAILABLE",
              f"The pod-ledger reconciliation did not run ({_fve}). No claim is "
              f"being made about attribution coverage this cycle — treat the "
              f"absence of a LEDGER_ATTRIBUTION flag as unknown, not as clean.")

    # ── CHECK 0: do the two copies of the guardrails still agree? ──────────
    bounds, drift = _mirror_check()
    cash_floor  = bounds["MIN_CASH_FLOOR_PCT"]
    max_pos     = bounds["MAX_OPEN_POSITIONS"]
    stop_pct    = bounds["STOP_LOSS_PCT"]
    max_size    = bounds["MAX_POSITION_SIZE_PCT"]
    logger.info("\n  [2/9] Guardrail mirror...")
    for name, local, theirs, detail in drift:
        _flag(flags, "HIGH", "GUARDRAIL_DRIFT", f"{name}: {detail}")
    if not drift:
        logger.info("    ✓ all %d guardrails match config.py" % len(_MIRRORED))

    # ── CHECK 1: Cash floor ────────────────────────────────────────────────
    logger.info("  [3/9] Cash floor...")
    if cash_pct < cash_floor:
        # This used to end "A buy bypassed the cash floor guardrail." That was
        # true and is not any more: the floor lived only in
        # AlpacaTrader.submit_buy while six other paths reached the broker
        # without it. It now sits in risk_gate, at the order_ledger choke point
        # every path shares. Leaving the old sentence would keep reporting a
        # fixed defect as live, and the reader would eventually stop believing
        # the flag — so the wording states the breach and who is holding it.
        _flag(flags, "HIGH", "CASH_FLOOR",
              f"Cash is {cash_pct*100:.1f}% of portfolio (${cash:,.0f}) — "
              f"below {cash_floor*100:.0f}% floor. New buys are blocked by "
              f"risk_gate until cash recovers; exits are not. Cash rises by "
              f"selling, which is a Chairman decision, not a code change.")
    else:
        logger.info(f"    ✓ Cash {cash_pct*100:.1f}% ≥ {cash_floor*100:.0f}% floor")

    # ── CHECK 2: Position count ────────────────────────────────────────────
    logger.info("  [4/9] Position count...")
    if n_positions > max_pos:
        _flag(flags, "HIGH", "MAX_POSITIONS",
              f"{n_positions} positions open — exceeds {max_pos} maximum.")
    else:
        logger.info(f"    ✓ {n_positions}/{max_pos} positions")

    # ── CHECK 3 & 4: Per-position compliance ──────────────────────────────
    logger.info("  [5/9] Stop-loss compliance...")
    logger.info("  [6/9] Position size compliance...")
    if positions is not None and not positions.empty:
        evaluated, unevaluable = 0, []
        for _, pos in positions.iterrows():
            ticker  = pos.get("ticker", "?")
            try:
                # Column is `unrealized_pct`, already in percent — see
                # deputy_monitor. STOP_LOSS_BREACH could never be raised.
                pnl_pct = float(pos["unrealized_pct"])
                mv      = float(pos.get("market_value", 0))
                weight  = mv / portfolio_value

                # NaN IS NOT MISSING. float("nan") survives float(), passes every
                # `is not None` guard, and then LOSES every comparison: `nan <=
                # -7.0` is False and `nan > 0.077` is False, so a position the
                # broker could not price sails through both breach tests with no
                # flag and — because nothing raised — not even the debug line in
                # the handler below. The most severe check in this file would
                # have reported clean on a position it never evaluated (memory
                # `nan-is-not-none`). Raise so the position lands in
                # `unevaluable` and is counted against the denominator.
                if pnl_pct != pnl_pct or weight != weight:
                    raise ValueError("unrealized_pct or market_value is NaN — "
                                     "the broker did not price this position")

                if pnl_pct <= -(stop_pct * 100):
                    _flag(flags, "CRITICAL", "STOP_LOSS_BREACH",
                          f"{ticker} is at {pnl_pct:+.1f}% — breaches "
                          f"-{stop_pct*100:.0f}% stop-loss and is still held. "
                          f"Stop-loss enforcement missed this position.")

                if weight > max_size * 1.10:   # 10% grace for intraday drift
                    _flag(flags, "MEDIUM", "POSITION_SIZE_BREACH",
                          f"{ticker} is {weight*100:.1f}% of portfolio (${mv:,.0f}) — "
                          f"exceeds {max_size*100:.0f}% limit.")
                evaluated += 1
            except Exception as _pe:
                unevaluable.append(f"{ticker} ({_pe})")
                logger.debug(f"    Position parse error for {ticker}: {_pe}")

        # THE DENOMINATOR. "No stop-loss breach" across 19 positions and across
        # the 3 it could actually read are the same sentence and opposite facts,
        # and this loop used to emit the first while meaning the second. The
        # silence of a CRITICAL check only means something if you know what it
        # covered.
        if unevaluable:
            _flag(flags, "HIGH", "STOP_LOSS_UNEVALUABLE",
                  f"Stop-loss and position-size were evaluated on {evaluated} of "
                  f"{len(positions)} open position(s); {len(unevaluable)} could not "
                  f"be read ({', '.join(unevaluable[:5])}). The absence of a "
                  f"STOP_LOSS_BREACH flag says nothing about those positions.")
        else:
            logger.info(f"    ✓ {evaluated}/{len(positions)} positions evaluated — "
                        f"no stop-loss or size breach")
    else:
        logger.info("    ✓ No open positions")

    # ── CHECK 5: CRO CRITICAL buy compliance ──────────────────────────────
    logger.info("  [7/9] CRO CRITICAL buy compliance...")
    try:
        rb = _load_json("data/risk_bulletin.json")
        risk_level = rb.get("risk_level", "")
        guidance   = rb.get("capital_guidance", "")
        rb_ts      = rb.get("generated_at", "")

        if risk_level == "CRITICAL" and "PRESERVE" in guidance.upper():
            # Check if bulletin is recent (within last 24h — could be yesterday's CRITICAL run)
            try:
                rb_age_h = (now - datetime.fromisoformat(rb_ts[:19])).total_seconds() / 3600
            except Exception:
                rb_age_h = 0

            if rb_age_h < 24:
                # Fetch today's Alpaca orders
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=today_start)
                orders = trader.client.get_orders(filter=req)
                today_buys = [o for o in orders if str(o.side).lower() == "buy"]
                if today_buys:
                    tickers = [o.symbol for o in today_buys]
                    _flag(flags, "CRITICAL", "CRO_CRITICAL_BUY_VIOLATION",
                          f"CRO bulletin is CRITICAL/PRESERVE CAPITAL (issued {rb_age_h:.0f}h ago) "
                          f"but {len(today_buys)} buy order(s) executed today: {tickers}. "
                          f"CRO CRITICAL enforcement failed.")
                else:
                    logger.info(f"    ✓ No buys today — CRITICAL/PRESERVE CAPITAL respected")
            else:
                logger.info(f"    ✓ CRITICAL bulletin is {rb_age_h:.0f}h old — outside 24h window")
        else:
            logger.info(f"    ✓ CRO risk level is {risk_level or 'UNKNOWN'} — no buy freeze active")
    except Exception as e:
        logger.warning(f"    CRO compliance check error: {e}")
        skipped.append("CRO_BUY_FREEZE")

    # ── CHECK 6: Unexecuted IMMEDIATE board decisions ──────────────────────
    logger.info("  [8/9] IMMEDIATE board decision execution...")
    try:
        # Check both regular and emergency board reports
        for board_path in ["data/board_meeting_active.json", "data/emergency_board_report.json"]:
            board = _load_json(board_path)
            if not board:
                continue

            # Parse meeting timestamp
            ts_str = board.get("timestamp") or board.get("started_at") or board.get("date", "")
            try:
                meeting_time = datetime.fromisoformat(ts_str[:19])
                age_h = (now - meeting_time).total_seconds() / 3600
            except Exception:
                continue

            if age_h > 12:
                continue   # Too old — not relevant to current cycle

            # Find IMMEDIATE PROCEED actions
            consensus = board.get("consensus", board.get("consensus_actions", []))
            if isinstance(consensus, dict):
                consensus = consensus.get("actions", [])

            immediate_actions = [
                a for a in (consensus or [])
                if a.get("proceed") and a.get("urgency") == "IMMEDIATE"
            ]

            if not immediate_actions:
                logger.info(f"    ✓ No IMMEDIATE board actions in {os.path.basename(board_path)}")
                continue

            # Fetch today's orders to verify execution
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            cutoff = meeting_time - timedelta(minutes=5)  # orders after meeting start
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=cutoff)
            orders = trader.client.get_orders(filter=req)
            executed_tickers = {str(o.symbol) for o in orders}

            for action in immediate_actions:
                ticker  = action.get("ticker", "")
                verb    = action.get("verb", action.get("action", "")).upper()
                action_id = action.get("id", "?")

                if not ticker:
                    continue

                is_sell = verb in ("SELL", "CLOSE", "TRIM", "REDUCE")
                is_buy  = verb in ("BUY", "ADD", "BUY_NEW")

                if is_sell:
                    # Verify it was sold: ticker should no longer be in positions
                    # OR a sell order should appear in today's orders
                    still_held = (
                        positions is not None
                        and not positions.empty
                        and ticker in positions["ticker"].values
                    )
                    sell_order_placed = ticker in executed_tickers

                    if still_held and not sell_order_placed and age_h > _IMMEDIATE_TIMEOUT_H:
                        _flag(flags, "CRITICAL", "BOARD_DECISION_NOT_EXECUTED",
                              f"Board voted {verb} {ticker} (IMMEDIATE, action {action_id}) "
                              f"{age_h:.1f}h ago but {ticker} is still in the portfolio "
                              f"and no sell order found. Decision was not executed.")
                    else:
                        logger.info(f"    ✓ IMMEDIATE {verb} {ticker} — appears executed")

    except Exception as e:
        logger.warning(f"    Board decision check error: {e}")
        skipped.append("BOARD_EXECUTION")

    # ── CHECK 7: P&L drift ─────────────────────────────────────────────────
    logger.info("  [9/9] P&L drift (pipeline vs Alpaca)...")
    try:
        state = _load_json("data/state.json")
        starting_capital = 100000.0  # from config — hardcoded for independence
        pipeline_pnl = portfolio_value - starting_capital

        # Compare against Alpaca's own P&L tracking
        alpaca_pnl_today = account.get("pnl_today", 0) or 0

        # Check if ledger tracks a materially different portfolio value
        ledger = _load_json("data/performance_ledger.json")
        if ledger:
            picks = ledger.get("picks", [])
            acted_picks = [p for p in picks if p.get("acted_on")]
            if acted_picks:
                # Simple sanity: portfolio value should be within $1000 of a reasonable range
                if abs(pipeline_pnl) > portfolio_value * 0.5:
                    _flag(flags, "MEDIUM", "PNL_DRIFT",
                          f"Pipeline total P&L (${pipeline_pnl:+,.0f}) looks implausible "
                          f"against portfolio value (${portfolio_value:,.0f}). "
                          f"Performance tracking may be out of sync.")
                else:
                    logger.info(f"    ✓ P&L tracking looks consistent (total P&L: ${pipeline_pnl:+,.0f})")
            else:
                logger.info("    ✓ No acted picks yet — P&L drift check skipped")
        else:
            logger.info("    ✓ No performance ledger yet — P&L drift check skipped")
    except Exception as e:
        logger.warning(f"    P&L drift check error: {e}")
        skipped.append("PNL_DRIFT")

    # ── Write report ──────────────────────────────────────────────────────
    # SORT BEFORE ANYTHING SLICES A PREFIX OFF THIS LIST. Three readers do:
    # the Fable 5 diagnosis below (flags[:4]), the firm-channel summary
    # (flags[:3]) and morning_briefing.py's `top_flags` (recon_flags[:3]).
    # Until 2026-08-24 the list was in APPEND order, which is source order, and
    # the three CRITICAL checks are appended 7th, 9th and 10th — STOP_LOSS_
    # BREACH, CRO_CRITICAL_BUY_VIOLATION and BOARD_DECISION_NOT_EXECUTED. The
    # first four slots are always the LEDGER_ATTRIBUTION trio and GUARDRAIL_
    # DRIFT, so no prefix of any length below 7 could contain a CRITICAL.
    #
    # The diagnosis is *gated* on `n_critical or n_high` and was then handed
    # four flags that could not contain the CRITICAL which opened the gate:
    # the trigger and the evidence disagreed, and the model was asked "is
    # capital at risk?" about the wrong rows. A `[:N]` with no ordering is a
    # silent sampling rule (memory `budget-spent-in-the-wrong-order`) — so
    # sort, don't filter, and do it once at the producer so every consumer
    # inherits it rather than each re-deriving a rule it may get wrong.
    #
    # Stable sort on severity alone: within a band the append order IS source
    # order, which is the order the checks ran, and that is worth keeping.
    flags.sort(key=lambda f: _SEV_RANK.get(f["severity"], 3))

    n_critical = sum(1 for f in flags if f["severity"] == "CRITICAL")
    n_high     = sum(1 for f in flags if f["severity"] == "HIGH")
    n_medium   = sum(1 for f in flags if f["severity"] == "MEDIUM")

    report = {
        "generated_at":   now.isoformat(),
        "portfolio_value": portfolio_value,
        "cash":            cash,
        "cash_pct":        round(cash_pct * 100, 1),
        "n_positions":     n_positions,
        "n_flags":         len(flags),
        "n_critical":      n_critical,
        "n_high":          n_high,
        "n_medium":        n_medium,
        "flags":           flags,
        # Coverage travels with the verdict. A reader that sees status CLEAN and
        # checks_skipped ["CRO_BUY_FREEZE"] knows the clean is partial; without
        # these two fields the two cases are the same JSON.
        "checks_total":    len(_CHECKS),
        "checks_skipped":  list(skipped),
        "status":          ("CLEAN" if not flags else ("CRITICAL" if n_critical else "WARNING")),
    }

    # ── Fable 5 discrepancy diagnosis — gated to CRITICAL/HIGH flags ──────
    if n_critical or n_high:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("reconciliation"):
                shown = flags[:4]          # now severity-ordered, see the sort above
                flag_txt = "; ".join(
                    f"[{f['severity']}] {f['check']}: {f['detail'][:70]}"
                    for f in shown)
                # Say what was left out. A model handed 4 of 11 flags with no
                # note reasons as though it had seen all 11, and answers "is
                # capital at risk?" from a sample it does not know is a sample.
                if len(flags) > len(shown):
                    flag_txt += (f" (+{len(flags) - len(shown)} lower-severity "
                                 f"flag(s) not shown; full list in {REPORT_PATH})")
                raw = _r(
                    "You diagnose data-reconciliation failures in a trading system.",
                    f"Portfolio ${portfolio_value:,.0f}, {n_positions} positions.\n"
                    f"Flags: {flag_txt}\n"
                    "2 sentences: most likely root cause, and is capital actually at "
                    "risk or is this a bookkeeping lag?",
                    agent_name="reconciliation", max_tokens=110,
                )
                if raw:
                    report["llm_diagnosis"] = raw.strip()
        except Exception:
            pass

    os.makedirs("data", exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # ── Post to firm channel ──────────────────────────────────────────────
    try:
        from firm_channel import post
        if flags:
            shown = flags[:3]              # severity-ordered, so this is the worst 3
            summary = (
                f"RECONCILIATION: {len(flags)} flag(s) — "
                + ", ".join(f['check'] for f in shown)
                + (f" (+{len(flags) - len(shown)} more)" if len(flags) > len(shown) else "")
            )
            priority = "URGENT" if n_critical else "HIGH"
            post("Reconciliation Engine", "PIPELINE_ALERT", summary, priority=priority)
        else:
            # A check that could not run is UNKNOWN, not clean. Posting it as
            # part of an all-clear at NORMAL priority is how a parse failure in
            # the CRO buy-freeze check becomes a green light.
            ran = len(_CHECKS) - len(skipped)
            post("Reconciliation Engine", "PIPELINE_ALERT",
                 f"Reconciliation: {ran} of {len(_CHECKS)} checks ran and passed"
                 + (f"; {len(skipped)} could NOT run ({', '.join(skipped)}) — "
                    f"those are unknown, not clean" if skipped else "")
                 + f". Portfolio ${portfolio_value:,.0f} | "
                 f"Cash {cash_pct*100:.1f}% | {n_positions} positions.",
                 priority="HIGH" if skipped else "NORMAL")
    except Exception as e:
        logger.warning(f"  Firm channel post failed: {e}")

    # ── Email Jack if CRITICAL ────────────────────────────────────────────
    if n_critical > 0:
        try:
            from email_sender import send_alert_email
            subject = f"🚨 Apex Capital — Reconciliation CRITICAL: {n_critical} flag(s)"
            body_lines = [
                f"Reconciliation Engine found {n_critical} CRITICAL flag(s) at {now.strftime('%H:%M ET')}.",
                f"Portfolio: ${portfolio_value:,.2f} | Cash: {cash_pct*100:.1f}% | Positions: {n_positions}",
                "",
                "CRITICAL FLAGS:",
            ]
            for f in flags:
                if f["severity"] == "CRITICAL":
                    body_lines.append(f"  [{f['check']}] {f['detail']}")
            if n_high or n_medium:
                body_lines.append(f"\nAlso: {n_high} HIGH, {n_medium} MEDIUM flags — see data/reconciliation_report.json")
            send_alert_email(subject, "\n".join(body_lines))
            logger.info(f"  🚨 Alert email sent to Jack ({n_critical} CRITICAL flag(s))")
        except Exception as e:
            logger.warning(f"  Alert email failed: {e}")

    # ── Trigger emergency board meeting for CRITICAL flags ────────────────
    if n_critical > 0:
        try:
            from board_meeting import call_emergency_board_meeting
            critical_flags = [f for f in flags if f["severity"] == "CRITICAL"]
            reason = (
                f"Reconciliation Engine detected {n_critical} CRITICAL compliance breach(es): "
                + "; ".join(f['check'] + ': ' + f['detail'][:80] for f in critical_flags[:2])
                + ("..." if len(critical_flags) > 2 else "")
            )
            call_emergency_board_meeting(
                reason=reason,
                severity="CRITICAL",
                caller="Reconciliation Engine",
            )
            logger.info(f"  🚨 Emergency board meeting triggered ({n_critical} CRITICAL flag(s))")
        except Exception as e:
            logger.warning(f"  Emergency board trigger failed: {e}")

    # ── Summary ───────────────────────────────────────────────────────────
    logger.info("")
    if not flags:
        logger.info("  ✅ All checks passed — portfolio is clean.")
    else:
        logger.info(f"  ⚠️  {len(flags)} flag(s): {n_critical} CRITICAL | {n_high} HIGH | {n_medium} MEDIUM")
        for f in flags:
            logger.info(f"     [{f['severity']}] {f['check']}: {f['detail'][:100]}")

    return report


if __name__ == "__main__":
    report = run_reconciliation()
    sys.exit(1 if report.get("n_critical", 0) > 0 else 0)
