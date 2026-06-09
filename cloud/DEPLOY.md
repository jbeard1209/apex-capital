# Apex Capital — Deploy to the Internet (Free, 24/7)

## What you'll end up with

- A permanent URL like `https://apex-capital-dashboard.up.railway.app`
- Password-protected login page
- Data updates automatically every 6 hours from your Mac
- Always live — even when your Mac is off
- **Cost: $0** (Railway free hobby plan, no credit card)

---

## Step 1 — Create a GitHub account (if you don't have one)

Go to https://github.com and sign up. It's free.

---

## Step 2 — Create a private GitHub repository

1. Go to https://github.com/new
2. Repository name: `apex-capital-dashboard`  
3. Set it to **Private** (important — keeps your code off the public internet)
4. Click **Create repository**

---

## Step 3 — Upload the cloud files to GitHub

On the next page, click **"uploading an existing file"** (or drag and drop).

Upload these 4 files from your `cloud/` folder:
- `server_cloud.py`
- `apex_live_dashboard.html`  ← copy this from the parent folder
- `requirements.txt`
- `Procfile`

Click **Commit changes**.

---

## Step 4 — Create a Railway account (free, no credit card)

1. Go to https://railway.app
2. Click **"Login with GitHub"** — use the GitHub account you just made
3. That's it — you're signed in

---

## Step 5 — Deploy to Railway

1. Click **"New Project"**
2. Click **"Deploy from GitHub repo"**
3. Select your `apex-capital-dashboard` repository
4. Railway detects the `Procfile` automatically and starts deploying
5. Wait about 60 seconds — you'll see the build log

---

## Step 6 — Set your environment variables (passwords & API key)

In your Railway project, click **"Variables"** tab and add these:

| Variable | Value | What it does |
|----------|-------|--------------|
| `API_KEY` | `make-up-any-long-random-string` | Secret key your Mac uses to push data |
| `USERS` | `{"jack":"yourpassword","guest":"guestpassword"}` | Login credentials |
| `PASSWORD_REQUIRED` | `true` | Require login (set `false` to make it public) |

Click **"Deploy"** after adding variables.

> **Pick a strong API_KEY** — something like `apex-2026-xK9mLq3rPvWz`. Write it down.

---

## Step 7 — Get your URL

In Railway, click the **"Settings"** tab → **"Domains"**.

You'll see a URL like: `https://apex-capital-dashboard.up.railway.app`

**That's your permanent public URL.** Open it — you'll see the login page.
Try logging in with your username and password.

---

## Step 8 — Set up the Mac sync (data push every 6 hours)

**First, update `sync_to_cloud.py`** with your actual URL and API key:

Open `sync_to_cloud.py` in TextEdit or any editor and change:

```python
CLOUD_URL = "https://apex-capital-dashboard.up.railway.app"  # ← your Railway URL
API_KEY   = "apex-2026-xK9mLq3rPvWz"                         # ← your API_KEY
```

**Test it manually first:**
```
python3 "/Users/jackbeard/Claude investment strategy/cloud/sync_to_cloud.py"
```

You should see:
```
✓ Push successful: 19 keys pushed at 2026-06-09T...
```

**Then add it to your Mac's crontab** to run every 6 hours automatically.

Open Terminal and run:
```
crontab -e
```

This opens a text editor. Press `i` to insert, then paste this line:
```
0 */6 * * * python3 "/Users/jackbeard/Claude investment strategy/cloud/sync_to_cloud.py" >> "/Users/jackbeard/Claude investment strategy/cloud/sync.log" 2>&1
```

Press `Esc`, then type `:wq` and hit Enter to save.

From now on, your Mac will push fresh data to the cloud server every 6 hours automatically. If your Mac is off, it'll sync next time it's on.

---

## Step 9 — Custom domain (optional, ~$10–15/year)

If you want `apexcapital.com` or similar instead of the Railway URL:

1. Buy a domain at https://namecheap.com (~$10/year for .com)
2. In Railway → Settings → Domains → click **"Custom Domain"**
3. Follow the DNS instructions Railway shows you (takes ~10 minutes)

Not required — the Railway URL works perfectly fine.

---

## Sharing the URL

Send people your URL + their credentials:
- URL: `https://apex-capital-dashboard.up.railway.app`
- Username: `guest`
- Password: `guestpassword` (or whatever you set)

To add more users, update the `USERS` variable in Railway's Variables tab.

---

## Updating the dashboard design

If you ever want to update the dashboard HTML:
1. Replace `apex_live_dashboard.html` in GitHub (drag and drop the new file)
2. Railway auto-deploys within 60 seconds

---

## How the data flow works

```
Your Mac (cron, every 6h)
    └─► sync_to_cloud.py
            └─► POST /api/push (with API key)
                    └─► Railway server stores data in memory
                            └─► Anyone visits URL → login → dashboard
                                    └─► JS fetches /api/all → renders live data
```

The dashboard shows when data was last synced ("Updated 3h 22m ago").
If your Mac hasn't pushed in a while, the dashboard still works — it just shows older data.

---

## Troubleshooting

**"Application failed to start" on Railway**
→ Check the Deploy Logs tab. Usually a typo in environment variables.

**Login page shows but login fails**
→ Check your USERS variable in Railway — must be valid JSON with double quotes:
   `{"jack":"password123"}` ✓
   `{'jack':'password123'}` ✗ (single quotes don't work in JSON)

**Sync script fails with "connection refused"**
→ Make sure Railway deployed successfully and the URL is correct in sync_to_cloud.py.

**Dashboard shows "No data pushed yet"**
→ Run the sync script manually: `python3 ".../sync_to_cloud.py"`
