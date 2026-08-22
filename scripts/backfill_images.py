"""
ONE-TIME backfill: describe and append images for bookmarks that were synced
before the image-description pipeline existed. Targets rows with a photo
attachment in media_urls but image_processing_status still 'no_images_found'
— these were never revisited when image handling was added.

Only appends an image description to the end of full_content — never
touches category, summary, tags, quote-tweet handling, or article
re-scraping. The underlying text these bookmarks already have is real and
correct; only the missing image piece is being filled in.

Persists after every row (not batched at the end), same resumability
pattern as the other backfills.

Safe to re-run: only touches rows with a photo in media_urls that are still
marked no_images_found — a row that gets fixed drops out of the query.

Usage:
    python scripts/backfill_images.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.pipeline.fetch import backfill_missing_images


def main():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    print(f"Using database: {db_path}")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, author_username, full_content FROM bookmarks "
            "WHERE media_urls LIKE '%photo%' AND image_processing_status = 'no_images_found'"
        ).fetchall()

    if not rows:
        print("No rows need an image backfill. Nothing to do.")
        return

    print(f"Backfilling images for {len(rows)} bookmark(s)...")
    fixed, no_image_found = 0, 0

    for tweet_id, author_username, full_content in rows:
        result = backfill_missing_images(author_username, tweet_id, full_content)
        if result is None:
            print(f"  {tweet_id}: no fetchable image found, leaving as-is")
            no_image_found += 1
            continue

        new_content, image_status = result
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE bookmarks SET full_content = ?, image_processing_status = ? WHERE tweet_id = ?",
                (new_content, image_status, tweet_id),
            )
            conn.commit()
        print(f"  {tweet_id}: backfilled -> image_processing_status={image_status}")
        fixed += 1

    print(f"\nBackfill complete. Fixed {fixed}, no image found for {no_image_found}.")


if __name__ == "__main__":
    main()
