# =============================================================================
# REPORT GENERATOR — Daily / Monthly / Yearly Charts & HTML Report
# =============================================================================
# Produces a rich HTML email body with embedded charts showing:
# - Daily portfolio performance vs SPY
# - Monthly returns heatmap
# - Yearly summary bar chart
# - Open positions table
# - Trade log summary
# =============================================================================

import json
import os
import io
import base64
import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")   # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.colors import TwoSlopeNorm

logger = logging.getLogger(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
CHARTS_DIR  = os.path.join(os.path.dirname(__file__), "charts")
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(CHARTS_DIR,  exist_ok=True)


# ---------------------------------------------------------------------------
# Chart Helpers
# ---------------------------------------------------------------------------

DARK_BG   = "#0d1117"
CARD_BG   = "#161b22"
GREEN     = "#26a641"
RED       = "#f85149"
BLUE      = "#58a6ff"
GOLD      = "#e3b341"
TEXT_COL  = "#c9d1d9"
GRID_COL  = "#21262d"

plt.style.use("dark_background")

def _fig_to_base64(fig) -> str:
    """Convert a matplotlib figure to a base64-encoded PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=140, facecolor=fig.get_facecolor())
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return encoded


def chart_cumulative_returns(daily_df: pd.DataFrame, title: str = "Portfolio vs SPY") -> str:
    """Line chart: cumulative portfolio return vs SPY."""
    fig, ax = plt.subplots(figsize=(11, 4.5), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    if "portfolio_return_cumulative" in daily_df.columns:
        ax.plot(daily_df.index, daily_df["portfolio_return_cumulative"],
                color=BLUE, linewidth=2, label="Our Strategy", zorder=3)

    if "spy_return_cumulative" in daily_df.columns:
        spy_col = daily_df["spy_return_cumulative"].dropna()
        if not spy_col.empty:
            ax.plot(spy_col.index, spy_col.values,
                    color=GOLD, linewidth=1.5, linestyle="--", label="S&P 500 (SPY)", alpha=0.85, zorder=2)

    ax.axhline(0, color=GRID_COL, linewidth=1)
    ax.fill_between(daily_df.index,
                    daily_df["portfolio_return_cumulative"].fillna(0),
                    0, alpha=0.10, color=BLUE)

    ax.set_title(title, color=TEXT_COL, fontsize=13, pad=12)
    ax.set_ylabel("Cumulative Return (%)", color=TEXT_COL, fontsize=10)
    ax.tick_params(colors=TEXT_COL)
    ax.spines[:].set_color(GRID_COL)
    ax.yaxis.label.set_color(TEXT_COL)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.xticks(rotation=30, ha="right")
    ax.legend(facecolor=CARD_BG, edgecolor=GRID_COL, labelcolor=TEXT_COL, fontsize=9)
    ax.grid(True, color=GRID_COL, linewidth=0.5, alpha=0.6)
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_monthly_heatmap(monthly_df: pd.DataFrame) -> str:
    """Returns heatmap of monthly returns (green/red)."""
    if monthly_df.empty or "monthly_return_pct" not in monthly_df.columns:
        return ""

    # Pivot: rows = year, cols = month
    df = monthly_df.copy()
    df.index = pd.to_datetime(df.index)
    df["year"]  = df.index.year
    df["month"] = df.index.month

    pivot = df.pivot(index="year", columns="month", values="monthly_return_pct")
    pivot.columns = [datetime(2000, m, 1).strftime("%b") for m in pivot.columns]

    fig, ax = plt.subplots(
        figsize=(max(10, len(pivot.columns) * 0.85), max(3, len(pivot.index) * 0.7)),
        facecolor=DARK_BG
    )
    ax.set_facecolor(DARK_BG)

    _valid = pivot.values[~np.isnan(pivot.values)]
    vmax = max(float(abs(_valid).max()) if len(_valid) > 0 else 1.0, 1)
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
    cmap = plt.cm.RdYlGn

    im = ax.imshow(pivot.values, cmap=cmap, norm=norm, aspect="auto")

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, color=TEXT_COL, fontsize=9)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, color=TEXT_COL, fontsize=9)

    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if not np.isnan(val):
                color = "white" if abs(val) > vmax * 0.5 else TEXT_COL
                ax.text(j, i, f"{val:+.1f}%", ha="center", va="center",
                        fontsize=7.5, color=color, fontweight="bold")

    ax.set_title("Monthly Returns Heatmap", color=TEXT_COL, fontsize=12, pad=10)
    cbar = plt.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.ax.yaxis.set_tick_params(color=TEXT_COL)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color=TEXT_COL, fontsize=8)
    ax.spines[:].set_color(GRID_COL)
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_yearly_bar(yearly_df: pd.DataFrame) -> str:
    """Bar chart of annual returns per year."""
    if yearly_df.empty or "annual_return_pct" not in yearly_df.columns:
        return ""

    df = yearly_df.copy()
    df.index = pd.to_datetime(df.index).year.astype(str)
    returns  = df["annual_return_pct"]

    fig, ax = plt.subplots(figsize=(7, 4), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    colors = [GREEN if v >= 0 else RED for v in returns]
    bars   = ax.bar(returns.index, returns.values, color=colors, width=0.55, zorder=3)

    for bar, val in zip(bars, returns.values):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + (0.5 if val >= 0 else -1.5),
                f"{val:+.1f}%", ha="center", va="bottom" if val >= 0 else "top",
                color=TEXT_COL, fontsize=9, fontweight="bold")

    ax.axhline(0, color=GRID_COL, linewidth=1)
    ax.set_title("Annual Returns by Year", color=TEXT_COL, fontsize=12, pad=10)
    ax.set_ylabel("Annual Return (%)", color=TEXT_COL, fontsize=10)
    ax.tick_params(colors=TEXT_COL)
    ax.spines[:].set_color(GRID_COL)
    ax.grid(True, axis="y", color=GRID_COL, linewidth=0.5, alpha=0.6, zorder=0)
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_drawdown(daily_df: pd.DataFrame) -> str:
    """Drawdown chart."""
    fig, ax = plt.subplots(figsize=(11, 3), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    rolling_max = daily_df["portfolio_value"].cummax()
    drawdown    = (daily_df["portfolio_value"] - rolling_max) / rolling_max * 100

    ax.fill_between(drawdown.index, drawdown.values, 0, color=RED, alpha=0.4)
    ax.plot(drawdown.index, drawdown.values, color=RED, linewidth=1)
    ax.axhline(0, color=GRID_COL, linewidth=1)

    ax.set_title("Drawdown", color=TEXT_COL, fontsize=12, pad=10)
    ax.set_ylabel("Drawdown (%)", color=TEXT_COL, fontsize=9)
    ax.tick_params(colors=TEXT_COL)
    ax.spines[:].set_color(GRID_COL)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.xticks(rotation=30, ha="right")
    ax.grid(True, color=GRID_COL, linewidth=0.5, alpha=0.6)
    fig.tight_layout()
    return _fig_to_base64(fig)


# ---------------------------------------------------------------------------
# HTML Report Builder
# ---------------------------------------------------------------------------

def build_html_report(
    snapshot: dict,
    backtest_results: Optional[dict] = None,
    paper_day: int = 1,
    report_type: str = "daily",  # "daily" | "backtest"
) -> str:
    """
    Build a full HTML email report with embedded charts and tables.
    """
    today      = datetime.now().strftime("%B %d, %Y")
    pnl_today  = snapshot.get("pnl_today", 0)
    pnl_color  = GREEN if pnl_today >= 0 else RED
    pnl_sign   = "+" if pnl_today >= 0 else ""

    port_val   = snapshot.get("portfolio_value", 500)
    ret_start  = snapshot.get("return_vs_start", 0)
    ret_color  = GREEN if ret_start >= 0 else RED

    positions_html = ""
    positions = snapshot.get("positions", [])
    if positions:
        rows = ""
        for p in positions:
            pct   = p.get("unrealized_pct", 0)
            color = GREEN if pct >= 0 else RED
            rows += f"""
            <tr>
                <td><b>{p['ticker']}</b></td>
                <td>{p['qty']:.0f}</td>
                <td>${p['avg_entry']:.2f}</td>
                <td>${p['current_price']:.2f}</td>
                <td>${p['market_value']:.2f}</td>
                <td style="color:{color}"><b>{pct:+.1f}%</b></td>
            </tr>"""
        positions_html = f"""
        <h3 style="color:{TEXT_COL};margin-top:28px;">📋 Open Positions</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px;color:{TEXT_COL};">
            <tr style="background:{CARD_BG};color:{GOLD};">
                <th style="padding:8px;text-align:left;">Ticker</th>
                <th>Shares</th>
                <th>Entry</th>
                <th>Current</th>
                <th>Value</th>
                <th>P&L %</th>
            </tr>
            {rows}
        </table>"""

    # --- Backtest charts ---
    bt_section = ""
    if backtest_results:
        daily_df   = backtest_results.get("daily_df", pd.DataFrame())
        monthly_df = backtest_results.get("monthly_df", pd.DataFrame())
        yearly_df  = backtest_results.get("yearly_df", pd.DataFrame())

        chart_cum  = chart_cumulative_returns(daily_df, "5-Year Cumulative Return: Strategy vs SPY (2020–2024)")
        chart_dd   = chart_drawdown(daily_df)
        chart_mo   = chart_monthly_heatmap(monthly_df)
        chart_yr   = chart_yearly_bar(yearly_df)

        cagr     = backtest_results.get("cagr", 0)
        alpha    = backtest_results.get("alpha", 0)
        sharpe   = backtest_results.get("sharpe_ratio", 0)
        maxdd    = backtest_results.get("max_drawdown_pct", 0)
        winrate  = backtest_results.get("win_rate", 0)
        n_trades = backtest_results.get("n_trades", 0)
        spy_cagr = backtest_results.get("spy_cagr", 0)
        final_v  = backtest_results.get("final_value", 500)
        spy_tot  = backtest_results.get("spy_total_return", 0)
        tot_ret  = backtest_results.get("total_return_pct", 0)

        def _img(b64):
            if not b64:
                return ""
            return f'<img src="data:image/png;base64,{b64}" style="width:100%;border-radius:8px;margin:8px 0;">'

        bt_section = f"""
        <hr style="border-color:{GRID_COL};margin:32px 0;">
        <h2 style="color:{GOLD};">📊 5-Year Backtest Results (2020–2024)</h2>

        <div style="display:flex;gap:12px;flex-wrap:wrap;margin:16px 0;">
            {_metric_card("Starting Capital",  f"${500:,.2f}",         BLUE)}
            {_metric_card("Final Value",       f"${final_v:,.2f}",     BLUE)}
            {_metric_card("Total Return",      f"{tot_ret:+.1f}%",     GREEN if tot_ret>=0 else RED)}
            {_metric_card("CAGR",              f"{cagr:+.1f}%",        GREEN if cagr>=0 else RED)}
            {_metric_card("vs SPY CAGR",       f"{spy_cagr:+.1f}%",   GOLD)}
            {_metric_card("Alpha",             f"{alpha:+.1f}%",       GREEN if alpha>=0 else RED)}
            {_metric_card("Sharpe Ratio",      f"{sharpe:.2f}",        BLUE)}
            {_metric_card("Max Drawdown",      f"{maxdd:.1f}%",        RED)}
            {_metric_card("Win Rate",          f"{winrate:.1f}%",      GREEN if winrate>=50 else RED)}
            {_metric_card("Total Trades",      f"{n_trades}",          BLUE)}
        </div>

        <h3 style="color:{TEXT_COL};margin-top:24px;">📈 Cumulative Return vs S&P 500</h3>
        {_img(chart_cum)}

        <h3 style="color:{TEXT_COL};margin-top:24px;">📉 Drawdown</h3>
        {_img(chart_dd)}

        <h3 style="color:{TEXT_COL};margin-top:24px;">📅 Annual Returns</h3>
        {_img(chart_yr)}

        <h3 style="color:{TEXT_COL};margin-top:24px;">🗓️ Monthly Returns Heatmap</h3>
        {_img(chart_mo)}

        {_yearly_table(yearly_df, spy_cagr)}
        {_monthly_table(monthly_df)}
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <style>
        body {{ background-color:{DARK_BG}; color:{TEXT_COL}; font-family:'Segoe UI',Arial,sans-serif;
                margin:0; padding:20px; }}
        h1, h2, h3 {{ color:{TEXT_COL}; }}
        table tr:nth-child(even) {{ background:{CARD_BG}; }}
        table td, table th {{ padding:8px 12px; text-align:right; border-bottom:1px solid {GRID_COL}; }}
        table th {{ color:{GOLD}; text-align:left; }}
        table td:first-child {{ text-align:left; }}
    </style>
    </head>
    <body style="max-width:860px;margin:auto;">

    <!-- HEADER -->
    <div style="background:{CARD_BG};border-radius:12px;padding:24px 28px;margin-bottom:24px;
                border-left:4px solid {BLUE};">
        <h1 style="margin:0 0 4px 0;color:{BLUE};">🏦 Institutional Tracker</h1>
        <p style="margin:0;color:#8b949e;font-size:13px;">
            Trading Report · Day {paper_day} · {today}
        </p>
    </div>

    <!-- TODAY'S METRICS -->
    <h2 style="color:{TEXT_COL};">📊 Today's Trading Performance</h2>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin:16px 0;">
        {_metric_card("Portfolio Value",  f"${port_val:,.2f}",                       BLUE)}
        {_metric_card("Today's P&L",      f"{pnl_sign}${abs(pnl_today):,.2f}",       pnl_color)}
        {_metric_card("Today's Return",   f"{pnl_sign}{abs(snapshot.get('pnl_today_pct',0)):.2f}%", pnl_color)}
        {_metric_card("Total Return",     f"{ret_start:+.2f}%",                       ret_color)}
        {_metric_card("Cash Available",   f"${snapshot.get('cash',0):,.2f}",          GOLD)}
        {_metric_card("Open Positions",   f"{snapshot.get('n_positions',0)}",         BLUE)}
    </div>

    {positions_html}

    {_build_notable_changes_section()}

    {_build_thematic_section()}

    {bt_section}

    <!-- FOOTER -->
    <hr style="border-color:{GRID_COL};margin:32px 0 16px 0;">
    <p style="color:#8b949e;font-size:11px;text-align:center;">
        Institutional Tracker · Apex Capital — Proprietary Trading · Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")}
    </p>
    </body>
    </html>
    """
    return html


def _metric_card(label: str, value: str, color: str) -> str:
    return f"""
    <div style="background:{CARD_BG};border-radius:10px;padding:16px 20px;
                flex:1;min-width:120px;border-top:3px solid {color};">
        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:0.5px;margin-bottom:6px;">{label}</div>
        <div style="font-size:20px;font-weight:700;color:{color};">{value}</div>
    </div>"""


def _yearly_table(yearly_df: pd.DataFrame, spy_cagr: float) -> str:
    if yearly_df.empty:
        return ""
    rows = ""
    for idx, row in yearly_df.iterrows():
        yr  = pd.to_datetime(idx).year
        ret = row.get("annual_return_pct", 0)
        val = row.get("end_value", 0)
        clr = GREEN if ret >= 0 else RED
        rows += f"""
        <tr>
            <td><b>{yr}</b></td>
            <td>${val:,.2f}</td>
            <td style="color:{clr}"><b>{ret:+.1f}%</b></td>
        </tr>"""

    return f"""
    <h3 style="color:{TEXT_COL};margin-top:24px;">📆 Yearly Summary</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;color:{TEXT_COL};">
        <tr style="background:{CARD_BG};color:{GOLD};">
            <th>Year</th><th>End Value</th><th>Annual Return</th>
        </tr>{rows}
    </table>"""


def _monthly_table(monthly_df: pd.DataFrame) -> str:
    if monthly_df.empty:
        return ""
    rows = ""
    for idx, row in monthly_df.iterrows():
        dt  = pd.to_datetime(idx)
        ret = row.get("monthly_return_pct", 0)
        val = row.get("portfolio_value", 0)
        clr = GREEN if ret >= 0 else RED
        rows += f"""
        <tr>
            <td>{dt.strftime("%B %Y")}</td>
            <td>${val:,.2f}</td>
            <td style="color:{clr}"><b>{ret:+.1f}%</b></td>
        </tr>"""

    return f"""
    <h3 style="color:{TEXT_COL};margin-top:24px;">📅 Monthly Returns</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;color:{TEXT_COL};">
        <tr style="background:{CARD_BG};color:{GOLD};">
            <th>Month</th><th>Portfolio Value</th><th>Monthly Return</th>
        </tr>{rows}
    </table>"""


THEMATIC_LOG = os.path.join(os.path.dirname(__file__), "data", "thematic_positions.json")


def _build_notable_changes_section() -> str:
    """
    Read data/notable_changes.json and render a 'Notable Changes' section
    for board-approved ThinkTank proposals. Returns empty string if none exist.
    """
    notable_path = os.path.join("data", "notable_changes.json")
    if not os.path.exists(notable_path):
        return ""
    try:
        with open(notable_path) as f:
            changes = json.load(f)
    except Exception:
        return ""
    if not changes:
        return ""

    rows = ""
    for c in changes:
        approved_at = c.get("approved_at", "")[:10]
        status = c.get("status", "approved")
        sev = c.get("severity", "MEDIUM")
        sev_color = {"HIGH": "#f85149", "MEDIUM": "#f0883e", "LOW": "#58a6ff"}.get(sev, "#8b949e")
        rows += f"""
        <div style="border-left:3px solid {sev_color};padding:12px 16px;margin-bottom:14px;
                    background:#161b22;border-radius:0 8px 8px 0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <strong style="color:#e6edf3;font-size:14px;">{c.get('title','')}</strong>
            <span style="font-size:11px;color:#8b949e;white-space:nowrap;margin-left:12px;">
              Approved {approved_at} · Board unanimous
            </span>
          </div>
          <p style="margin:0 0 6px 0;color:#8b949e;font-size:13px;">{c.get('description','')[:200]}{"..." if len(c.get("description","")) > 200 else ""}</p>
          <div style="font-size:12px;color:#3fb950;">
            <strong>Implementation path:</strong> {c.get('proposed_fix','')[:160]}{"..." if len(c.get("proposed_fix","")) > 160 else ""}
          </div>
          <div style="margin-top:6px;font-size:11px;color:#8b949e;">
            Status: <span style="color:{sev_color};">{status}</span>
          </div>
        </div>"""

    return f"""
    <div style="background:#0d1117;border-radius:12px;padding:24px 28px;margin-bottom:24px;
                border-left:4px solid #3fb950;">
      <h2 style="color:#3fb950;margin:0 0 16px 0;">✅ Notable Changes — Board Approved</h2>
      <p style="color:#8b949e;font-size:13px;margin:0 0 16px 0;">
        The following proposals were unanimously approved by the board and are queued for implementation.
      </p>
      {rows}
    </div>"""


def _build_thematic_section() -> str:
    """
    Read thematic_positions.json and render an HTML section showing the
    $10,000 Research Intelligence allocation with live P&L.
    Returns an empty string if no positions are logged yet.
    """
    if not os.path.exists(THEMATIC_LOG):
        return ""

    try:
        with open(THEMATIC_LOG) as f:
            positions = json.load(f)
    except Exception:
        return ""

    if not positions:
        return ""

    # Try to get current prices via yfinance (best-effort, no crash if unavailable)
    current_prices = {}
    try:
        import yfinance as yf
        tickers = [p["ticker"] for p in positions if p.get("status") in ("submitted", "dry_run")]
        if tickers:
            raw = yf.download(tickers, period="2d", progress=False, auto_adjust=True)
            if isinstance(raw.columns, pd.MultiIndex):
                close = raw["Close"]
            else:
                close = raw[["Close"]] if "Close" in raw.columns else raw
            for t in tickers:
                try:
                    col = close[t] if t in close.columns else close.iloc[:, 0]
                    current_prices[t] = float(col.dropna().iloc[-1])
                except Exception:
                    pass
    except Exception:
        pass

    star_map = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆"}

    total_cost    = sum(p.get("actual_cost") or 0 for p in positions if p.get("status") in ("submitted", "dry_run"))
    total_current = 0.0
    rows = ""

    for p in positions:
        ticker    = p["ticker"]
        status    = p.get("status", "")
        entry_px  = p.get("price") or 0
        shares    = p.get("shares") or 0
        cost      = p.get("actual_cost") or 0
        conviction = p.get("conviction", 4)
        theme     = p.get("theme", "")
        stars     = star_map.get(conviction, "★★★★")

        cur_px  = current_prices.get(ticker)
        cur_val = (cur_px * shares) if cur_px and shares else cost
        pnl_d   = cur_val - cost if cost else 0
        pnl_pct = (pnl_d / cost * 100) if cost else 0
        total_current += cur_val

        pnl_color  = GREEN if pnl_d >= 0 else RED
        pnl_sign   = "+" if pnl_d >= 0 else ""
        price_str  = f"${entry_px:.2f}" if entry_px else "—"
        cur_str    = f"${cur_px:.2f}"   if cur_px  else "pending"
        val_str    = f"${cur_val:,.2f}" if cost     else "—"
        pnl_str    = f"{pnl_sign}${abs(pnl_d):,.2f} ({pnl_sign}{pnl_pct:.1f}%)"

        rows += f"""
        <tr>
          <td style="padding:10px 12px;font-weight:bold;color:{BLUE};">{ticker}</td>
          <td style="padding:10px 12px;color:{TEXT_COL};">{stars}</td>
          <td style="padding:10px 12px;color:{TEXT_COL};">{shares}</td>
          <td style="padding:10px 12px;color:{TEXT_COL};">{price_str}</td>
          <td style="padding:10px 12px;color:{TEXT_COL};">{cur_str}</td>
          <td style="padding:10px 12px;color:{TEXT_COL};">{val_str}</td>
          <td style="padding:10px 12px;color:{pnl_color};font-weight:bold;">{pnl_str}</td>
        </tr>
        <tr>
          <td colspan="7" style="padding:0 12px 10px 12px;color:#8b949e;font-size:12px;
              border-bottom:1px solid {GRID_COL};">{theme}</td>
        </tr>"""

    total_pnl   = total_current - total_cost
    total_pnl_p = (total_pnl / total_cost * 100) if total_cost else 0
    tp_color    = GREEN if total_pnl >= 0 else RED
    tp_sign     = "+" if total_pnl >= 0 else ""

    return f"""
    <hr style="border-color:{GRID_COL};margin:32px 0;">
    <h2 style="color:{GOLD};">🧠 Research Intelligence — Thematic Positions</h2>
    <p style="color:#8b949e;font-size:13px;margin:0 0 16px 0;">
      $10,000 thematic allocation from Apex Capital Head Agent research synthesis
      (US–China semiconductor & geopolitical analysis · 9 research agents)
    </p>

    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;">
      {_metric_card("Budget Deployed", f"${total_cost:,.2f}", BLUE)}
      {_metric_card("Current Value",   f"${total_current:,.2f}", BLUE)}
      {_metric_card("Thematic P&L",    f"{tp_sign}${abs(total_pnl):,.2f} ({tp_sign}{total_pnl_p:.1f}%)", tp_color)}
    </div>

    <table style="width:100%;border-collapse:collapse;background:{CARD_BG};
                  border:1px solid {GRID_COL};border-radius:8px;font-size:13px;">
      <thead>
        <tr style="background:#21262d;">
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">TICKER</th>
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">CONVICTION</th>
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">SHARES</th>
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">ENTRY</th>
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">CURRENT</th>
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">VALUE</th>
          <th style="padding:10px 12px;text-align:left;color:#8b949e;">P&amp;L</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>

    <p style="color:#8b949e;font-size:11px;margin-top:10px;">
      Short thesis maintained: <b>INTC</b> — catastrophic AI chip market share loss to NVDA/AMD,
      Gaudi 3 underperforming. In momentum short candidate list.
    </p>"""


def save_report(html: str, filename: str) -> str:
    path = os.path.join(REPORTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Report saved: {path}")
    return path
