# X Bookmarks Analysis

I built this to automatically fetch, categorize, and search my X bookmarks using AI. Every bookmark gets a category and a ~128 word summary written by Claude. I can search, filter, and manage everything conversationally from any device.

---

## What You Need

- Python 3.11+
- [X Developer account](https://developer.x.com) (free)
- [Anthropic API key](https://console.anthropic.com)

---

## Setup

### 1. X Developer Portal

1. Go to [developer.x.com](https://developer.x.com) → sign in → **Projects & Apps** → **+ New Project**
2. Give it a name, pick any use case, and create an app inside it
3. You'll see a Keys page with Consumer Key/Secret — **ignore these, they're the wrong credential type**
4. Click the app name → **User Authentication Settings** → **Set up**
5. Fill in:
   - **Type of App**: `Web App, Automated App or Bot`
   - **Callback URI**: `http://localhost:8080/callback`
   - **Website URL**: any valid URL
6. Save — you'll now see a **Client ID** and **Client Secret**. Copy both immediately.

### 2. Local Setup

```bash
git clone https://github.com/sixftninja/x-bookmarks-analysis.git
cd x-bookmarks-analysis
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
X_CLIENT_ID=         # from step 1
X_CLIENT_SECRET=     # from step 1
ANTHROPIC_API_KEY=   # from console.anthropic.com
SYNC_SECRET=         # run: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
DATABASE_URL=./bookmarks.db
```

### 3. Authorize with X (one-time)

```bash
python scripts/first_auth.py
```

Opens your browser. Approve the app. Tokens are saved locally and refresh automatically — you won't need to do this again.

### 4. Sync Your Bookmarks

```bash
python scripts/sync.py
```

Fetches all your bookmarks from X, categorizes and summarizes them via Claude. Shows a live progress bar with time elapsed, ETA, and cost.

**Cost:** roughly $1–2 for 500 bookmarks (`claude-sonnet-4`, $3/MTok input · $15/MTok output). Re-running only processes new bookmarks — incremental syncs are cheap.

Run this script any time you want to pull in new bookmarks.

---

## Using Your Bookmarks with AI

After syncing, everything lives in `bookmarks.db`. Here's how to use it with different AI tools.

### Any chatbot — ChatGPT, Gemini, Grok, Claude, etc.

Export your bookmarks to a JSON file:

```bash
python scripts/export.py
```

This creates `bookmarks_export.json`. Upload it to any AI chat and ask questions like:
- *"What topics do I bookmark most?"*
- *"Find bookmarks about AI agents"*
- *"Summarize everything in the robotics category"*

### Claude Projects

Upload `bookmarks_export.json` as a file inside a [Claude Project](https://claude.ai). The file persists across all conversations in that project — you don't have to re-upload every time.

### Open source / local models

Load `bookmarks_export.json` into your RAG pipeline of choice. Each record has: `tweet_id`, `author_username`, `category`, `summary`, `full_content`, `tweet_url`, `bookmarked_at`.

---

## Deploying for Automation (Optional)

Everything above works fully locally. Deploy if you want automatic daily syncs and a live API — and if you want to use the Claude.ai MCP connector to query your bookmarks from any device without uploading files.

### What I use: Railway

1. Fork this repo and push your code to GitHub
2. Go to [railway.app](https://railway.app) → new project → **GitHub Repository** → select your fork
3. On the canvas, add a **Volume** → set mount path to `/app/data`
4. In the service → **Variables** tab, add all your `.env` values, but set `DATABASE_URL=/app/data/bookmarks.db`
5. In **Settings** → **Networking** → **Generate Domain** — note your URL
6. Upload your local database to the Railway volume:

```bash
curl -X POST https://{YOUR_RAILWAY_URL}/sync/upload-db \
  -H "X-Sync-Secret: {YOUR_SYNC_SECRET}" \
  -F "file=@./bookmarks.db"
```

7. Verify: `curl https://{YOUR_RAILWAY_URL}/stats` should return your categories
8. Add a cron job: on the canvas → **+** → **Empty Service** → **Settings** → **Deploy**:
   - **Start Command**: `curl -X POST https://{YOUR_RAILWAY_URL}/sync -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"`
   - **Schedule**: Custom → `0 11 * * *` (daily at 11:00 UTC)

### Other Platforms

| Platform | Notes |
|----------|-------|
| Render, Fly.io | Work with this repo as-is — nearly identical setup to Railway |
| Any VPS / Docker | Mount a volume at `/app/data`, set `DATABASE_URL=/app/data/bookmarks.db` |
| Vercel | Won't work — no persistent filesystem. Would require replacing SQLite with a hosted DB (e.g. Turso, Neon) |

---

## Claude.ai MCP Connector (if deployed)

This gives you a live connection to your bookmark database from any device — no file uploads needed. Claude can search, filter, and edit your bookmarks in any conversation.

1. [claude.ai](https://claude.ai) → **Settings** → **Connectors** → **Add custom connector**
2. **Name**: `X Bookmarks`
3. **Remote MCP server URL**: `https://{YOUR_RAILWAY_URL}/mcp/sse`

You can then ask Claude things like:
- *"What are my most bookmarked topics?"*
- *"Search my bookmarks for MCP servers"*
- *"Rename category 'External Links' to 'Unread Articles'"*
- *"Merge 'Link Shares' and 'Link-Only Posts' into 'Bare Links'"*
- *"Move these bookmarks to a new category called 'AI Infrastructure'"*

**Available tools:** `get_bookmark_stats`, `get_categories`, `get_bookmarks_by_category`, `search_bookmarks`, `get_recent_bookmarks`, `get_sync_status`, `rename_category`, `move_bookmarks`, `merge_categories`, `delete_bookmarks`, `delete_category`, `edit_bookmark`

---

## API Reference

| Endpoint | Description |
|----------|-------------|
| `GET /stats` | Bookmark counts per category |
| `GET /categories` | All category names |
| `GET /bookmarks/category/{name}` | Bookmarks in a category (case-insensitive) |
| `GET /bookmarks/search?q={term}` | Keyword search across summaries and full text |
| `GET /bookmarks/recent?n=20` | Most recently categorized bookmarks |
| `GET /sync/status` | Last sync time and total count |
| `POST /sync` | Trigger a sync (`X-Sync-Secret` header required) |
| `POST /sync/upload-db` | Upload a local DB to the volume (`X-Sync-Secret` header required) |
