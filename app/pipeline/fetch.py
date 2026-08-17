import json
import os
import re
import time
import httpx
import trafilatura
from dotenv import load_dotenv
from app.pipeline.auth import get_valid_access_token

load_dotenv()

BASE_URL = "https://api.x.com/2"

_SELF_STATUS_RE = re.compile(r"/status/\d+/?$")


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _get_user_id(token):
    resp = httpx.get(f"{BASE_URL}/users/me", headers=_headers(token))
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def _extract_media_urls(entities):
    if not entities:
        return None
    urls = entities.get("urls", [])
    media = [
        u["expanded_url"]
        for u in urls
        if any(
            ext in u.get("expanded_url", "")
            for ext in ["/photo/", "/video/", ".jpg", ".jpeg", ".png", ".gif", ".mp4"]
        )
    ]
    return json.dumps(media) if media else None


def _find_article_source_url(tweet):
    """Detect X Article 'cover' posts: text is nothing but a link to an
    /i/article/ page or a self-referencing status URL. The v2 bookmarks
    endpoint only returns teaser text for these, so the real body has to
    be scraped from the article page itself."""
    entities = tweet.get("entities") or {}
    text = (tweet.get("text") or "").strip()
    if not text:
        return None
    for u in entities.get("urls", []):
        expanded = u.get("expanded_url") or ""
        short = u.get("url") or ""
        if not expanded or not short:
            continue
        is_article_link = "/i/article/" in expanded
        is_self_link = bool(_SELF_STATUS_RE.search(expanded))
        if (is_article_link or is_self_link) and text == short:
            return expanded
    return None


def _fetch_article_text(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return text.strip() if text else None
    except Exception as e:
        print(f"Article scrape failed for {url}: {e}")
        return None


def fetch_bookmarks(existing_tweet_ids=None, db_path=None):
    if db_path is None:
        db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    if existing_tweet_ids is None:
        existing_tweet_ids = set()

    token = get_valid_access_token(db_path)
    user_id = _get_user_id(token)

    params = {
        "tweet.fields": "text,author_id,created_at,entities,attachments",
        "expansions": "author_id",
        "user.fields": "username,name",
        "max_results": 100,
    }

    results = []
    page = 1
    pagination_token = None

    while True:
        if pagination_token:
            params["pagination_token"] = pagination_token
        elif "pagination_token" in params:
            del params["pagination_token"]

        resp = _make_request_with_retry(
            f"{BASE_URL}/users/{user_id}/bookmarks",
            params=params,
            token=token,
            db_path=db_path,
        )

        data = resp.json()
        tweets = data.get("data", [])
        users_map = {
            u["id"]: u for u in data.get("includes", {}).get("users", [])
        }

        page_results = []
        all_known = True
        for tweet in tweets:
            tweet_id = tweet["id"]
            if tweet_id not in existing_tweet_ids:
                all_known = False
            author = users_map.get(tweet.get("author_id", ""), {})
            author_username = author.get("username")

            full_content = tweet.get("text", "")
            article_url = _find_article_source_url(tweet)
            if article_url:
                scraped = _fetch_article_text(article_url)
                if scraped:
                    full_content = scraped

            page_results.append(
                {
                    "tweet_id": tweet_id,
                    "author_username": author_username,
                    "author_name": author.get("name"),
                    "full_content": full_content,
                    "media_urls": _extract_media_urls(tweet.get("entities")),
                    "tweet_url": (
                        f"https://x.com/{author_username}/status/{tweet_id}"
                        if author_username
                        else None
                    ),
                    "bookmarked_at": tweet.get("created_at"),
                }
            )

        new_in_page = [t for t in page_results if t["tweet_id"] not in existing_tweet_ids]
        results.extend(new_in_page)

        print(f"Fetched page {page} ({len(results)} new bookmarks so far)...")

        next_token = data.get("meta", {}).get("next_token")
        if not next_token or all_known:
            break

        pagination_token = next_token
        page += 1

    return results


def _make_request_with_retry(url, params, token, db_path, attempt=0):
    resp = httpx.get(url, params=params, headers=_headers(token))

    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 60))
        print(f"Rate limited. Waiting {wait}s...")
        time.sleep(wait)
        return _make_request_with_retry(url, params, token, db_path, attempt)

    if resp.status_code == 401 and attempt == 0:
        print("Got 401, refreshing token and retrying...")
        from app.pipeline.auth import _refresh_token
        with __import__("sqlite3").connect(db_path) as conn:
            row = conn.execute(
                "SELECT refresh_token FROM oauth_tokens WHERE id = 1"
            ).fetchone()
        if row:
            token = _refresh_token(row[0], db_path)
            return _make_request_with_retry(url, params, token, db_path, attempt=1)
        raise RuntimeError("401 after token refresh attempt — tokens may be invalid.")

    if resp.status_code == 401:
        raise RuntimeError(f"401 Unauthorized after retry: {resp.text}")

    resp.raise_for_status()
    return resp
