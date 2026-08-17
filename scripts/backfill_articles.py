"""
One-off backfill for bookmarks affected by the X Article teaser-text bug
(see app/pipeline/fetch.py — _find_article_source_url / _fetch_article_text).

Finds bookmarks whose full_content is nothing but a bare t.co link (the
signature of an Article cover post that fetch_bookmarks stored before the
fix), re-fetches those tweets from the X API to recover entities, and
re-runs the same article-detection + scrape logic to backfill full_content.

Run this on the deployed service (or anywhere with real network access to
x.com), not in a network-restricted sandbox.

Usage:
    python scripts/backfill_articles.py [tweet_id ...]

With no arguments, scans the whole DB for bare-link candidates. Pass one or
more tweet IDs to target specific bookmarks only (e.g. the one you already
know is broken).
"""

import os
import re
import sqlite3
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

from app.pipeline.auth import get_valid_access_token
from app.pipeline.fetch import BASE_URL, _headers, _find_article_source_url, _fetch_article_text

BARE_TCO_RE = re.compile(r"^https://t\.co/\w+$")


def _find_candidates(db_path, tweet_ids=None):
    with sqlite3.connect(db_path) as conn:
        if tweet_ids:
            placeholders = ",".join("?" for _ in tweet_ids)
            rows = conn.execute(
                f"SELECT tweet_id, full_content FROM bookmarks WHERE tweet_id IN ({placeholders})",
                tweet_ids,
            ).fetchall()
        else:
            rows = conn.execute("SELECT tweet_id, full_content FROM bookmarks").fetchall()
    return [tid for tid, content in rows if content and BARE_TCO_RE.match(content.strip())]


def _fetch_tweets_by_id(tweet_ids, token):
    tweets = {}
    for i in range(0, len(tweet_ids), 100):
        batch = tweet_ids[i : i + 100]
        resp = httpx.get(
            f"{BASE_URL}/tweets",
            params={"ids": ",".join(batch), "tweet.fields": "text,entities"},
            headers=_headers(token),
        )
        resp.raise_for_status()
        for tweet in resp.json().get("data", []):
            tweets[tweet["id"]] = tweet
    return tweets


def main():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    requested_ids = sys.argv[1:] or None

    candidates = _find_candidates(db_path, requested_ids)
    if not candidates:
        print("No bare-link bookmarks found to backfill.")
        return

    print(f"Found {len(candidates)} candidate bookmark(s): {candidates}")

    token = get_valid_access_token(db_path)
    tweets = _fetch_tweets_by_id(candidates, token)

    fixed, skipped = 0, 0
    with sqlite3.connect(db_path) as conn:
        for tweet_id in candidates:
            tweet = tweets.get(tweet_id)
            if not tweet:
                print(f"  {tweet_id}: not found via API (deleted/inaccessible?), skipping")
                skipped += 1
                continue

            article_url = _find_article_source_url(tweet)
            if not article_url:
                print(f"  {tweet_id}: no article/self-link detected, skipping")
                skipped += 1
                continue

            scraped = _fetch_article_text(article_url)
            if not scraped:
                print(f"  {tweet_id}: scrape of {article_url} failed, skipping")
                skipped += 1
                continue

            conn.execute(
                "UPDATE bookmarks SET full_content = ? WHERE tweet_id = ?",
                (scraped, tweet_id),
            )
            print(f"  {tweet_id}: backfilled ({len(scraped)} chars from {article_url})")
            fixed += 1
        conn.commit()

    print(f"\nDone. Backfilled {fixed}, skipped {skipped}.")
    if fixed:
        print("Note: 'summary'/'category' for these rows were generated from the old")
        print("teaser text and were not regenerated — re-categorize separately if needed.")


if __name__ == "__main__":
    main()
