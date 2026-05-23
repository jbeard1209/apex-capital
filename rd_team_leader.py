# =============================================================================
# APEX CAPITAL — R&D TEAM LEADER
# =============================================================================
# The R&D Team Leader is the director of the Research & Development Team.
# Every 4 days, this agent:
#
#   1. Scans all major sector ETFs (energy excluded per firm policy)
#   2. Scores each sector on a "capture the momentum" composite metric:
#      trend strength, RSI position, 1M/3M returns, and volatility
#   3. Selects the top 8 sectors most suited for momentum-capture strategy
#   4. Builds tailored research assignments (sector + watchlist + thesis context)
#      and distributes them to the 8 Research Agents
#   5. After the Research Agents complete their work, compiles all findings
#      into a consolidated briefing
#   6. Sends the briefing directly to the Chairman (Jack Beard) via email
#   7. Passes the research picks to the Manager Agent for capital deployment
#
# The R&D Team Leader is the only agent with a direct reporting line to both
# the Manager Agent AND the Chairman.
#
# Energy sector (XLE, XOP, oil, gas, solar, nuclear, etc.) is EXCLUDED from
# all research per firm policy.
#
# Usage:
#   python3 rd_team_leader.py              # full cycle: scan → assign → report
#   python3 rd_team_leader.py --scan-only  # scan and assign only (no email)
# =============================================================================

import fcntl
import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rd_team_leader")

# Suppress yfinance's noisy download-failure logs — our code handles None gracefully
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)
try:
    import os as _yf_os
    _yf_cache = _yf_os.path.expanduser("~/.cache/yfinance")
    _yf_os.makedirs(_yf_cache, exist_ok=True)
    yf.set_tz_cache_location(_yf_cache)
except Exception:
    pass

ASSIGNMENTS_PATH    = "data/sector_assignments.json"
RESEARCH_REPORT_PATH = "data/research_team_report.json"

# =============================================================================
# SECTOR UNIVERSE  (energy explicitly excluded)
# =============================================================================

SECTOR_UNIVERSE = {
    "Technology": {
        "etf": "XLK",
        "description": "Software, hardware, and IT services",
        "watchlist": ["MSFT", "AAPL", "NVDA", "META", "GOOGL", "ORCL",
                      "CRM", "ADBE", "NOW", "INTU"],
    },
    "Semiconductors": {
        "etf": "SMH",
        "description": "Chip design, manufacturing, and equipment",
        "watchlist": ["NVDA", "AMD", "AVGO", "QCOM", "TXN", "AMAT",
                      "LRCX", "KLAC", "MU", "ARM"],
    },
    "Financials": {
        "etf": "XLF",
        "description": "Banks, insurance, and asset managers",
        "watchlist": ["JPM", "BAC", "GS", "MS", "WFC", "BLK",
                      "V", "MA", "AXP", "SPGI"],
    },
    "Healthcare": {
        "etf": "XLV",
        "description": "Pharmaceuticals, medical devices, and managed care",
        "watchlist": ["UNH", "LLY", "ABBV", "JNJ", "MRK", "TMO",
                      "ABT", "ISRG", "VRTX", "ELV"],
    },
    "Biotech": {
        "etf": "IBB",
        "description": "Biotechnology, genomics, and drug discovery",
        "watchlist": ["AMGN", "GILD", "VRTX", "REGN", "MRNA",
                      "BIIB", "ALNY", "INCY", "EXAS", "NBIX"],
    },
    "Industrials": {
        "etf": "XLI",
        "description": "Aerospace, machinery, and transportation",
        "watchlist": ["HON", "GE", "CAT", "DE", "RTX",
                      "UPS", "EMR", "ETN", "CARR", "OTIS"],
    },
    "Defense & Aerospace": {
        "etf": "ITA",
        "description": "Defense contractors, aerospace, and government services",
        "watchlist": ["LMT", "RTX", "NOC", "GD", "BA",
                      "HII", "LDOS", "SAIC", "CACI", "BWXT"],
    },
    "Consumer Discretionary": {
        "etf": "XLY",
        "description": "Retail, autos, travel, and leisure",
        "watchlist": ["AMZN", "TSLA", "MCD", "NKE", "LOW",
                      "HD", "BKNG", "ABNB", "SBUX", "RCL"],
    },
    "Consumer Staples": {
        "etf": "XLP",
        "description": "Food, beverages, and household products",
        "watchlist": ["WMT", "PG", "KO", "PEP", "COST",
                      "PM", "MO", "CL", "KMB", "GIS"],
    },
    "Materials": {
        "etf": "XLB",
        "description": "Chemicals, metals, and mining (non-energy)",
        "watchlist": ["LIN", "APD", "FCX", "NEM", "DOW",
                      "PPG", "SHW", "ECL", "ALB", "MP"],
    },
    "Real Estate": {
        "etf": "XLRE",
        "description": "REITs — datacenter, logistics, residential, healthcare",
        "watchlist": ["PLD", "AMT", "EQIX", "CCI", "PSA",
                      "DLR", "O", "WELL", "AVB", "EQR"],
    },
    "Communication Services": {
        "etf": "XLC",
        "description": "Social media, streaming, telecom, and gaming",
        "watchlist": ["META", "GOOGL", "NFLX", "DIS", "VZ",
                      "T", "TMUS", "CHTR", "EA", "TTWO"],
    },
    "Cybersecurity": {
        "etf": "CIBR",
        "description": "Network security, endpoint, and cloud security",
        "watchlist": ["CRWD", "PANW", "ZS", "FTNT", "OKTA",
                      "S", "CYBR", "TENB", "RPM", "SAIL"],
    },
    "Cloud & SaaS": {
        "etf": "CLOU",
        "description": "Cloud infrastructure, SaaS platforms, and data",
        "watchlist": ["MSFT", "AMZN", "GOOGL", "CRM", "NOW",
                      "SNOW", "DDOG", "MDB", "NET", "CFLT"],
    },
    "Regional Banks": {
        "etf": "KRE",
        "description": "US regional and community banking sector",
        "watchlist": ["USB", "PNC", "TFC", "FITB", "HBAN",
                      "RF", "CFG", "KEY", "MTB", "ZION"],
    },
    "Retail": {
        "etf": "XRT",
        "description": "US retail — big box, specialty, and e-commerce",
        "watchlist": ["WMT", "AMZN", "TGT", "COST", "TJX",
                      "ROST", "DG", "DLTR", "KSS", "M"],
    },
}

# Energy sectors explicitly excluded (firm policy)
EXCLUDED_SECTORS = {
    "Energy", "Oil & Gas", "Clean Energy", "Solar", "Nuclear",
    "Renewables", "LNG", "Utilities",   # utilities adjacent to energy — excluded too
}


# =============================================================================
# SECTOR SCORING ENGINE
# =============================================================================

def _score_etf(etf: str) -> Optional[dict]:
    """
    Download ETF price data and compute a momentum-capture composite score.

    Composite (max 12 pts):
      trend_pts  (0–3): bullish > neutral > bearish
      rsi_pts    (0–3): 45–65 sweet spot; penalise overbought/oversold
      ret1m_pts  (0–2): +2%–+15% is ideal momentum entry range
      ret3m_pts  (0–2): strong 3-month tailwind
      vol_pts    (0–2): 18–35% annualised vol — enough movement to capture
    """
    try:
        df = yf.download(etf, period="300d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 30:
            return None

        close  = df["Close"]
        price  = float(close.iloc[-1])
        ma50   = float(close.iloc[-50:].mean())  if len(close) >= 50  else float(close.mean())
        ma200  = float(close.iloc[-200:].mean()) if len(close) >= 200 else ma50

        # RSI-14
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1]) if len(rs.dropna()) > 0 else 50.0

        # Returns
        ret1m  = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0.0
        ret3m  = float((close.iloc[-1] / close.iloc[-63] - 1) * 100) if len(close) >= 63 else 0.0

        # 20-day annualised volatility
        returns  = close.pct_change().dropna()
        vol_20d  = float(returns.iloc[-20:].std() * (252 ** 0.5) * 100) if len(returns) >= 20 else 20.0

        # Trend
        if price >= ma50 and price >= ma200:
            trend     = "bullish"
            trend_pts = 3
        elif price >= ma50 or price >= ma200:
            trend     = "neutral"
            trend_pts = 1
        else:
            trend     = "bearish"
            trend_pts = 0

        # RSI sweet spot: 45–65 is momentum building but not exhausted
        if 45 <= rsi <= 65:
            rsi_pts = 3
        elif 40 <= rsi < 45 or 65 < rsi <= 72:
            rsi_pts = 2
        elif 35 <= rsi < 40 or 72 < rsi <= 78:
            rsi_pts = 1
        else:
            rsi_pts = 0   # oversold or severely overbought

        # 1M return: 2–15% is ideal for momentum entry
        if 2 <= ret1m <= 15:
            ret1m_pts = 2
        elif ret1m > 0 or (-2 <= ret1m < 0):
            ret1m_pts = 1
        else:
            ret1m_pts = 0

        # 3M return: strong tailwind wanted
        if ret3m >= 8:
            ret3m_pts = 2
        elif ret3m >= 3:
            ret3m_pts = 1
        else:
            ret3m_pts = 0

        # Volatility: 18–35% annualised is the sweet spot
        if 18 <= vol_20d <= 35:
            vol_pts = 2
        elif 12 <= vol_20d < 18 or 35 < vol_20d <= 45:
            vol_pts = 1
        else:
            vol_pts = 0

        composite = trend_pts + rsi_pts + ret1m_pts + ret3m_pts + vol_pts

        return {
            "etf":             etf,
            "price":           round(price,   2),
            "ma50":            round(ma50,    2),
            "ma200":           round(ma200,   2),
            "trend":           trend,
            "rsi_14":          round(rsi,     1),
            "ret_1m_pct":      round(ret1m,   2),
            "ret_3m_pct":      round(ret3m,   2),
            "volatility_20d":  round(vol_20d, 1),
            "pct_from_50ma":   round((price - ma50)  / ma50  * 100, 2),
            "pct_from_200ma":  round((price - ma200) / ma200 * 100, 2),
            "score":           composite,
            "score_breakdown": {
                "trend":  trend_pts,
                "rsi":    rsi_pts,
                "ret_1m": ret1m_pts,
                "ret_3m": ret3m_pts,
                "vol":    vol_pts,
            },
        }
    except Exception as e:
        logger.warning(f"    ETF score failed for {etf}: {e}")
        return None


def _build_earnings_html_section() -> str:
    """Build an HTML card showing upcoming earnings predictions."""
    try:
        ep_path = "data/earnings_predictions.json"
        if not os.path.exists(ep_path):
            return ""
        with open(ep_path) as _f:
            ep = json.load(_f)
        preds = ep.get("predictions", [])
        if not preds:
            return ""
        summ = ep.get("summary", {})

        rows = ""
        for p in preds[:20]:
            col   = {"BEAT": "#3fb950", "MISS": "#f85149", "INLINE": "#f0883e"}.get(p["prediction"], "#8b949e")
            icon  = {"BEAT": "▲", "MISS": "▼", "INLINE": "–"}.get(p["prediction"], "")
            conf  = "★" * p["confidence"]
            br    = f"{p['beat_rate']:.0%}" if p.get("beat_rate") is not None else "n/a"
            rows += f"""
            <tr style="border-bottom:1px solid #21262d;">
              <td style="padding:7px 8px;font-weight:bold;color:#58a6ff;">{p['ticker']}</td>
              <td style="padding:7px 8px;color:#8b949e;">{p['earnings_date']}</td>
              <td style="padding:7px 8px;color:#8b949e;">{p['days_until']}d</td>
              <td style="padding:7px 8px;color:{col};font-weight:bold;">{icon} {p['prediction']}</td>
              <td style="padding:7px 8px;color:#f0c000;">{conf}</td>
              <td style="padding:7px 8px;color:#8b949e;">{br}</td>
              <td style="padding:7px 8px;color:#8b949e;font-size:11px;">{p['reasoning'][:70]}</td>
            </tr>"""

        return f"""
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:20px;margin-bottom:20px;">
    <h2 style="color:#58a6ff;font-size:15px;margin:0 0 4px 0;">
      Earnings Predictions — Next 90 Days
    </h2>
    <p style="color:#8b949e;font-size:12px;margin:0 0 14px 0;">
      {summ.get('total',0)} upcoming earnings &nbsp;·&nbsp;
      <span style="color:#3fb950;">{summ.get('beat_calls',0)} BEAT</span> &nbsp;·&nbsp;
      <span style="color:#f85149;">{summ.get('miss_calls',0)} MISS</span> &nbsp;·&nbsp;
      <span style="color:#f0883e;">{summ.get('inline_calls',0)} INLINE</span> &nbsp;·&nbsp;
      {summ.get('high_conf',0)} high-confidence (4★+)
    </p>
    <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:13px;">
      <thead>
        <tr style="border-bottom:1px solid #30363d;color:#8b949e;font-size:11px;">
          <th style="padding:6px 8px;text-align:left;">TICKER</th>
          <th style="padding:6px 8px;text-align:left;">DATE</th>
          <th style="padding:6px 8px;text-align:left;">DAYS</th>
          <th style="padding:6px 8px;text-align:left;">PREDICTION</th>
          <th style="padding:6px 8px;text-align:left;">CONF</th>
          <th style="padding:6px 8px;text-align:left;">BEAT RATE</th>
          <th style="padding:6px 8px;text-align:left;">REASONING</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>"""
    except Exception:
        return ""


def _build_intl_html_section() -> str:
    """Build an HTML card showing the latest international markets snapshot."""
    try:
        rpt_path = "data/international_report.json"
        if not os.path.exists(rpt_path):
            return ""
        with open(rpt_path) as _f:
            rpt = json.load(_f)

        sentiment = rpt.get("sentiment", "NEUTRAL")
        score     = rpt.get("sentiment_score", 0)
        session   = rpt.get("session", "")
        watch     = rpt.get("watch_tickers", [])
        impl      = rpt.get("us_implications", [])

        sent_color = {"RISK_ON": "#3fb950", "RISK_OFF": "#f85149", "NEUTRAL": "#f0883e"}.get(sentiment, "#8b949e")

        notable_rows = ""
        for r in rpt.get("notable_indices", [])[:8]:
            pct = r.get("pct")
            if pct is None:
                continue
            col = "#3fb950" if pct > 0 else "#f85149"
            notable_rows += f"""
            <tr style="border-bottom:1px solid #21262d;">
              <td style="padding:6px 8px;color:#e6edf3;">{r['name']}</td>
              <td style="padding:6px 8px;color:{col};font-weight:bold;">{pct:+.2f}%</td>
            </tr>"""

        fx_rows = ""
        for r in rpt.get("notable_currencies", [])[:4]:
            pct = r.get("pct")
            if pct is None:
                continue
            col = "#3fb950" if pct > 0 else "#f85149"
            fx_rows += f"""
            <tr style="border-bottom:1px solid #21262d;">
              <td style="padding:6px 8px;color:#e6edf3;">{r['name']}</td>
              <td style="padding:6px 8px;color:{col};font-weight:bold;">{pct:+.2f}%</td>
            </tr>"""

        impl_html = "".join(
            f'<li style="margin-bottom:6px;color:#8b949e;">{i}</li>' for i in impl[:4]
        )
        watch_html = (
            " &nbsp;·&nbsp; ".join(
                f'<span style="color:#58a6ff;font-weight:bold;">{t}</span>' for t in watch[:8]
            ) if watch else '<span style="color:#8b949e;">none flagged</span>'
        )

        return f"""
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:20px;margin-bottom:20px;">
    <h2 style="color:#58a6ff;font-size:15px;margin:0 0 4px 0;">
      International Markets Intelligence — {session.replace('_',' ').title()}
    </h2>
    <p style="color:#8b949e;font-size:12px;margin:0 0 14px 0;">
      Sentiment: <span style="color:{sent_color};font-weight:bold;">{sentiment}</span>
      &nbsp;({score:+d}) &nbsp;·&nbsp; IMI Desk / R&D Department
    </p>
    <div style="display:flex;gap:20px;flex-wrap:wrap;">
      <div style="flex:1;min-width:180px;">
        <p style="color:#8b949e;font-size:11px;margin:0 0 6px 0;">NOTABLE INDEX MOVES</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <tbody>{notable_rows or '<tr><td style="color:#8b949e;padding:6px 8px;">No major moves</td></tr>'}</tbody>
        </table>
      </div>
      <div style="flex:1;min-width:160px;">
        <p style="color:#8b949e;font-size:11px;margin:0 0 6px 0;">FX MOVES</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <tbody>{fx_rows or '<tr><td style="color:#8b949e;padding:6px 8px;">No major moves</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    <p style="color:#8b949e;font-size:11px;margin:16px 0 6px 0;">US IMPLICATIONS</p>
    <ul style="margin:0;padding-left:18px;font-size:13px;">{impl_html}</ul>
    <p style="color:#8b949e;font-size:11px;margin:14px 0 6px 0;">UNIVERSE TICKERS TO WATCH</p>
    <p style="font-size:13px;margin:0;">{watch_html}</p>
  </div>"""
    except Exception:
        return ""


# =============================================================================
# R&D TEAM LEADER
# =============================================================================

class RDTeamLeader:
    """
    Director of the Research & Development Team.
    Scans sectors, assigns research targets, compiles findings,
    and reports directly to the Chairman and Manager Agent.
    """

    N_AGENTS = 8   # number of research agents available

    # ------------------------------------------------------------------
    # Step 1 — Scan sectors and build assignments
    # ------------------------------------------------------------------

    def scan_and_assign(self) -> list[dict]:
        """
        Scan all non-energy sectors, score for momentum-capture suitability,
        select top N_AGENTS sectors, and build research assignments.
        Returns list of assignment dicts.
        """
        # ── Read canary regime (CIT-2026-203) ─────────────────────────────
        # Shift sector scoring mandate based on current market regime:
        #   BULL      → standard momentum scoring (no adjustment)
        #   NEUTRAL   → slight preference for quality/low-beta sectors (+1 pt bonus)
        #   DEFENSIVE → strong preference for defensive sectors (+3 pt bonus);
        #               momentum-heavy sectors penalised (-2 pts)
        #   CRISIS    → only capital-preservation sectors score positively;
        #               all momentum sectors penalised (-4 pts)
        _regime = "BULL"
        try:
            import json as _json
            _canary = _json.load(open("data/canary_state.json")) if os.path.exists("data/canary_state.json") else {}
            _regime = _canary.get("regime", "BULL")
        except Exception:
            pass

        _DEFENSIVE_SECTORS = {"Consumer Staples", "Healthcare", "Utilities", "Real Estate"}
        _MOMENTUM_SECTORS  = {"Technology", "Semiconductors", "Cloud & SaaS",
                               "Consumer Discretionary", "Biotech"}

        if _regime in ("DEFENSIVE", "CRISIS"):
            logger.info(
                f"  [R&D Leader] REGIME={_regime} — shifting mandate to defensive/quality sectors. "
                f"Boosting: {', '.join(_DEFENSIVE_SECTORS)}. "
                f"Penalising momentum sectors."
            )
        else:
            logger.info(f"  [R&D Leader] REGIME={_regime} — standard momentum mandate.")

        logger.info("  [R&D Leader] Scanning sector universe for momentum opportunities...")
        logger.info(f"  [R&D Leader] Energy sector excluded per firm policy.")

        scored_sectors = []

        for sector_name, meta in SECTOR_UNIVERSE.items():
            # Skip any sector that overlaps with energy
            if any(e.lower() in sector_name.lower() for e in EXCLUDED_SECTORS):
                logger.debug(f"    Skipping excluded sector: {sector_name}")
                continue

            etf   = meta["etf"]
            score = _score_etf(etf)
            if score is None:
                continue

            # ── Regime-adaptive score adjustment (CIT-2026-203) ──────────
            regime_adj = 0
            if _regime == "NEUTRAL":
                if sector_name in _DEFENSIVE_SECTORS:
                    regime_adj = +1
            elif _regime == "DEFENSIVE":
                if sector_name in _DEFENSIVE_SECTORS:
                    regime_adj = +3
                elif sector_name in _MOMENTUM_SECTORS:
                    regime_adj = -2
            elif _regime == "CRISIS":
                if sector_name in _DEFENSIVE_SECTORS:
                    regime_adj = +4
                elif sector_name in _MOMENTUM_SECTORS:
                    regime_adj = -4
                else:
                    regime_adj = -1

            if regime_adj != 0:
                # Apply adjustment to a copy so original score data is preserved
                score = {**score, "score": score["score"] + regime_adj,
                         "regime_adj": regime_adj}

            scored_sectors.append({
                "sector_name": sector_name,
                "etf":         etf,
                "description": meta["description"],
                "watchlist":   meta["watchlist"],
                "score_data":  score,
            })
            logger.info(
                f"    {sector_name:<28} ETF:{etf:<6}  "
                f"Score:{score['score']:>2}/12  "
                f"Trend:{score['trend']:<8}  "
                f"RSI:{score['rsi_14']:>5.1f}  "
                f"1M:{score['ret_1m_pct']:>+6.1f}%"
                + (f"  [regime_adj={regime_adj:+d}]" if regime_adj != 0 else "")
            )

        # Sort by composite score descending
        scored_sectors.sort(key=lambda x: x["score_data"]["score"], reverse=True)

        # Select top N_AGENTS
        top_sectors = scored_sectors[:self.N_AGENTS]
        dropped     = scored_sectors[self.N_AGENTS:]

        logger.info(f"\n  [R&D Leader] Top {len(top_sectors)} sectors selected for research:")
        for i, s in enumerate(top_sectors, 1):
            sd = s["score_data"]
            logger.info(
                f"    {i}. {s['sector_name']:<28} "
                f"Score:{sd['score']:>2}/12  "
                f"Vol:{sd['volatility_20d']:.0f}%  "
                f"3M:{sd['ret_3m_pct']:>+.1f}%"
            )

        if dropped:
            logger.info(
                f"  [R&D Leader] Sectors not assigned this cycle: "
                + ", ".join(s["sector_name"] for s in dropped)
            )

        # Build formal assignments
        assignments = []
        for i, s in enumerate(top_sectors):
            sd = s["score_data"]
            assignments.append({
                "agent_id":           i + 1,
                "sector_name":        s["sector_name"],
                "sector_etf":         s["etf"],
                "description":        s["description"],
                "watchlist":          s["watchlist"],
                "momentum_score":     sd["score"],
                "momentum_data":      sd,
                "investment_horizon": "long_term" if sd["ret_3m_pct"] > 10 else "both",
                "leader_rationale":   self._build_rationale(s["sector_name"], sd),
                "assigned_at":        datetime.now().isoformat(),
            })

        # ── CRO Risk Overlay ─────────────────────────────────────────────
        # Read the Chief Risk Officer's public bulletin and annotate each
        # assignment with the current risk context so research agents can
        # calibrate their conviction thresholds accordingly.
        cro_bulletin = {}
        try:
            if os.path.exists("data/risk_bulletin.json"):
                with open("data/risk_bulletin.json") as _f:
                    cro_bulletin = json.load(_f)
        except Exception:
            pass

        cro_risk = cro_bulletin.get("risk_level", "UNKNOWN")
        cro_dir  = (cro_bulletin.get("directives") or [""])[0]

        if cro_risk in ("HIGH", "CRITICAL"):
            logger.warning(
                f"  [CRO] Risk level {cro_risk} — R&D raising conviction bar. "
                "Only 4★+ picks will be forwarded to Manager Agent."
            )
        for asgn in assignments:
            asgn["cro_risk_level"] = cro_risk
            asgn["cro_advisory"]   = cro_dir if cro_dir else f"Risk level: {cro_risk}."
            # In HIGH/CRITICAL, instruct agents to raise conviction threshold
            if cro_risk in ("HIGH", "CRITICAL"):
                asgn["min_conviction_override"] = 4  # override normal 3★ minimum

        # Save assignments for research agents to read
        os.makedirs("data", exist_ok=True)
        payload = {
            "generated_at":    datetime.now().isoformat(),
            "leader":          "R&D Team Leader",
            "cro_risk_level":  cro_risk,
            "sectors_scanned": len(scored_sectors),
            "sectors_assigned": len(assignments),
            "all_scored":      scored_sectors,
            "assignments":     assignments,
        }
        with open(ASSIGNMENTS_PATH, "w") as f:
            json.dump(payload, f, indent=2, default=str)

        logger.info(f"  [R&D Leader] Assignments saved → {ASSIGNMENTS_PATH}")

        # ── Context Analysts — run alongside sector assignments ───────────
        # Historical Context Analyst and Geopolitical Team both receive the
        # final assignments and produce context reports for the briefing.
        # Their output is saved to data/ for compile_briefing() to embed.
        try:
            from rd_historical_analyst import HistoricalContextAnalyst
            hist_analyst = HistoricalContextAnalyst()
            hist_analyst.analyze(assignments, quiet=True)
            logger.info("  [R&D Leader] Historical Context Analyst complete")
        except Exception as e:
            logger.warning(f"  [R&D Leader] Historical Analyst error: {e}")

        try:
            from rd_geopolitical_team import GeopoliticalResearchTeam
            geo_team = GeopoliticalResearchTeam()
            # Pass a minimal regime dict — analyst will read risk_bulletin.json itself
            geo_team.analyze(assignments, regime={})
            logger.info("  [R&D Leader] Geopolitical Research Team complete")
        except Exception as e:
            logger.warning(f"  [R&D Leader] Geopolitical Team error: {e}")

        # System Performance Analyst — audits operational health of the system
        # itself. Catches take-profit leakage, stop-loss enforcement gaps,
        # alert fatigue, and signal execution failures.
        try:
            from rd_system_performance import SystemPerformanceAnalyst
            sys_analyst = SystemPerformanceAnalyst()
            sys_analyst.analyse(quiet=True)
            logger.info("  [R&D Leader] System Performance Analyst complete")
        except Exception as e:
            logger.warning(f"  [R&D Leader] System Performance Analyst error: {e}")

        return assignments

    def _build_rationale(self, sector: str, sd: dict) -> str:
        parts = []
        if sd["trend"] == "bullish":
            parts.append(
                f"{sector} is in a confirmed uptrend "
                f"({sd['pct_from_50ma']:+.1f}% above 50MA, "
                f"{sd['pct_from_200ma']:+.1f}% above 200MA)"
            )
        elif sd["trend"] == "neutral":
            parts.append(f"{sector} is at a trend inflection point — momentum may be building")
        else:
            parts.append(f"{sector} is under pressure but showing relative strength worth watching")

        if 45 <= sd["rsi_14"] <= 65:
            parts.append(
                f"RSI at {sd['rsi_14']:.0f} indicates momentum is building "
                f"without being overbought — ideal capture-the-momentum entry zone"
            )
        elif sd["rsi_14"] > 65:
            parts.append(
                f"RSI at {sd['rsi_14']:.0f} — overbought territory; "
                f"research should focus on pullback entry candidates"
            )

        parts.append(
            f"Sector returned {sd['ret_1m_pct']:+.1f}% (1M) / {sd['ret_3m_pct']:+.1f}% (3M) "
            f"with {sd['volatility_20d']:.0f}% annualised volatility — "
            f"{'sufficient movement to capture momentum' if sd['volatility_20d'] >= 18 else 'lower volatility; focus on high-conviction individual names'}"
        )

        return ". ".join(parts) + "."

    # ------------------------------------------------------------------
    # Step 2 — Compile findings after research agents complete
    # ------------------------------------------------------------------

    def compile_briefing(self, research_report: dict,
                          assignment_payload: dict) -> str:
        """
        After the Research Team has run, compile a full briefing for the Chairman.
        Covers: market landscape, sector selection rationale, top picks, and
        investment themes.
        """
        now    = datetime.now().strftime("%B %d, %Y at %H:%M")
        picks  = research_report.get("recommendations", [])
        agents = research_report.get("agent_reports", [])
        asgns  = assignment_payload.get("assignments", [])
        scanned = assignment_payload.get("sectors_scanned", 0)

        high_conv = [p for p in picks if p.get("conviction", 0) >= 4]

        lines = [
            "═" * 72,
            "  APEX CAPITAL INTELLIGENCE",
            "  R&D TEAM LEADER — CHAIRMAN BRIEFING",
            f"  {now}",
            "═" * 72,
            "",
            "  TO:      Jack Beard (Chairman)",
            "  FROM:    R&D Team Leader",
            "  SUBJECT: Sector Momentum Analysis & Research Findings",
            "",
            "─" * 72,
            "  MARKET LANDSCAPE SUMMARY",
            "─" * 72,
            "",
        ]

        # Sector scores summary
        all_scored = assignment_payload.get("all_scored", [])
        lines.append(
            f"  Scanned {scanned} sectors (energy excluded per firm policy)."
        )
        lines.append(
            f"  Assigned the top {len(asgns)} to the Research & Development Team."
        )
        lines.append("")

        lines.append("  SECTOR SCORES (momentum-capture composite, max 12):")
        lines.append(f"  {'RANK':<5} {'SECTOR':<28} {'SCORE':>5} {'TREND':<10} "
                     f"{'RSI':>5} {'1M RTN':>7} {'3M RTN':>7} {'VOL':>6}")
        lines.append("  " + "─" * 68)
        for i, s in enumerate(all_scored[:12], 1):   # show top 12
            sd    = s["score_data"]
            flag  = " ← ASSIGNED" if i <= len(asgns) else ""
            lines.append(
                f"  {i:<5} {s['sector_name']:<28} {sd['score']:>5}/12 "
                f"{sd['trend']:<10} {sd['rsi_14']:>5.1f} "
                f"{sd['ret_1m_pct']:>+6.1f}% {sd['ret_3m_pct']:>+6.1f}% "
                f"{sd['volatility_20d']:>5.0f}%{flag}"
            )

        lines += ["", "─" * 72, "  RESEARCH ASSIGNMENTS & AGENT FINDINGS", "─" * 72, ""]

        for report in agents:
            asgn = next((a for a in asgns if a["sector_name"] == report.get("sector_name")), {})
            lines.append(f"  AGENT {report.get('agent_id', '?')} — {report.get('sector_name', '?')}")
            lines.append(f"  Rationale: {asgn.get('leader_rationale', '')[:80]}...")
            lines.append(f"  Tickers analysed: {report.get('tickers_analysed', 0)}")
            top = report.get("top_picks", [])[:3]
            if top:
                for pick in top:
                    conv = "★" * pick.get("conviction", 1)
                    mom  = pick.get("momentum", {})
                    lines.append(
                        f"    • {pick['ticker']:<7} {conv:<5}  "
                        f"Trend:{mom.get('trend','?'):<9}  "
                        f"RSI:{mom.get('rsi_14', 0):>5.1f}  "
                        f"1M vs 50MA:{mom.get('pct_from_50ma', 0):>+6.1f}%"
                    )
                    reasons = pick.get("reasons", [])
                    if reasons:
                        lines.append(f"      {reasons[0][:65]}")
            else:
                lines.append("    No high-conviction picks identified in this sector this cycle.")
            lines.append("")

        lines += ["─" * 72, "  TOP PICKS ACROSS ALL SECTORS", "─" * 72, ""]

        if picks:
            lines.append(
                f"  {'TICKER':<7} {'SECTOR':<26} {'CONV':>5} "
                f"{'HORIZON':<12} {'TREND':<10} {'RSI':>5}"
            )
            lines.append("  " + "─" * 65)
            for p in picks[:10]:
                mom  = p.get("momentum", {})
                conv = "★" * p.get("conviction", 1)
                lines.append(
                    f"  {p['ticker']:<7} {p.get('specialty','?'):<26} "
                    f"{conv:<5} {p.get('investment_horizon','?'):<12} "
                    f"{mom.get('trend','?'):<10} {mom.get('rsi_14',0):>5.1f}"
                )
                if p.get("reasons"):
                    lines.append(f"    → {p['reasons'][0][:70]}")
        else:
            lines.append("  No picks met the minimum 3-star conviction threshold this cycle.")

        lines += [
            "",
            "─" * 72,
            "  INVESTMENT THEMES IDENTIFIED",
            "─" * 72,
            "",
        ]

        # Identify themes from assigned sectors
        top_sectors = [a["sector_name"] for a in asgns[:3]]
        if top_sectors:
            lines.append(
                f"  The strongest momentum-capture opportunities this cycle are concentrated "
                f"in: {', '.join(top_sectors)}."
            )
        if high_conv:
            tickers = [p["ticker"] for p in high_conv[:5]]
            lines.append(
                f"  High-conviction picks (4★+): {', '.join(tickers)} — "
                f"these have been forwarded to the Manager Agent for capital deployment review."
            )
        lines.append(
            f"  All picks are being reviewed by the Manager Agent alongside the "
            f"Portfolio Deployment Officer's capital efficiency signal."
        )

        # ── Historical Context Analyst Section ────────────────────────────
        try:
            if os.path.exists("data/rd_historical_context.json"):
                with open("data/rd_historical_context.json") as _hf:
                    _hist = json.load(_hf)
                _syn  = _hist.get("synthesis", {})
                _warn = _hist.get("warning_signals", [])
                _bull = _hist.get("historical_analogues", {}).get("most_relevant_bull_analogues", [])
                _bear = _hist.get("historical_analogues", {}).get("most_relevant_bear_analogues", [])

                lines += [
                    "", "─" * 72,
                    "  HISTORICAL CONTEXT ANALYST — BALANCED MARKET HISTORY",
                    "─" * 72,
                    f"  Historical Stance:  {_syn.get('historical_stance', '—')}",
                    f"  Regime:             {_syn.get('regime_summary', '—')}",
                    f"  Strategy Win Rate:  {_syn.get('strategy_win_rate_pct', '—')}% (across {_hist.get('strategy_track_record',{}).get('periods_analysed','?')} annotated periods)",
                    "",
                ]

                if _bull:
                    lines.append(f"  BEST BULL ANALOGUE: {_bull[0]['label']}")
                    lines.append(f"  Lesson: {_bull[0]['lesson'][:120]}...")
                    lines.append("")
                if _bear:
                    lines.append(f"  KEY BEAR RISK:      {_bear[0]['label']}")
                    lines.append(f"  Lesson: {_bear[0]['lesson'][:120]}...")
                    lines.append("")

                warnings = [s for s in _warn if s["type"] == "WARNING"]
                opps     = [s for s in _warn if s["type"] == "OPPORTUNITY"]
                if warnings:
                    lines.append("  WARNING SIGNALS:")
                    for w in warnings[:3]:
                        lines.append(f"  ⚑  {w['signal']}: {w['message'][:90]}...")
                    lines.append("")
                if opps:
                    lines.append("  OPPORTUNITY SIGNALS:")
                    for o in opps[:2]:
                        lines.append(f"  ✓  {o['signal']}: {o['message'][:90]}...")
                    lines.append("")
        except Exception:
            pass

        # ── Geopolitical Research Team Section ───────────────────────────
        try:
            if os.path.exists("data/rd_geopolitical_context.json"):
                with open("data/rd_geopolitical_context.json") as _gf:
                    _geo = json.load(_gf)

                lines += [
                    "─" * 72,
                    "  GEOPOLITICAL RESEARCH TEAM (Dr. Reyes · Dr. Okafor)",
                    "─" * 72,
                    f"  {_geo.get('team_summary', '')}",
                    "",
                ]

                if _geo.get("double_confirmed_sectors"):
                    lines.append(f"  ✓  POLICY + GEO CONFIRMED: {', '.join(_geo['double_confirmed_sectors'])}")
                if _geo.get("double_flagged_sectors"):
                    lines.append(f"  ⚑  POLICY + GEO RISK:      {', '.join(_geo['double_flagged_sectors'])}")

                for note in _geo.get("synthesis_notes", [])[:2]:
                    lines.append(f"  {note[:100]}")

                _pol = _geo.get("policy_analysis", {})
                if _pol.get("policy_note"):
                    lines.append(f"  Policy: {_pol['policy_note'][:110]}...")

                lines.append("")
        except Exception:
            pass

        # ── System Performance Analyst Section ───────────────────────────
        try:
            if os.path.exists("data/rd_system_performance.json"):
                with open("data/rd_system_performance.json") as _spf:
                    _sp = json.load(_spf)

                _score   = _sp.get("health_score", 10)
                _verdict = _sp.get("verdict", "—")
                _crit    = _sp.get("critical_count", 0)
                _high    = _sp.get("high_count", 0)
                _med     = _sp.get("medium_count", 0)
                _score_icon = "🔴" if _score < 5 else "🟠" if _score < 7 else "🟢"

                lines += [
                    "─" * 72,
                    "  SYSTEM PERFORMANCE ANALYST",
                    "─" * 72,
                    f"  {_score_icon}  System Health: {_score}/10 — {_verdict}",
                    f"  Findings: {_sp.get('total_findings',0)} total  "
                    f"({_crit} critical · {_high} high · {_med} medium)",
                    "",
                ]

                for finding in _sp.get("findings", [])[:5]:
                    sev  = finding.get("severity", "?")
                    icon = "🔴" if sev == "CRITICAL" else "🟠" if sev == "HIGH" else "🟡"
                    lines.append(f"  {icon}  [{sev}] {finding.get('type','?')}")
                    lines.append(f"       {finding.get('message','')[:100]}")

                recs = _sp.get("recommendations", [])
                if recs:
                    lines.append("")
                    lines.append("  Recommended Actions:")
                    for rec in recs[:3]:
                        lines.append(f"    → {rec[:100]}")

                lines.append("")
        except Exception:
            pass

        # ── Earnings Predictions Section ─────────────────────────────────
        try:
            if os.path.exists("data/earnings_predictions.json"):
                with open("data/earnings_predictions.json") as _epf:
                    _ep = json.load(_epf)
                _preds = _ep.get("predictions", [])
                if _preds:
                    _summ = _ep.get("summary", {})
                    lines += [
                        "─" * 72,
                        "  EARNINGS PREDICTIONS (next 90 days)",
                        "─" * 72,
                        f"  {_summ.get('total', 0)} upcoming earnings detected  |  "
                        f"{_summ.get('beat_calls', 0)} BEAT · "
                        f"{_summ.get('miss_calls', 0)} MISS · "
                        f"{_summ.get('inline_calls', 0)} INLINE  |  "
                        f"{_summ.get('high_conf', 0)} high-confidence (4★+)",
                        "",
                        f"  {'TICKER':<7} {'DATE':<12} {'DAYS':>4}  {'PRED':<8} {'CONF':>4}  REASONING",
                        "  " + "─" * 68,
                    ]
                    for pred in _preds[:20]:
                        _icon = {"BEAT": "+", "MISS": "✗", "INLINE": "~"}.get(pred["prediction"], " ")
                        lines.append(
                            f"  {pred['ticker']:<7} {pred['earnings_date']:<12} "
                            f"{pred['days_until']:>4}d  "
                            f"{_icon} {pred['prediction']:<6}  {'★' * pred['confidence']:<5}  "
                            f"{pred['reasoning'][:60]}"
                        )
                    lines.append("")
        except Exception:
            pass

        lines += [
            "",
            "═" * 72,
            "  R&D Team Leader — Apex Capital Intelligence",
            "  Next research cycle in 4 days.",
            f"  Generated {now}",
            "═" * 72,
        ]

        return "\n".join(lines)

    def compile_html_briefing(self, research_report: dict,
                               assignment_payload: dict) -> str:
        """HTML version of the Chairman briefing for email."""
        now       = datetime.now().strftime("%B %d, %Y at %H:%M")
        picks     = research_report.get("recommendations", [])
        agents    = research_report.get("agent_reports", [])
        asgns     = assignment_payload.get("assignments", [])
        all_scored = assignment_payload.get("all_scored", [])
        high_conv = [p for p in picks if p.get("conviction", 0) >= 4]

        # Build sector rows
        sector_rows = ""
        for i, s in enumerate(all_scored[:12], 1):
            sd     = s["score_data"]
            is_asgn = i <= len(asgns)
            bg      = "#1c2a1c" if is_asgn else ""
            flag    = '<span style="color:#3fb950;font-size:11px;"> ✓ ASSIGNED</span>' if is_asgn else ""
            trend_c = {"bullish": "#3fb950", "neutral": "#f0883e", "bearish": "#f85149"}.get(
                sd["trend"], "#8b949e")
            sector_rows += f"""
            <tr style="background:{bg};">
              <td style="padding:7px 8px;color:#8b949e;">{i}</td>
              <td style="padding:7px 8px;color:#c9d1d9;font-weight:{'bold' if is_asgn else 'normal'};">
                {s['sector_name']}{flag}</td>
              <td style="padding:7px 8px;color:#58a6ff;font-weight:bold;">{sd['score']}/12</td>
              <td style="padding:7px 8px;color:{trend_c};">{sd['trend']}</td>
              <td style="padding:7px 8px;color:#c9d1d9;">{sd['rsi_14']:.0f}</td>
              <td style="padding:7px 8px;color:{'#3fb950' if sd['ret_1m_pct']>=0 else '#f85149'};">
                {sd['ret_1m_pct']:+.1f}%</td>
              <td style="padding:7px 8px;color:{'#3fb950' if sd['ret_3m_pct']>=0 else '#f85149'};">
                {sd['ret_3m_pct']:+.1f}%</td>
              <td style="padding:7px 8px;color:#c9d1d9;">{sd['volatility_20d']:.0f}%</td>
            </tr>"""

        # Build picks rows
        picks_rows = ""
        for p in picks[:10]:
            mom  = p.get("momentum", {})
            conv = "★" * p.get("conviction", 1)
            trend_c = {"bullish": "#3fb950", "neutral": "#f0883e", "bearish": "#f85149"}.get(
                mom.get("trend"), "#8b949e")
            horizon_badge = (
                '<span style="color:#58a6ff;">long-term</span>'
                if p.get("investment_horizon") == "long_term" else
                '<span style="color:#f0883e;">short/long</span>'
            )
            picks_rows += f"""
            <tr>
              <td style="padding:9px 8px;font-weight:bold;color:#58a6ff;">{p['ticker']}</td>
              <td style="padding:9px 8px;color:#c9d1d9;">{p.get('specialty','—')}</td>
              <td style="padding:9px 8px;color:#f0c000;">{conv}</td>
              <td style="padding:9px 8px;">{horizon_badge}</td>
              <td style="padding:9px 8px;color:{trend_c};">{mom.get('trend','—')}</td>
              <td style="padding:9px 8px;color:#c9d1d9;">{mom.get('rsi_14',0):.0f}</td>
            </tr>
            <tr>
              <td colspan="6" style="padding:0 8px 9px 8px;color:#8b949e;font-size:12px;
                  border-bottom:1px solid #21262d;">
                {p['reasons'][0][:80] if p.get('reasons') else ''}
              </td>
            </tr>"""

        if not picks_rows:
            picks_rows = '<tr><td colspan="6" style="color:#8b949e;padding:12px;">No picks met 3★ threshold this cycle.</td></tr>'

        top_sectors_str = ", ".join(a["sector_name"] for a in asgns[:3])
        hc_str = ", ".join(p["ticker"] for p in high_conv[:5]) if high_conv else "None this cycle"

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:980px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#58a6ff;margin:0;font-size:22px;">
      APEX CAPITAL — R&amp;D Team Leader Briefing
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">
      To: Jack Beard (Chairman) &nbsp;·&nbsp; {now} &nbsp;·&nbsp;
      Sector momentum analysis &amp; research findings
    </p>
  </div>

  <!-- summary -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:28px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Sectors Scanned</div>
      <div style="font-size:22px;font-weight:bold;">{assignment_payload.get('sectors_scanned',0)}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Assigned to Team</div>
      <div style="font-size:22px;font-weight:bold;">{len(asgns)}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Total Picks</div>
      <div style="font-size:22px;font-weight:bold;">{len(picks)}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">High Conviction (4★+)</div>
      <div style="font-size:22px;font-weight:bold;color:#3fb950;">{len(high_conv)}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Top Sectors</div>
      <div style="font-size:14px;font-weight:bold;color:#58a6ff;">{top_sectors_str}</div>
    </div>
  </div>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Sector Momentum Scores</h2>
  <p style="color:#8b949e;font-size:12px;margin-bottom:10px;">
    Energy sector excluded per firm policy. Composite score: trend + RSI position +
    1M/3M returns + volatility (max 12). Green rows = assigned to Research Team.
  </p>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead>
      <tr style="background:#21262d;">
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">#</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">SECTOR</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">SCORE</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">TREND</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">RSI</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">1M RTN</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">3M RTN</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">VOL</th>
      </tr>
    </thead>
    <tbody>{sector_rows}</tbody>
  </table>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">
    Top Research Picks — Forwarded to Manager Agent</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead>
      <tr style="background:#21262d;">
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">TICKER</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">SECTOR</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">CONVICTION</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">HORIZON</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">TREND</th>
        <th style="padding:9px 8px;text-align:left;color:#8b949e;font-size:11px;">RSI</th>
      </tr>
    </thead>
    <tbody>{picks_rows}</tbody>
  </table>

  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;">
    <h3 style="color:#e6edf3;font-size:14px;margin:0 0 10px 0;">Investment Themes This Cycle</h3>
    <p style="color:#c9d1d9;margin:0 0 8px 0;">
      <strong style="color:#58a6ff;">Strongest momentum sectors:</strong> {top_sectors_str}
    </p>
    <p style="color:#c9d1d9;margin:0 0 8px 0;">
      <strong style="color:#3fb950;">High-conviction picks (4★+):</strong> {hc_str}
    </p>
    <p style="color:#8b949e;font-size:12px;margin:0;">
      All picks forwarded to Manager Agent for capital deployment evaluation alongside
      the Portfolio Deployment Officer's efficiency signal.
      Next research cycle in 4 days.
    </p>
  </div>

  {_build_earnings_html_section()}

  {_build_intl_html_section()}

  <p style="color:#484f58;font-size:11px;margin-top:28px;border-top:1px solid #21262d;
            padding-top:12px;">
    Apex Capital Intelligence — R&amp;D Team Leader &nbsp;·&nbsp;
    Apex Capital — Proprietary Trading &nbsp;·&nbsp; Generated {now}
  </p>
</body></html>"""
        return html

    # ------------------------------------------------------------------
    # Step 3 — Email Chairman directly
    # ------------------------------------------------------------------

    def email_chairman(self, text_briefing: str, html_briefing: str) -> bool:
        try:
            from email_sender import send_daily_report
            subject = (
                f"Apex Capital — R&D Team Briefing | "
                f"{datetime.now().strftime('%b %d, %Y')}"
            )
            from creative_agent import polish_email, metrics_for_rd_leader
            # Extract summary stats from saved report if available
            _rpt_sectors = 0; _rpt_picks = 0; _rpt_hc = 0; _rpt_top = "—"
            import os as _os, json as _json
            _rpt_path = "data/research_team_report.json"
            if _os.path.exists(_rpt_path):
                try:
                    with open(_rpt_path) as _f:
                        _rpt = _json.load(_f)
                    _s = _rpt.get("summary", {})
                    _rpt_sectors = _s.get("agents_run", 0)
                    _rpt_picks   = _s.get("total_picks", 0)
                    _rpt_hc      = _s.get("high_conviction", 0)
                    _recs = _rpt.get("recommendations", [])
                    if _recs:
                        _rpt_top = _recs[0].get("sector", "—")
                except Exception:
                    pass
            key_metrics = metrics_for_rd_leader(
                sectors_assigned=_rpt_sectors,
                total_picks=_rpt_picks,
                high_conviction=_rpt_hc,
                top_sector=_rpt_top,
            )
            polished_html = polish_email(html_briefing, subject, "rd_team_leader", key_metrics)
            success = send_daily_report(
                html_body=polished_html,
                subject=subject,
                paper_day=0,
                sender_name="rd_team_leader",
                key_metrics=key_metrics,
            )
            if success:
                logger.info("  [R&D Leader] Chairman briefing emailed ✓")
            else:
                logger.warning("  [R&D Leader] Email failed — briefing printed to console")
            return success
        except Exception as e:
            logger.warning(f"  [R&D Leader] Email error: {e}")
            return False

    # ------------------------------------------------------------------
    # Full cycle
    # ------------------------------------------------------------------

    def run(self, scan_only: bool = False) -> dict:
        logger.info("=" * 60)
        logger.info("  APEX CAPITAL — R&D TEAM LEADER")
        logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # Read HR coaching note at cycle start
        try:
            from performance_ledger import get_coaching_note
            _coaching = get_coaching_note("RDTeamLeader") or get_coaching_note("R&D Team Leader")
            if _coaching:
                logger.info(f"  [R&D Leader] HR coaching note: {_coaching[:100]}...")
        except Exception:
            pass

        # Phase 1: Scan sectors and assign
        assignments = self.scan_and_assign()

        if scan_only:
            return {"assignments": assignments, "briefing": None}

        # Phase 2: Run Research Team — all 8 agents investigate their assigned sectors
        logger.info("  [R&D Leader] Dispatching Research Team agents...")
        try:
            from research_team import ResearchTeamCoordinator
            coordinator  = ResearchTeamCoordinator()
            research_rpt = coordinator.run(assignments=assignments, quiet=False)
            s = research_rpt.get("summary", {})
            logger.info(
                f"  [R&D Leader] Research Team complete — "
                f"{s.get('agents_run', 0)} agents ran, "
                f"{s.get('total_picks', 0)} picks, "
                f"{s.get('high_conviction', 0)} high-conviction"
            )
        except Exception as e:
            logger.warning(f"  [R&D Leader] Research Team error: {e}")

        # Phase 2b: Earnings Prediction Agent
        logger.info("  [R&D Leader] Running Earnings Prediction Agent...")
        ep_results = []
        try:
            import earnings_predictor
            ep_results = earnings_predictor.run()
            logger.info(
                f"  [R&D Leader] Earnings Predictor complete — "
                f"{len(ep_results)} upcoming earnings predicted"
            )
        except Exception as e:
            logger.warning(f"  [R&D Leader] Earnings Predictor error: {e}")

        # Phase 2c: International Markets Intelligence Desk
        logger.info("  [R&D Leader] Running International Markets Intelligence (IMI) Desk...")
        intl_report = {}
        try:
            import international_analyst
            # Import STOCK_UNIVERSE here so a missing config doesn't abort the whole run;
            # STOCK_UNIVERSE is defined in config.py, not in this file.
            try:
                from config import STOCK_UNIVERSE as _stock_universe
            except Exception:
                _stock_universe = None
            intl_report = international_analyst.run(universe=_stock_universe) or {}
            logger.info(
                f"  [R&D Leader] IMI Desk complete — "
                f"sentiment: {intl_report.get('sentiment','?')} | "
                f"{len(intl_report.get('notable_indices', []))} notable index moves"
            )
        except Exception as e:
            logger.warning(f"  [R&D Leader] IMI Desk error: {e}")

        # Phase 3: Load research report (freshly written by ResearchTeamCoordinator above)
        research_report = {}
        if os.path.exists(RESEARCH_REPORT_PATH):
            try:
                with open(RESEARCH_REPORT_PATH) as f:
                    research_report = json.load(f)
            except Exception as e:
                logger.warning(f"  [R&D Leader] Could not load research report: {e}")

        # Phase 4: Compile briefing
        # Guard the load — if the write in scan_and_assign failed we'd crash here.
        try:
            with open(ASSIGNMENTS_PATH) as f:
                assignment_payload = json.load(f)
        except Exception as e:
            logger.warning(f"  [R&D Leader] Could not load assignments file: {e}")
            assignment_payload = {"assignments": [], "all_scored": [], "sectors_scanned": 0}

        text_brief = self.compile_briefing(research_report, assignment_payload)
        html_brief = self.compile_html_briefing(research_report, assignment_payload)

        print("\n" + text_brief)

        # Phase 5: Email Chairman
        self.email_chairman(text_brief, html_brief)

        # Phase 6: Broadcast to firm channel
        try:
            from firm_channel import post as _fc_post
            picks       = research_report.get("recommendations", [])
            high_conv   = [p for p in picks if p.get("conviction", 0) >= 4]
            top_sectors = [a.get("sector_name","?") for a in assignments[:3]]
            _fc_post(
                "R&D Team Leader", "SECTOR_OBS",
                f"Research cycle complete: {len(picks)} picks across {len(assignments)} sectors "
                f"({len(high_conv)} high-conviction 4★+). "
                f"Top sectors: {', '.join(top_sectors)}. "
                f"Top picks: {', '.join(p['ticker'] for p in high_conv[:5])}.",
                priority="HIGH" if high_conv else "NORMAL",
            )
            # Post individual sector momentum observations
            # Use a with-block instead of a bare open() to ensure the file handle is always closed.
            if os.path.exists(ASSIGNMENTS_PATH):
                try:
                    with open(ASSIGNMENTS_PATH) as _af:
                        asgn_payload = json.load(_af)
                except Exception:
                    asgn_payload = {}
            else:
                asgn_payload = {}
            for s in asgn_payload.get("all_scored", [])[:4]:
                sd = s.get("score_data", {})
                _fc_post(
                    "R&D Team Leader", "SECTOR_OBS",
                    # Use .get() so a malformed entry never crashes the firm-channel post loop.
                    f"{s.get('sector_name','?')}: score {sd.get('score','?')}/12 | "
                    f"trend={sd.get('trend','?')} | RSI={sd.get('rsi_14',0):.0f} | "
                    f"1M={sd.get('ret_1m_pct',0):+.1f}% | 3M={sd.get('ret_3m_pct',0):+.1f}%",
                )
            # Post high-confidence earnings predictions
            for ep in ep_results[:3]:
                if ep.get("confidence", 0) >= 4:
                    _fc_post(
                        "R&D Team Leader", "EARNINGS",
                        f"EARNINGS ALERT: {ep['ticker']} — predicted {ep['prediction']} "
                        f"(confidence {ep['confidence']}★) in {ep['days_until']}d. "
                        f"{ep['reasoning'][:80]}",
                        priority="HIGH",
                    )
            # Post IMI summary if significant
            if intl_report:
                _sent  = intl_report.get("sentiment", "NEUTRAL")
                _score = intl_report.get("sentiment_score", 0)
                _notable_idx = intl_report.get("notable_indices", [])
                _watch = intl_report.get("watch_tickers", [])
                _impl  = intl_report.get("us_implications", [])
                # Guard r['pct'] — yfinance can return None for an index; skip those entries
                # to avoid TypeError when formatting the f-string with a None value.
                _notable_str = ", ".join(
                    f"{r.get('name','?')} {r['pct']:+.1f}%"
                    for r in _notable_idx[:3]
                    if r.get("pct") is not None
                ) or "no major moves"
                _fc_post(
                    "International Analyst (IMI)", "INTL_MARKETS",
                    f"[IMI Desk cycle report] Global sentiment: {_sent} ({_score:+d}) | "
                    f"{_notable_str}. "
                    f"{'Watch: ' + ', '.join(_watch[:5]) + '. ' if _watch else ''}"
                    f"{'Key implication: ' + _impl[0] if _impl else ''}",
                    priority="HIGH" if _sent == "RISK_OFF" and _score <= -2 else "NORMAL",
                )
        except Exception as _fce:
            logger.debug(f"  [FirmChannel] R&D post failed: {_fce}")

        # ── Write ML feature snapshot (timestamped for historical training) ────
        # Each run appends one record to rd_ml_history.json.  Once we have
        # enough records, the ML model can use date-matched R&D signals as
        # training features instead of today's R&D state.
        try:
            import json as _json_ml
            _ml_snap_path = "data/rd_ml_history.json"
            _existing: list = []
            if os.path.exists(_ml_snap_path):
                try:
                    with open(_ml_snap_path) as _mf:
                        _existing = _json_ml.load(_mf)
                except Exception:
                    _existing = []

            # Extract precise numeric features from this run
            _picks = research_report.get("recommendations", [])
            _convictions = [p.get("conviction", 3) for p in _picks]
            _n_high_conv = sum(1 for c in _convictions if c >= 4)
            _avg_conv    = round(sum(_convictions) / max(len(_convictions), 1) / 5.0, 4) \
                           if _convictions else 0.5

            _scored = assignments  # already computed above
            _sector_scores = []
            for _s in _scored:
                _sd = _s.get("score_data", {})
                _sector_scores.append({
                    "sector":    _s.get("sector_name", "?"),
                    "score":     _sd.get("score", 0),
                    "trend":     _sd.get("trend", "NEUTRAL"),
                    "rsi":       round(float(_sd.get("rsi_14", 50)), 1),
                    "ret_1m":    round(float(_sd.get("ret_1m_pct", 0)), 2),
                    "ret_3m":    round(float(_sd.get("ret_3m_pct", 0)), 2),
                })
            _n_strong  = sum(1 for s in _sector_scores if s["score"] >= 6)
            _breadth   = round(_n_strong / max(len(_sector_scores), 1), 3)
            _regime    = 1 if _breadth > 0.6 else (-1 if _breadth < 0.3 else 0)

            _cro_risk_str = assignment_payload.get("cro_risk_level", "MEDIUM")
            _risk_map     = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3, "CRISIS": 3}

            _intl_sent   = 0.0
            _intl_raw    = 0
            if intl_report:
                _sent_map = {"RISK_ON": 1.0, "NEUTRAL": 0.0, "RISK_OFF": -1.0}
                _intl_sent = _sent_map.get(
                    intl_report.get("sentiment", "NEUTRAL").upper(), 0.0)
                _intl_raw  = int(intl_report.get("sentiment_score", 0))

            _snapshot = {
                "ts":                    datetime.now().isoformat(),
                "date":                  datetime.now().strftime("%Y-%m-%d"),
                # Market regime
                "rd_macro_regime":       _regime,
                "rd_sector_breadth":     _breadth,
                "rd_cro_risk_level":     _risk_map.get(_cro_risk_str, 1),
                "rd_intl_sentiment":     _intl_sent,
                "rd_intl_score_raw":     _intl_raw,
                "rd_n_high_conviction":  _n_high_conv,
                "rd_avg_conviction":     _avg_conv,
                # Detailed sector breakdown
                "sector_scores":         _sector_scores,
                # Top picks with tickers (for ticker-specific lookup)
                "top_picks": [
                    {"ticker": p.get("ticker"), "conviction": p.get("conviction", 0),
                     "action": p.get("action"), "momentum": p.get("momentum")}
                    for p in sorted(_picks, key=lambda x: x.get("conviction", 0), reverse=True)[:20]
                ],
            }

            # Keep last 365 snapshots (1 year of hourly R&D data → ~8,760 max)
            _existing.append(_snapshot)
            _existing = _existing[-8760:]
            with open(_ml_snap_path, "w") as _mf:
                _json_ml.dump(_existing, _mf, separators=(",", ":"))
            logger.info(f"  [R&D] ML snapshot written → {_ml_snap_path} "
                        f"({len(_existing)} records | regime: "
                        f"{'BULLISH' if _regime==1 else 'BEARISH' if _regime==-1 else 'NEUTRAL'} "
                        f"| breadth: {_breadth:.0%})")
        except Exception as _ml_e:
            logger.debug(f"  [R&D] ML snapshot write failed: {_ml_e}")

        return {
            "assignments":     assignments,
            "briefing":        text_brief,
            "research_report": research_report,
        }


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    # ── Single-instance guard ─────────────────────────────────────────────
    # macOS enforces kern.maxfilesperproc=10240 regardless of ulimit.
    # A stalled previous run (slow yfinance downloads) that overlaps with a
    # new cron invocation doubles the open-FD count and can exhaust the limit.
    # This lock ensures only one rd_team_leader process runs at a time.
    _LOCK_PATH = "data/.rd_team_leader.lock"
    os.makedirs("data", exist_ok=True)
    _lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning(
            "Another rd_team_leader instance is already running — "
            "skipping this invocation to avoid fd exhaustion."
        )
        _lock_fh.close()
        sys.exit(0)

    try:
        scan_only = "--scan-only" in sys.argv
        leader = RDTeamLeader()
        leader.run(scan_only=scan_only)
    finally:
        fcntl.flock(_lock_fh, fcntl.LOCK_UN)
        _lock_fh.close()
        try:
            os.unlink(_LOCK_PATH)
        except OSError:
            pass
