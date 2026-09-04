#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE PIPELINE
======================================
Main daily orchestrator for the First Eagle Lane 4.

Pipeline steps:
  1. Run all 10 FE R&D agents -> collect picks -> data/fe_picks_queue.json
  2. For each pick, run fe_intrinsic_value.calculate_iv() -> attach IV, MoS%, verdict
  3. Filter: only picks with MoS > 25% AND verdict == 'BUY' proceed to board queue
  4. Board review (fe_board.py) -> approved picks -> data/fe_approved_picks.json
  5. Position sizing:
       - Base: $5000 * 0.20 = $1000 max per position
       - Gold miners: always maintain 15% ($750) in gold/hard money
       - MoS drives size: >=40% full ($1000), >=30% 75% ($750), >=25% 50% ($500)
  6. Log all decisions to data/fe_pipeline_log.json
  7. Email daily summary

Run: python3 fe_pipeline.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

PICKS_QUEUE_FILE    = DATA_DIR / "fe_picks_queue.json"
PIPELINE_LOG_FILE   = DATA_DIR / "fe_pipeline_log.json"
APPROVED_FILE       = DATA_DIR / "fe_approved_picks.json"


# First Eagle Lane 4 capital parameters
LANE_CAPITAL        = 5000.00
MAX_POSITION_PCT    = 0.20    # 20% max per position = $1000
GOLD_TARGET_PCT     = 0.15   # 15% gold allocation target = $750
MIN_MOS_FOR_BOARD   = 0.25   # 25% minimum margin of safety
GOLD_TICKERS = {
    "NEM", "AEM", "WPM", "FNV", "RGLD", "GDX", "GDXJ",
    "SAND", "OR", "MAG", "WPM",
}

# First Eagle R&D agent registry (10 agents)
FE_AGENT_REGISTRY = [
    ("agents/cos_fe_qualitycompounders_agent.py",   "FEQualityCompoundersAgent"),
    ("agents/cos_fe_hardmoney_agent.py",            "FEHardMoneyAgent"),
    ("agents/cos_fe_specialsituations_agent.py",    "FESpecialSituationsAgent"),
    ("agents/cos_fe_insurance_agent.py",            "FEInsuranceValueAgent"),
    ("agents/cos_fe_familybusiness_agent.py",       "FEFamilyBusinessAgent"),
    ("agents/cos_fe_hiddenassets_agent.py",         "FEHiddenAssetsAgent"),
    ("agents/cos_fe_internationalvalue_agent.py",   "FEInternationalValueAgent"),
    ("agents/cos_fe_contrarianrecovery_agent.py",   "FEContrarianRecoveryAgent"),
    ("agents/cos_fe_smallmidcapvalue_agent.py",     "FESmallMidCapValueAgent"),
    ("agents/cos_fe_capitalallocation_agent.py",    "FECapitalAllocationAgent"),
]


try:
    from fe_scoring_suite import full_score as scoring_suite_score
    SCORING_SUITE_AVAILABLE = True
except ImportError:
    SCORING_SUITE_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fe_pipeline")


def _load_agent(fpath: str, class_name: str):
    """Dynamically load a First Eagle agent class."""
    full_path = BASE_DIR / fpath
    if not full_path.exists():
        logger.warning(f"[Pipeline] Agent file not found: {full_path}")
        return None
    try:
        spec   = importlib.util.spec_from_file_location(class_name, str(full_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, class_name)
    except Exception as e:
        logger.error(f"[Pipeline] Failed to load {class_name}: {e}")
        return None


def _run_all_rd_agents() -> list[dict]:
    """Run all 10 First Eagle R&D agents and collect raw picks."""
    all_picks = []
    seen_tickers = set()

    for fpath, class_name in FE_AGENT_REGISTRY:
        AgentClass = _load_agent(fpath, class_name)
        if AgentClass is None:
            continue
        try:
            agent  = AgentClass()
            report = agent.run()
            for rec in report.get("recommendations", []):
                ticker = rec.get("ticker")
                if not ticker:
                    continue
                if ticker not in seen_tickers:
                    seen_tickers.add(ticker)
                    rec["source_agent_class"] = class_name
                    all_picks.append(rec)
                else:
                    # Keep higher conviction duplicate
                    for i, existing in enumerate(all_picks):
                        if existing.get("ticker") == ticker:
                            if rec.get("conviction", 0) > existing.get("conviction", 0):
                                rec["source_agent_class"] = class_name
                                all_picks[i] = rec
                            break
            logger.info(
                f"[Pipeline] {class_name}: "
                f"{len(report.get('recommendations', []))} pick(s)"
            )
        except Exception as e:
            logger.error(f"[Pipeline] Agent {class_name} failed: {e}")

    return all_picks


def _attach_iv(picks: list[dict]) -> list[dict]:
    """Attach IV calculations to each pick. Skip ticker on IV failure."""
    try:
        from fe_intrinsic_value import calculate_iv
    except ImportError:
        logger.error("[Pipeline] Could not import fe_intrinsic_value -- skipping IV attachment")
        return picks

    enriched = []
    for pick in picks:
        ticker = pick.get("ticker", "?")
        try:
            iv_result = calculate_iv(ticker)
            if iv_result:
                pick["dcf_iv"]           = iv_result.get("dcf_iv")
                pick["epv_iv"]           = iv_result.get("epv_iv")
                pick["conservative_iv"]  = iv_result.get("conservative_iv")
                pick["margin_of_safety"] = iv_result.get("margin_of_safety")
                pick["iv_verdict"]       = iv_result.get("verdict")
                # Recalculate conviction from MoS so it reflects actual IV data
                _mos = iv_result.get("margin_of_safety") or 0
                if _mos >= 0.35:   pick["conviction"] = 5
                elif _mos >= 0.25: pick["conviction"] = 4
                elif _mos >= 0.15: pick["conviction"] = 3
                elif _mos >= 0.05: pick["conviction"] = 2
                else:              pick["conviction"] = 1
                pick["fcf_yield"]        = iv_result.get("fcf_yield")
                pick["roic"]             = iv_result.get("roic")
                pick["net_debt_ebitda"]  = iv_result.get("net_debt_ebitda")
                enriched.append(pick)
                logger.info(
                    f"[Pipeline] IV attached for {ticker}: "
                    f"MoS={iv_result.get('margin_of_safety', 0)*100:.1f}% | "
                    f"{iv_result.get('verdict', 'unknown')}"
                )
            else:
                logger.warning(f"[Pipeline] IV calc failed for {ticker} -- skipping ticker")
        except Exception as e:
            logger.warning(f"[Pipeline] IV error for {ticker}: {e} -- skipping ticker")

    return enriched


def _filter_for_board(picks: list[dict]) -> list[dict]:
    """Filter picks: MoS > 25% AND verdict == 'BUY' proceed to board.
    Near-miss stocks (MoS between -35% and 0%) are auto-added to watchlist
    with target entry price at which MoS would equal 25%.
    """
    filtered = []
    for pick in picks:
        mos           = pick.get("margin_of_safety", 0) or 0.0
        verdict       = pick.get("iv_verdict", "")
        conservative_iv = pick.get("conservative_iv")
        ticker        = pick.get("ticker", "?")

        if mos >= MIN_MOS_FOR_BOARD and verdict == "BUY":
            filtered.append(pick)
            logger.info(
                f"[Pipeline] {ticker} -> BOARD QUEUE "
                f"(MoS={mos*100:.1f}%, {verdict})"
            )
        else:
            logger.info(
                f"[Pipeline] {ticker} -> FILTERED OUT "
                f"(MoS={mos*100:.1f}%, verdict={verdict})"
            )
            # Auto-watchlist near-miss stocks (within 35% of fair value)
            # Target entry = IV * (1 - MOS_BUY_THRESHOLD) = the price at which MoS = 25%
            if conservative_iv and conservative_iv > 0 and mos >= -0.35:
                try:
                    from fe_watchlist import add_to_watchlist
                    target_price = round(conservative_iv * (1 - MIN_MOS_FOR_BOARD), 2)
                    add_to_watchlist(
                        ticker=ticker,
                        target_entry_price=target_price,
                        current_iv=conservative_iv,
                        notes=f"Near-miss: MoS={mos*100:.1f}% | Target entry at 25% MoS"
                    )
                    logger.info(
                        f"[Pipeline] {ticker} -> WATCHLISTED at target=${target_price} "
                        f"(current IV=${conservative_iv})"
                    )
                except Exception as _wl_err:
                    logger.debug(f"[Pipeline] Watchlist add failed for {ticker}: {_wl_err}")
    return filtered


def _calculate_gold_allocation(approved_picks: list[dict]) -> float:
    """Calculate current gold allocation percentage of approved picks."""
    if not approved_picks:
        return 0.0
    gold_count = sum(
        1 for p in approved_picks
        if p.get("pick_data", p).get("ticker", "").upper() in GOLD_TICKERS
    )
    return gold_count / len(approved_picks)


def _size_positions(approved_picks: list[dict]) -> list[dict]:
    """
    Size positions based purely on margin of safety.

    Gold miners (NEM, AEM, WPM, GDX, GDXJ, FNV, RGLD, SAND):
      Always sized to maintain 15% lane allocation ($750 target).

    All other positions:
      MoS >= 40%: full position = $1,000
      MoS >= 30%: 75% position = $750
      MoS >= 25%: 50% position = $500
      Below 25%:  rejected (never reaches pipeline)
    """
    gold_target_usd = LANE_CAPITAL * GOLD_TARGET_PCT   # $750
    max_pos_usd     = LANE_CAPITAL * MAX_POSITION_PCT  # $1000

    sized = []
    gold_budget_remaining = gold_target_usd

    for item in approved_picks:
        pick   = item.get("pick_data", item)
        ticker = pick.get("ticker", "?")
        mos    = pick.get("margin_of_safety", 0) or 0.0
        is_gold = ticker.upper() in GOLD_TICKERS

        conviction_pos = item.get("conviction_position")
        if is_gold:
            # Gold miners: target $750 lane allocation, cap at max_pos_usd
            if conviction_pos and conviction_pos > 0:
                base_size = min(float(conviction_pos), max_pos_usd)
                sizing_rule = f"gold_conviction_{conviction_pos}"
            else:
                base_size = min(gold_budget_remaining, max_pos_usd)
                sizing_rule = "gold_target_15pct"
            gold_budget_remaining = max(0.0, gold_budget_remaining - base_size)
        elif conviction_pos and conviction_pos > 0:
            # Use conviction-based sizing (multi-factor score)
            base_size   = float(conviction_pos)
            sizing_rule = f"conviction_{item.get('conviction_score','?')}_score"
        else:
            # MoS-driven sizing (fallback)
            if mos >= 0.40:
                base_size   = 1000.0
                sizing_rule = "mos_>=40pct_full"
            elif mos >= 0.30:
                base_size   = 750.0
                sizing_rule = "mos_>=30pct_75pct"
            else:
                base_size   = 500.0
                sizing_rule = "mos_>=25pct_50pct"

        position_usd = round(min(base_size, max_pos_usd), 2)

        sized_item = dict(item)
        sized_item["position_usd"]     = position_usd
        sized_item["is_gold_position"] = is_gold
        sized_item["sizing_rule"]      = sizing_rule
        sized_item["lane_capital"]     = LANE_CAPITAL
        sized_item["position_pct"]     = round(position_usd / LANE_CAPITAL, 4)
        sized.append(sized_item)

        logger.info(
            f"[Pipeline] {ticker}: ${position_usd:.0f} "
            f"(MoS={mos*100:.1f}%, rule={sizing_rule})"
        )

    return sized


def _send_daily_email(summary: dict) -> None:
    """
    Buffer the First Eagle daily summary for the 7 PM evening digest.

    Uses the email_buffer system so all pod reports arrive in one consolidated
    email instead of separate messages throughout the day.  A sentinel file
    (data/fe_email_sent_YYYY-MM-DD.flag) prevents duplicate buffering if this
    function is called more than once on the same calendar day.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sentinel = DATA_DIR / f"fe_email_sent_{date_str}.flag"

    # Guard: only buffer once per calendar day
    if sentinel.exists():
        logger.info("[Pipeline] Daily email already buffered for %s — skipping duplicate", date_str)
        return

    approved    = summary.get("approved_count", 0)
    filtered    = summary.get("filtered_count", 0)
    total_picks = summary.get("total_rd_picks", 0)
    positions   = summary.get("sized_positions", [])

    lines = [
        "Apex Capital — First Eagle Lane 4 Daily Pipeline",
        f"Date: {summary.get('generated_at', 'unknown')}",
        "",
        f"R&D agents run: {summary.get('agents_run', 10)}/10",
        f"Total picks from R&D agents: {total_picks}",
        f"Picks with IV (Intrinsic Value) calculated: {summary.get('iv_enriched_count', 0)}",
        f"Filtered — MoS (Margin of Safety) > 25% + BUY verdict: {summary.get('board_queue_count', 0)}",
        f"Board approved: {approved}",
        f"Board rejected/held: {filtered}",
        "",
    ]

    if positions:
        lines.append("SIZED POSITIONS FOR EXECUTION:")
        for item in positions:
            pick = item.get("pick_data", item)
            ticker  = pick.get("ticker", "?")
            mos     = pick.get("margin_of_safety", 0) or 0
            pos_usd = item.get("position_usd", 0)
            gold    = " [GOLD]" if item.get("is_gold_position") else ""
            rule    = item.get("sizing_rule", "")
            lines.append(
                f"  {ticker}{gold}: ${pos_usd:.0f} | "
                f"MoS={mos*100:.1f}% | "
                f"IV=${pick.get('conservative_iv','n/a')} | "
                f"rule={rule}"
            )
        lines.append("")

    lines += [
        "Position sizing: MoS>=40% -> $1,000 | MoS>=30% -> $750 | MoS>=25% -> $500",
        "Gold miners (GLD, GDX etc.): $750 target (15% of lane allocation)",
        "",
        "First Eagle philosophy: absolute return, margin of safety only.",
        "Lane capital: $5,000 | Max single position: $1,000 (20%) | Gold target: $750 (15%)",
        "— Apex Capital First Eagle Pipeline",
    ]

    body = "\n".join(lines)
    subject = f"Apex Capital — First Eagle Lane 4 Daily | {date_str}"

    try:
        from email_buffer import buffer_email
        buffer_email(subject, body, sender_name="FE Pipeline")
        sentinel.touch()
        logger.info("[Pipeline] Daily email buffered for evening digest.")
    except Exception as e:
        logger.error(f"[Pipeline] Failed to buffer daily email: {e}")




def calculate_conviction(mos, fraud_result, mgmt_result, moat_result):
    """
    Multi-factor conviction score 0-100, returns (score, position_usd).
    Blocked if fraud HIGH or MoS < 25%.
    """
    if fraud_result.get("risk_level") == "HIGH":
        return 0, 0
    if mos < 0.25:
        return 0, 0

    mos_score   = 40 if mos >= 0.40 else (30 if mos >= 0.30 else 20)
    fraud_score = 20 if fraud_result.get("risk_level") == "LOW" else 10
    mgmt_score  = {"EXCELLENT": 20, "GOOD": 15, "FAIR": 8, "POOR": 0}.get(
        mgmt_result.get("category", "FAIR"), 8
    )
    moat_score  = {"WIDE": 20, "NARROW": 12, "NONE": 0}.get(
        moat_result.get("moat_width", "NONE"), 0
    )

    total    = mos_score + fraud_score + mgmt_score + moat_score
    position = max(500, min(250 * (total // 20), 1500)) if total >= 40 else 0
    return total, position


def _run_multi_factor_analysis(picks: list) -> list:
    """
    Run fraud_check, assess_management, analyze_moat per ticker.
    Add results to pick. Uses calculate_conviction for sizing.
    Falls back to MoS-only sizing if any module fails.
    """
    try:
        from fe_fraud_watchdog    import fraud_check
        from fe_management_quality import assess_management
        from fe_moat_analyzer      import analyze_moat
        modules_ok = True
    except Exception as e:
        logger.warning(f"[Pipeline] Multi-factor modules unavailable: {e} -- MoS-only sizing")
        modules_ok = False

    enriched = []
    for pick in picks:
        ticker = pick.get("ticker", "?")
        mos    = pick.get("margin_of_safety", 0) or 0.0
        try:
            if modules_ok:
                # fraud_check takes (ticker, picks) and returns (clean, flagged).
                # This call site used to pass one arg and read the result as a
                # dict, so every FE pick since ea31ead landed swallowed a
                # TypeError in the outer try and downstream treated fraud as
                # LOW-by-default. Unpack the flagged list into the dict shape
                # the rest of this branch expects.
                _clean, _flagged = fraud_check(ticker, [pick])
                if _flagged:
                    fraud_result = {"risk_level": "HIGH", "recommendation": "AVOID",
                                    "flags": _flagged[0].get("fraud_flags", [])}
                else:
                    fraud_result = {"risk_level": "LOW", "recommendation": "CLEAR"}
                mgmt_result  = assess_management(ticker)
                moat_result  = analyze_moat(ticker)
            else:
                fraud_result = {"risk_level": "LOW",  "recommendation": "CLEAR"}
                mgmt_result  = {"category":   "FAIR", "score": 50}
                moat_result  = {"moat_width": "NONE", "score": 0}

            conviction_score, conviction_position = calculate_conviction(
                mos, fraud_result, mgmt_result, moat_result
            )

            # Block if fraud HIGH
            if fraud_result.get("risk_level") == "HIGH":
                logger.warning(
                    f"[Pipeline] {ticker} BLOCKED by fraud watchdog "
                    f"(M-Score={fraud_result.get('mscore','N/A')})"
                )
                pick["conviction_score"]    = 0
                pick["conviction_position"] = 0
                pick["fraud_result"]        = fraud_result
                pick["mgmt_result"]         = mgmt_result
                pick["moat_result"]         = moat_result
                pick["blocked_by_fraud"]    = True
                enriched.append(pick)
                continue

            pick["conviction_score"]    = conviction_score
            pick["conviction_position"] = conviction_position
            pick["fraud_result"]        = fraud_result
            pick["mgmt_result"]         = mgmt_result
            pick["moat_result"]         = moat_result
            pick["blocked_by_fraud"]    = False
            enriched.append(pick)

            if SCORING_SUITE_AVAILABLE:
                try:
                    pick["scoring_suite"] = scoring_suite_score(ticker)
                    ss = pick["scoring_suite"]
                    logger.info(
                        f"[Scoring] {ticker}: "
                        f"Piotroski {ss.get('piotroski', {}).get('score', '?')} | "
                        f"Altman {ss.get('altman', {}).get('zone', '?')} | "
                        f"ValueTrap {ss.get('value_trap', {}).get('verdict', '?')} | "
                        f"SmartMoney {ss.get('smart_money', {}).get('smart_money_signal', '?')}"
                    )
                except Exception as _se:
                    logger.warning(f"[Scoring] {ticker} scoring suite failed: {_se}")

            logger.info(
                f"[Pipeline] {ticker}: conviction={conviction_score} | "
                f"pos_usd=${conviction_position} | "
                f"fraud={fraud_result.get('risk_level')} | "
                f"mgmt={mgmt_result.get('category')} | "
                f"moat={moat_result.get('moat_width')}"
            )

        except Exception as e:
            logger.warning(f"[Pipeline] Multi-factor error for {ticker}: {e} -- keeping pick")
            pick["conviction_score"]    = None
            pick["conviction_position"] = None
            pick["fraud_result"]        = {}
            pick["mgmt_result"]         = {}
            pick["moat_result"]         = {}
            pick["blocked_by_fraud"]    = False
            enriched.append(pick)

    return enriched


# Broad value/quality candidate universe — the FE bar is deliberately HIGH (25%
# margin of safety), so the way to find qualifying picks is to scan WIDER, not to
# lower the bar. These are liquid, EDGAR-covered US large/mid-caps concentrated in
# the value-rich sectors (financials, energy, healthcare, staples, industrials,
# materials, telecom, utilities, cyclical consumer) where deep value actually lives
# — deliberately NOT growth/tech, which a DCF/EPV value model will always reject.
# The IV scan runs the same 25%-MoS standard over all of these; most are rejected,
# and that's the point — we want the rare few that clear it (e.g. KHC at ~41%).
FE_VALUE_UNIVERSE = [
    # Financials
    "JPM","BAC","C","WFC","GS","MS","USB","PNC","TFC","COF","AIG","MET","PRU","ALL",
    "TRV","AFL","HIG","CINF","BEN","TROW","STT","BK","SCHW","CFG","KEY","RF","FITB","HBAN","MTB",
    # Energy
    "XOM","CVX","COP","EOG","SLB","MPC","VLO","PSX","OXY","HES","DVN","FANG","KMI","WMB","OKE","BKR","HAL",
    # Healthcare
    "JNJ","PFE","MRK","ABBV","BMY","CVS","CI","ELV","HUM","GILD","AMGN","MDT","BDX","CAH","MCK","COR","LH","DGX","VTRS","ZBH",
    # Consumer staples
    "KO","PEP","PG","MO","PM","KHC","GIS","K","HSY","KMB","CL","CLX","SYY","ADM","KR","STZ","TAP","CAG","CPB","HRL",
    # Industrials
    "MMM","GE","HON","CAT","DE","EMR","ETN","ITW","GD","LMT","NOC","RTX","BA","UPS","FDX","CSX","NSC","UNP","PCAR","DOV","PH","ROK","CMI","JCI","SWK",
    # Materials
    "LIN","SHW","FCX","NEM","NUE","DOW","DD","PPG","ECL","APD","MLM","VMC","IP","CF","MOS",
    # Telecom / Media
    "T","VZ","CMCSA","DIS","CHTR","FOXA","PARA","WBD","IPG","OMC",
    # Utilities
    "NEE","DUK","SO","D","AEP","EXC","XEL","ED","WEC","ES","PEG","SRE","PCG",
    # Cyclical consumer (value)
    "F","GM","HD","LOW","TGT","BBY","GPC","AAP","WHR","LEN","DHI","PHM","MGM","LVS",
]


def prescreen_universe():
    """
    Pre-compute fraud scores for all tickers in all agent watchlists PLUS the broad
    FE_VALUE_UNIVERSE. Returns (blocked_set, full_universe). Caches 7 days.
    """
    import glob
    import re as _re
    import time
    from fe_fraud_watchdog import fraud_check

    cache_file = DATA_DIR / "prescreen_cache.json"
    cache = {}
    if cache_file.exists():
        try:
            import json as _json
            cached = _json.loads(cache_file.read_text())
            cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
            cache = {k: v for k, v in cached.items() if v.get("checked_at", "") > cutoff}
        except Exception:
            pass

    all_tickers = set()
    agent_files = glob.glob(str(BASE_DIR / "agents" / "cos_fe_*.py")) + [
        str(BASE_DIR / "agents" / "cos_firsteaglevalueagent.py")
    ]
    for agent_file in agent_files:
        try:
            file_content = open(agent_file).read()
            tickers = _re.findall(r'"([A-Z]{1,5})".*?#', file_content)
            all_tickers.update(tickers)
        except Exception:
            pass

    # Widen the net: union the broad value/quality universe so the IV scan has many
    # more chances to surface a name that clears the high 25%-MoS bar.
    all_tickers.update(FE_VALUE_UNIVERSE)

    # Bigger funnel — the strict elite bar (corroborated, debt-adjusted, 25-50% MoS)
    # passes very few names, so we feed it a large, institutionally-vetted candidate
    # set: the most widely-held tickers from the 13F holdings DB (each held by >=5
    # institutions). These are liquid, real businesses — exactly where to hunt value.
    try:
        import sqlite3 as _sql
        _db = BASE_DIR / "data" / "historical_13f.db"
        if _db.exists():
            _c = _sql.connect(str(_db))
            held = _c.execute(
                "SELECT ticker FROM holdings WHERE ticker!='' "
                "GROUP BY ticker HAVING COUNT(DISTINCT institution) >= 5 "
                "ORDER BY COUNT(DISTINCT institution) DESC LIMIT 600"
            ).fetchall()
            _c.close()
            all_tickers.update(t[0] for t in held if t[0])
    except Exception as _he:
        logger.warning(f"[Prescreen] 13F funnel unavailable: {_he}")

    blocked = set()
    for ticker in all_tickers:
        if ticker in cache:
            if cache[ticker].get("risk_level") == "HIGH":
                blocked.add(ticker)
            continue
        try:
            # Prescreen runs BEFORE valuation, so there is no MoS/D/E on the
            # pick yet. fraud_check needs those fields to have anything to
            # say, so at this stage it can only return "no evidence" — the
            # per-pick fraud gate at line ~426 is where the real screen fires
            # once valuation has produced the fields the check reads.
            cache[ticker] = {
                "risk_level": "LOW",
                "checked_at": datetime.utcnow().isoformat(),
                "note": "prescreen-stage: pre-valuation, no fraud signal available",
            }
            time.sleep(0.05)
        except Exception as e:
            logger.debug(f"[Prescreen] {ticker} cache write failed: {e}")

    import json as _json2
    cache_file.write_text(_json2.dumps(cache, indent=2))
    logger.info(f"[Prescreen] {len(blocked)} tickers blocked out of {len(all_tickers)} universe")
    return blocked, all_tickers  # also return full universe for IV scan


def _iv_opportunity_scan(all_tickers: set, blocked_tickers: set, existing_tickers: set) -> list[dict]:
    """
    IV-first opportunity scan: runs intrinsic value on the full watchlist universe
    and surfaces any BUY-rated ticker (MoS > 25%) that the R&D agents didn't pick.

    This catches value opportunities that R&D agents may reject on qualitative
    grounds despite strong IV discount (e.g., negative ROIC from goodwill impairment,
    cyclical trough earnings, or simply not favored by agent's internal conviction model).

    Results are tagged source='iv_opportunity_scan' and merged into the board queue
    alongside R&D agent picks.

    IV results are cached for 4h to keep pipeline runs fast.
    """
    from fe_intrinsic_value import calculate_iv

    cache_file = DATA_DIR / "iv_scan_cache.json"
    cache: dict = {}
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            cutoff = (datetime.utcnow() - timedelta(hours=4)).isoformat()
            cache  = {k: v for k, v in cached.items() if v.get("scanned_at", "") > cutoff}
        except Exception:
            pass

    candidates   = all_tickers - blocked_tickers - existing_tickers
    opportunities: list[dict] = []

    logger.info(f"[IVScan] Scanning {len(candidates)} universe tickers for value opportunities...")

    for ticker in sorted(candidates):
        # Use cached result if fresh
        if ticker in cache:
            iv_result = cache[ticker].get("iv_result")
        else:
            try:
                iv_result = calculate_iv(ticker)
                cache[ticker] = {
                    "iv_result":  iv_result,
                    "scanned_at": datetime.utcnow().isoformat(),
                }
            except Exception as e:
                logger.debug(f"[IVScan] IV failed for {ticker}: {e}")
                cache[ticker] = {"iv_result": None, "scanned_at": datetime.utcnow().isoformat()}
                continue

        if not iv_result:
            continue

        mos     = iv_result.get("margin_of_safety", 0) or 0
        verdict = iv_result.get("verdict", "")

        if mos >= MIN_MOS_FOR_BOARD and verdict == "BUY":
            pick = {
                "ticker":           ticker,
                "source":           "iv_opportunity_scan",
                "conviction":       3,  # neutral conviction — needs R&D due diligence
                "verdict":          "BUY",
                "iv_verdict":       verdict,
                "margin_of_safety": mos,
                "conservative_iv":  iv_result.get("conservative_iv"),
                "dcf_iv":           iv_result.get("dcf_iv"),
                "epv_iv":           iv_result.get("epv_iv"),
                "current_price":    iv_result.get("current_price"),
                "fcf_yield":        iv_result.get("fcf_yield"),
                "roic":             iv_result.get("roic"),
                "data_source":      iv_result.get("data_source"),
                "notes":            (
                    f"IV-scan direct pick: MoS={mos*100:.1f}% | "
                    f"IV=${iv_result.get('conservative_iv')} | "
                    f"Price=${iv_result.get('current_price')} | "
                    f"R&D agents did not nominate this ticker — board should apply enhanced due diligence"
                ),
            }
            opportunities.append(pick)
            logger.info(
                f"[IVScan] ✓ OPPORTUNITY: {ticker} | MoS={mos*100:.1f}% | "
                f"IV=${iv_result.get('conservative_iv')} | {iv_result.get('data_source')}"
            )
        elif mos >= -0.35 and mos < MIN_MOS_FOR_BOARD:
            logger.debug(f"[IVScan] Near-miss: {ticker} MoS={mos*100:.1f}%")

    # Persist cache
    try:
        cache_file.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass

    logger.info(f"[IVScan] {len(opportunities)} direct IV opportunity(ies) found from {len(candidates)} scanned")
    return opportunities


def run_pipeline() -> dict:
    """Main pipeline entry point."""
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- FIRST EAGLE PIPELINE")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Research Library: check for pre-researched deep-research memos
    research_dir = DATA_DIR / "company_research"
    research_ready = []
    if research_dir.exists():
        for ticker_dir in research_dir.iterdir():
            if not ticker_dir.is_dir():
                continue
            synthesis = ticker_dir / "synthesis_memo.json"
            if synthesis.exists():
                try:
                    memo = json.loads(synthesis.read_text())
                    age_days = (
                        datetime.utcnow() - datetime.fromisoformat(memo.get("synthesis_date", "2000-01-01"))
                    ).days
                    if (
                        age_days <= 90
                        and memo.get("overall_verdict") in ("BUY", "WATCH")
                        and memo.get("margin_of_safety", 0) >= 0.25
                    ):
                        research_ready.append(memo)
                        logger.info(
                            f"[ResearchLib] {memo.get('ticker', '?')}: "
                            f"{memo.get('overall_verdict')} | "
                            f"MoS {memo.get('margin_of_safety', 0):.1%} | "
                            f"deep research {age_days}d old"
                        )
                except Exception:
                    pass
    if research_ready:
        logger.info(f"[ResearchLib] {len(research_ready)} deep-researched tickers ready for board")

    # Prescreen: fraud-check entire universe before agent analysis
    logger.info("[Pipeline] Prescreen: Running fraud pre-screen on full watchlist universe...")
    try:
        blocked_tickers, universe_tickers = prescreen_universe()
    except Exception as _e:
        logger.warning(f"[Pipeline] Prescreen failed: {_e} -- proceeding without pre-screening")
        blocked_tickers  = set()
        universe_tickers = set()

    # Step 1: Run all 10 R&D agents
    logger.info("[Pipeline] Step 1: Running all 10 FE R&D agents...")
    raw_picks = _run_all_rd_agents()
    logger.info(f"[Pipeline] {len(raw_picks)} unique picks collected from R&D agents.")

    # Filter out pre-screened (fraud HIGH) tickers from agent picks
    if blocked_tickers:
        filtered_prescreen = []
        for _p in raw_picks:
            if _p.get("ticker", "").upper() in blocked_tickers:
                logger.info(
                    f"[Pipeline] {_p.get('ticker')} removed -- pre-screened: fraud HIGH"
                )
            else:
                filtered_prescreen.append(_p)
        raw_picks = filtered_prescreen
        logger.info(f"[Pipeline] {len(raw_picks)} picks remain after pre-screen filter.")

    # Step 1.5: IV-first opportunity scan — catches value plays R&D agents overlooked
    logger.info("[Pipeline] Step 1.5: IV opportunity scan across full universe...")
    try:
        existing_tickers = {p.get("ticker", "").upper() for p in raw_picks}
        iv_opps = _iv_opportunity_scan(universe_tickers, blocked_tickers, existing_tickers)
        if iv_opps:
            for _op in iv_opps:
                raw_picks.append(_op)
            logger.info(f"[Pipeline] {len(iv_opps)} IV opportunity picks added to queue")
    except Exception as _ive:
        logger.warning(f"[Pipeline] IV opportunity scan failed: {_ive} — continuing without it")

    # Step 2: Attach IV calculations
    logger.info("[Pipeline] Step 2: Calculating intrinsic value for each pick...")
    enriched_picks = _attach_iv(raw_picks)
    logger.info(f"[Pipeline] {len(enriched_picks)} picks with IV attached.")

    # Step 3: Filter for board
    logger.info("[Pipeline] Step 3: Filtering picks (MoS > 25% + BUY verdict)...")
    board_queue = _filter_for_board(enriched_picks)
    logger.info(f"[Pipeline] {len(board_queue)} picks proceed to board review.")

    # Merge deep-research picks into board_queue (tagged by source)
    for _pick in board_queue:
        _pick.setdefault("source", "quick_screen")
    if research_ready:
        _seen = {p.get("ticker") for p in board_queue}
        for _memo in research_ready:
            _t = _memo.get("ticker")
            if not _t or _t in _seen:
                continue
            _deep_pick = {
                "ticker":            _t,
                "source":            "deep_research",
                "iv_verdict":        _memo.get("overall_verdict", "BUY"),
                "margin_of_safety":  _memo.get("margin_of_safety", 0),
                "conservative_iv":   _memo.get("intrinsic_value"),
                "conviction":        _memo.get("conviction", 3),
                "investment_horizon": "long",
                "reasons":           [_memo.get("summary", "")],
                "synthesis_date":    _memo.get("synthesis_date"),
            }
            board_queue.append(_deep_pick)
            _seen.add(_t)
            logger.info(f"[ResearchLib] {_t} added to board queue from deep research library")

    # Save picks queue
    with open(PICKS_QUEUE_FILE, "w") as f:
        json.dump(board_queue, f, indent=2, default=str)
    logger.info(f"[Pipeline] Board queue written to {PICKS_QUEUE_FILE}")

    # Step 4b: Multi-factor conviction scoring (fraud/moat/mgmt -- must run BEFORE CIO and board)
    logger.info("[Pipeline] Step 4b: Running fraud/management/moat analysis...")
    try:
        board_queue_enriched = _run_multi_factor_analysis(board_queue)
        # Remove fraud-blocked picks
        board_queue_enriched = [p for p in board_queue_enriched if not p.get("blocked_by_fraud")]
        logger.info(
            f"[Pipeline] Multi-factor: {len(board_queue_enriched)} picks after fraud filter"
        )
        # Update board_queue and picks queue file with enriched data
        board_queue = board_queue_enriched
        with open(PICKS_QUEUE_FILE, "w") as f:
            import json as _json
            _json.dump(board_queue, f, indent=2, default=str)
        logger.info(f"[Pipeline] Picks queue updated with multi-factor data: {PICKS_QUEUE_FILE}")
    except Exception as e:
        logger.warning(f"[Pipeline] Multi-factor step failed: {e} -- using original board_queue")
        board_queue_enriched = board_queue

    # Step 4c: CIO synthesis -- curate slate for board (must run AFTER multi-factor, BEFORE board)
    logger.info("[Pipeline] Step 4c: Running CIO synthesis (curating board slate)...")
    try:
        import importlib.util as _ilu
        _cio_path = BASE_DIR / "fe_cio_agent.py"
        if _cio_path.exists():
            _spec = _ilu.spec_from_file_location("fe_cio_agent", str(_cio_path))
            _cio_mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_cio_mod)
            _cio_mod.run_cio_synthesis([], LANE_CAPITAL)
            logger.info("[Pipeline] CIO slate written to fe_cio_slate.json")
        else:
            logger.warning("[Pipeline] fe_cio_agent.py not found -- skipping CIO step")
    except Exception as _ce:
        logger.warning(f"[Pipeline] CIO step failed: {_ce}")

    # Step 5: Board review (reads from fe_cio_slate.json written by CIO above)
    logger.info("[Pipeline] Step 4: Running First Eagle Board review...")
    approved_picks_data = []
    try:
        from fe_board import run_board
        board_report = run_board()
        approved_picks_data = board_report.get("approved", board_report.get("approved_picks", []))
        logger.info(
            f"[Pipeline] Board: {len(approved_picks_data)} approved, "
            f"{len(board_report.get('rejected', board_report.get('rejected_picks', [])))} rejected."
        )
    except Exception as e:
        logger.error(f"[Pipeline] Board failed: {e}")

    # Step 5: Position sizing (MoS-based)
    logger.info("[Pipeline] Step 5: Sizing positions (margin-of-safety based)...")
    sized_positions = _size_positions(approved_picks_data)

    # Gold allocation check
    gold_pct = _calculate_gold_allocation(approved_picks_data)
    logger.info(f"[Pipeline] Current gold allocation: {gold_pct*100:.1f}% (target: 15%)")

    if gold_pct < 0.12:
        logger.warning(
            f"[Pipeline] Gold allocation {gold_pct*100:.1f}% below 12% minimum floor! "
            "Consider adding hard money position."
        )

    # Step 6: Log all decisions
    pipeline_log = {
        "generated_at":       started_at,
        "completed_at":       datetime.now(timezone.utc).isoformat(),
        "agents_run":         len(FE_AGENT_REGISTRY),
        "total_rd_picks":     len(raw_picks),
        "iv_enriched_count":  len(enriched_picks),
        "board_queue_count":  len(board_queue),
        "approved_count":     len(approved_picks_data),
        "filtered_count":     len(board_queue) - len(approved_picks_data),
        "sized_positions":    sized_positions,
        "gold_allocation_pct": round(gold_pct, 4),
        "lane_capital":       LANE_CAPITAL,
    }
    with open(PIPELINE_LOG_FILE, "w") as f:
        json.dump(pipeline_log, f, indent=2, default=str)
    logger.info(f"[Pipeline] Log written to {PIPELINE_LOG_FILE}")

    # Step 7: Email daily summary
    logger.info("[Pipeline] Step 7: Sending daily email...")
    _send_daily_email(pipeline_log)

    logger.info("[Pipeline] Done.")
    return pipeline_log


if __name__ == "__main__":
    run_pipeline()


# ============================================================================
# STEP 0 -- RESEARCH LIBRARY INTEGRATION
# ============================================================================
# Added: research library check before agent quick-screen.
# Companies with complete deep research + BUY verdict go directly to board.
# Agent quick-screen nominations go to research queue, not directly to board.

RESEARCH_LIBRARY_DIR   = DATA_DIR / "company_research"
RESEARCH_NOMINATION_FILE = DATA_DIR / "fe_research_nominations.json"
RESEARCH_FRESH_DAYS_PIPELINE = 90


def _check_research_library() -> list:
    """
    Step 0: Check research library for companies ready for board review.
    A company is research-complete if:
      - synthesis_memo.json exists
      - verdict is BUY
      - data < 90 days old
      - current MoS >= 25% (live check)
    Returns list of picks formatted for the board queue.
    """
    import json
    from datetime import datetime, timezone, timedelta, timedelta

    research_picks = []

    if not RESEARCH_LIBRARY_DIR.exists():
        logger.info("[Pipeline Step 0] No research library found -- skipping")
        return research_picks

    for ticker_dir in RESEARCH_LIBRARY_DIR.iterdir():
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name.upper()
        synthesis_file = ticker_dir / "synthesis_memo.json"
        if not synthesis_file.exists():
            continue

        try:
            with open(synthesis_file) as f:
                memo = json.load(f)
        except Exception:
            continue

        # Check verdict
        if memo.get("overall_verdict") != "BUY":
            continue

        # Check data freshness
        try:
            generated_at = datetime.fromisoformat(memo.get("generated_at", ""))
            age_days = (datetime.now(timezone.utc) - generated_at).days
            if age_days > RESEARCH_FRESH_DAYS_PIPELINE:
                logger.info(f"[Pipeline Step 0] {ticker} research stale ({age_days}d) -- skipping")
                continue
        except Exception:
            continue

        # Check current MoS >= 25% (live price check)
        mos_stored = memo.get("margin_of_safety", 0) or 0
        current_iv  = memo.get("composite_iv")
        if not current_iv or current_iv <= 0:
            continue

        try:
            from fe_edgar_fundamentals import get_current_price
            live_price = get_current_price(ticker)
            if live_price and live_price > 0:
                live_mos = (current_iv - live_price) / current_iv
            else:
                live_mos = mos_stored
        except Exception:
            live_mos = mos_stored

        if live_mos < MIN_MOS_FOR_BOARD:
            logger.info(
                f"[Pipeline Step 0] {ticker} MoS now {live_mos*100:.1f}% -- below threshold, skipping"
            )
            continue

        # Build pick record compatible with pipeline
        pick = {
            "ticker": ticker,
            "source": "research_library",
            "overall_verdict": "BUY",
            "conviction_level": memo.get("conviction_level", "MEDIUM"),
            "margin_of_safety": live_mos,
            "conservative_iv": current_iv,
            "current_price": live_price if live_price else memo.get("current_price"),
            "iv_verdict": "BUY",
            "investment_thesis": memo.get("investment_thesis", ""),
            "research_quality_score": memo.get("research_quality_score", 0),
            "component_scores": memo.get("component_scores", {}),
            "research_age_days": age_days,
            "from_research_library": True,
        }
        research_picks.append(pick)
        logger.info(
            f"[Pipeline Step 0] {ticker} -> BOARD (research library BUY, "
            f"MoS={live_mos*100:.1f}%, age={age_days}d)"
        )

    logger.info(
        f"[Pipeline Step 0] {len(research_picks)} companies have complete research profiles "
        f"ready for board review."
    )
    return research_picks


def _nominate_to_research_queue(picks: list) -> int:
    """
    Add picks from agent quick-screen to the research queue (not directly to board).
    Returns count of new nominations added.
    """
    import json

    # Load existing nominations
    existing = {}
    if RESEARCH_NOMINATION_FILE.exists():
        try:
            with open(RESEARCH_NOMINATION_FILE) as f:
                existing_list = json.load(f)
                existing = {item["ticker"]: item for item in existing_list}
        except Exception:
            existing = {}

    new_count = 0
    for pick in picks:
        ticker = pick.get("ticker", "")
        if not ticker:
            continue
        if ticker not in existing:
            existing[ticker] = {
                "ticker": ticker,
                "nominated_at": datetime.now(timezone.utc).isoformat(),
                "nominated_by": pick.get("source_agent_class", "unknown"),
                "quick_screen_mos": pick.get("margin_of_safety"),
                "quick_screen_verdict": pick.get("iv_verdict"),
                "status": "queued_for_research",
            }
            new_count += 1
            logger.info(f"[Pipeline] {ticker} -> research queue (agent nomination)")
        else:
            # Update nomination timestamp
            existing[ticker]["re_nominated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        with open(RESEARCH_NOMINATION_FILE, "w") as f:
            json.dump(list(existing.values()), f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"[Pipeline] Could not save research nominations: {e}")

    return new_count


# ── Patched run_pipeline with Step 0 ─────────────────────────────────────────

def run_pipeline_v2() -> dict:
    """
    Main pipeline entry point with research library integration.
    Step 0: Research library -> board direct
    Step 1: Agent quick-screen -> nominations into research queue
    Steps 2-5: Run for Step 0 picks only
    """
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- FIRST EAGLE PIPELINE (v2 + Research Library)")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Step 0: Research library -- companies with complete BUY research go to board
    logger.info("[Pipeline] Step 0: Checking research library for board-ready picks...")
    research_library_picks = _check_research_library()
    logger.info(
        f"[Pipeline] Step 0 complete: {len(research_library_picks)} research-library picks "
        f"going directly to board."
    )

    # Step 1: Agent quick-screen (DISCOVERY only -- nominees go to research queue)
    logger.info("[Pipeline] Step 1: Running all 10 FE R&D agents (discovery mode)...")
    raw_picks = _run_all_rd_agents()
    logger.info(f"[Pipeline] {len(raw_picks)} unique picks from R&D agents.")

    # Step 1b: Attach IV for quick-screen (needed to assess if worth researching)
    enriched_picks = _attach_iv(raw_picks)
    logger.info(f"[Pipeline] {len(enriched_picks)} picks with IV calculated.")

    # Step 1c: Nominate agent picks to research queue (NOT to board)
    mos_qualified = [p for p in enriched_picks if (p.get("margin_of_safety") or 0) >= 0.10]
    new_nominations = _nominate_to_research_queue(mos_qualified)
    logger.info(
        f"[Pipeline] {new_nominations} new companies nominated to research queue from agent screen."
    )

    # Step 2-5: Process ONLY research-library picks for board/sizing
    board_queue = research_library_picks

    # Save picks queue
    with open(PICKS_QUEUE_FILE, "w") as f:
        json.dump(board_queue, f, indent=2, default=str)
    logger.info(f"[Pipeline] Board queue written ({len(board_queue)} picks): {PICKS_QUEUE_FILE}")

    # Step 4: Board review
    logger.info("[Pipeline] Step 4: Running First Eagle Board review...")
    approved_picks_data = []
    try:
        from fe_board import run_board
        board_report = run_board()
        approved_picks_data = board_report.get("approved_picks", [])
        logger.info(
            f"[Pipeline] Board: {len(approved_picks_data)} approved, "
            f"{len(board_report.get('rejected_picks', []))} rejected."
        )
    except Exception as e:
        logger.error(f"[Pipeline] Board failed: {e}")
        # Fall back to research library picks if board fails
        approved_picks_data = research_library_picks

    # Step 5: Position sizing
    logger.info("[Pipeline] Step 5: Sizing positions...")
    sized_positions = _size_positions(approved_picks_data)
    gold_pct = _calculate_gold_allocation(approved_picks_data)

    if gold_pct < 0.12:
        logger.warning(
            f"[Pipeline] Gold allocation {gold_pct*100:.1f}% below 12% floor! "
            "Consider adding hard money position."
        )

    # Step 6: Log
    pipeline_log = {
        "generated_at":              started_at,
        "completed_at":              datetime.now(timezone.utc).isoformat(),
        "pipeline_version":          "v2_research_library",
        "research_library_picks":    len(research_library_picks),
        "agents_run":                len(FE_AGENT_REGISTRY),
        "total_rd_picks":            len(raw_picks),
        "iv_enriched_count":         len(enriched_picks),
        "new_research_nominations":  new_nominations,
        "board_queue_count":         len(board_queue),
        "approved_count":            len(approved_picks_data),
        "sized_positions":           sized_positions,
        "gold_allocation_pct":       round(gold_pct, 4),
        "lane_capital":              LANE_CAPITAL,
    }

    with open(PIPELINE_LOG_FILE, "w") as f:
        json.dump(pipeline_log, f, indent=2, default=str)
    logger.info(f"[Pipeline] Log written to {PIPELINE_LOG_FILE}")

    # Step 7: Email
    logger.info("[Pipeline] Step 7: Sending daily email...")
    _send_daily_email(pipeline_log)

    logger.info("[Pipeline] Done.")
    return pipeline_log
