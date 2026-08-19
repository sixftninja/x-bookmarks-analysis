"""
ONE-TIME backfill: assign tags to every existing bookmark that doesn't have
any yet. Tags-only — does NOT touch category or summary, even though the
categorizer call computes them internally (they're just discarded on write).

Batches of 25 (same as normal categorization), so this is far cheaper/faster
than the earlier content_source backfill: no network scraping per row, just
one LLM call per batch of already-stored full_content.

Persists after EVERY batch (not just at the end), so a single bad batch
can't lose already-completed work — just re-run the script and it picks up
exactly where it left off, since the query below only selects still-untagged
rows.

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
    tagged_count = 0

    def persist_batch(idx, total, usage, batch_merged):
        nonlocal tagged_count
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "UPDATE bookmarks SET tags = ? WHERE tweet_id = ?",
                [(b["tags"], b["tweet_id"]) for b in batch_merged],
            )
            conn.commit()
        tagged_count += len(batch_merged)
        print(f"  batch {idx}/{total}: tagged {len(batch_merged)} (running total {tagged_count}/{len(bookmarks)})")

    try:
        categorize_bookmarks(bookmarks, known_tags=known_tags, on_batch_complete=persist_batch)
    except Exception as e:
        print(f"\nStopped early after a batch failure: {type(e).__name__}: {e}")
        print(f"{tagged_count}/{len(bookmarks)} tagged and saved before the failure.")
        print("Safe to just re-run this script — it will pick up the remaining untagged rows.")
        raise

    print(f"\nBackfill complete. Tagged {tagged_count}/{len(bookmarks)}.")


if __name__ == "__main__":
    main()
