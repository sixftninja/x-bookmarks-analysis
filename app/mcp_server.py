import json
import os
import sqlite3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("X Bookmarks")

# The real enriched content lives in summary (~200 words) or post_text
# (verbatim, for a row too thin to independently summarize) — neither gets
# large enough to need a separate, trimmed field list for multi-result
# tools, unlike the old full_content column this replaced (some pre-redesign
# rows carried 20,000+ characters of scraped article, large enough to break
# at least one client's tool-result handling when returned for every match
# in a list/search call). Use get_full_content for a live, on-demand full
# read of any one bookmark.
BOOKMARK_FIELDS = (
    "tweet_id, author_username, author_name, category, summary, "
    "tweet_url, bookmarked_at, tags, post_text, quoted_from_tweet_id"
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
            f"""SELECT {BOOKMARK_FIELDS}
               FROM bookmarks WHERE LOWER(category) = LOWER(?)
               ORDER BY bookmarked_at DESC LIMIT ?""",
            (category, min(limit, 200)),
        ).fetchall()
    return [_row_with_parsed_tags(row) for row in rows]


@mcp.tool()
def search_bookmarks(query: str, limit: int = 20) -> list[dict]:
    """Search bookmarks by keyword, matching against the summary and post_text. Returns summary,
    not full article text — call get_full_content(tweet_id) for that, one at a time."""
    term = f"%{query}%"
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT {BOOKMARK_FIELDS}
               FROM bookmarks WHERE summary LIKE ? OR post_text LIKE ?
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
            f"""SELECT {BOOKMARK_FIELDS}
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

        # Rows with a real content_for_summary need the LLM; rows already
        # resolved deterministically (nothing substantial anywhere) skip it
        # entirely — see resolve_bookmark_rows / NOT_ENOUGH_CONTENT_CATEGORY.
        needs_categorization = [t for t in new_tweets if "category" not in t]
        already_resolved = [t for t in new_tweets if "category" in t]

        existing_cats = get_categories(db_path)
        known_tags = get_all_tags(db_path)
        categorized = (
            categorize_bookmarks(needs_categorization, existing_categories=existing_cats, known_tags=known_tags)
            if needs_categorization else []
        )
        all_final = categorized + already_resolved
        count = insert_bookmarks(all_final, db_path)
        log_sync(count, "success", None, db_path)

        categories_used = list({b["category"] for b in all_final})
        return {"status": "ok", "new_bookmarks": count, "categories_used": categories_used}
    except Exception as e:
        log_sync(0, "error", str(e), db_path)
        return {"status": "error", "detail": str(e)}


@mcp.tool()
def edit_bookmark(tweet_id: str, category: str = None, summary: str = None) -> dict:
    """Edit the category and/or summary of a specific bookmark. The full article isn't persisted
    anywhere — see get_full_content for a live, on-demand read instead."""
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
            f"""SELECT {BOOKMARK_FIELDS} FROM bookmarks WHERE LOWER(author_username) = LOWER(?)
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


@mcp.tool()
def get_bookmarks_by_tag(tags: list[str], match_all: bool = True, limit: int = 50) -> list[dict]:
    """Get bookmarks carrying one or more tags. match_all=True (default) requires every listed tag
    to be present (AND); match_all=False returns bookmarks with any of the listed tags (OR).
    Pass tags=["Uncategorized"] to find bookmarks still needing a real tag assigned."""
    wanted = set(tags)
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {BOOKMARK_FIELDS} FROM bookmarks WHERE tags IS NOT NULL ORDER BY bookmarked_at DESC"
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
    scraped, quote-tweet text merged, and any linked external article or PDF (abstract, if it's a
    research paper) resolved. None of this is stored in the database; only the summary is kept
    long-term, so this re-derives the full content on every call.

    This is a real-time fetch, not a cheap lookup — expect it to take several seconds.

    IMPORTANT: call this for ONE tweet_id at a time only. After reading the result, present your
    findings on this specific article to the user BEFORE calling this tool again for a different
    tweet_id. Never call it more than once in the same response."""
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT tweet_id, author_username, post_text FROM bookmarks WHERE tweet_id = ?",
            (tweet_id,),
        ).fetchone()
    if row is None:
        return {"error": f"No bookmark found with tweet_id {tweet_id}"}

    from app.pipeline.fetch import enrich_tweet_content

    full_content, content_source, embedded_quote_tweet_id = enrich_tweet_content(
        row["post_text"], row["author_username"], tweet_id
    )
    return {
        "tweet_id": tweet_id,
        "full_content": full_content,
        "content_source": content_source,
        "embedded_quote_tweet_id": embedded_quote_tweet_id,
    }
