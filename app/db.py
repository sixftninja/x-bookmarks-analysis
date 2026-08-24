import json
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB = os.getenv("DATABASE_URL", "./bookmarks.db")

CREATE_BOOKMARKS = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT UNIQUE NOT NULL,
    author_username TEXT,
    author_name TEXT,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    media_urls TEXT,
    tweet_url TEXT,
    bookmarked_at TEXT,
    categorized_at TEXT DEFAULT (datetime('now')),
    quoted_from_tweet_id TEXT,
    tags TEXT,
    post_text TEXT
);
"""

# Columns added after the original schema shipped. init_db() adds these to
# already-existing tables via ALTER TABLE, since CREATE TABLE IF NOT EXISTS
# is a no-op on a table that already exists.
_ADDED_COLUMNS = [
    ("quoted_from_tweet_id", "TEXT"),
    ("tags", "TEXT"),  # JSON array of strings, e.g. '["Open Source", "Cloud Hosting & Inference Costs"]'
    ("post_text", "TEXT"),  # verbatim short (<40-300 word) post text; NULL when the row's own summary covers it instead
]

# full_content, content_source, and image_processing_status are dead —
# full_content just mirrored post_text, and the other two only ever held
# static defaults, once every row has gone through the current pipeline
# (see scripts/backfill_new_pipeline.py, which every production row has
# been through as of the migration that removes these). Native
# ALTER TABLE ... DROP COLUMN (SQLite 3.35+) rather than a table rebuild.
_DROPPED_COLUMNS = ["full_content", "content_source", "image_processing_status"]

CREATE_SYNC_LOG = """
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT DEFAULT (datetime('now')),
    new_bookmarks_added INTEGER,
    status TEXT,
    error_message TEXT
);
"""

CREATE_OAUTH_TOKENS = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    scope TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_category ON bookmarks(category);",
    "CREATE INDEX IF NOT EXISTS idx_tweet_id ON bookmarks(tweet_id);",
]


def _migrate_bookmarks_columns(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bookmarks)")}

    # quoted_tweet_id (forward pointer: "I quote X") is being replaced by
    # quoted_from_tweet_id (backward pointer: "I was quoted by X", living on
    # the derived row) — rename in place rather than add a redundant column.
    if "quoted_tweet_id" in existing and "quoted_from_tweet_id" not in existing:
        conn.execute("ALTER TABLE bookmarks RENAME COLUMN quoted_tweet_id TO quoted_from_tweet_id")
        existing.discard("quoted_tweet_id")
        existing.add("quoted_from_tweet_id")

    for name, col_type in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE bookmarks ADD COLUMN {name} {col_type}")


def _migrate_drop_dead_columns(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bookmarks)")}
    for name in _DROPPED_COLUMNS:
        if name in existing:
            conn.execute(f"ALTER TABLE bookmarks DROP COLUMN {name}")


def init_db(db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_BOOKMARKS)
        conn.execute(CREATE_SYNC_LOG)
        conn.execute(CREATE_OAUTH_TOKENS)
        _migrate_bookmarks_columns(conn)
        _migrate_drop_dead_columns(conn)
        for idx in CREATE_INDEXES:
            conn.execute(idx)
        conn.commit()


def insert_bookmarks(bookmarks, db_path=DEFAULT_DB):
    if not bookmarks:
        return 0
    sql = """
        INSERT OR IGNORE INTO bookmarks
            (tweet_id, author_username, author_name, category, summary,
             media_urls, tweet_url, bookmarked_at,
             quoted_from_tweet_id, tags, post_text)
        VALUES
            (:tweet_id, :author_username, :author_name, :category, :summary,
             :media_urls, :tweet_url, :bookmarked_at,
             :quoted_from_tweet_id, :tags, :post_text)
    """
    defaults = {
        "quoted_from_tweet_id": None,
        "tags": "[]",
        "post_text": None,
    }
    rows = [{**defaults, **b} for b in bookmarks]
    with sqlite3.connect(db_path) as conn:
        cursor = conn.executemany(sql, rows)
        conn.commit()
        return cursor.rowcount


def get_all_tags(db_path=DEFAULT_DB):
    """Every tag currently in use across all bookmarks, deduped and sorted.
    This — not any separate registry — is the live tag vocabulary."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT tags FROM bookmarks WHERE tags IS NOT NULL").fetchall()
    tags = set()
    for (raw,) in rows:
        try:
            tags.update(json.loads(raw) or [])
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(tags)


def get_existing_tweet_ids(db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT tweet_id FROM bookmarks").fetchall()
    return {row[0] for row in rows}


def get_categories(db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM bookmarks ORDER BY category"
        ).fetchall()
    return [row[0] for row in rows]


def get_stats(db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) as count FROM bookmarks GROUP BY category ORDER BY count DESC"
        ).fetchall()
    return [{"category": row[0], "count": row[1]} for row in rows]


def log_sync(new_count, status, error_message, db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sync_log (new_bookmarks_added, status, error_message) VALUES (?, ?, ?)",
            (new_count, status, error_message),
        )
        conn.commit()


def get_last_sync(db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT synced_at, new_bookmarks_added, status, error_message FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {
        "synced_at": row[0],
        "new_bookmarks_added": row[1],
        "status": row[2],
        "error_message": row[3],
    }
