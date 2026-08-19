"""
ONE-TIME migration + backfill for the content_source / image_processing_status
/ quoted_tweet_id columns. Run this once, after deploying the schema/pipeline
changes in app/db.py and app/pipeline/fetch.py, against whichever database
DATABASE_URL points at.

What it does, in order:

1. Adds the new columns (via app.db.init_db — safe/idempotent).
2. Classifies every existing row that doesn't have a content_source yet:
   - In the "Link-Only Posts" category, still broken (empty, or nothing
     but a bare URL) -> api_teaser_only
   - In "Link-Only Posts" but already has real content (was pasted in by
     hand) -> manual
   - Everything else -> api_text
2b. Catches stray broken rows outside "Link-Only Posts": the categorization
    AI scattered this same bug across many differently-named categories
    over time (External Links, AI Development Tools, General Links, ...),
    so category name alone isn't a reliable signal — and "broken" isn't
    just t.co links either (empty full_content and bare non-Twitter URLs
    both showed up in production data too). Any row anywhere whose
    full_content is still broken by that definition gets reclassified to
    api_teaser_only regardless of its current category or content_source,
    so step 3 picks it up too.
3. Retries every api_teaser_only row through the new HTML-scrape pipeline
   (app.pipeline.fetch.enrich_tweet_content). Success -> full_content,
   content_source, image_processing_status, quoted_tweet_id all get updated.
   Failure -> left as api_teaser_only untouched (out of scope to solve
   further right now).
4. Re-runs categorization (category + summary) on every row that is now
   `manual`, or was just successfully backfilled out of api_teaser_only —
   their categories were assigned back when they only had teaser text.
   Backfilled rows are recategorized one at a time, immediately after their
   own backfill (not batched at the end) — so a crash partway through still
   leaves already-processed rows fully done, and a re-run picks up exactly
   where it left off instead of silently skipping them.

Safe to re-run: classification only touches rows where content_source IS
NULL, the stray-broken-row reclassification only touches rows that are
still genuinely broken, and the backfill/recategorize steps only touch
rows still in the states they target.

Usage:
    python scripts/migrate_content_columns.py
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.db import init_db, get_categories
from app.pipeline.fetch import enrich_tweet_content, is_bare_url_only
from app.pipeline.categorize import categorize_bookmarks

LINK_ONLY_CATEGORY = "link-only posts"


def _is_broken_content(content):
    """"Broken", broadly: nothing at all, or a single bare URL and nothing
    else — whether that's an X t.co short link or a plain external URL
    pasted with no surrounding text (e.g. "arxiv.org/pdf/2509.14252").
    Bare-URL detection is shared with app.pipeline.fetch, which also uses
    it to try resolving such links (e.g. PDFs) instead of leaving them."""
    text = (content or "").strip()
    return (not text) or is_bare_url_only(text)


def _categorize_with_retries(bookmarks, existing_categories, attempts=3, base_delay=5):
    """categorize_bookmarks() hits a real LLM API — retry a few times on
    transient network errors (DNS blips, connection resets) rather than
    letting one flaky call kill the whole migration run."""
    for attempt in range(1, attempts + 1):
        try:
            return categorize_bookmarks(bookmarks, existing_categories=existing_categories)
        except Exception as e:
            if attempt == attempts:
                raise
            wait = base_delay * attempt
            print(f"  categorize_bookmarks failed ({type(e).__name__}: {e}), retrying in {wait}s ({attempt}/{attempts})...")
            time.sleep(wait)


def _classify_existing_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, category, full_content FROM bookmarks WHERE content_source IS NULL"
        ).fetchall()

        updates = []
        for tweet_id, category, full_content in rows:
            if (category or "").strip().lower() == LINK_ONLY_CATEGORY:
                content_source = "api_teaser_only" if _is_broken_content(full_content) else "manual"
            else:
                content_source = "api_text"
            updates.append((content_source, "no_images_found", tweet_id))

        conn.executemany(
            "UPDATE bookmarks SET content_source = ?, image_processing_status = ? WHERE tweet_id = ?",
            updates,
        )
        conn.commit()

    counts = {}
    for content_source, _, _ in updates:
        counts[content_source] = counts.get(content_source, 0) + 1
    print(f"Classified {len(updates)} existing row(s): {counts}")
    return counts


def _reclassify_stray_bare_links(db_path):
    """Category name alone isn't a reliable signal for "this row is still
    broken" — the categorization AI scattered this bug across many
    differently-named categories, not just "Link-Only Posts", and it isn't
    just t.co links either (empty full_content, bare non-Twitter URLs like
    a pasted-in arxiv link, both showed up too). Find any row, anywhere,
    whose full_content is still broken by that broader definition and flag
    it api_teaser_only so the backfill step below picks it up too."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, full_content, content_source FROM bookmarks"
        ).fetchall()

        stray_ids = [
            tweet_id
            for tweet_id, full_content, content_source in rows
            if content_source != "api_teaser_only" and _is_broken_content(full_content)
        ]

        if stray_ids:
            conn.executemany(
                "UPDATE bookmarks SET content_source = 'api_teaser_only' WHERE tweet_id = ?",
                [(tid,) for tid in stray_ids],
            )
            conn.commit()

    print(f"Reclassified {len(stray_ids)} stray broken row(s) outside 'Link-Only Posts' as api_teaser_only.")
    return len(stray_ids)


def _backfill_teaser_only_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, author_username, full_content FROM bookmarks WHERE content_source = 'api_teaser_only'"
        ).fetchall()

    if not rows:
        print("No api_teaser_only rows to backfill.")
        return 0, 0

    print(f"Retrying {len(rows)} api_teaser_only row(s) with the new scrape pipeline...")
    fixed, still_broken = 0, 0

    for tweet_id, author_username, full_content in rows:
        # Never pass the stored full_content in as the "clean API text" here —
        # by definition every row in this query has full_content that's just
        # the bare t.co link (that's what made it a candidate). Passing it
        # through would make enrich_tweet_content() trust and keep that bare
        # link instead of the real content freshly scraped from the page.
        full, content_source, image_status, quoted_id = enrich_tweet_content(
            "", author_username, tweet_id
        )
        if content_source == "api_teaser_only":
            print(f"  {tweet_id}: still unresolved, leaving as api_teaser_only")
            still_broken += 1
            continue

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """UPDATE bookmarks
                   SET full_content = ?, content_source = ?, image_processing_status = ?, quoted_tweet_id = ?
                   WHERE tweet_id = ?""",
                (full, content_source, image_status, quoted_id, tweet_id),
            )
            conn.commit()
        print(f"  {tweet_id}: backfilled -> content_source={content_source} image_processing_status={image_status}")

        # Recategorize immediately, not batched at the end: if this crashes
        # partway through, already-backfilled rows are already fully done
        # (content + category), and a re-run naturally skips them since
        # they're no longer api_teaser_only.
        existing_categories = get_categories(db_path)
        recategorized = _categorize_with_retries(
            [{"tweet_id": tweet_id, "full_content": full, "author_username": author_username}],
            existing_categories=existing_categories,
        )
        if recategorized:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE bookmarks SET category = ?, summary = ?, categorized_at = datetime('now') WHERE tweet_id = ?",
                    (recategorized[0]["category"], recategorized[0]["summary"], tweet_id),
                )
                conn.commit()
            print(f"  {tweet_id}: recategorized -> {recategorized[0]['category']}")
        else:
            print(f"  {tweet_id}: recategorization returned no result, category/summary left as-is")

        fixed += 1

    print(f"Backfill done: {fixed} fixed, {still_broken} still unresolved.")
    return fixed, still_broken


def _recategorize_manual_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, author_username, full_content FROM bookmarks WHERE content_source = 'manual'"
        ).fetchall()

    if not rows:
        print("No manual rows to recategorize.")
        return 0

    bookmarks = [{"tweet_id": tid, "author_username": u, "full_content": c} for tid, u, c in rows]
    print(f"Recategorizing {len(bookmarks)} manual row(s)...")
    existing_categories = get_categories(db_path)
    recategorized = _categorize_with_retries(bookmarks, existing_categories=existing_categories)

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "UPDATE bookmarks SET category = ?, summary = ?, categorized_at = datetime('now') WHERE tweet_id = ?",
            [(b["category"], b["summary"], b["tweet_id"]) for b in recategorized],
        )
        conn.commit()
    print(f"Recategorized {len(recategorized)} manual row(s).")
    return len(recategorized)


def main():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    print(f"Using database: {db_path}")

    init_db(db_path)

    _classify_existing_rows(db_path)
    _reclassify_stray_bare_links(db_path)
    _backfill_teaser_only_rows(db_path)
    _recategorize_manual_rows(db_path)

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
