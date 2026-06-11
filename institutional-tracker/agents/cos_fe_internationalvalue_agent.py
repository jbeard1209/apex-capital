#!/usr/bin/env python3
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from research_team import SectorResearchAgent
except ImportError:
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("research_team",
        str(pathlib.Path(__file__).parent.parent / "research_team.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    SectorResearchAgent = module.SectorResearchAgent
ENERGY_BLOCK = set()
try:
    from research_team import ENERGY_BLOCK
except ImportError:
    pass

class FEInternationalValueAgent(SectorResearchAgent):
    DEFAULT_WATCHLIST = ['BTI', 'DEO', 'BUD', 'BP', 'SONY', 'TM', 'HMC', 'NSRGY', 'UL', 'ASML', 'SAP', 'AZN', 'GSK', 'RHHBY', 'BAYRY', 'LVMUY']
    DEFAULT_SECTOR    = 'FE-InternationalValue'
    DEFAULT_ETF       = 'VEA'
    DEFAULT_THESIS    = 'Non-US value. European quality cheap Japanese below cash Korean conglomerates'

    def _conviction(self, mom, fund):
        mos = fund.get("margin_of_safety")
        if mos is None:
            base = 2
            if fund.get("revenue_growth") and fund["revenue_growth"] > 0: base += 1
            if fund.get("profit_margin") and fund["profit_margin"] > 0.05: base += 1
            return min(5, base)
        if mos >= 0.35: return 5
        elif mos >= 0.25: return 4
        elif mos >= 0.15: return 3
        elif mos >= 0.05: return 2
        else: return 1

    def _get_fundamentals(self, ticker):
        fund = super()._get_fundamentals(ticker)
        try:
            from fe_intrinsic_value import calculate_iv
            iv = calculate_iv(ticker)
            if iv:
                fund["margin_of_safety"] = iv.get("margin_of_safety")
                fund["conservative_iv"]  = iv.get("conservative_iv")
                fund["current_price"]    = iv.get("current_price")
        except Exception:
            pass
        return fund

    def __init__(self, agent_id='fe_international'):
        super().__init__({
            'agent_id': agent_id, 'sector_name': self.DEFAULT_SECTOR,
            'etf_symbol': self.DEFAULT_ETF, 'description': self.DEFAULT_THESIS,
            'watchlist': [t for t in self.DEFAULT_WATCHLIST if t not in ENERGY_BLOCK],
            'investment_horizon': 'long',
        })
