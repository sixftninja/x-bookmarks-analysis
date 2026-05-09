# X Bookmarks Analysis

Fetch all your X (Twitter) bookmarks, categorize and summarize them using Claude, store them in a hosted SQLite database, and query them conversationally from any device via a Claude.ai connector.

---

## How It Works

```
Mac (one-time setup)          Railway (FastAPI + SQLite)          Claude.ai (any device)
        │                               │                                  │
        ├─ first_auth.py ──────────────► stores OAuth tokens in DB         │
        ├─ first_sync.py ──────────────► fetches X bookmarks               │
        │                               ├─ categorizes via Claude API      │
        │                               └─ stores in SQLite on volume      │
        │                               │                                  │
        │                        POST /sync (daily cron)                   │
        │                        ├─ fetches new bookmarks only             │
        │                        └─ categorizes + stores incrementally     │
        │                               │                                  │
        │                               │◄── MCP tools (search, edit) ────┤
```

---

## Prerequisites

- Python 3.11+
- A [Railway](https://railway.app) account (Hobby plan, $5/month)
- An [X Developer account](https://developer.x.com) (free)
- An [Anthropic API key](https://console.anthropic.com)
- A [Claude.ai](https://claude.ai) account (Pro plan)

---

## First-Time Setup

### Step 1: X Developer Portal

1. Go to [developer.x.com](https://developer.x.com) → sign in → **Developer Portal**
2. Left sidebar → **Projects & Apps** → **+ New Project**
   - Give it a name (e.g. `bookmarks-analysis`), pick any use case, add a description
   - An app is created inside the project — name it (e.g. `x-bookmarks-analysis`)
3. After the app is created you land on a **Keys and Tokens** page showing Consumer Key, Consumer Secret, and Bearer Token — **these are OAuth 1.0a credentials. Ignore them.**
4. Click the app name to open its settings
5. Find **User Authentication Settings** → click **Set up**
6. Fill in:
   - **Type of App**: `Web App, Automated App or Bot` (Confidential client)
   - **Callback URI / Redirect URL**: `http://localhost:8080/callback`
   - **Website URL**: any valid URL (e.g. your GitHub repo)
   - Leave all other fields blank
7. Click **Save** — you'll now see a **Client ID** and **Client Secret** (OAuth 2.0). Copy both immediately — the Client Secret won't be shown again.

> Ignore the Toolbox section in the sidebar (Event subscriptions, Webhooks, Connections, Streaming rules) — none of those are needed.

---

### Step 2: Local Setup

```bash
git clone https://github.com/sixftninja/x-bookmarks-analysis.git
cd x-bookmarks-analysis
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in:

```
X_CLIENT_ID=        # from Step 1
X_CLIENT_SECRET=    # from Step 1
ANTHROPIC_API_KEY=  # from console.anthropic.com
SYNC_SECRET=        # generate: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
DATABASE_URL=./bookmarks.db
```

---

### Step 3: Generate OAuth Tokens (one-time)

```bash
python scripts/first_auth.py
```

This opens your browser to X's authorization page. Approve the app. Tokens are stored in `./bookmarks.db` and persist indefinitely via automatic refresh — you won't need to do this again.

---

### Step 4: Run First Sync Locally

```bash
python scripts/first_sync.py
```

This fetches all your bookmarks from X, categorizes and summarizes them in batches of 25 via Claude, and stores everything in `bookmarks.db`. A live progress bar shows elapsed time, ETA, token usage, and cost.

**Cost estimate:** roughly $1–2 per 500 bookmarks using `claude-sonnet-4-20250514` ($3/MTok input, $15/MTok output). The first run processes everything; subsequent syncs only process new bookmarks.

---

### Step 5: Deploy to Railway

**5a. Push code to GitHub**

```bash
git add .
git commit -m "initial build"
git push origin main
```

**5b. Set up Railway service**

1. Go to [railway.app](https://railway.app) → create a new project
2. Add a new service → **GitHub Repository** → select `x-bookmarks-analysis`
3. Railway will auto-detect the Dockerfile and start building

**5c. Add a persistent volume**

On the canvas, click **+** → the volume option → set mount path to `/app/data`

**5d. Set environment variables**

In the service → **Variables** tab, add all values from your `.env` file, but change:
```
DATABASE_URL=/app/data/bookmarks.db
```

**5e. Generate a public URL**

In **Settings** → **Networking** → click **Generate Domain**. Note your URL (e.g. `x-bookmarks-analysis-production.up.railway.app`).

---

### Step 6: Upload Your Database to Railway

Your `bookmarks.db` contains both the OAuth tokens and all your categorized bookmarks. Upload it to the Railway volume:

```bash
npm install -g @railway/cli
railway login
railway link   # select your project and service
curl -X POST https://{YOUR_RAILWAY_URL}/sync/upload-db \
  -H "X-Sync-Secret: {YOUR_SYNC_SECRET}" \
  -F "file=@./bookmarks.db"
```

You should get back `{"status":"ok","path":"/app/data/bookmarks.db","size_bytes":...}`.

Verify it worked:
```bash
curl https://{YOUR_RAILWAY_URL}/stats
```

You should see a JSON list of your categories with bookmark counts.

---

### Step 7: Set Up Daily Cron Job

On the Railway canvas, click **+** → **Empty Service** → go to its **Settings** → **Deploy** section:

- **Custom Start Command**: `curl -X POST https://{YOUR_RAILWAY_URL}/sync -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"`
- **Cron Schedule**: select **Custom** → enter `0 11 * * *` (11:00 UTC = 6:00 AM EST daily)

---

### Step 8: Connect to Claude.ai

1. Go to [claude.ai](https://claude.ai) → **Settings** → **Connectors** → **Add custom connector**
2. Fill in:
   - **Name**: `X Bookmarks`
   - **Remote MCP server URL**: `https://{YOUR_RAILWAY_URL}/mcp/sse`
3. Click **Add**

You'll see 12 tools registered under the connector. The connector is available in all Claude.ai conversations and projects, on any device.

---

## Using the Connector

Start any Claude.ai conversation and ask naturally:

- *"What are my most bookmarked categories?"*
- *"Show me bookmarks about AI agents"*
- *"Search my bookmarks for MCP"*
- *"Show me recent bookmarks"*
- *"When was the last sync and how many bookmarks do I have?"*

### Managing Your Bookmarks

You can edit your bookmark database directly from the chat:

- *"Rename category 'External Links' to 'Unread Articles'"*
- *"Merge 'Link Shares' and 'Link-Only Posts' into 'Bare Links'"*
- *"Move tweet IDs 123, 456, 789 to a new category called 'AI Infrastructure'"*
- *"Delete the category 'General Links'"*
- *"Delete bookmark with tweet ID 1234567890"*
- *"Edit the summary of tweet ID 123 to say '...'"*

---

## Manual Sync

Trigger a sync at any time without waiting for the cron:

```bash
curl -X POST https://{YOUR_RAILWAY_URL}/sync \
  -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"
```

Or just tell Claude in chat: *"trigger a sync"*.

---

## API Endpoints

The Railway service exposes these REST endpoints (all return JSON):

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| GET | `/stats` | Bookmark counts per category |
| GET | `/categories` | All category names |
| GET | `/bookmarks/category/{name}` | Bookmarks in a category (case-insensitive) |
| GET | `/bookmarks/search?q={term}` | Search summaries and full content |
| GET | `/bookmarks/recent?n=20` | Most recently categorized bookmarks |
| GET | `/sync/status` | Last sync time and total bookmark count |
| POST | `/sync` | Trigger a sync (requires `X-Sync-Secret` header) |
| POST | `/sync/upload-db` | Upload a local `bookmarks.db` to the volume (requires `X-Sync-Secret` header) |

---

## MCP Tools

The `/mcp/sse` endpoint exposes these tools to Claude.ai:

**Read**
- `get_bookmark_stats` — counts per category
- `get_categories` — all category names
- `get_bookmarks_by_category(category, limit)` — bookmarks in a category
- `search_bookmarks(query, limit)` — keyword search
- `get_recent_bookmarks(n)` — most recently added
- `get_sync_status` — last sync info and total count

**Write**
- `rename_category(old_name, new_name)` — rename a category
- `move_bookmarks(tweet_ids, new_category)` — move bookmarks to any category
- `merge_categories(source, target)` — merge two categories
- `delete_bookmarks(tweet_ids)` — delete specific bookmarks
- `delete_category(category)` — delete all bookmarks in a category
- `edit_bookmark(tweet_id, category, summary)` — edit a specific bookmark
