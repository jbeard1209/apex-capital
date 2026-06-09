# Deploy to Railway — Step by Step

## What you'll end up with
- A permanent public URL (e.g. `https://apex-capital.up.railway.app`)
- Password-protected login — anyone needs a username + password to see anything
- Auto-updates every 6 hours from your Mac
- Always live, even when your Mac is off
- **Cost: Free** (Railway hobby plan, no credit card)

---

## Step 1 — Run the one-time git migration (do this first)

This moves the git repo root to the top-level folder so everything gets pushed.
Open Terminal and run:

```
bash "/Users/jackbeard/Claude investment strategy/migrate_git.sh"
```

This takes about 10 seconds. After it runs, your GitHub repo includes everything —
`institutional-tracker/`, `dashboard/`, `railway.json`, `Procfile`, all of it.

---

## Step 2 — Sign into Railway with GitHub

1. Go to **https://railway.app**
2. Click **"Login with GitHub"**
3. Authorise Railway to access your GitHub account

Free hobby plan — no credit card needed.

---

## Step 3 — Deploy your repo

1. Click **"New Project"**
2. Click **"Deploy from GitHub repo"**
3. Select **`apex-capital`** (your repo)
4. Click **"Deploy Now"**

That's it — no Root Directory setting, no extra config. Railway reads `railway.json`
at the root and knows exactly what to do. Build takes about 30 seconds.

---

## Step 4 — Set your passwords and API key

In Railway, click your project → **"Variables"** tab → **"New Variable"** and add:

| Variable | Value | Notes |
|----------|-------|-------|
| `API_KEY` | `apex-2026-xK9mLq3rPvWz` | Make up any long string. Write it down. |
| `USERS` | `{"jack":"yourpassword","guest":"guestpass"}` | Your login credentials |
| `PASSWORD_REQUIRED` | `true` | Set `false` to skip login entirely |

Click **"Deploy"** to apply. Takes 10 seconds.

> ⚠️ Use double quotes inside `USERS` — it must be valid JSON.
> ✓ `{"jack":"password123"}` 
> ✗ `{'jack':'password123'}`

---

## Step 5 — Get your URL

Railway → **Settings** tab → **Domains** → copy the `.up.railway.app` URL.

Open it in your browser. You'll see the Apex Capital login page. Log in.
(Dashboard will say "No data pushed yet" until Step 6.)

---

## Step 6 — Wire up the Mac data sync

Open `dashboard/sync_to_cloud.py` in any text editor and update the top two lines:

```python
CLOUD_URL = "https://your-actual-url.up.railway.app"   # ← paste your Railway URL
API_KEY   = "apex-2026-xK9mLq3rPvWz"                   # ← must match Railway var
```

**Test it manually:**
```
python3 "/Users/jackbeard/Claude investment strategy/dashboard/sync_to_cloud.py"
```

Expected output:
```
✓ Push successful: 19 keys pushed at 2026-...
```

**Add to crontab for automatic 6-hour syncs:**
```
crontab -e
```
Press `i`, paste this line, then `Esc` → `:wq` → Enter:
```
0 */6 * * * python3 "/Users/jackbeard/Claude investment strategy/dashboard/sync_to_cloud.py" >> "/Users/jackbeard/Claude investment strategy/dashboard/sync.log" 2>&1
```

---

## You're done

Your dashboard is now live at a permanent public URL, password-protected,
and syncs automatically every 6 hours from your Mac.

Share it as: URL + username + password. That's all anyone needs.

---

## Quick reference

| Task | How |
|------|-----|
| Add/change users | Update `USERS` in Railway Variables tab |
| Update dashboard design | Push changes to GitHub — Railway auto-deploys in ~60s |
| Push data manually now | `python3 ".../dashboard/sync_to_cloud.py"` |
| Check sync history | `cat ".../dashboard/sync.log"` |
| Make it public (no login) | Set `PASSWORD_REQUIRED` = `false` in Railway Variables |
