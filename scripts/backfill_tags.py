"""
ONE-TIME backfill: assign tags to every existing bookmark that doesn't have
any yet. Tags-only — does NOT touch category or summary, even though the
categorizer call computes them internally (they're just discarded on write).

Batches of 25 (same as normal categorization), so this is far cheaper/faster
than the earlier content_source backfill: no network scraping per row, just
one LLM call per batch of already-stored full_content.

Safe to re-run: only touches rows where tags IS NULL or tags = '[]'.

Usage:
    python scripts/backfill_tags.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.db import get_all_tags
from app.pipeline.categorize import categorize_bookmarks


def main():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    print(f"Using database: {db_path}")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, author_username, full_content FROM bookmarks "
            "WHERE tags IS NULL OR tags = '[]'"
        ).fetchall()

    if not rows:
        print("No untagged rows found. Nothing to do.")
        return

    bookmarks = [{"tweet_id": tid, "author_username": u, "full_content": c} for tid, u, c in rows]
    print(f"Tagging {len(bookmarks)} untagged bookmark(s)...")

    known_tags = get_all_tags(db_path)
    tagged = categorize_bookmarks(bookmarks, known_tags=known_tags)

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "UPDATE bookmarks SET tags = ? WHERE tweet_id = ?",
            [(b["tags"], b["tweet_id"]) for b in tagged],
        )
        conn.commit()

    print(f"Tagged {len(tagged)} bookmark(s). {len(bookmarks) - len(tagged)} skipped (see WARNING above, if any).")
    print("\nBackfill complete.")


if __name__ == "__main__":
    main()
