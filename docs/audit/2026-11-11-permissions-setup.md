# Letting Claude run routine Apex commands (permissions proposal, 2026-11-11)

Nothing here has been applied. Jack applies it by hand (steps at the bottom).

## What actually blocked the earlier sessions

Checked in the session logs (`~/.claude/projects/.../b51db249-*.jsonl`). Every session ran in
**auto mode**, and the refusals came from auto mode's safety classifier, not from an allow-list
or a hook:

| When (UTC) | Command | Classifier rule that fired |
|---|---|---|
| 2026-11-09 07:33 | script that ran tests then `bash gcp_setup/deploy.sh` | `[Production Deploy]` |
| 2026-11-09 16:47 | `cd institutional-tracker && bash gcp_setup/deploy.sh \| tail -25` | `[Production Deploy]` |
| 2026-11-09 23:06 | `ssh apex-capital '... reclose_stale_reopened.py --apply'; ...; /usr/bin/python3 ...` | `[Modify Shared Resources]` |

Why the existing `"Bash(*)"` allow rule didn't help: on entering auto mode, Claude Code
**drops** allow rules that grant arbitrary code execution (`Bash(*)`, `Bash(python3:*)` and
other wildcarded interpreters). Everything not matched by a surviving narrow rule goes to the
classifier, and its defaults block production deploys, in-place edits of shared state, and
even reads over `ssh` into "production". It knows nothing about your setup, so to it the VM is
just someone's production server.

The classifier only reads `autoMode` settings from `~/.claude/settings.json` (your user file).
It ignores them in the repo's `.claude/settings*.json`, so that's where the fix goes.

## A hole found along the way (worth fixing even if you change nothing else)

The repo's `.claude/settings.local.json` holds ~150 one-off rules accumulated from past
approvals. A few of them are narrow enough to **survive** auto mode, which means they skip the
classifier entirely:

- `Bash(ssh apex-capital *)` and `Bash(ssh -i *)`: any single command on the VM runs with
  no check at all. That includes `rm`, editing the crontab, or creating
  `data/.kalshi_armed`, which is the file that lets real-money Kalshi orders go out.
- `Bash(crontab -)`: replaces the Mac's whole crontab from stdin, unchecked.
- `Bash(git push *)`: any push, including a force push.

I could only confirm this from the docs. I didn't run a destructive command to test it. The
deploy and `--apply` runs above were only checked because they were compound commands
(`a; b; c`) whose other parts didn't match a rule.

## The proposal

The idea is to tell the classifier what your setup is, give it explicit exceptions for the
routine work, and put hard rules on the few things that move money or can't be undone.

### 1. Merge this into `~/.claude/settings.json`

Your current file has only `env` and `agentPushNotifEnabled`. Keep those and add the two blocks:

```json
{
  "env": {
    "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"
  },
  "agentPushNotifEnabled": true,

  "autoMode": {
    "environment": [
      "$defaults",
      "Organization: Apex Capital, Jack Beard's personal automated investing system. Jack is the only user and owns every host and account below.",
      "Source control: github.com/jbeard1209 (apex-capital code, apex-reports published reports). Both are Jack's own repos.",
      "Key internal services: the GCP VM 'apex-capital' (ssh alias apex-capital, project apex-capital-495523, code in ~/institutional-tracker, python ~/venv/bin/python3). It is Jack's own always-on host for the firm, not a shared or multi-tenant production system. Running commands on it over ssh is routine.",
      "Key internal services: the Alpaca account is a PAPER trading account (paper-api.alpaca.markets); its orders move no real money.",
      "Sensitive remote targets: Kalshi is the only real-money venue (caps $1 per market, $3 per day). Real orders are enabled only by the files data/.kalshi_armed and data/.kalshi_copytrade_armed on either host.",
      "CI/CD deploy targets: the only sanctioned deploy path is `bash gcp_setup/deploy.sh` or `bash gcp_setup/ship.sh`, run from institutional-tracker/ on the Mac. It rsyncs code and the Mac's .env to the VM."
    ],
    "allow": [
      "$defaults",
      "Deploying Apex code to the apex-capital VM with `bash gcp_setup/deploy.sh` or `bash gcp_setup/ship.sh` is allowed: it is Jack's own host and this is the routine, sanctioned release path.",
      "Running the repo's own Python scripts under ~/institutional-tracker on the apex-capital VM over ssh is allowed, including their --apply / --confirm-keep style flags that update the firm's own data/ JSON state (queues, inboxes, archives). These are Jack's maintenance tools for his own data.",
      "Reading files, logs, crontab and checksums on the apex-capital VM over ssh is allowed."
    ],
    "soft_deny": [
      "$defaults",
      "Creating, editing or deleting data/.kalshi_armed or data/.kalshi_copytrade_armed, setting KALSHI_ENV=live, or otherwise enabling real-money Kalshi orders needs Jack's explicit request naming that action.",
      "Changing Kalshi copy-trade settings (--reset-breaker, --add-leader, --remove-leader, spend caps) needs Jack's explicit request naming the change.",
      "Deleting or truncating anything under data/ or logs/ on either host, or replacing either host's crontab, needs Jack's explicit request.",
      "Editing any .env file needs Jack's explicit request (deploy.sh copies the Mac's .env over the VM's)."
    ]
  },

  "permissions": {
    "allow": [
      "Bash(bash gcp_setup/deploy.sh)",
      "Bash(bash gcp_setup/ship.sh)"
    ],
    "deny": [
      "Bash(*kalshi_armed*)",
      "Bash(*kalshi_copytrade_armed*)",
      "Write(**/.kalshi_armed)",
      "Edit(**/.kalshi_armed)",
      "Write(**/.kalshi_copytrade_armed)",
      "Edit(**/.kalshi_copytrade_armed)"
    ],
    "ask": [
      "Bash(crontab -)",
      "Bash(crontab -r*)",
      "Bash(git push --force*)",
      "Edit(**/.env)",
      "Write(**/.env)"
    ]
  }
}
```

What each piece does, in plain terms:

- **`environment`**: background for the classifier. It says the VM is yours, Alpaca is
  paper, and Kalshi is the only real money. On its own this should stop most of the false
  "production" and "shared resource" blocks. `"$defaults"` keeps all the built-in safety rules.
- **`allow` (autoMode)**: the exceptions. Deploys via deploy.sh/ship.sh, running repo scripts
  on the VM (including `--apply`), and reading the VM all go through without being stopped.
  The classifier still reads every one of these commands, so something like
  `ssh apex-capital 'rm -rf ~/institutional-tracker'` is still blocked.
- **`soft_deny` (autoMode)**: four extra "stop and ask" rules for this firm. They cover
  arming Kalshi, changing copy-trade settings, deleting data or replacing crontabs, and
  editing `.env`. A session can only do these when your own message names the exact action.
- **`permissions.allow`**: the two deploy commands, written exactly, skip the classifier
  for speed. Only the bare command matches. `cd x && bash gcp_setup/deploy.sh | tail` goes
  through the classifier instead, and the autoMode allow rule above lets it through.
- **`permissions.deny`**: no session can touch the Kalshi arm files, whatever it's told.
  Arming real money stays a job you do yourself in a terminal.
- **`permissions.ask`**: always shows you a prompt for replacing or wiping the Mac crontab, force pushes and
  `.env` edits, even in auto mode.

### 2. Remove these lines from the repo's `.claude/settings.local.json`

Delete these entries from its `permissions.allow` list. They're the ones that let commands
skip every check:

```
"Bash(ssh apex-capital *)",
"Bash(ssh -i *)",
"Bash(crontab -)",
"Bash(git push *)",
```

The rest of that file is mostly dead one-off rules: old IPs, `kill <pid>`, `/tmp` scripts.
They're harmless, but you could delete the whole file and let it rebuild. I wouldn't do that
without asking you.

## How to apply (about 5 minutes)

1. Open `~/.claude/settings.json` in a text editor and replace its contents with the JSON
   in section 1. Your two existing keys are already included in it.
2. Open `"/Users/jackbeard/Claude investment strategy/.claude/settings.local.json"` and
   delete the four lines in section 2. Watch the commas so the JSON stays valid.
3. Check that both files parse, and that the classifier sees your entries:
   ```bash
   python3 -m json.tool ~/.claude/settings.json > /dev/null && echo user-ok
   ```
   ```bash
   python3 -m json.tool "/Users/jackbeard/Claude investment strategy/.claude/settings.local.json" > /dev/null && echo local-ok
   ```
   ```bash
   claude auto-mode config | grep -c "apex-capital"
   ```
   The last one should print a number above 0.
4. Optional: `claude auto-mode critique` gives AI feedback on the rules you added.
5. Start a new session. Settings are read at session start. Then ask it to do a deploy.

## Known limits

- The classifier is a model. These rules make it much more likely to allow the routine
  commands, but they don't guarantee it. If something routine is still blocked, the error
  shows the rule name in `[brackets]`. Send me that and I'll tighten the wording.
- The `claude` CLI on PATH is 2.1.126. The desktop app runs its own bundled version, so
  sessions may behave a little differently from what the CLI reports.
- Deploys stay irreversible. Project instructions still say sessions must get your go-ahead
  before deploying to prod. This setup only removes the copy-paste step after you've said yes.
