# X Bookmarks Analysis

Fetch, categorize, and query your X (Twitter) bookmarks via a REST API — queryable from any Claude Project.

## Architecture Overview

```
Mac (first_auth.py)         Railway (FastAPI + SQLite)         Claude Project
        │                           │                                │
        │── python scripts/first_auth.py ──► stores tokens in DB    │
        │                           │                                │
        │                    POST /sync (cron)                       │
        │                    ├── fetch bookmarks (X API)             │
        │                    ├── categorize + summarize (Claude API) │
        │                    └── store in SQLite on volume           │
        │                           │                                │
        │                           │◄── GET /bookmarks/... ────────┤
        │                           │◄── GET /stats, /search ───────┤
```

## First-Time Setup

### Step 1: X Developer Portal

1. Go to [developer.x.com](https://developer.x.com) → sign in → **Developer Portal**
2. In the left sidebar → **Projects & Apps** → **+ New Project**
   - Give it a name (e.g. `bookmarks-analysis`), pick any use case, add a description
   - At the end of project creation an **App** is created inside it — name it (e.g. `x-bookmarks-analysis`)
3. After the app is created you'll see a **Keys and Tokens** page showing a Consumer Key, Consumer Secret, and Bearer Token — **these are OAuth 1.0a credentials, not what we need. You can ignore them.**
4. Click on the app name to open its settings page
5. Find **User Authentication Settings** → click **Set up**
6. Fill in:
   - **Type of App**: `Web App, Automated App or Bot` (this is the Confidential client option)
   - **Callback URI / Redirect URL**: `http://localhost:8080/callback`
   - **Website URL**: `https://github.com/sixftninja/x-bookmarks-analysis`
   - Leave all other fields (Organization, Terms of Service, Privacy Policy) blank
7. Click **Save** — X will now show you a **Client ID** and **Client Secret** (OAuth 2.0 credentials). Copy both immediately; the Client Secret won't be shown again.

> **Note:** Ignore the Toolbox section in the sidebar (Event subscriptions, Webhooks, Connections, Streaming rules) — none of those are needed for this project.

### Step 2: Local Setup

```bash
cd x-bookmarks-analysis
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in:
- `X_CLIENT_ID` — the Client ID from Step 1
- `X_CLIENT_SECRET` — the Client Secret from Step 1
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)
- `SYNC_SECRET` — generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

### Step 3: Generate OAuth Tokens (one-time)

```bash
python scripts/first_auth.py
```

This opens your browser to X's authorization page. Approve the app. Tokens are saved into `./bookmarks.db` and won't need to be regenerated unless you revoke access.

### Step 4: Run First Full Sync Locally

```bash
python scripts/first_sync.py
```

This fetches all your bookmarks from X, categorizes them in batches via Claude, and stores them in `bookmarks.db`. A live progress bar shows elapsed time, estimated time remaining, token usage, and cost. Depending on how many bookmarks you have this may take several minutes.

### Step 5: Deploy to Railway

```bash
git add .
git commit -m "initial build"
git push origin main
```

In the Railway dashboard:
- Go to your `x-bookmarks-analysis` service → **Volumes** tab → **Add Volume** → Mount path: `/app/data`
- Go to **Variables** tab → add all variables from `.env`, but set `DATABASE_URL=/app/data/bookmarks.db`
- Railway will redeploy automatically. Note your Railway URL (e.g. `x-bookmarks-analysis-production.up.railway.app`)

### Step 6: Upload Local DB to Railway

The OAuth tokens and all your bookmarks live in `bookmarks.db`. Copy it to Railway's persistent volume so the deployment can use them:

```bash
npm install -g @railway/cli
railway login
railway link   # select your project and service
railway run cp ./bookmarks.db /app/data/bookmarks.db
```

### Step 7: Set Up Daily Cron Job

In Railway dashboard → your project → **New Service** → **Cron Job**:
- **Command**: `curl -X POST https://{YOUR_RAILWAY_URL}/sync -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"`
- **Schedule**: `0 11 * * *` (11:00 UTC = 06:00 EST)

## Querying from Claude

Create a Claude Project and add to the Project instructions:

```
You have access to my X bookmarks database via a REST API.

Base URL: https://{YOUR_RAILWAY_URL}

Available endpoints:
- GET /stats — bookmark counts per category
- GET /categories — list all category names
- GET /bookmarks/category/{name} — all bookmarks in a category (case-insensitive)
- GET /bookmarks/search?q={term} — search across summaries and content
- GET /bookmarks/recent?n=20 — most recently added bookmarks
- GET /sync/status — when last sync ran and how many bookmarks total

When I ask about my bookmarks, call the relevant endpoint using web fetch and present results clearly. Group by category when showing multiple results. Always show the tweet_url so I can open the original post.
```

## Manual Sync

```bash
curl -X POST https://{YOUR_RAILWAY_URL}/sync \
  -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"
```

Or tell Claude in your Project: *"trigger a sync"* and Claude will call the endpoint.
