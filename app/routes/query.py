from fastapi import APIRouter, HTTPException, Query
from app.db import get_stats, get_categories, get_last_sync
import sqlite3
import os

router = APIRouter()


def _db():
    return os.getenv("DATABASE_URL", "./bookmarks.db")


def _row_to_dict(row, cursor):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


@router.get("/")
def health():
    return {"status": "ok", "service": "x-bookmarks-analysis"}


@router.get("/stats")
def stats():
    return get_stats(_db())


@router.get("/categories")
def categories():
    return get_categories(_db())


@router.get("/bookmarks/category/{name}")
def bookmarks_by_category(name: str, limit: int = Query(default=50, le=200)):
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, tweet_id, author_username, author_name, category,
                      summary, full_content, tweet_url, bookmarked_at
               FROM bookmarks
               WHERE LOWER(category) = LOWER(?)
               ORDER BY bookmarked_at DESC
               LIMIT ?""",
            (name, limit),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"Category '{name}' not found")
    return [dict(row) for row in rows]


@router.get("/bookmarks/search")
def search_bookmarks(q: str, limit: int = Query(default=20, le=100)):
    term = f"%{q}%"
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, tweet_id, author_username, author_name, category,
                      summary, full_content, tweet_url, bookmarked_at
               FROM bookmarks
               WHERE summary LIKE ? OR full_content LIKE ?
               ORDER BY bookmarked_at DESC
               LIMIT ?""",
            (term, term, limit),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/bookmarks/recent")
def recent_bookmarks(n: int = Query(default=20, le=100)):
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, tweet_id, author_username, author_name, category,
                      summary, full_content, tweet_url, bookmarked_at
               FROM bookmarks
               ORDER BY categorized_at DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/sync/status")
def sync_status():
    last = get_last_sync(_db())
    with sqlite3.connect(_db()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
    return {
        "last_sync_at": last["synced_at"] if last else None,
        "new_bookmarks_added": last["new_bookmarks_added"] if last else None,
        "status": last["status"] if last else None,
        "total_bookmarks": total,
    }
