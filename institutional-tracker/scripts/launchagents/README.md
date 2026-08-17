# macOS LaunchAgents

The Mac-side schedule. These are the *only* Mac jobs the firm depends on, and
they are deliberately not Claude scheduled tasks.

## Why not Claude scheduled tasks

On 2026-08-16 the scheduled-task list looked like this:

| task | schedule | enabled | last actually ran |
|---|---|---|---|
| `gcp-vm-health-monitor` | every 15 min | ✅ true | **2026-08-08** |
| `cit-code-bug-fixer` | every 10 min | ✅ true | **2026-08-07** |
| `apex-weekly-investigator` | weekly, Sat | ✅ true | 2026-08-16 (fine) |

Two high-frequency tasks had not fired for nine days while still reporting
`enabled: true` and a future `nextRunAt`. Nothing raised anything, because
nothing was watching the watcher — the same shape as memory
`orphan_agents_blindness`, and the same trap as `presence-is-not-currency`: a
task record existed, so coverage looked present.

`gcp-vm-health-monitor` was the dead-man check on the GCP VM. It was dead for
nine of the days it was supposed to be watching. That is exactly the window in
which the free trial ended and the instance sat STOPPED.

launchd is owned by the OS. It survives logout, runs with no app open, and does
not depend on any part of the system it is monitoring.

## Installing

```bash
cp scripts/launchagents/com.apexcapital.vmstate.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.apexcapital.vmstate.plist
launchctl list | grep apexcapital        # both agents should appear
```

Verify it actually ran — a loaded agent that never fires is the failure above
in a different costume:

```bash
tail -5 logs/vmstate_launchd.log
```

## The agents

- **`com.apexcapital.vmstate`** — `vm_state_monitor.py` every 15 min. Asks
  Google whether the instance is powered on, and the broker whether the
  designated writer is still writing. Exits non-zero when it *cannot tell*
  (missing/unauthenticated gcloud) rather than passing.

  Note the explicit `PATH` in the plist: `gcloud` lives in `/opt/homebrew/bin`,
  which is not on launchd's default PATH. Without it the monitor would report
  "cannot tell" on every run forever.

- **`com.apexcapital.catchup`** — `catch_up.py` on login and every 20 min,
  to run jobs missed while the Mac was asleep.

Both plists are committed here so a Mac rebuild restores them. The copies in
`~/Library/LaunchAgents/` are what actually run — if you edit one, copy it back.
