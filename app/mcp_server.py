import os
import sqlite3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("X Bookmarks")


def _db():
    return os.getenv("DATABASE_URL", "./bookmarks.db")


@mcp.tool()
def get_bookmark_stats() -> list[dict]:
    """Get the count of bookmarks in each category, sorted by count descending."""
    with sqlite3.connect(_db()) as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) as count FROM bookmarks GROUP BY category ORDER BY count DESC"
        ).fetchall()
    return [{"category": row[0], "count": row[1]} for row in rows]


@mcp.tool()
def get_categories() -> list[str]:
    """Get all bookmark category names, sorted alphabetically."""
    with sqlite3.connect(_db()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM bookmarks ORDER BY category"
        ).fetchall()
    return [row[0] for row in rows]


@mcp.tool()
def get_bookmarks_by_category(category: str, limit: int = 50) -> list[dict]:
    """Get bookmarks in a specific category. Case-insensitive match. Returns tweet URL, summary, and full content."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT tweet_id, author_username, author_name, category, summary,
                      full_content, tweet_url, bookmarked_at
               FROM bookmarks WHERE LOWER(category) = LOWER(?)
               ORDER BY bookmarked_at DESC LIMIT ?""",
            (category, min(limit, 200)),
        ).fetchall()
    return [dict(row) for row in rows]


@mcp.tool()
def search_bookmarks(query: str, limit: int = 20) -> list[dict]:
    """Search bookmarks by keyword. Searches across both the AI-generated summary and the full tweet text."""
    term = f"%{query}%"
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT tweet_id, author_username, author_name, category, summary,
                      full_content, tweet_url, bookmarked_at
               FROM bookmarks WHERE summary LIKE ? OR full_content LIKE ?
               ORDER BY bookmarked_at DESC LIMIT ?""",
            (term, term, min(limit, 100)),
        ).fetchall()
    return [dict(row) for row in rows]


@mcp.tool()
def get_recent_bookmarks(n: int = 20) -> list[dict]:
    """Get the most recently categorized bookmarks."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT tweet_id, author_username, author_name, category, summary,
                      full_content, tweet_url, bookmarked_at
               FROM bookmarks ORDER BY categorized_at DESC LIMIT ?""",
            (min(n, 100),),
        ).fetchall()
    return [dict(row) for row in rows]


@mcp.tool()
def get_sync_status() -> dict:
    """Get info about the last sync — when it ran, how many bookmarks were added, and the total count."""
    with sqlite3.connect(_db()) as conn:
        row = conn.execute(
            "SELECT synced_at, new_bookmarks_added, status FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
    return {
        "last_sync_at": row[0] if row else None,
        "new_bookmarks_added": row[1] if row else None,
        "status": row[2] if row else None,
        "total_bookmarks": total,
    }
