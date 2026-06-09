# Deploy to Railway — Your Situation (Existing GitHub Repo)

Your GitHub repo is `institutional-tracker/`. The dashboard files are now in
`institutional-tracker/dashboard/`. Claude Code will push them automatically
on the next commit.

---

## Step 1 — Make sure the files are on GitHub

Ask Claude Code to commit and push, or run in Terminal:
```
cd "/Users/jackbeard/Claude investment strategy/institutional-tracker"
git add dashboard/
git commit -m "Add live dashboard"
git push
```

---

## Step 2 — Connect to Railway (free, no credit card)

1. Go to **https://railway.app**
2. Click **"Login with GitHub"** — use your existing GitHub account
3. Click **"New Project"** → **"Deploy from GitHub repo"**
4. Select your `institutional-tracker` repository
5. **Important:** Click **"Configure"** and set **Root Directory** to `dashboard`
   (This tells Railway to look in the `dashboard/` subfolder for the Procfile)
6. Click **"Deploy"**

Railway will build and start the server in about 60 seconds.

---

## Step 3 — Set environment variables

In Railway, click your project → **"Variables"** tab → add these:

| Variable | Value |
|----------|-------|
| `API_KEY` | Pick any long random string, e.g. `apex-2026-xK9mLq3rPvWz` |
| `USERS` | `{"jack":"yourpassword","guest":"guestpassword"}` |
| `PASSWORD_REQUIRED` | `true` |

Click **"Deploy"** to apply.

---

## Step 4 — Get your permanent URL

Railway → **Settings** → **Domains** → copy the URL.
Looks like: `https://institutional-tracker-dashboard.up.railway.app`

Open it — you'll see the Apex Capital login page.

---

## Step 5 — Wire up the data sync

Open `dashboard/sync_to_cloud.py` and update the top two lines:

```python
CLOUD_URL = "https://your-actual-url.up.railway.app"
API_KEY   = "apex-2026-xK9mLq3rPvWz"   # same as Railway env var
```

Test it manually:
```
python3 "/Users/jackbeard/Claude investment strategy/institutional-tracker/dashboard/sync_to_cloud.py"
```

Should print: `✓ Push successful: 19 keys pushed`

---

## Step 6 — Add to crontab (auto-sync every 6 hours)

Run `crontab -e` in Terminal, press `i`, paste:

```
0 */6 * * * python3 "/Users/jackbeard/Claude investment strategy/institutional-tracker/dashboard/sync_to_cloud.py" >> "/Users/jackbeard/Claude investment strategy/institutional-tracker/dashboard/sync.log" 2>&1
```

Press `Esc`, type `:wq`, hit Enter.

Done. Your dashboard is live 24/7 at a public URL, password-protected,
and updates every 6 hours from your Mac automatically.

---

## Adding/changing users

Update the `USERS` variable in Railway's Variables tab at any time.
Changes apply in seconds — no redeploy needed.

## Updating the dashboard design

Changes to `apex_live_dashboard.html` pushed to GitHub auto-deploy to
Railway within ~60 seconds.
