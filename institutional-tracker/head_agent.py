# =============================================================================
# ⚠️  ARCHIVED — DO NOT RUN
# =============================================================================
# Superseded by main.py (multi-agent pipeline) on 2026-05-01.
# This file is kept for historical reference only.
# Archived copy: agents/archive/head_agent.py
# See agents/archive/README.md for full context.
# =============================================================================
# APEX CAPITAL HEAD AGENT (LEGACY)
# =============================================================================
# Reads the consolidated research report, executes $10,000 in thematic paper
# trades via Alpaca, and sends a confirmation email with full trade details.
#
# Thematic Allocation (from research_report.md):
#   NVDA  $4,000  — AI GPU monopoly       ★★★★★
#   AVGO  $3,000  — Custom ASIC + networking ★★★★★
#   AMD   $2,000  — AI GPU challenger + EPYC ★★★★
#   MRVL  $1,000  — Custom ASIC / optical interconnects ★★★★
#
# Usage:
#   python3 head_agent.py
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("head_agent")

# ── thematic allocations ─────────────────────────────────────────────────────
# Dollar amounts to invest in each ticker from the $10,000 research budget.
# These come directly from the consolidated research report scored by conviction.
THEMATIC_ALLOCATIONS = {
    "NVDA": {"dollars": 4_000, "conviction": 5, "theme": "AI GPU Monopoly (Blackwell supercycle, CUDA moat)"},
    "AVGO": {"dollars": 3_000, "conviction": 5, "theme": "Custom ASIC + AI Networking (Google TPU, Ethernet switch)"},
    "AMD":  {"dollars": 2_000, "conviction": 4, "theme": "AI GPU Challenger + EPYC (~28% server CPU share)"},
    "MRVL": {"dollars": 1_000, "conviction": 4, "theme": "Custom ASIC / Optical Interconnects (Trainium2, 800G DSPs)"},
}

TOTAL_BUDGET = sum(v["dollars"] for v in THEMATIC_ALLOCATIONS.values())  # $10,000

# Path for persisting positions so daily reports can read them
POSITIONS_LOG = os.path.join(os.path.dirname(__file__), "data", "thematic_positions.json")


# =============================================================================
# CORE LOGIC
# =============================================================================

def execute_thematic_trades(dry_run: bool = False) -> list[dict]:
    """
    Place market buy orders for each thematic ticker using the fixed dollar
    allocations.  Returns a list of trade result dicts.
    """
    from alpaca_trader import AlpacaTrader
    trader = AlpacaTrader()

    # Refresh positions — skip any ticker already held
    existing = trader.get_positions()
    held = set(existing["ticker"].tolist()) if not existing.empty else set()

    trades = []
    for ticker, meta in THEMATIC_ALLOCATIONS.items():
        target_dollars = meta["dollars"]
        conviction     = meta["conviction"]
        theme          = meta["theme"]

        logger.info(f"\n{'─'*60}")
        logger.info(f"  [{ticker}]  Target: ${target_dollars:,}  |  Conviction: {'★'*conviction}")
        logger.info(f"  Theme: {theme}")

        if ticker in held:
            logger.warning(f"  ⚠ Already holding {ticker} — skipping to avoid duplicate position")
            trades.append({
                "ticker":         ticker,
                "target_dollars": target_dollars,
                "conviction":     conviction,
                "theme":          theme,
                "status":         "skipped_already_held",
                "shares":         None,
                "price":          None,
                "actual_cost":    None,
                "order_id":       None,
                "timestamp":      datetime.now().isoformat(),
            })
            continue

        price = trader.get_latest_price(ticker)
        if not price or price <= 0:
            logger.error(f"  ✗ Could not fetch price for {ticker}")
            trades.append({
                "ticker":         ticker,
                "target_dollars": target_dollars,
                "conviction":     conviction,
                "theme":          theme,
                "status":         "failed_no_price",
                "shares":         None,
                "price":          None,
                "actual_cost":    None,
                "order_id":       None,
                "timestamp":      datetime.now().isoformat(),
            })
            continue

        shares = int(target_dollars / price)
        if shares <= 0:
            logger.error(f"  ✗ {ticker} price ${price:.2f} too high for ${target_dollars:,} budget")
            trades.append({
                "ticker":         ticker,
                "target_dollars": target_dollars,
                "conviction":     conviction,
                "theme":          theme,
                "status":         "failed_price_too_high",
                "shares":         0,
                "price":          price,
                "actual_cost":    0.0,
                "order_id":       None,
                "timestamp":      datetime.now().isoformat(),
            })
            continue

        actual_cost = shares * price
        logger.info(f"  → {shares} shares @ ${price:.2f} ≈ ${actual_cost:,.2f}")

        if dry_run:
            logger.info(f"  [DRY RUN] Would submit BUY {shares} {ticker}")
            trades.append({
                "ticker":         ticker,
                "target_dollars": target_dollars,
                "conviction":     conviction,
                "theme":          theme,
                "status":         "dry_run",
                "shares":         shares,
                "price":          price,
                "actual_cost":    actual_cost,
                "order_id":       "DRY_RUN",
                "timestamp":      datetime.now().isoformat(),
            })
            continue

        # Submit order directly with our dollar-sized share count
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            order_req = MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = trader.client.submit_order(order_req)
            logger.info(f"  ✓ ORDER SUBMITTED — ID: {order.id}  Status: {order.status}")

            trades.append({
                "ticker":         ticker,
                "target_dollars": target_dollars,
                "conviction":     conviction,
                "theme":          theme,
                "status":         "submitted",
                "shares":         shares,
                "price":          price,
                "actual_cost":    actual_cost,
                "order_id":       str(order.id),
                "order_status":   str(order.status),
                "timestamp":      datetime.now().isoformat(),
            })
            held.add(ticker)  # mark as held so we don't double-buy in this run

        except Exception as e:
            logger.error(f"  ✗ Order failed for {ticker}: {e}")
            trades.append({
                "ticker":         ticker,
                "target_dollars": target_dollars,
                "conviction":     conviction,
                "theme":          theme,
                "status":         f"error: {e}",
                "shares":         shares,
                "price":          price,
                "actual_cost":    actual_cost,
                "order_id":       None,
                "timestamp":      datetime.now().isoformat(),
            })

    return trades


def save_positions_log(trades: list[dict]) -> None:
    """Persist trade results to JSON so daily reports can include them."""
    os.makedirs(os.path.dirname(POSITIONS_LOG), exist_ok=True)

    # Load existing log (if any) and merge — don't overwrite skipped/prior entries
    existing = []
    if os.path.exists(POSITIONS_LOG):
        try:
            with open(POSITIONS_LOG) as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # Index existing by ticker for de-duplication
    by_ticker = {e["ticker"]: e for e in existing}
    for t in trades:
        if t["status"] in ("submitted", "dry_run"):
            by_ticker[t["ticker"]] = t

    with open(POSITIONS_LOG, "w") as f:
        json.dump(list(by_ticker.values()), f, indent=2)

    logger.info(f"\n  Positions log saved → {POSITIONS_LOG}")


# =============================================================================
# EMAIL REPORT
# =============================================================================

def build_confirmation_email(trades: list[dict], account_snap: dict) -> str:
    """Build an HTML email body summarising the thematic trades."""

    now_str   = datetime.now().strftime("%B %d, %Y at %H:%M")
    total_deployed = sum(t["actual_cost"] or 0 for t in trades if t["status"] in ("submitted", "dry_run"))
    n_filled  = sum(1 for t in trades if t["status"] in ("submitted", "dry_run"))
    n_skipped = len(trades) - n_filled

    star_map  = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆"}

    # ── trade rows ─────────────────────────────────────────────────────────
    rows_html = ""
    for t in trades:
        status_badge = {
            "submitted":          '<span style="color:#3fb950;font-weight:bold;">✓ SUBMITTED</span>',
            "dry_run":            '<span style="color:#f0883e;">DRY RUN</span>',
            "skipped_already_held": '<span style="color:#8b949e;">ALREADY HELD</span>',
            "failed_no_price":    '<span style="color:#f85149;">✗ NO PRICE</span>',
            "failed_price_too_high": '<span style="color:#f85149;">✗ PRICE TOO HIGH</span>',
        }.get(t["status"], f'<span style="color:#f85149;">✗ ERROR</span>')

        price_str = f"${t['price']:.2f}"  if t["price"]  else "—"
        shares_str = str(t["shares"])      if t["shares"] else "—"
        cost_str   = f"${t['actual_cost']:,.2f}" if t["actual_cost"] else "—"
        stars      = star_map.get(t["conviction"], "")

        rows_html += f"""
        <tr>
          <td style="padding:10px 8px;font-weight:bold;color:#58a6ff;">{t['ticker']}</td>
          <td style="padding:10px 8px;color:#c9d1d9;">{stars}</td>
          <td style="padding:10px 8px;color:#c9d1d9;">${t['target_dollars']:,}</td>
          <td style="padding:10px 8px;color:#c9d1d9;">{price_str}</td>
          <td style="padding:10px 8px;color:#c9d1d9;">{shares_str}</td>
          <td style="padding:10px 8px;color:#c9d1d9;">{cost_str}</td>
          <td style="padding:10px 8px;">{status_badge}</td>
        </tr>
        <tr>
          <td colspan="7" style="padding:0 8px 10px 8px;color:#8b949e;font-size:12px;
              border-bottom:1px solid #21262d;">{t['theme']}</td>
        </tr>"""

    # ── account snapshot ───────────────────────────────────────────────────
    portfolio_val = f"${account_snap.get('portfolio_value', 0):,.2f}"
    cash_val      = f"${account_snap.get('cash', 0):,.2f}"
    buying_pwr    = f"${account_snap.get('buying_power', 0):,.2f}"
    pnl_today     = account_snap.get('pnl_today', 0)
    pnl_pct       = account_snap.get('pnl_today_pct', 0)
    pnl_color     = "#3fb950" if pnl_today >= 0 else "#f85149"
    pnl_sign      = "+" if pnl_today >= 0 else ""

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:900px;margin:0 auto;">

  <!-- header -->
  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#58a6ff;margin:0;font-size:22px;">
      🧠 APEX CAPITAL — Research Intelligence
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">
      Head Agent Report &nbsp;·&nbsp; {now_str} &nbsp;·&nbsp; Paper Trading
    </p>
  </div>

  <!-- summary banner -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:32px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
        Thematic Budget</div>
      <div style="font-size:24px;font-weight:bold;color:#58a6ff;">${TOTAL_BUDGET:,}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
        Deployed</div>
      <div style="font-size:24px;font-weight:bold;color:#3fb950;">${total_deployed:,.2f}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
        Orders Submitted</div>
      <div style="font-size:24px;font-weight:bold;color:#c9d1d9;">{n_filled} / {len(trades)}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
        Portfolio Value</div>
      <div style="font-size:24px;font-weight:bold;color:#c9d1d9;">{portfolio_val}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
        Today's P&amp;L</div>
      <div style="font-size:24px;font-weight:bold;color:{pnl_color};">
        {pnl_sign}${abs(pnl_today):.2f} ({pnl_sign}{pnl_pct:.2f}%)</div>
    </div>
  </div>

  <!-- trades table -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">
    Thematic Positions Initiated
  </h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;">
    <thead>
      <tr style="background:#21262d;">
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">TICKER</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">CONVICTION</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">TARGET $</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">PRICE</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">SHARES</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">COST</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">STATUS</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <!-- research thesis -->
  <h2 style="color:#e6edf3;font-size:16px;margin-top:28px;margin-bottom:12px;">
    Research Thesis Summary
  </h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 20px;">
    <p style="margin:0 0 10px 0;color:#c9d1d9;">
      <strong style="color:#58a6ff;">Core thesis:</strong>
      The AI semiconductor supercycle is real, structurally durable, and the US fabless
      ecosystem is the primary beneficiary. Nine parallel research agents analysed US–China
      semiconductor competition and identified four high-conviction long positions.
    </p>
    <ul style="color:#c9d1d9;margin:0;padding-left:20px;line-height:1.8;">
      <li><strong style="color:#58a6ff;">NVDA (★★★★★)</strong> — 75–80% AI training GPU share;
          Blackwell sold out through 2026; CUDA = 15-year moat; $115B datacenter revenue FY2025E</li>
      <li><strong style="color:#58a6ff;">AVGO (★★★★★)</strong> — Google TPU + Meta MTIA design partner;
          AI chip rev ~$12B→$15–20B; 60–70% Ethernet switch share</li>
      <li><strong style="color:#58a6ff;">AMD (★★★★)</strong> — MI300X at 10–15% AI GPU share;
          EPYC at 27–30% server CPU and growing; credible Nvidia challenger</li>
      <li><strong style="color:#58a6ff;">MRVL (★★★★)</strong> — Amazon Trainium2 designer;
          dominant in 400G/800G optical DSPs; AI revenue doubling to $2.5B+ FY2025</li>
    </ul>
    <p style="color:#8b949e;font-size:12px;margin:14px 0 0 0;">
      <strong>Short thesis maintained:</strong> INTC — catastrophic AI chip market share loss to NVDA/AMD,
      Gaudi 3 underperforming. Already in momentum short candidate list.
    </p>
  </div>

  <!-- key risks -->
  <h2 style="color:#e6edf3;font-size:16px;margin-top:24px;margin-bottom:12px;">
    Key Risks to Monitor
  </h2>
  <div style="background:#161b22;border:1px solid #f85149;border-radius:8px;padding:16px 20px;">
    <ol style="color:#c9d1d9;margin:0;padding-left:20px;line-height:1.9;">
      <li>Taiwan Strait escalation — TSMC disruption would crater the entire sector</li>
      <li>AI capex digestion — hyperscaler ROI disappointment moderates the $300B+ cycle</li>
      <li>DeepSeek efficiency risk — algorithmic breakthroughs reduce compute-per-capability</li>
      <li>Chinese REE export embargo — hits defense/wind; paradoxically benefits MP Materials</li>
      <li>NVDA antitrust — CUDA ecosystem dominance drawing regulatory scrutiny</li>
    </ol>
  </div>

  <!-- account details -->
  <h2 style="color:#e6edf3;font-size:16px;margin-top:24px;margin-bottom:12px;">
    Paper Trading Account
  </h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;display:flex;gap:32px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Cash</div>
      <div style="font-size:18px;font-weight:bold;">{cash_val}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Buying Power</div>
      <div style="font-size:18px;font-weight:bold;">{buying_pwr}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Positions</div>
      <div style="font-size:18px;font-weight:bold;">{account_snap.get('n_positions', '—')}</div>
    </div>
  </div>

  <!-- footer -->
  <p style="color:#484f58;font-size:11px;margin-top:28px;border-top:1px solid #21262d;
            padding-top:12px;">
    Apex Capital Intelligence — Head Agent &nbsp;·&nbsp; Paper trading simulation —
    not real investment advice &nbsp;·&nbsp; Generated {now_str}
  </p>

</body>
</html>"""
    return html


# =============================================================================
# ENTRY POINT
# =============================================================================

def main(dry_run: bool = False) -> None:
    banner = "DRY RUN — no real orders will be placed" if dry_run else "LIVE PAPER TRADING"
    logger.info(f"\n{'='*60}")
    logger.info(f"  APEX CAPITAL HEAD AGENT  |  {banner}")
    logger.info(f"  Total thematic budget: ${TOTAL_BUDGET:,}")
    logger.info(f"{'='*60}")

    # 1. Execute trades
    trades = execute_thematic_trades(dry_run=dry_run)

    # 2. Fetch current account snapshot
    try:
        from alpaca_trader import AlpacaTrader
        trader  = AlpacaTrader()
        snap    = trader.get_portfolio_snapshot()
    except Exception as e:
        logger.warning(f"Could not fetch account snapshot: {e}")
        snap = {}

    # 3. Save positions log for daily report integration
    save_positions_log(trades)

    # 4. Build and send confirmation email
    html_body = build_confirmation_email(trades, snap)

    logger.info("\n  Sending confirmation email …")
    from email_sender import send_daily_report
    success = send_daily_report(
        html_body=html_body,
        subject=f"🧠 Apex Capital — $10K Thematic Trades {'(DRY RUN)' if dry_run else 'EXECUTED'} | {datetime.now().strftime('%b %d, %Y')}",
        paper_day=1,
        sender_name="head_agent",
    )

    if success:
        logger.info("  ✓ Confirmation email sent to jack.beard07@gmail.com")
    else:
        logger.error("  ✗ Email failed — check Gmail credentials in config.py")

    # 5. Print summary to console
    print(f"\n{'═'*60}")
    print(f"  THEMATIC TRADE SUMMARY")
    print(f"{'═'*60}")
    for t in trades:
        icon = "✓" if t["status"] in ("submitted", "dry_run") else "✗"
        cost = f"${t['actual_cost']:,.2f}" if t["actual_cost"] else "—"
        price = f"${t['price']:.2f}" if t["price"] else "—"
        shares = str(t["shares"]) if t["shares"] else "—"
        print(f"  {icon} {t['ticker']:<5}  {shares:>5} shares @ {price:>10}  =  {cost:>12}  [{t['status']}]")
    total_deployed = sum(t["actual_cost"] or 0 for t in trades if t["status"] in ("submitted", "dry_run"))
    print(f"{'─'*60}")
    print(f"  Total deployed: ${total_deployed:,.2f} / ${TOTAL_BUDGET:,}")
    print(f"  Email sent:     {'Yes' if success else 'No'}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    dry_run_flag = "--dry-run" in sys.argv
    main(dry_run=dry_run_flag)
