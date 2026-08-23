"""
One-time backfill: re-processes every existing top-level bookmark through
the current content-resolution pipeline (the 40/300-word band system,
link-chasing for PDFs and generic external articles, quote-tweet-as-its-own-
row up to 3 levels deep). None of the bookmarks in the database have been
through this pipeline yet — it only applies to rows synced from here on,
unless this script is run.

For each existing top-level bookmark (quoted_from_tweet_id IS NULL — i.e.
everything, since no row has ever had that set before this script runs):
  - re-fetch its status page fresh and resolve it via resolve_bookmark_rows
  - UPDATE that row's post_text/full_content/category/summary/tags in place
    (author_name/media_urls/tweet_url/bookmarked_at are left untouched —
    those came from the original sync and this script has no API access to
    recompute them)
  - INSERT any newly-discovered quoted tweets as their own new rows, same
    as a live sync would

Resumable: a checkpoint file records which tweet_ids are done, so a crash
or interrupt only costs the one row in flight, and a re-run skips
everything already completed rather than reprocessing it (and re-spending
the API calls that cost money).

Usage: python -u scripts/backfill_new_pipeline.py
"""
import json
import os
import sqlite3
import time
from dotenv import load_dotenv

load_dotenv()

from app.pipeline.fetch import resolve_bookmark_rows, build_bookmark_dict
from app.pipeline.categorize import categorize_bookmarks
from app.db import get_categories, get_all_tags, insert_bookmarks

CHECKPOINT_PATH = os.getenv("BACKFILL_CHECKPOINT", "./backfill_checkpoint.json")


def _load_checkpoint():
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH) as f:
            return set(json.load(f))
    return set()


def _save_checkpoint(done_ids):
    with open(CHECKPOINT_PATH, "w") as f:
        json.dump(sorted(done_ids), f)


def _update_op_row(db_path, bookmark):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE bookmarks
               SET post_text = ?, full_content = ?, category = ?, summary = ?, tags = ?
               WHERE tweet_id = ?""",
            (
                bookmark["post_text"],
                bookmark["full_content"],
                bookmark["category"],
                bookmark["summary"],
                bookmark["tags"],
                bookmark["tweet_id"],
            ),
        )
        conn.commit()


def main():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT tweet_id, author_username FROM bookmarks WHERE quoted_from_tweet_id IS NULL ORDER BY id"
        ).fetchall()

    done_ids = _load_checkpoint()
    todo = [r for r in rows if r["tweet_id"] not in done_ids]
    print(f"{len(rows)} top-level bookmark(s) total, {len(done_ids)} already done, {len(todo)} remaining.\n")

    seen_new_quote_ids = set()
    start = time.time()

    for i, row in enumerate(todo, 1):
        tweet_id = row["tweet_id"]
        author_username = row["author_username"]
        print(f"[{i}/{len(todo)}] {tweet_id} (@{author_username})...", flush=True)

        if not author_username:
            print("  skipping — no author_username on file")
            done_ids.add(tweet_id)
            _save_checkpoint(done_ids)
            continue

        try:
            resolved_rows = resolve_bookmark_rows(tweet_id, author_username, api_text=None)
        except Exception as e:
            print(f"  FAILED to resolve, leaving for next run: {e}")
            continue

        op_bookmark = None
        new_quote_bookmarks = []
        for r in resolved_rows:
            is_op = r["tweet_id"] == tweet_id
            bookmark = build_bookmark_dict(r, is_op)
            if is_op:
                op_bookmark = bookmark
            elif r["tweet_id"] not in seen_new_quote_ids:
                seen_new_quote_ids.add(r["tweet_id"])
                new_quote_bookmarks.append(bookmark)

        if op_bookmark is None:
            print("  FAILED — resolve_bookmark_rows returned no OP row, leaving for next run")
            continue

        candidates = [op_bookmark] + new_quote_bookmarks
        needs_categorization = [b for b in candidates if "category" not in b]
        already_resolved = [b for b in candidates if "category" in b]

        if needs_categorization:
            try:
                existing_categories = get_categories(db_path)
                known_tags = get_all_tags(db_path)
                categorized = categorize_bookmarks(
                    needs_categorization, existing_categories=existing_categories, known_tags=known_tags
                )
            except Exception as e:
                print(f"  FAILED categorization, leaving for next run: {e}")
                continue
        else:
            categorized = []

        finalized_by_id = {b["tweet_id"]: b for b in categorized + already_resolved}

        final_op = finalized_by_id.get(op_bookmark["tweet_id"])
        if final_op is None:
            print("  FAILED — OP row dropped during categorization (malformed LLM response), leaving for next run")
            continue

        _update_op_row(db_path, final_op)

        final_new_quotes = [
            finalized_by_id[b["tweet_id"]] for b in new_quote_bookmarks if b["tweet_id"] in finalized_by_id
        ]
        if final_new_quotes:
            inserted = insert_bookmarks(final_new_quotes, db_path)
            print(f"  updated OP, inserted {inserted} new quote row(s): {[b['tweet_id'] for b in final_new_quotes]}")
        else:
            print("  updated OP, no quote rows found")

        done_ids.add(tweet_id)
        _save_checkpoint(done_ids)

    elapsed = time.time() - start
    print(f"\nDone. {len(done_ids)}/{len(rows)} top-level bookmarks backfilled in {elapsed/60:.1f}m.")
    print(f"{len(seen_new_quote_ids)} new quote-tweet row(s) discovered and inserted this run.")


if __name__ == "__main__":
    main()
