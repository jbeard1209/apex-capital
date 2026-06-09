# Apex Capital Dashboard — Start Guide

## Run locally (just you, on your Mac)

Open Terminal and run:

```
python3 "/Users/jackbeard/Claude investment strategy/dashboard_server.py"
```

Your browser will open automatically to **http://localhost:5050**.

Log in with:
- Username: `jack` / Password: `REDACTED_DASHBOARD_PASSWORD`
- Username: `guest` / Password: `REDACTED_GUEST_PASSWORD`

The dashboard auto-refreshes every 30 seconds.
Press **Ctrl+C** in Terminal to stop the server.

---

## Share with anyone over the internet (ngrok)

ngrok creates a real HTTPS link (like `https://abc123.ngrok.io`) that anyone
in the world can use to reach your local dashboard — while it's running.

### One-time setup (takes ~2 minutes)

**Step 1 — Install ngrok**

Go to https://ngrok.com/download and click **Mac (Apple Silicon)** or
**Mac (Intel)** depending on your machine. Unzip it and move the `ngrok`
file to your Applications folder (or anywhere you like).

**Step 2 — Create a free account**

Sign up at https://ngrok.com (free). After signing up, go to:
https://dashboard.ngrok.com/get-started/your-authtoken

Copy your authtoken, then run in Terminal:

```
ngrok config add-authtoken YOUR_TOKEN_HERE
```

That's it — setup is done. You only do this once.

---

### Every time you want to share

**Step 1 — Start the dashboard server** (in one Terminal window):

```
python3 "/Users/jackbeard/Claude investment strategy/dashboard_server.py"
```

**Step 2 — Start ngrok** (open a second Terminal window):

```
ngrok http 5050
```

ngrok will show something like:

```
Forwarding   https://a1b2c3d4.ngrok-free.app -> http://localhost:5050
```

**Step 3 — Share that URL** with whoever you want.

They'll see the Apex Capital login page and need a username + password to
get in. The dashboard is live and updates every 30 seconds.

**Step 4 — Stop sharing:** press Ctrl+C in the ngrok Terminal window.
The public link immediately stops working. Your server is still running
locally but is unreachable from outside.

---

## Passwords and users

Edit the top of `dashboard_server.py` to change passwords or add users:

```python
PASSWORD_REQUIRED = True   # Set False to make it fully public (no login)

USERS = {
    "jack":  "REDACTED_DASHBOARD_PASSWORD",   # your login
    "guest": "REDACTED_GUEST_PASSWORD",       # share this
    "newuser": "theirpassword",   # add more like this
}
```

Save the file and restart the server (`Ctrl+C` then run it again).

---

## Security notes

- The login uses secure session tokens (random, 64-character hex strings).
- Passwords are compared with a timing-safe function (prevents brute-force timing attacks).
- Sessions are stored in memory only — they reset when you restart the server.
- ngrok connections are HTTPS (encrypted) end-to-end.
- When ngrok is stopped, the public URL is gone. No one can reach the server.
- Portfolio data never leaves your Mac except through the encrypted ngrok tunnel.

---

## Quick reference

| What | Command |
|------|---------|
| Start dashboard | `python3 "/Users/jackbeard/Claude investment strategy/dashboard_server.py"` |
| Start ngrok | `ngrok http 5050` |
| Stop either | Ctrl+C in that Terminal window |
| Change passwords | Edit top of `dashboard_server.py` |
| Make it public (no password) | Set `PASSWORD_REQUIRED = False` in server file |
