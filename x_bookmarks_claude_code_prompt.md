# Claude Code Prompt: X Bookmarks Analysis

---

## Context

Build a complete X (Twitter) bookmarks analysis system. The project has two parts:

1. **A local Mac pipeline** (`/Users/anand/Desktop/x-bookmarks-analysis`) — fetches bookmarks from X API, categorizes them via Claude API, stores them in a hosted SQLite database on Railway.
2. **A FastAPI app** deployed on Railway — hosts the SQLite database on a persistent volume, exposes read endpoints for Claude to query from anywhere, and exposes a protected `/sync` endpoint that triggers the full pipeline on a schedule.

The GitHub repo is `https://github.com/sixftninja/x-bookmarks-analysis`. All code goes in this single repo. Railway deploys the FastAPI app from this repo automatically on every push.

---

## Project Structure

```
/Users/anand/Desktop/x-bookmarks-analysis/
├── .env                        # Local secrets (gitignored)
├── .env.example                # Template with all required keys (committed)
├── .gitignore
├── requirements.txt
├── README.md
├── Dockerfile                  # For Railway deployment
├── railway.json                # Railway config
│
├── app/                        # FastAPI application (runs on Railway)
│   ├── main.py                 # FastAPI app entrypoint
│   ├── db.py                   # SQLite helpers (read + write)
│   ├── routes/
│   │   ├── query.py            # GET endpoints for Claude to call
│   │   └── sync.py             # POST /sync endpoint (protected)
│   └── pipeline/
│       ├── auth.py             # OAuth 2.0 PKCE flow for X API
│       ├── fetch.py            # Fetch bookmarks from X API
│       └── categorize.py       # Categorize + summarize via Claude API
│
└── scripts/
    └── first_auth.py           # One-time interactive OAuth flow (run locally)
```

---

## Environment Variables

Generate the `.env` file with all required keys. For `SYNC_SECRET`, generate it programmatically:

```python
import secrets
print(secrets.token_urlsafe(32))
```

The `.env` file should contain:

```
# X API (OAuth 2.0 confidential client)
X_CLIENT_ID=
X_CLIENT_SECRET=

# Anthropic
ANTHROPIC_API_KEY=

# Sync endpoint protection
SYNC_SECRET=<generate using secrets.token_urlsafe(32)>

# Database path (Railway uses /app/data/bookmarks.db, local uses ./bookmarks.db)
DATABASE_URL=./bookmarks.db
```

The `.env.example` should contain all the same keys with empty values and comments explaining where to get each one. Commit `.env.example`, never commit `.env`.

---

## Database Schema (SQLite)

The database file lives at the path specified by `DATABASE_URL` env var. On Railway this will be `/app/data/bookmarks.db` (persistent volume). Locally it's `./bookmarks.db`.

```sql
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT UNIQUE NOT NULL,
    author_username TEXT,
    author_name TEXT,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,          -- ~128 word summary
    full_content TEXT NOT NULL,     -- full tweet text
    media_urls TEXT,                -- JSON array or NULL
    tweet_url TEXT,
    bookmarked_at TEXT,
    categorized_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_category ON bookmarks(category);
CREATE INDEX IF NOT EXISTS idx_tweet_id ON bookmarks(tweet_id);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT DEFAULT (datetime('now')),
    new_bookmarks_added INTEGER,
    status TEXT,                    -- 'success' or 'error'
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    scope TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
```

OAuth tokens are stored in the database itself (not in a file), so the Railway deployment has persistent access to them across restarts.

---

## Module Details

### scripts/first_auth.py — One-Time Interactive OAuth Setup

This is run **once, locally on the Mac**, to generate and store OAuth tokens. It must never run on Railway.

- Implements OAuth 2.0 Authorization Code Flow with PKCE.
- App type is **confidential client** — uses both `X_CLIENT_ID` and `X_CLIENT_SECRET`.
- Required scopes: `tweet.read users.read bookmark.read offline.access`
- Opens the user's browser to X's authorization URL.
- Spins up a temporary local HTTP server on `http://localhost:8080/callback` to catch the redirect.
- Exchanges the auth code + PKCE verifier for access token + refresh token.
- Stores tokens in the `oauth_tokens` table in the local SQLite DB.
- After running this once locally, the `bookmarks.db` file (with tokens inside) needs to be uploaded to Railway by triggering a deploy — see README instructions.
- Prints clear success/failure messages.

### app/pipeline/auth.py — Token Management

- `get_valid_access_token(db_path)` — reads tokens from `oauth_tokens` table, checks expiry, refreshes if needed using `X_CLIENT_ID` + `X_CLIENT_SECRET` + refresh token against `https://api.x.com/2/oauth2/token`, saves updated tokens back to DB, returns a valid access token.
- Never does the interactive browser flow — that's only in `scripts/first_auth.py`.
- If tokens are missing entirely, raises a clear exception: "OAuth tokens not found. Run scripts/first_auth.py locally first."

### app/pipeline/fetch.py — Fetch Bookmarks from X API

- Uses `GET /2/users/{user_id}/bookmarks` endpoint.
- First calls `GET /2/users/me` to get the authenticated user's numeric ID.
- Request parameters:
  - `tweet.fields`: `text,author_id,created_at,entities,attachments`
  - `expansions`: `author_id`
  - `user.fields`: `username,name`
  - `max_results`: `100` (maximum per page)
- Paginates using `next_token` / `pagination_token` until all bookmarks are fetched OR until all returned tweet_ids are already in the DB (incremental mode stop condition).
- For each tweet, extracts:
  - `tweet_id`: the tweet's ID string
  - `author_username`: from the expanded author object
  - `author_name`: from the expanded author object
  - `full_content`: the tweet's `text` field
  - `media_urls`: extracted from `entities.urls` where `expanded_url` contains images/video, as a JSON array string. NULL if none.
  - `tweet_url`: constructed as `https://x.com/{author_username}/status/{tweet_id}`
  - `bookmarked_at`: from `created_at` field
- Handles rate limiting: if 429 received, waits for `Retry-After` header value (or 60 seconds default) and retries.
- Handles auth errors: if 401 received, attempts one token refresh then retries. If still 401, raises clearly.
- Prints progress: `Fetched page 1 (100 bookmarks)...`, `Fetched page 2 (200 bookmarks)...` etc.
- Takes an optional `existing_tweet_ids: set` parameter. In incremental mode, stops paginating when a full page consists entirely of already-known IDs.
- Returns a list of dicts.

### app/pipeline/categorize.py — Categorize and Summarize via Claude API

- Uses the official `anthropic` Python SDK.
- Model: `claude-sonnet-4-20250514`
- Processes bookmarks in **batches of 25**.
- Two modes based on whether existing categories are provided:

**First run (no existing categories):**

System prompt:
```
You are analyzing a collection of bookmarked posts from X (Twitter). Your job is to:
1. Identify meaningful, specific categories that reflect the actual content themes
2. Assign each post to exactly one category
3. Write a ~128 word summary for each post capturing its key insight or argument

Rules:
- Create 5-15 categories maximum, depending on content diversity
- Category names must be specific (e.g. "AI Safety Research", "Startup GTM Strategy") not generic (e.g. "Interesting", "Tech", "Other")
- Summaries must capture what the post actually argues or reveals, not just describe it
- Return ONLY a valid JSON array. No markdown, no explanation, no code fences.
```

User prompt per batch:
```
Categorize these bookmarked posts. Return a JSON array where each object has exactly these keys:
- tweet_id (string, unchanged from input)
- category (string)
- summary (string, ~128 words)

Posts:
[{tweet_id: "...", content: "...", author: "..."}, ...]
```

**Incremental run (existing categories provided):**

Same system prompt but with an additional rule:
```
- You have these existing categories: [list]. Assign to existing categories where possible. Only create a new category if the content genuinely doesn't fit any existing one.
```

- Parses the JSON response. If parsing fails, logs the raw response and retries once with an explicit "fix the JSON" follow-up message.
- Merges categorized results back with the original fetched data (tweet_id is the join key).
- Prints progress: `Categorizing batch 1/4 (25 posts)...` etc.
- Returns a list of complete bookmark dicts ready for DB insertion.

### app/db.py — Database Helpers

All functions accept `db_path` as a parameter (defaults to `os.getenv("DATABASE_URL", "./bookmarks.db")`).

- `init_db(db_path)` — creates all tables and indexes if they don't exist.
- `insert_bookmarks(bookmarks, db_path)` — bulk insert using `INSERT OR IGNORE` on `tweet_id`. Returns count of actually inserted rows.
- `get_existing_tweet_ids(db_path) -> set` — returns all tweet_ids in DB.
- `get_categories(db_path) -> list[str]` — returns distinct category names, sorted alphabetically.
- `get_stats(db_path) -> list[dict]` — returns `[{category, count}]` sorted by count descending.
- `log_sync(new_count, status, error_message, db_path)` — inserts a row into `sync_log`.
- `get_last_sync(db_path) -> dict` — returns the most recent sync_log entry.

### app/routes/query.py — Read Endpoints for Claude

All endpoints are public (no auth required — the data is not sensitive and the Railway URL is not advertised). All responses return JSON.

```
GET /                          → {"status": "ok", "service": "x-bookmarks-analysis"}

GET /stats                     → [{category: str, count: int}, ...]

GET /categories                → [str, ...]  (list of category names)

GET /bookmarks/category/{name} → [{id, tweet_id, author_username, author_name, category,
                                    summary, full_content, tweet_url, bookmarked_at}, ...]
                                 Supports ?limit=N (default 50, max 200)

GET /bookmarks/search?q={term} → Same shape as above, searches across summary + full_content
                                 Supports ?limit=N (default 20, max 100)

GET /bookmarks/recent?n={N}    → Most recently categorized bookmarks (default 20, max 100)

GET /sync/status               → {last_sync_at, new_bookmarks_added, status, total_bookmarks}
```

For `/bookmarks/category/{name}`, do a **case-insensitive match** so `AI` and `ai` both work.

For `/bookmarks/search`, search using SQL `LIKE '%term%'` across both `summary` and `full_content` columns (OR condition).

### app/routes/sync.py — Protected Sync Endpoint

```
POST /sync
Headers: X-Sync-Secret: {SYNC_SECRET}
```

- Returns 401 if `X-Sync-Secret` header is missing or doesn't match `SYNC_SECRET` env var.
- Runs the full pipeline asynchronously (use `asyncio` + `run_in_executor` so it doesn't block):
  1. `get_valid_access_token()`
  2. `get_existing_tweet_ids()`
  3. `fetch_bookmarks(existing_ids=existing_ids)` — incremental
  4. If no new bookmarks: log sync with 0 new, return `{"status": "ok", "new_bookmarks": 0, "message": "No new bookmarks found"}`
  5. `get_categories()` — pass to categorize for incremental mode
  6. `categorize_bookmarks(new_bookmarks, existing_categories)`
  7. `insert_bookmarks(categorized)`
  8. `log_sync(count, "success", None)`
  9. Return `{"status": "ok", "new_bookmarks": N, "categories_used": [...]}`
- On any exception: `log_sync(0, "error", str(e))` and return 500 with error detail.
- This endpoint is called by the Railway Cron Job daily at 06:00 EST (11:00 UTC).

### app/main.py — FastAPI Entrypoint

```python
from fastapi import FastAPI
from app.routes import query, sync
from app.db import init_db
import os

app = FastAPI(title="X Bookmarks Analysis API")

@app.on_event("startup")
async def startup():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    init_db(db_path)

app.include_router(query.router)
app.include_router(sync.router)
```

Runs on port 8080 (Railway default). Use `uvicorn app.main:app --host 0.0.0.0 --port 8080`.

---

## Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway mounts the persistent volume at /app/data
# The DATABASE_URL env var should be set to /app/data/bookmarks.db in Railway
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## railway.json

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE"
  },
  "deploy": {
    "startCommand": "uvicorn app.main:app --host 0.0.0.0 --port 8080",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 3
  }
}
```

---

## requirements.txt

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
httpx==0.27.0
anthropic==0.28.0
python-dotenv==1.0.1
```

---

## .gitignore

```
.env
tokens.json
bookmarks.db
__pycache__/
*.pyc
.DS_Store
```

---

## README.md

The README must cover these sections clearly:

### 1. Architecture Overview
Briefly explain: Mac pipeline script → POST /sync on Railway → Railway fetches X API + calls Claude API → stores in SQLite on Railway volume → Claude (in any Project conversation) → GET endpoints on Railway.

### 2. First-Time Setup

**Step 1: X Developer Portal configuration**
- Go to developer.x.com → your `bookmarks-analysis` app → User Authentication Settings → Edit
- Set App Type to: **Confidential client**
- Set Callback URI to: `http://localhost:8080/callback`
- Set Website URL to: `https://github.com/sixftninja/x-bookmarks-analysis`
- Enable OAuth 2.0
- Save. Copy your Client ID and Client Secret into `.env`

**Step 2: Local setup**
```bash
cd /Users/anand/Desktop/x-bookmarks-analysis
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your keys
```

**Step 3: Generate OAuth tokens (one-time)**
```bash
python scripts/first_auth.py
```
This opens your browser. Authorize the app. Tokens are saved to `./bookmarks.db`.

**Step 4: Run the first full sync locally to verify**
```bash
# Set DATABASE_URL=./bookmarks.db in .env for local run
python -c "
from app.pipeline.fetch import fetch_bookmarks
from app.pipeline.categorize import categorize_bookmarks
from app.db import init_db, get_existing_tweet_ids, insert_bookmarks, get_categories, log_sync
import os
db = os.getenv('DATABASE_URL', './bookmarks.db')
init_db(db)
existing = get_existing_tweet_ids(db)
tweets = fetch_bookmarks(existing_tweet_ids=existing)
print(f'Fetched {len(tweets)} new bookmarks')
cats = get_categories(db)
categorized = categorize_bookmarks(tweets, existing_categories=cats)
count = insert_bookmarks(categorized, db)
log_sync(count, 'success', None, db)
print(f'Done. {count} bookmarks stored.')
"
```

**Step 5: Deploy to Railway**
```bash
git add .
git commit -m "initial build"
git push origin main
```

Then in Railway dashboard:
- Go to your `x-bookmarks-analysis` service → **Volumes** tab → **Add Volume** → Mount path: `/app/data`
- Go to **Variables** tab → add all variables from `.env` but set `DATABASE_URL=/app/data/bookmarks.db`
- Railway will redeploy automatically. Note your Railway-provided URL (e.g. `x-bookmarks-analysis-production.up.railway.app`)

**Step 6: Upload your local DB to Railway**
Since tokens are stored in `bookmarks.db`, you need to copy your local DB to Railway's volume. The easiest way: use the Railway CLI.
```bash
npm install -g @railway/cli
railway login
railway link  # select your project and service
railway run cp ./bookmarks.db /app/data/bookmarks.db
```

**Step 7: Set up the daily cron job**
In Railway dashboard → your project → **New Service** → **Cron Job**:
- Command: `curl -X POST https://{YOUR_RAILWAY_URL}/sync -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"`
- Schedule: `0 11 * * *` (11:00 UTC = 06:00 EST)

### 3. Querying from Claude

Create a Claude Project and add this to the Project instructions:

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

### 4. Manual Sync
To trigger a sync manually at any time (without waiting for the cron):
```bash
curl -X POST https://{YOUR_RAILWAY_URL}/sync \
  -H "X-Sync-Secret: {YOUR_SYNC_SECRET}"
```
Or simply tell Claude in your Project: "trigger a sync" and Claude will call the endpoint.

---

## Implementation Notes

- Use `python-dotenv` to load `.env` in all modules with `load_dotenv()` at the top.
- All database connections should use `with sqlite3.connect(db_path) as conn:` pattern (auto-commit + auto-close).
- The `DATABASE_URL` env var controls where the DB lives. Locally it's `./bookmarks.db`. On Railway it's `/app/data/bookmarks.db`. This single variable is the only difference between local and production.
- FastAPI should return proper HTTP status codes: 200 for success, 401 for bad sync secret, 404 for category not found, 500 for pipeline errors.
- All pipeline functions should print progress to stdout — Railway captures this in deployment logs.
- `first_auth.py` must work completely standalone — it should not import from `app/` to avoid any Railway/production concerns. It uses `httpx` directly and writes to SQLite directly.
- Do not use any async database libraries — plain `sqlite3` is sufficient and simpler.
- After building all files, run a syntax check: `python -m py_compile app/main.py app/db.py app/routes/query.py app/routes/sync.py app/pipeline/auth.py app/pipeline/fetch.py app/pipeline/categorize.py scripts/first_auth.py` and fix any errors before finishing.
