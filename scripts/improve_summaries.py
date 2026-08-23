"""
ONE-TIME backfill: rewrite existing summaries to the new ~200-word target
(the old target was ~128 words). Summary-only — does NOT touch category,
tags, or full_content, even though the categorizer call computes category/
tags internally (they're discarded on write-back).

Uses each bookmark's EXISTING stored full_content as input — per the
"keep current content as-is" decision, this does not re-scrape or touch
full_content itself, it just asks for a better summary of what's already
there.

Persists after every batch (not at the end), same resumability pattern as
the other backfills.

Safe to re-run: only selects rows whose current summary is under ~170
words, which is a reliable signal a row hasn't been through this backfill
yet (170 comfortably below the 200 target, comfortably above the old 128
target).

Usage:
    python scripts/improve_summaries.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.db import get_all_tags
from app.pipeline.categorize import categorize_bookmarks

WORD_COUNT_CUTOFF = 170


def main():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    print(f"Using database: {db_path}")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, author_username, full_content, summary FROM bookmarks"
        ).fetchall()

    candidates = [
        {"tweet_id": tid, "author_username": u, "content_for_summary": c}
        for tid, u, c, s in rows
        if len((s or "").split()) < WORD_COUNT_CUTOFF
    ]

    if not candidates:
        print("No summaries need improving. Nothing to do.")
        return

    print(f"Improving {len(candidates)} summar{'y' if len(candidates) == 1 else 'ies'} (out of {len(rows)} total)...")

    known_tags = get_all_tags(db_path)
    improved_count = 0

    def persist_batch(idx, total, usage, batch_merged):
        nonlocal improved_count
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "UPDATE bookmarks SET summary = ? WHERE tweet_id = ?",
                [(b["summary"], b["tweet_id"]) for b in batch_merged],
            )
            conn.commit()
        improved_count += len(batch_merged)
        print(f"  batch {idx}/{total}: improved {len(batch_merged)} (running total {improved_count}/{len(candidates)})")

    try:
        categorize_bookmarks(candidates, known_tags=known_tags, on_batch_complete=persist_batch)
    except Exception as e:
        print(f"\nStopped early after a batch failure: {type(e).__name__}: {e}")
        print(f"{improved_count}/{len(candidates)} improved and saved before the failure.")
        print("Safe to just re-run this script — it will pick up the rest.")
        raise

    print(f"\nBackfill complete. Improved {improved_count}/{len(candidates)}.")


if __name__ == "__main__":
    main()
