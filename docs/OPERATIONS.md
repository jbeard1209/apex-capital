# Operations — how to run the dashboard and the firm

This is the day-to-day guide. For the pitch and architecture see
[the root README](../README.md); for the cross-host layout see
[INFRASTRUCTURE.md](INFRASTRUCTURE.md); for the agent brief see
[CLAUDE.md](../CLAUDE.md).

---

## Local dashboard

The dashboard reads live JSON out of `institutional-tracker/data/` and
serves it at `http://localhost:5050`.

### One-time setup

```bash
cp .env.example .env
$EDITOR .env      # set APEX_DASHBOARD_USERS at minimum
```

`APEX_DASHBOARD_USERS` is a JSON object mapping usernames to passwords:

```
APEX_DASHBOARD_USERS='{"you":"StrongPass!","guest":"viewonly"}'
```

The server refuses to start when password protection is on and no users are
configured — better a clear failure than a wide-open box.

### Run

From the repo root:

```bash
set -a && source .env && set +a
python3 dashboard_server.py
```

The dashboard auto-refreshes every 30 seconds. `Ctrl+C` to stop.

### Share it (optional)

To let someone reach the local dashboard over the internet, use
[ngrok](https://ngrok.com):

```bash
ngrok http 5050
```

`ngrok` prints an HTTPS URL that forwards to `localhost:5050`. Anyone with
the URL still has to log in with a username/password from your `.env`.

`Ctrl+C` in the `ngrok` window kills the public link immediately.

---

## Running the pipeline

The daily pipeline lives under `institutional-tracker/`. Common commands:

```bash
cd institutional-tracker

python3 main.py                     # full daily pipeline (signals + review)
python3 test_pipeline_smoke.py      # smoke + wiring-contract tests
python3 wiring_audit.py             # dead-integration sweep
python3 logic_team.py               # coherence-law checks
python3 model_test_harness.py       # walk-forward AUC vs baseline
python3 chairman_inbox.py --full    # decisions awaiting the Chairman
```

Most agents accept `--report` to print a summary rather than side-effecting.

---

## Two hosts, one paper account

The firm runs on two hosts trading the same Alpaca paper account:

- **Mac** (this checkout) — development, editing, and one copy of the daily
  pipeline.
- **GCP VM** `apex-capital-495523` — always-on second brain with its own
  crontab (~63 jobs). Deploy to it with:

  ```bash
  cd institutional-tracker
  bash gcp_setup/deploy.sh
  ```

  The VM does not auto-pull. Running code you haven't deployed is a
  common failure mode. See [INFRASTRUCTURE.md](INFRASTRUCTURE.md) for the
  full account of hosts, credentials, and where each pipeline actually
  runs.

---

## Security notes

- All login uses timing-safe password comparison and 64-char random session
  tokens.
- Sessions are in-memory only and reset on server restart.
- `.env` is gitignored. Never commit real credentials.
- The trading account is a paper account; the firm has no path to place
  live orders. Kalshi contracts are CFTC-regulated and are strictly
  off-limits for real capital.
