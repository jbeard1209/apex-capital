# =============================================================================
# APEX CAPITAL — CREATIVE AGENT
# =============================================================================
# The Creative Agent is a communications specialist and email middleware.
# Every outgoing email from every other agent passes through here before
# being sent. The Creative Agent ensures:
#
#   1. TLDR / Executive Summary at the very top
#      The most critical information (decision, risk level, P&L, grade) is
#      surfaced immediately — the Chairman should know the headline within
#      two seconds of opening the email.
#
#   2. Visual hierarchy
#      Most important → least important. Tables before prose. Numbers before
#      explanations. Alerts before routine updates.
#
#   3. Consistent Apex Capital brand identity
#      Dark theme (#0d1117 background), blue accent (#58a6ff), green/red
#      for positive/negative. All emails look like they come from the same firm.
#
#   4. Scannable layout
#      No walls of text. Bullet points over paragraphs. Bold key numbers.
#      Section headers clearly delineated.
#
#   5. Appropriate tone calibration
#      Risk alerts are urgent. Research briefings are analytical.
#      Daily reports are concise. Manager decisions are decisive.
#
# Usage:
#   from creative_agent import polish_email
#   html = polish_email(html_body, subject, sender_name, key_metrics)
#
# The polish_email() function is imported by all other agents' email paths.
# =============================================================================

from datetime import datetime
from typing import Optional


APEX_COLORS = {
    "bg":           "#0d1117",
    "surface":      "#161b22",
    "border":       "#30363d",
    "border_light": "#21262d",
    "text_primary": "#c9d1d9",
    "text_muted":   "#8b949e",
    "text_dim":     "#484f58",
    "accent_blue":  "#58a6ff",
    "accent_green": "#3fb950",
    "accent_red":   "#f85149",
    "accent_orange":"#f0883e",
    "accent_yellow":"#f0c000",
    "text_heading": "#e6edf3",
}

AGENT_DISPLAY_NAMES = {
    "chief_of_staff":   ("CHIEF OF STAFF",   "#e6edf3", "Second in Command · Strategy & Oversight"),
    "manager_agent":    ("MANAGER AGENT",    "#58a6ff", "Capital Allocation & Execution"),
    "review_agent":     ("REVIEW AGENT",     "#3fb950", "Portfolio Position Review"),
    "rd_team_leader":   ("R&D TEAM LEADER",  "#f0c000", "Sector Research & Assignments"),
    "risk_manager":          ("CHIEF RISK OFFICER", "#f85149", "Open Risk Advisor · Portfolio Risk Analytics"),
    "chief_risk_officer":    ("CHIEF RISK OFFICER", "#f85149", "Open Risk Advisor · Portfolio Risk Analytics"),
    "pdo":              ("PORTFOLIO DEPLOYMENT OFFICER", "#f0883e", "Capital Efficiency"),
    "head_agent":       ("HEAD AGENT",       "#58a6ff", "Thematic Trade Execution"),
    "daily_report":     ("DAILY REPORT",     "#58a6ff", "Full Pipeline Summary"),
    "review_team":      ("TRADE REVIEW TEAM","#d2a679", "Historical Performance Analysis"),
    "canary":           ("CANARY AGENT",     "#f0c000", "24/7 Market Stress Monitor"),
    "irm_review":       ("IRM — INTERNAL RISK MONITOR", "#9b59b6", "Chairman's Eyes Only · Independent Audit"),
    "irm_daily":        ("IRM DAILY CHECK",  "#9b59b6", "Chairman's Eyes Only · Independent Board Consultation"),
    "premarket_check":  ("PRE-MARKET CHECK", "#f0883e", "Morning Scan · Before Market Open"),
    "trade_review_team":("TRADE REVIEW TEAM","#d2a679", "Weekly Historical Performance Analysis"),
    "hr_team":          ("HR TEAM",          "#f0883e", "Agent Performance Review · HR Performance Officer · HR Development Officer"),
}

# ---------------------------------------------------------------------------
# Per-agent context paragraph — shown directly below the TLDR chips.
# Gives the Chairman a plain-English sentence explaining what this email IS
# and what action (if any) he needs to take.  Fixes the confusion caused when
# retrospective or analytical reports look like real-time crisis alerts.
# ---------------------------------------------------------------------------
AGENT_CONTEXT = {
    "chief_of_staff": (
        "This is the <strong>daily Chief of Staff brief</strong> — a strategic overview of firm "
        "health, outstanding risk flags, agent performance, and the Agent Factory's expansion "
        "activity. It synthesises all other agents' outputs into one document. "
        "<em>No immediate action required unless flagged below.</em>"
    ),
    "manager_agent": (
        "This is the <strong>Manager Agent's daily decision report</strong>. It covers every "
        "buy, sell, trim, and hold decision made today across all open positions and new "
        "recommendations. <strong>All trades listed here have already been executed.</strong>"
    ),
    "review_agent": (
        "This is the <strong>Review Agent's portfolio analysis</strong> — a systematic scan of "
        "every open position checked against momentum signals, stop-loss rules, take-profit "
        "levels, and concentration limits. It feeds directly into the Manager Agent's decisions."
    ),
    "rd_team_leader": (
        "This is the <strong>R&D Team's sector research briefing</strong>. The team scanned all "
        "non-energy sectors, selected the strongest momentum opportunities, and generated "
        "investment picks for the Manager Agent's review. Picks here are candidates, not "
        "confirmed trades — the Manager Agent evaluates and executes them separately."
    ),
    "risk_manager": (
        "This is the <strong>Chief Risk Officer's independent risk report</strong>. It covers "
        "portfolio Value-at-Risk, historical stress scenarios, position concentration warnings, "
        "and a private performance evaluation of management. This report <strong>bypasses all "
        "other agents</strong> and goes directly to you — no agent has seen it."
    ),
    "chief_risk_officer": (
        "This is the <strong>Chief Risk Officer's independent risk report</strong>. It covers "
        "portfolio Value-at-Risk, historical stress scenarios, position concentration warnings, "
        "and a private performance evaluation of management. This report <strong>bypasses all "
        "other agents</strong> and goes directly to you — no agent has seen it."
    ),
    "pdo": (
        "This is the <strong>Portfolio Deployment Officer's capital efficiency report</strong>. "
        "It assesses whether the current cash-to-equity split is optimal and issues a "
        "directional signal (DEPLOY_MORE / NEAR_OPTIMAL / REDUCE_EXPOSURE / HOLD_CASH) "
        "that the Manager Agent uses to calibrate buy thresholds."
    ),
    "daily_report": (
        "This is the <strong>full daily pipeline summary</strong> — a consolidated view of "
        "all agent activity, trades executed, and portfolio performance for today's session."
    ),
    "review_team": (
        "This is the <strong>Trade Review Team's weekly post-mortem</strong>. It analyses "
        "historical trade performance, win/loss rates, average P&amp;L, and benchmarks results "
        "against SPY. This is a backward-looking report — no trades are being executed."
    ),
    "trade_review_team": (
        "This is the <strong>Trade Review Team's weekly post-mortem</strong>. It analyses "
        "historical trade performance, win/loss rates, average P&amp;L, and benchmarks results "
        "against SPY. This is a backward-looking report — no trades are being executed."
    ),
    "canary": (
        "This is a <strong>Canary Agent market stress alert</strong>. The Canary monitors "
        "markets 24/7 and has detected stress signals that warrant defensive posture. "
        "<strong>All new buy orders are currently blocked</strong> while this alert is active. "
        "No money has been lost — this is a protective pause, not a crisis."
    ),
    "irm_review": (
        "⚠️ <strong>Important context before reading this email:</strong> This is an "
        "<strong>IRM second-opinion review</strong> of a <em>previous</em> email listed in "
        "the subject line above. The Internal Risk Monitor independently audits every report "
        "sent to you <em>after the fact</em> — it is a retrospective analysis, "
        "<strong>not a real-time alert</strong>. No money has been lost and no trades have "
        "been executed as a result of this review. The original sending agent is unaware "
        "this review exists. Red flags listed below are concerns in the report, not "
        "events that have already occurred."
    ),
    "irm_daily": (
        "This is the <strong>IRM Daily Board Check</strong> — an independent consultation of "
        "the three-specialist IRM Board (Quantitative Risk, Behavioral Finance, Macro). "
        "This runs every day regardless of whether other emails were sent. It is a "
        "risk-overlay snapshot, not a trade execution report."
    ),
    "premarket_check": (
        "This is the <strong>Pre-Market Check</strong>, run at ~8:30 AM before market open. "
        "It scans overnight price moves for every open position and flags anything approaching "
        "a stop-loss level. <strong>No trades have been executed yet</strong> — this is an "
        "early-warning scan to give you time to act before the open if needed."
    ),
    "hr_team": (
        "This is the <strong>HR Team's agent performance review</strong>. Two officers run "
        "simultaneously: the HR Performance Officer (quantitative — picks, approval rates, "
        "execution accuracy) and the HR Development Officer (qualitative — trajectory, "
        "thesis sharpness, cultural alignment). Each cross-audits the other for bias. "
        "Emails are sent only when agents require attention — clean cycles produce no email."
    ),
}


# =============================================================================
# TLDR BUILDER
# =============================================================================

def _build_tldr(subject: str, sender_name: str, key_metrics: dict) -> str:
    """
    Build a visually prominent TLDR block that sits at the very top of the email.
    key_metrics: dict of label → value pairs to feature in the summary.
    """
    agent_info  = AGENT_DISPLAY_NAMES.get(
        sender_name.lower().replace(" ", "_"),
        (sender_name.upper(), "#58a6ff", ""),
    )
    agent_label, agent_color, agent_subtitle = agent_info

    now = datetime.now().strftime("%B %d, %Y  %H:%M")

    # Build metric chips
    chips_html = ""
    for label, value in (key_metrics or {}).items():
        # Choose chip color based on value semantics
        if isinstance(value, str):
            chip_color = (
                APEX_COLORS["accent_green"]  if any(w in value.upper() for w in
                    ("OPTIMAL", "BULL", "LOW RISK", "GRADE A", "APPROVED", "PROFIT", "✓")) else
                APEX_COLORS["accent_red"]    if any(w in value.upper() for w in
                    ("CRITICAL", "BEAR", "HIGH RISK", "GRADE F", "REJECTED", "LOSS", "✗")) else
                APEX_COLORS["accent_orange"] if any(w in value.upper() for w in
                    ("DEPLOY", "ELEVATED", "MODIFIED", "WATCH", "GRADE C", "GRADE D")) else
                APEX_COLORS["accent_blue"]
            )
        elif isinstance(value, (int, float)):
            chip_color = (
                APEX_COLORS["accent_green"] if value > 0 else
                APEX_COLORS["accent_red"]   if value < 0 else
                APEX_COLORS["accent_blue"]
            )
        else:
            chip_color = APEX_COLORS["accent_blue"]

        chips_html += f"""
          <div style="background:{APEX_COLORS['bg']};border:1px solid {chip_color};
                      border-radius:6px;padding:10px 14px;min-width:100px;">
            <div style="color:{APEX_COLORS['text_muted']};font-size:10px;
                        text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">
              {label}</div>
            <div style="color:{chip_color};font-size:18px;font-weight:bold;">
              {value}</div>
          </div>"""

    # Urgency tag
    is_urgent = any(w in subject.upper() for w in
        ["RISK", "CRITICAL", "ALERT", "STOP", "URGENT", "FLAG"])
    urgency_badge = (
        f'<span style="background:#f85149;color:white;font-size:10px;font-weight:bold;'
        f'padding:3px 8px;border-radius:4px;margin-left:10px;">URGENT</span>'
        if is_urgent else ""
    )

    # Context paragraph — plain-English explanation of what this email is.
    # Solves the problem of the Chairman misreading a retrospective IRM audit
    # as a real-time crisis alert.
    _sender_key  = sender_name.lower().replace(" ", "_")
    context_text = AGENT_CONTEXT.get(_sender_key, "")
    context_block = ""
    if context_text:
        # IRM review gets a warning-style block; others get a neutral info block
        is_irm = "irm" in _sender_key
        ctx_bg      = "#1a1520" if is_irm else APEX_COLORS["surface"]
        ctx_border  = "#9b59b6" if is_irm else APEX_COLORS["border_light"]
        ctx_icon    = "⚠️ " if is_irm else "ℹ️ "
        context_block = f"""
    <!-- Context paragraph — what is this email and what does it mean? -->
    <div style="background:{ctx_bg};border-left:3px solid {ctx_border};
                border-radius:0 6px 6px 0;padding:10px 14px;margin-bottom:16px;">
      <p style="color:{APEX_COLORS['text_primary']};font-size:13px;margin:0;line-height:1.6;">
        {ctx_icon}{context_text}
      </p>
    </div>"""

    return f"""
  <!-- TLDR BLOCK — always first thing the Chairman sees -->
  <div style="background:linear-gradient(135deg,{APEX_COLORS['surface']} 0%,#1a2332 100%);
              border:1px solid {agent_color};border-radius:10px;
              padding:20px 24px;margin-bottom:28px;">

    <!-- Agent + timestamp header -->
    <div style="display:flex;justify-content:space-between;align-items:center;
                margin-bottom:16px;flex-wrap:wrap;gap:8px;">
      <div>
        <span style="background:{agent_color};color:{APEX_COLORS['bg']};font-size:11px;
                     font-weight:bold;padding:4px 10px;border-radius:4px;
                     text-transform:uppercase;letter-spacing:1px;">
          {agent_label}</span>{urgency_badge}
        {f'<div style="color:{APEX_COLORS["text_muted"]};font-size:12px;margin-top:4px;">{agent_subtitle}</div>' if agent_subtitle else ''}
      </div>
      <div style="color:{APEX_COLORS['text_dim']};font-size:12px;text-align:right;">
        {now}<br>
        <span style="font-size:11px;">To: Jack Beard (Chairman)</span>
      </div>
    </div>

    <!-- Subject line -->
    <p style="color:{APEX_COLORS['text_heading']};font-size:17px;font-weight:bold;
              margin:0 0 16px 0;border-bottom:1px solid {APEX_COLORS['border_light']};
              padding-bottom:12px;">
      {subject}
    </p>

    {context_block}

    <!-- Key metrics chips -->
    {f'<div style="display:flex;gap:10px;flex-wrap:wrap;">{chips_html}</div>' if chips_html else ''}

  </div>"""


# =============================================================================
# CORE POLISH FUNCTION
# =============================================================================

def polish_email(
    html_body:    str,
    subject:      str,
    sender_name:  str  = "apex_capital",
    key_metrics:  Optional[dict] = None,
    priority:     str  = "normal",    # "urgent" | "normal" | "routine"
) -> str:
    """
    Process an outgoing email HTML body through the Creative Agent.

    1. Injects a branded TLDR header at the top
    2. Wraps content in consistent Apex Capital styling
    3. Adds a standard footer
    4. Ensures visual hierarchy

    Returns the polished HTML string.
    """

    tldr_block = _build_tldr(subject, sender_name, key_metrics or {})

    # Priority banner
    priority_banner = ""
    if priority == "urgent":
        priority_banner = f"""
  <div style="background:#f85149;color:white;padding:10px 20px;margin-bottom:16px;
              border-radius:6px;font-weight:bold;font-size:13px;text-align:center;">
    ⚑ URGENT COMMUNICATION — Requires Chairman Attention
  </div>"""

    # Footer
    now = datetime.now().strftime("%B %d, %Y at %H:%M")
    footer = f"""
  <div style="margin-top:36px;padding-top:16px;border-top:1px solid {APEX_COLORS['border_light']};
              display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
    <div>
      <span style="color:{APEX_COLORS['text_dim']};font-size:11px;">
        Apex Capital Intelligence &nbsp;·&nbsp;
        Paper trading simulation — not real investment advice &nbsp;·&nbsp;
        Generated {now}
      </span>
    </div>
    <div style="color:{APEX_COLORS['text_dim']};font-size:11px;text-align:right;">
      Chairman: Jack Beard &nbsp;·&nbsp; President: Claude AI
    </div>
  </div>"""

    # If the html_body already has a full HTML structure, inject inside <body>
    if "<body" in html_body:
        # Find where the existing body content starts (after <body ...>)
        body_start = html_body.find(">", html_body.find("<body")) + 1
        body_end   = html_body.rfind("</body>")

        inner_content = html_body[body_start:body_end]

        # Remove the existing footer if it references paper trading (we'll add ours)
        # This prevents duplicate footers
        if "not real investment advice" in inner_content:
            # Find and remove last <p> containing the standard footer text
            last_footer_start = inner_content.rfind(
                '<p style="color:#484f58')
            if last_footer_start != -1:
                last_footer_end = inner_content.find("</p>", last_footer_start) + 4
                inner_content   = inner_content[:last_footer_start] + inner_content[last_footer_end:]

        polished_inner = f"{priority_banner}{tldr_block}{inner_content}{footer}"

        return (
            html_body[:body_start] +
            polished_inner +
            html_body[body_end:]
        )

    else:
        # Plain content — wrap in full Apex Capital template
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ margin: 0; padding: 0; }}
  table {{ border-collapse: collapse; }}
  @media (max-width: 600px) {{
    .metrics-row {{ flex-direction: column !important; }}
  }}
</style>
</head>
<body style="background:{APEX_COLORS['bg']};color:{APEX_COLORS['text_primary']};
             font-family:'Segoe UI',Arial,sans-serif;padding:24px;
             max-width:980px;margin:0 auto;">
  {priority_banner}
  {tldr_block}
  <div style="background:{APEX_COLORS['surface']};border:1px solid {APEX_COLORS['border']};
              border-radius:8px;padding:20px 24px;margin-bottom:24px;">
    {html_body}
  </div>
  {footer}
</body></html>"""


# =============================================================================
# METRIC EXTRACTORS — helpers for agents to build key_metrics dicts
# =============================================================================

def metrics_for_manager(decisions: list, executions: list,
                          account: dict, pdo_signal: str = "") -> dict:
    approved  = sum(1 for d in decisions
                    if d.get("manager_decision") == "APPROVED"
                    and d.get("manager_action") not in ("HOLD", "WATCH"))
    rejected  = sum(1 for d in decisions if d.get("manager_decision") == "REJECTED")
    pnl       = account.get("pnl_today", 0)
    pnl_pct   = account.get("pnl_today_pct", 0)
    pnl_str   = f"{'+' if pnl >= 0 else ''}${abs(pnl):,.2f} ({pnl_pct:+.2f}%)"
    return {
        "Trades Executed": approved,
        "Trades Rejected": rejected,
        "Today's P&L":     pnl_str,
        "Portfolio Value": f"${account.get('portfolio_value', 0):,.2f}",
        "PDO Signal":      pdo_signal or "—",
    }


def metrics_for_risk_manager(risk_level: str, manager_grade: str,
                               total_return_pct: float, var_1d: float) -> dict:
    return {
        "Risk Level":      risk_level,
        "Manager Grade":   manager_grade,
        "Return vs Start": f"{total_return_pct:+.2f}%",
        "VaR 95% 1-Day":   f"${var_1d:,.0f}",
    }


def metrics_for_rd_leader(sectors_assigned: int, total_picks: int,
                           high_conviction: int, top_sector: str) -> dict:
    return {
        "Sectors Assigned":   sectors_assigned,
        "Total Picks":        total_picks,
        "High Conviction 4★+": high_conviction,
        "Top Sector":         top_sector,
    }


def metrics_for_pdo(recommendation: str, deployed_pct: float,
                     target_pct: float, regime: str) -> dict:
    return {
        "PDO Signal":    recommendation,
        "Deployed":      f"{deployed_pct:.1f}%",
        "Target":        f"{target_pct:.1f}%",
        "Market Regime": regime.upper(),
    }


def metrics_for_daily_report(portfolio_value: float, pnl_today: float,
                               pnl_pct: float, n_positions: int) -> dict:
    return {
        "Portfolio Value":  f"${portfolio_value:,.2f}",
        "Today's P&L":      f"{'+' if pnl_today >= 0 else ''}${abs(pnl_today):,.2f}",
        "P&L %":            f"{pnl_pct:+.2f}%",
        "Open Positions":   n_positions,
    }
