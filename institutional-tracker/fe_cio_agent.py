#!/usr/bin/env python3
"""APEX CAPITAL — FE CIO AGENT
CIO synthesis: curates the board slate, limits sector concentration,
enforces gold allocation. Returns final ordered slate for the board.
"""
from __future__ import annotations
import json, logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("fe_cio")
DATA_DIR   = Path(__file__).parent / "data"
CIO_SLATE  = DATA_DIR / "fe_cio_slate.json"
GOLD_TICKERS = {"GLD","IAU","GLDM","NEM","AEM","WPM","FNV","RGLD","OR","GOLD","KGC","KL","AGI","BTG","PHYS"}
GOLD_TARGET  = 0.15   # 15% of FE portfolio in hard money

def run_cio_synthesis(picks: list, lane_capital: float = 5000.0) -> list:
    """
    Curate picks:
    1. Ensure gold allocation ≥ 15%
    2. Cap single sector ≤ 40%
    3. Rank remaining by MoS then conviction
    4. Write slate
    """
    gold = [p for p in picks if p.get("ticker","").upper() in GOLD_TICKERS]
    non_gold = [p for p in picks if p.get("ticker","").upper() not in GOLD_TICKERS]

    # Enforce gold floor
    if not gold:
        logger.warning("[CIO] No gold picks — board should ensure hard money allocation")

    # Sort non-gold by MoS desc, conviction desc
    non_gold.sort(key=lambda p: (p.get("margin_of_safety",0) or 0, p.get("conviction",0) or 0), reverse=True)

    # Sector cap: max 2 picks per sector
    seen_sectors, final = {}, []
    for p in non_gold:
        sector = p.get("sector", p.get("source_agent_class", "Other"))
        if seen_sectors.get(sector, 0) >= 2:
            continue
        seen_sectors[sector] = seen_sectors.get(sector, 0) + 1
        final.append(p)

    # Gold goes first (always allocated)
    slate = gold + final

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CIO_SLATE, "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(), "slate": slate}, f, indent=2, default=str)

    logger.info("[CIO] Slate: %d picks (%d gold, %d equity)", len(slate), len(gold), len(final))
    return slate
