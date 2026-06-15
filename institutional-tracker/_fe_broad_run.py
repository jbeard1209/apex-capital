import logging; logging.getLogger("yfinance").setLevel(logging.CRITICAL); logging.disable(logging.WARNING)
import sys,json; sys.path.insert(0,".")
from fe_pipeline import prescreen_universe
from fe_intrinsic_value import calculate_iv
_,univ=prescreen_universe()
univ=sorted(univ)
buys=[]; review=[]; scanned=0
for t in univ:
    try:
        r=calculate_iv(t)
        if not r: continue
        scanned+=1; v=r.get("verdict"); mos=round((r.get("margin_of_safety") or 0)*100)
        if v=="BUY": buys.append({"t":t,"mos":mos,"epv":r.get("epv_iv"),"dcf":r.get("dcf_iv"),"price":r.get("current_price")})
        elif v=="REVIEW": review.append({"t":t,"mos":mos})
    except Exception: pass
buys.sort(key=lambda x:-x["mos"]); review.sort(key=lambda x:-x["mos"])
json.dump({"scanned":scanned,"buys":buys,"review":review[:40]}, open("/tmp/fe_broad_result.json","w"), indent=2)
print(f"SCAN DONE: universe={len(univ)} scanned={scanned} BUY={len(buys)} REVIEW={len(review)}")
for b in buys: print(f"  BUY {b['t']}: MoS={b['mos']}% epv=${b['epv']} dcf=${b['dcf']} price=${b['price']}")
