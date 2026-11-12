---
description: Kalshi copy-trade desk status — armed/halted, cash by shard, leaders, today's spend vs caps, open positions, P&L
allowed-tools: Bash(ssh apex-capital:*)
---

The Kalshi copy-trade bot runs only on the GCP VM (`ssh apex-capital`). Both
commands below are read-only: `--report` and `--json` place no orders and
write no files.

## Manager report (health, caps, P&L by period, recent orders)

!`ssh -o BatchMode=yes -o ConnectTimeout=20 apex-capital 'cd ~/institutional-tracker && ~/venv/bin/python3 kalshi_copytrade_manager.py --report' 2>&1 | head -120`

## Daily P&L history, cash by shard, open positions

!`ssh -o BatchMode=yes -o ConnectTimeout=20 apex-capital 'cd ~/institutional-tracker && if [ -f kalshi_pnl_history.py ]; then ~/venv/bin/python3 kalshi_pnl_history.py --json | ~/venv/bin/python3 -c "import json,sys; d=json.load(sys.stdin); d.pop(\"description\",None); print(json.dumps(d, indent=1))"; else echo "kalshi_pnl_history.py is not deployed on the VM yet"; fi' 2>&1 | head -250`

## What to do

Using only the output above (do not run anything else unless the user asks),
give the Chairman a short status in plain sentences, in this order:

1. **Armed or halted.** If halted, quote the reason and when it happened, and
   say what clears it (the report names the file to delete). Say whether it is
   live or paper.
2. **Cash by shard.** The copier can only spend on the shard(s) it trades
   (shard 0); call out cash stranded on other shards.
3. **Followed leaders**, and any LEADER_HIDDEN / NO_LEADERS finding.
4. **Today's spend vs the caps** ($1 per market, $3 per day).
5. **Open positions** with their marks and open P&L.
6. **P&L**: realised, unrealised and total, then today / week / month / all
   time, plus the daily history (back-filled days have no mark; say so rather
   than treating them as $0).
7. Every finding from the health section, worst first.

If either ssh failed (timeout, "Permission denied", no output), say so plainly
and show the error — never present an unreachable VM as a quiet day. If the
two sources disagree on total P&L, report both numbers and flag it.
