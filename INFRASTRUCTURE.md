# Apex Capital — Infrastructure Registry (single source of truth)

**RULE: every host, account, or scheduled runner that touches this firm MUST be listed
here. If you (human or agent) create/find one that isn't listed, add it and alert the
Chairman — an unlisted actor is how the firm lost track of its own production server
for a month (June–July 2026, the "two brains" incident).**

| # | Resource | What it is / runs | How to check it |
|---|---|---|---|
| 1 | **GCP VM — PRODUCTION, ⚠ STOPPED since 2026-08-06** — instance `apex-capital-configuration`, zone `us-east4-a`, project `apex-capital-495523`, ssh alias `apex-capital` | THE firm: 55-job crontab (main.py 4×/day, all pods, canary, monitors, backups), venv python at `~/venv/bin/python3`, repo copy at `~/institutional-tracker` (rsync target — its .git is vestigial). **The instance name is `apex-capital-configuration`; `apex-capital-495523` is the PROJECT.** This row named the project for months, so nobody could have looked the instance up from it. Its guest hostname is why its order stamps read `apexcapitalc` (`apex-capital-configuration` → 12 chars). **Last order 2026-08-06T08:00Z; the GCP free trial ended and the instance was stopped. A stopped instance has no external IP — that, not an IP rotation, is why ssh times out. Resources are deleted after the grace period (2026-09-04); the boot disk holds a month of `data/` that exists on no other host.** | `ssh apex-capital 'cat ~/institutional-tracker/deploy_stamp.json; uptime; crontab -l \| grep -c python3'` — and when that times out, **`python3 single_writer.py`**, which reads the broker and needs no ssh |
| 2 | **Mac — SECOND LIVE HOST** (this machine) | **This row said "DEVELOPMENT ONLY" until 2026-08-07 and was FALSE.** The 2026-07-03 demotion removed *cron* jobs; nobody looked at **LaunchAgents**. `com.apexcapital.catchup` (`~/Library/LaunchAgents/com.apexcapital.catchup.plist` → `catch_up.py`, `StartInterval` 1200s) runs the full pipeline every 20 min and placed orders in 55 of 255 logged runs — incl. 10 on Aug 4 and 7 on Aug 6, against the same Alpaca account as the VM. It stays loaded on purpose: it is the only thing able to exit a position while the VM is unreachable. Since 2026-08-07 it may **only reduce** exposure (see Single-writer rule below). Claude scheduled tasks also live here (weekly investigator; paused daily runner) | `crontab -l` **is not enough** — also `launchctl list \| grep apex` and `tail institutional-tracker/logs/catchup_launchd.log` |
| 3 | **Alpaca paper account** | The shared trading account BOTH hosts can reach (keys in config.py fallback + .env). Also the **only medium every host shares** — no host can count hosts, but the broker's order history can, so this is where cross-host divergence is detected. `foreign_order_detector` asks "is this order ours?" and answers OK when two of our own hosts fork; `single_writer` asks "how many of us are there?" | `python3 single_writer.py` (48h host tally) and `python3 foreign_order_detector.py` |
| 4 | **GitHub** `jbeard1209/apex-capital` | Source of truth for code. Prod does NOT auto-pull — ship with `bash gcp_setup/ship.sh` | `git log origin/main -1` |
| 5 | **Railway** | DECOMMISSIONED 2026-07-10 (free tier ended; Chairman opted out of paying). Was a display-only dashboard whose data sync never functioned — zero operational impact. Do not re-add without Chairman approval. Free local alternative: `python3 dashboard_server.py` (port 5050) | n/a |
| 6 | **Oracle Cloud** | TERMINATED 2026-07-27. This "DEAD/no instance ever created" note was FALSE for months: an Always-Free instance `apex-capital-server` (150.136.182.51, us-ashburn-1) was silently RUNNING for ~69 days — a full 3rd parallel firm (47 cron jobs) trading the SHARED Alpaca paper account and sending the ghost "Short-Term ML Board Weekly Report" emails (traced via email header IP). Crontab wiped 2026-07-26, full archive pulled to `~/Desktop/apex_oracle_archive_20260727/`, instance terminated via OCI API (preserve_boot_volume=False). Lesson: trust artifacts (email headers, live IPs, uptime) over this file. Do not resurrect. | n/a |
| 7 | **Gmail** (email_sender) | Chairman notifications + daily digest. Creds in .env on BOTH hosts — after demotion, prod (VM) is the sender | check latest digest email timestamps |
| 8 | **healthchecks.io** — the ONLY out-of-firm watchdog | `APEX_HEALTHCHECK_URL` is set in the **VM crontab environment** (line 6 of the snapshot); `main.py` curls it at the end of each successful run. Everything else that watches this firm runs *inside* this firm, so it dies with it — this is the one alarm that fires when the VM stops answering. Deliberately **not** configured on the Mac (`config.py HEALTHCHECK_PING_URL = ""`, no `EnvironmentVariables` in the plist): the Mac runs `main.py` every 20 min, so giving it the same URL would let the Mac hold the check green while the VM is dead — a watchdog measuring "is *either* host alive" instead of "is prod alive". Do not add it to the Mac. | `grep APEX_HEALTHCHECK_URL institutional-tracker/data/vm_crontab_snapshot.txt` — **do not curl the ping URL to test it**, a GET *is* the all-clear signal and would suppress a real alarm |

## Single-writer rule (THE anti-fork rule) — Chairman decision 2026-08-07

Two hosts run this firm and they cannot see each other's `data/`. They are ONE system
only because of this rule; without it they are two firms sizing against the same capital.

| | |
|---|---|
| **Designated writer** | the **VM** (`APEX_TRADING_HOST=apexcapitalc` in `.env`, which `deploy.sh` scp's to both hosts, so the value is identical firm-wide — that is intentional, not a leak) |
| **May OPEN or increase exposure** | the VM only |
| **May REDUCE or close exposure** | **any host, always** |
| **On position-lookup failure** | entries fail **CLOSED**, exits fail **OPEN** |

The asymmetry is the point. The designated host is exactly the thing that goes missing —
the VM has been unreachable by ssh from the Mac for days while still trading. A blocked
entry costs an opportunity; a blocked stop-loss has no ceiling on its cost, so uncertainty
must never trap a position.

Enforced at the single choke point `order_ledger._submit_order` via `single_writer.check()`,
which raises `SingleWriterBlocked` rather than returning None (a caller must not be able to
record a position the broker never opened). Decisions land in `data/single_writer_log.json`.

### Two checks, and neither may stand in for the other (added 2026-08-14)

| Question | Function | Shape of the answer |
|---|---|---|
| *How many of us are writing?* | `hosts_trading_recently()` → `divergence_verdict()` | a **count** inside a 48h window |
| *Is the designated writer still writing?* | `hosts_last_seen()` → `writer_silence_verdict()` | a **timestamp** per host, no window |

**A count says a host WAS here; only a timestamp says it still IS.** The VM stopped on
2026-08-06 having placed six orders shortly before, so for the next two days the tally
still read `apexcapitalc=6` and was read as proof it was alive. Worse, `divergence_verdict`
returns on its *first* matching branch — three hosts appeared, so it reported CRITICAL about
the fork and never reached the liveness question. It was telling the truth about one problem
while a second one sat inside its own output. The firm ran exit-only for **eight days**.

`writer_silence_verdict` prefers **corroboration over the calendar**: another host trading
well after the writer's last order proves the market was open and the writer was absent, with
no holiday table to get wrong. With no second host to corroborate it downgrades to HIGH and
says so, rather than claiming a confidence it has not earned. Both run in
`cit_pipeline_integrity_agent.check_single_writer`; guarded by `TestDesignatedWriterSilence`.

Host identity is **assigned, not derived**: `data/.host_id`, seeded once from the hostname
and then persisted. `socket.gethostname()` is a network condition — this laptop stamped
orders as both `mac` and `macbookair17`, and `MacBook-Air-173`/`-179` both truncate to the
same 12-char tag, which would let a foreign machine inherit this one's identity. `.host_id`
is gitignored **and** excluded ahead of `deploy.sh`'s rsync includes: a shared `.host_id`
makes every host stamp the same tag and the detector reports one writer forever.
**A deploy must not be able to blind a detector.**

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
