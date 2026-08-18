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
    full_content TEXT NOT NULL,
    media_urls TEXT,
    tweet_url TEXT,
    bookmarked_at TEXT,
    categorized_at TEXT DEFAULT (datetime('now')),
    content_source TEXT,
    image_processing_status TEXT,
    quoted_tweet_id TEXT
);
"""

# Columns added after the original schema shipped. init_db() adds these to
# already-existing tables via ALTER TABLE, since CREATE TABLE IF NOT EXISTS
# is a no-op on a table that already exists.
_ADDED_COLUMNS = [
    ("content_source", "TEXT"),
    ("image_processing_status", "TEXT"),
    ("quoted_tweet_id", "TEXT"),
]

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
    for name, col_type in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE bookmarks ADD COLUMN {name} {col_type}")


def init_db(db_path=DEFAULT_DB):
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_BOOKMARKS)
        conn.execute(CREATE_SYNC_LOG)
        conn.execute(CREATE_OAUTH_TOKENS)
        _migrate_bookmarks_columns(conn)
        for idx in CREATE_INDEXES:
            conn.execute(idx)
        conn.commit()


def insert_bookmarks(bookmarks, db_path=DEFAULT_DB):
    if not bookmarks:
        return 0
    sql = """
        INSERT OR IGNORE INTO bookmarks
            (tweet_id, author_username, author_name, category, summary,
             full_content, media_urls, tweet_url, bookmarked_at,
             content_source, image_processing_status, quoted_tweet_id)
        VALUES
            (:tweet_id, :author_username, :author_name, :category, :summary,
             :full_content, :media_urls, :tweet_url, :bookmarked_at,
             :content_source, :image_processing_status, :quoted_tweet_id)
    """
    defaults = {"content_source": "api_text", "image_processing_status": "no_images_found", "quoted_tweet_id": None}
    rows = [{**defaults, **b} for b in bookmarks]
    with sqlite3.connect(db_path) as conn:
        cursor = conn.executemany(sql, rows)
        conn.commit()
        return cursor.rowcount


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
