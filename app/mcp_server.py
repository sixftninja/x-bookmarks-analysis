import json
import os
import sqlite3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("X Bookmarks")

# Single-bookmark lookups only — includes full_content (which, for anything
# synced after the storage redesign, is just the original short tweet text;
# use get_full_content for the actual enriched article).
BOOKMARK_FIELDS = (
    "tweet_id, author_username, author_name, category, summary, full_content, "
    "tweet_url, bookmarked_at, content_source, image_processing_status, tags"
)

# Multi-result tools — deliberately excludes full_content. Some rows still
# carry pre-redesign scraped articles 20,000+ characters long; returning
# that for every match in a list/search call produced response payloads
# large enough to break at least one client's tool-result handling. The
# 200-word summary is meant to carry the "can I judge relevance from this"
# job that full_content used to.
LIST_FIELDS = (
    "tweet_id, author_username, author_name, category, summary, "
    "tweet_url, bookmarked_at, content_source, image_processing_status, tags"
)


def _db():
    return os.getenv("DATABASE_URL", "./bookmarks.db")


def _row_with_parsed_tags(row):
    d = dict(row)
    try:
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d


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
    """Get bookmarks in a specific category. Case-insensitive match. Returns tweet URL and summary
    (not full article text — call get_full_content(tweet_id) for that, one at a time)."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT {LIST_FIELDS}
               FROM bookmarks WHERE LOWER(category) = LOWER(?)
               ORDER BY bookmarked_at DESC LIMIT ?""",
            (category, min(limit, 200)),
        ).fetchall()
    return [_row_with_parsed_tags(row) for row in rows]


@mcp.tool()
def search_bookmarks(query: str, limit: int = 20) -> list[dict]:
    """Search bookmarks by keyword, matching against the summary and (for older bookmarks that
    still have one stored) the full article text. Returns summary, not full article text —
    call get_full_content(tweet_id) for that, one at a time."""
    term = f"%{query}%"
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT {LIST_FIELDS}
               FROM bookmarks WHERE summary LIKE ? OR full_content LIKE ?
               ORDER BY bookmarked_at DESC LIMIT ?""",
            (term, term, min(limit, 100)),
        ).fetchall()
    return [_row_with_parsed_tags(row) for row in rows]


@mcp.tool()
def get_recent_bookmarks(n: int = 20) -> list[dict]:
    """Get the most recently categorized bookmarks. Returns summary, not full article text —
    call get_full_content(tweet_id) for that, one at a time."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT {LIST_FIELDS}
               FROM bookmarks ORDER BY categorized_at DESC LIMIT ?""",
            (min(n, 100),),
        ).fetchall()
    return [_row_with_parsed_tags(row) for row in rows]


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


@mcp.tool()
def rename_category(old_name: str, new_name: str) -> dict:
    """Rename a category across all bookmarks that have it."""
    with sqlite3.connect(_db()) as conn:
        cursor = conn.execute(
            "UPDATE bookmarks SET category = ? WHERE LOWER(category) = LOWER(?)",
            (new_name, old_name),
        )
        conn.commit()
    return {"updated": cursor.rowcount, "old_name": old_name, "new_name": new_name}


@mcp.tool()
def move_bookmarks(tweet_ids: list[str], new_category: str) -> dict:
    """Move one or more bookmarks to a category. The category is created if it doesn't exist yet."""
    with sqlite3.connect(_db()) as conn:
        cursor = conn.executemany(
            "UPDATE bookmarks SET category = ? WHERE tweet_id = ?",
            [(new_category, tid) for tid in tweet_ids],
        )
        conn.commit()
    return {"moved": cursor.rowcount, "new_category": new_category}


@mcp.tool()
def merge_categories(source: str, target: str) -> dict:
    """Move all bookmarks from source category into target category. Source category is removed."""
    with sqlite3.connect(_db()) as conn:
        cursor = conn.execute(
            "UPDATE bookmarks SET category = ? WHERE LOWER(category) = LOWER(?)",
            (target, source),
        )
        conn.commit()
    return {"moved": cursor.rowcount, "source": source, "target": target}


@mcp.tool()
def delete_bookmarks(tweet_ids: list[str]) -> dict:
    """Permanently delete one or more bookmarks by tweet_id."""
    with sqlite3.connect(_db()) as conn:
        cursor = conn.executemany(
            "DELETE FROM bookmarks WHERE tweet_id = ?",
            [(tid,) for tid in tweet_ids],
        )
        conn.commit()
    return {"deleted": cursor.rowcount}


@mcp.tool()
def delete_category(category: str) -> dict:
    """Permanently delete all bookmarks in a category."""
    with sqlite3.connect(_db()) as conn:
        cursor = conn.execute(
            "DELETE FROM bookmarks WHERE LOWER(category) = LOWER(?)",
            (category,),
        )
        conn.commit()
    return {"deleted": cursor.rowcount, "category": category}


@mcp.tool()
def trigger_sync() -> dict:
    """Fetch new bookmarks from X and categorize them. Returns how many were added."""
    from app.pipeline.auth import get_valid_access_token
    from app.pipeline.fetch import fetch_bookmarks
    from app.pipeline.categorize import categorize_bookmarks
    from app.db import get_existing_tweet_ids, get_categories, get_all_tags, insert_bookmarks, log_sync

    db_path = _db()
    try:
        get_valid_access_token(db_path)
        existing_ids = get_existing_tweet_ids(db_path)
        new_tweets = fetch_bookmarks(existing_tweet_ids=existing_ids, db_path=db_path)

        if not new_tweets:
            log_sync(0, "success", None, db_path)
            return {"status": "ok", "new_bookmarks": 0, "message": "No new bookmarks found"}

        existing_cats = get_categories(db_path)
        known_tags = get_all_tags(db_path)
        categorized = categorize_bookmarks(new_tweets, existing_categories=existing_cats, known_tags=known_tags)
        count = insert_bookmarks(categorized, db_path)
        log_sync(count, "success", None, db_path)

        categories_used = list({b["category"] for b in categorized})
        return {"status": "ok", "new_bookmarks": count, "categories_used": categories_used}
    except Exception as e:
        log_sync(0, "error", str(e), db_path)
        return {"status": "error", "detail": str(e)}


@mcp.tool()
def edit_bookmark(tweet_id: str, category: str = None, summary: str = None) -> dict:
    """Edit the category and/or summary of a specific bookmark. full_content is no longer
    editable here — it's not the persisted article anymore (see get_full_content)."""
    if not category and not summary:
        return {"error": "Provide at least one of: category, summary"}
    fields = []
    values = []
    if category:
        fields.append("category = ?")
        values.append(category)
    if summary:
        fields.append("summary = ?")
        values.append(summary)
    values.append(tweet_id)
    with sqlite3.connect(_db()) as conn:
        conn.execute(
            f"UPDATE bookmarks SET {', '.join(fields)} WHERE tweet_id = ?",
            values,
        )
        conn.commit()
    return {"updated": tweet_id, "category": category, "summary": summary}


@mcp.tool()
def get_bookmark_by_tweet_id(tweet_id: str) -> dict:
    """Get a single bookmark by its exact tweet_id. Returns an error if not found."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {BOOKMARK_FIELDS} FROM bookmarks WHERE tweet_id = ?",
            (tweet_id,),
        ).fetchone()
    if row is None:
        return {"error": f"No bookmark found with tweet_id {tweet_id}"}
    return _row_with_parsed_tags(row)


@mcp.tool()
def get_bookmarks_by_author(author_username: str, limit: int = 50) -> list[dict]:
    """Get bookmarks from a specific X author. Case-insensitive, matches with or without the @ sign."""
    handle = author_username.lstrip("@")
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT {LIST_FIELDS} FROM bookmarks WHERE LOWER(author_username) = LOWER(?)
               ORDER BY bookmarked_at DESC LIMIT ?""",
            (handle, min(limit, 200)),
        ).fetchall()
    return [_row_with_parsed_tags(row) for row in rows]


@mcp.tool()
def get_authors() -> list[dict]:
    """Get every author with a bookmark, and how many bookmarks each has, sorted by count descending."""
    with sqlite3.connect(_db()) as conn:
        rows = conn.execute(
            "SELECT author_username, COUNT(*) as count FROM bookmarks GROUP BY author_username ORDER BY count DESC"
        ).fetchall()
    return [{"author_username": row[0], "count": row[1]} for row in rows]


# DEAD as of the "don't store full articles / don't describe images at sync
# time" redesign — content_source and image_processing_status stop being
# meaningful signals for anything synced afterward (content_source no longer
# reflects what's stored, since full_content is just the raw tweet text
# either way; image_processing_status is never anything but the default,
# since images are never processed at sync time anymore). Left commented
# rather than deleted since they're still valid for pre-redesign rows if
# ever needed again.
#
# @mcp.tool()
# def get_bookmarks_by_content_source(content_source: str, limit: int = 50) -> list[dict]:
#     """Get bookmarks by content_source: api_text, api_scraped_article, manual, or api_teaser_only (still broken/unresolved)."""
#     with sqlite3.connect(_db()) as conn:
#         conn.row_factory = sqlite3.Row
#         rows = conn.execute(
#             f"""SELECT {BOOKMARK_FIELDS} FROM bookmarks WHERE content_source = ?
#                ORDER BY bookmarked_at DESC LIMIT ?""",
#             (content_source, min(limit, 200)),
#         ).fetchall()
#     return [_row_with_parsed_tags(row) for row in rows]
#
#
# @mcp.tool()
# def get_bookmarks_by_image_processing_status(image_processing_status: str, limit: int = 50) -> list[dict]:
#     """Get bookmarks by image_processing_status: images_appended_successfully, images_partially_appended, images_fetch_failed, or no_images_found."""
#     with sqlite3.connect(_db()) as conn:
#         conn.row_factory = sqlite3.Row
#         rows = conn.execute(
#             f"""SELECT {BOOKMARK_FIELDS} FROM bookmarks WHERE image_processing_status = ?
#                ORDER BY bookmarked_at DESC LIMIT ?""",
#             (image_processing_status, min(limit, 200)),
#         ).fetchall()
#     return [_row_with_parsed_tags(row) for row in rows]


@mcp.tool()
def get_bookmarks_by_tag(tags: list[str], match_all: bool = True, limit: int = 50) -> list[dict]:
    """Get bookmarks carrying one or more tags. match_all=True (default) requires every listed tag
    to be present (AND); match_all=False returns bookmarks with any of the listed tags (OR).
    Pass tags=["Uncategorized"] to find bookmarks still needing a real tag assigned."""
    wanted = set(tags)
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {LIST_FIELDS} FROM bookmarks WHERE tags IS NOT NULL ORDER BY bookmarked_at DESC"
        ).fetchall()

    matched = []
    for row in rows:
        parsed = _row_with_parsed_tags(row)
        have = set(parsed["tags"])
        hit = wanted.issubset(have) if match_all else bool(wanted & have)
        if hit:
            matched.append(parsed)
        if len(matched) >= min(limit, 200):
            break
    return matched


@mcp.tool()
def add_tag_to_bookmark(tweet_id: str, tag: str) -> dict:
    """Add a tag to a bookmark's tag list (no duplicates). If the bookmark's only tag was
    "Uncategorized", the new real tag replaces it. Use this after a human approves a tag
    suggestion — this is the only way new tags should enter the vocabulary."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT tags FROM bookmarks WHERE tweet_id = ?", (tweet_id,)).fetchone()
        if row is None:
            return {"error": f"No bookmark found with tweet_id {tweet_id}"}

        try:
            current = json.loads(row["tags"]) if row["tags"] else []
        except (json.JSONDecodeError, TypeError):
            current = []

        if tag not in current:
            current = [t for t in current if t != "Uncategorized"] + [tag]

        conn.execute(
            "UPDATE bookmarks SET tags = ? WHERE tweet_id = ?",
            (json.dumps(current), tweet_id),
        )
        conn.commit()
    return {"updated": tweet_id, "tags": current}


@mcp.tool()
def get_full_content(tweet_id: str) -> dict:
    """Fetch the FULL enriched content for exactly ONE bookmark, live, right now — article body
    scraped, quote-tweet text merged, images described. None of this is stored in the database;
    only the summary is kept long-term, so this re-derives the full article on every call.

    This is a real-time fetch, not a cheap lookup — expect it to take several seconds, longer if
    there are images to describe.

    IMPORTANT: call this for ONE tweet_id at a time only. After reading the result, present your
    findings on this specific article to the user BEFORE calling this tool again for a different
    tweet_id. Never call it more than once in the same response."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT tweet_id, author_username, full_content FROM bookmarks WHERE tweet_id = ?",
            (tweet_id,),
        ).fetchone()
    if row is None:
        return {"error": f"No bookmark found with tweet_id {tweet_id}"}

    from app.pipeline.fetch import enrich_tweet_content

    full_content, content_source, image_processing_status, quoted_tweet_id = enrich_tweet_content(
        row["full_content"], row["author_username"], tweet_id, describe_images=True
    )
    return {
        "tweet_id": tweet_id,
        "full_content": full_content,
        "content_source": content_source,
        "image_processing_status": image_processing_status,
        "quoted_tweet_id": quoted_tweet_id,
    }
