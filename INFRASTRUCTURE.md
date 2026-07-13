# Apex Capital — Infrastructure Registry (single source of truth)

**RULE: every host, account, or scheduled runner that touches this firm MUST be listed
here. If you (human or agent) create/find one that isn't listed, add it and alert the
Chairman — an unlisted actor is how the firm lost track of its own production server
for a month (June–July 2026, the "two brains" incident).**

| # | Resource | What it is / runs | How to check it |
|---|---|---|---|
| 1 | **GCP VM — PRODUCTION** (`apex-capital-495523`, ssh alias `apex-capital`) | THE firm: 55-job crontab (main.py 4×/day, all pods, canary, monitors, backups), venv python at `~/venv/bin/python3`, repo copy at `~/institutional-tracker` (rsync target — its .git is vestigial) | `ssh apex-capital 'cat ~/institutional-tracker/deploy_stamp.json; uptime; crontab -l \| grep -c python3'` |
| 2 | **Mac — DEVELOPMENT ONLY** (this machine) | Code editing + tests. NO trading crons (demoted 2026-07-03, see mac_dev_crontab.txt). Claude scheduled tasks live here (weekly investigator; paused daily runner) | `crontab -l` should show no python jobs |
| 3 | **Alpaca paper account** | The shared trading account BOTH hosts can reach (keys in config.py fallback + .env). The foreign-order detector (daily, on prod) flags orders no local decision explains | `python3 foreign_order_detector.py` |
| 4 | **GitHub** `jbeard1209/apex-capital` | Source of truth for code. Prod does NOT auto-pull — ship with `bash gcp_setup/ship.sh` | `git log origin/main -1` |
| 5 | **Railway** | DECOMMISSIONED 2026-07-10 (free tier ended; Chairman opted out of paying). Was a display-only dashboard whose data sync never functioned — zero operational impact. Do not re-add without Chairman approval. Free local alternative: `python3 dashboard_server.py` (port 5050) | n/a |
| 6 | **Oracle Cloud** | DEAD. Failed 2nd-cloud attempt; trial expired, no instance ever created. Retry job removed from schedules. Do not resurrect without Chairman approval (cost risk) | n/a |
| 7 | **Gmail** (email_sender) | Chairman notifications + daily digest. Creds in .env on BOTH hosts — after demotion, prod (VM) is the sender | check latest digest email timestamps |

## Deploy procedure (THE anti-drift rule)
Code changes are NOT live until shipped to prod. One command from the Mac:
```
bash gcp_setup/ship.sh        # = git push + deploy to VM + stamp + remote smoke test
```
`deploy.sh` writes `deploy_stamp.json` (repo SHA + date) that ships with the code.
**The VM checks its own staleness daily** (pod_health_monitor → deploy-drift check):
if the repo has moved and the stamp is >3 days old, it escalates CRITICAL to the
board + Chairman queue automatically. The weekly investigator independently
re-verifies via ssh and also sweeps `~/.ssh/config` for hosts not in this registry.
