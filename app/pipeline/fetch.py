import base64
import json
import os
import re
import time
import httpx
import trafilatura
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from app.pipeline.auth import get_valid_access_token

load_dotenv()

BASE_URL = "https://api.x.com/2"

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
ARTICLE_HREF_RE = re.compile(r"^/i/article/\d+")
BARE_LINK_RE = re.compile(r"^https://t\.co/\w+$")
BARE_URL_ONLY_RE = re.compile(r"^(https?://|www\.)?[\w.-]+\.[a-z]{2,}(/\S*)?$", re.IGNORECASE)
HANDLE_HREF_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})$")
ARTICLE_LEN_THRESHOLD = 1500  # chars — signals a tweet's own status page already inlines a full article body

# Chrome around a tweet block's own text (author name/handle header, date,
# "Article" label, trailing engagement-stat numbers) — stripped only when we
# have no clean API text to use instead (i.e. for quoted tweets, and for a
# primary tweet whose own status page inlined a full article body).
_MONTH_ABBR_RE = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z]*\.? \d{1,2}$")
_STAT_LINE_RE = re.compile(r"^[\d.,]+[KM]?$")
_TIME_LINE_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)?\s*[·\-]")

OPENAI_VISION_MODEL = "gpt-4o"
OPENAI_TEXT_MODEL = "gpt-4o"
ANTHROPIC_VISION_MODEL = "claude-sonnet-5"
ANTHROPIC_TEXT_MODEL = "claude-sonnet-5"

PDF_ABSTRACT_INSTRUCTION = (
    "You will be given text extracted from the first pages of a PDF. "
    "Determine whether this PDF is an academic/research paper (has an "
    "abstract, authors, citations, academic structure) as opposed to "
    "something else (a slide deck, product one-pager, blog export, report, "
    "etc). If it IS a research paper: reply with ONLY the abstract text, "
    "verbatim, nothing else — no preamble, no 'Abstract:' label. If it is "
    "NOT a research paper: reply with exactly the single word NO."
)

VISION_INSTRUCTION = (
    "You are continuing a piece of writing at the exact point an image appeared. "
    "Write 1-3 sentences describing what the image shows, but phrased as if the "
    "original author is directly describing/narrating it themselves as part of "
    "their own prose — never use meta-language like 'the image shows', 'this "
    "picture depicts', 'in this screenshot', etc. Just write the content directly, "
    "in a plain, matter-of-fact register consistent with tech/startup writing. "
    "Return only the text to splice in, nothing else."
)


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


# ---------------------------------------------------------------------------
# Plain-HTTP status-page parsing (primary path — validated against real
# tweets: reaches x.com fine without OAuth, article bodies come inlined into
# a tweet's own status page, and quote-tweets are a nested
# <article data-tweet-id> element inside the primary tweet's own block).
# ---------------------------------------------------------------------------

def _own_text(block, exclude_id=None):
    """get_text() of a block with any nested quote-tweet subtree stripped out."""
    copy = BeautifulSoup(str(block), "lxml")
    if exclude_id:
        nested = copy.find("article", attrs={"data-tweet-id": exclude_id})
        if nested:
            nested.decompose()
    return copy.get_text(separator="\n", strip=True)


def _find_article_href(block, exclude_id=None):
    copy = BeautifulSoup(str(block), "lxml")
    if exclude_id:
        nested = copy.find("article", attrs={"data-tweet-id": exclude_id})
        if nested:
            nested.decompose()
    a = copy.find("a", href=ARTICLE_HREF_RE)
    return a.get("href") if a else None


def _find_content_images(block, exclude_id=None):
    copy = BeautifulSoup(str(block), "lxml")
    if exclude_id:
        nested = copy.find("article", attrs={"data-tweet-id": exclude_id})
        if nested:
            nested.decompose()
    urls = []
    for img in copy.find_all("img"):
        src = img.get("src", "")
        if "pbs.twimg.com/media/" in src and src not in urls:
            urls.append(src)
    return urls


def _extract_handle(block):
    for a in block.find_all("a", href=True):
        m = HANDLE_HREF_RE.match(a["href"])
        if m:
            return m.group(1)
    return None


def _strip_chrome(text, handle):
    """Removes the author name/handle/date header and trailing engagement-stat
    footer from a block's raw get_text() output. Only used when there's no
    clean API text to prefer instead."""
    lines = text.split("\n")

    if handle:
        marker = f"@{handle}"
        if marker in lines[:4]:
            lines = lines[lines.index(marker) + 1:]
            while lines and (
                lines[0].strip() in ("Article", "")
                or _MONTH_ABBR_RE.match(lines[0].strip())
            ):
                lines.pop(0)

    while lines and (
        _STAT_LINE_RE.match(lines[-1].strip())
        or lines[-1].strip() in ("Views", "")
        or _TIME_LINE_RE.match(lines[-1].strip())
    ):
        lines.pop()

    return "\n".join(lines).strip()


def _fetch_article_text_trafilatura(url):
    """Fallback only — used when the BeautifulSoup article-page parse below
    comes back empty (page structure changed, etc)."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return text.strip() if text else None
    except Exception as e:
        print(f"trafilatura fallback failed for {url}: {e}")
        return None


def _scrape_article_page(article_url):
    """Dedicated x.com/i/article/<id> pages use a different template than
    status pages: <article class="mx-auto..."> > <h1> + <div class="x-article-body">.
    Returns (text, image_urls)."""
    try:
        resp = httpx.get(article_url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        body = soup.find("div", class_="x-article-body")
        if body:
            title_el = soup.find("h1")
            title = title_el.get_text(strip=True) if title_el else ""
            text = body.get_text(separator="\n", strip=True)
            full_text = f"{title}\n\n{text}".strip() if title else text
            images = [
                img.get("src")
                for img in body.find_all("img")
                if "pbs.twimg.com/media/" in (img.get("src") or "")
            ]
            if full_text:
                return full_text, images
    except Exception as e:
        print(f"Article page parse failed for {article_url}: {e}")

    text = _fetch_article_text_trafilatura(article_url)
    return (text, []) if text else (None, [])


def is_bare_url_only(text):
    """True if text is nothing but a single bare URL and no other content.
    Shared with scripts/migrate_content_columns.py."""
    stripped = (text or "").strip()
    return bool(stripped) and bool(BARE_URL_ONLY_RE.match(stripped))


def _fetch_pdf_bytes_if_pdf(url):
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        if "application/pdf" in content_type or resp.content[:5] == b"%PDF-":
            return resp.content
    except Exception as e:
        print(f"PDF check failed for {url}: {e}")
    return None


def _extract_pdf_text(pdf_bytes, max_pages=4):
    import io
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = reader.pages[:max_pages]
    return "\n".join((p.extract_text() or "") for p in pages)


def _classify_and_extract_abstract_openai(pdf_text):
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=OPENAI_TEXT_MODEL,
        max_tokens=800,
        messages=[
            {"role": "system", "content": PDF_ABSTRACT_INSTRUCTION},
            {"role": "user", "content": pdf_text[:12000]},
        ],
    )
    return resp.choices[0].message.content.strip()


def _classify_and_extract_abstract_anthropic(pdf_text):
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=ANTHROPIC_TEXT_MODEL,
        max_tokens=800,
        system=PDF_ABSTRACT_INSTRUCTION,
        messages=[{"role": "user", "content": pdf_text[:12000]}],
    )
    return next(b.text for b in resp.content if b.type == "text").strip()


def _classify_and_extract_abstract(pdf_text):
    """Decides, on the fly, whether a PDF is a research paper and (if so)
    returns just its abstract. OpenAI first, Anthropic fallback — if neither
    can decide, treat it the same as "not a research paper" rather than
    leaving the bookmark unresolved."""
    text = None
    if os.getenv("OPENAI_API_KEY"):
        try:
            text = _classify_and_extract_abstract_openai(pdf_text)
        except Exception as e:
            print(f"PDF abstract classification (OpenAI) failed: {e}")
    else:
        print("PDF abstract classification: OPENAI_API_KEY not set, skipping to Anthropic fallback")

    if text is None and os.getenv("ANTHROPIC_API_KEY"):
        try:
            text = _classify_and_extract_abstract_anthropic(pdf_text)
        except Exception as e:
            print(f"PDF abstract classification (Anthropic fallback) failed: {e}")

    return None if (not text or text.upper() == "NO") else text


def _resolve_bare_url_pdf(text):
    """If text is nothing but a bare URL and that URL serves a PDF, returns
    replacement text: the paper's abstract if it's a research paper, else an
    explanatory placeholder. Returns None if text isn't a bare URL, the URL
    isn't actually a PDF, or extraction fails — callers should keep the
    original text in that case."""
    stripped = text.strip()
    if not is_bare_url_only(stripped):
        return None

    url = stripped if stripped.startswith("http") else f"https://{stripped}"
    pdf_bytes = _fetch_pdf_bytes_if_pdf(url)
    if not pdf_bytes:
        return None

    not_a_paper_message = (
        f"This link leads to a PDF which doesn't seem to be a research paper, "
        f"hence only retaining the link: {url}"
    )

    try:
        pdf_text = _extract_pdf_text(pdf_bytes)
    except Exception as e:
        print(f"PDF text extraction failed for {url}: {e}")
        return not_a_paper_message

    if not pdf_text.strip():
        return not_a_paper_message

    abstract = _classify_and_extract_abstract(pdf_text)
    return abstract if abstract else not_a_paper_message


def _finalize_short_text(text, image_urls):
    """For short (non-article) block text: if it's literally just a bare
    URL, try resolving it as a PDF instead of leaving a dead link as the
    final content."""
    resolved = _resolve_bare_url_pdf(text)
    if resolved:
        return {"text": resolved, "scraped": True, "image_urls": image_urls}
    return {"text": text, "scraped": False, "image_urls": image_urls}


def _process_block(block, exclude_id, clean_text_fallback=None):
    """Returns {"text", "scraped", "image_urls"} for one tweet block (primary
    or quoted). Follows an /i/article/ link if present; otherwise, for the
    common case (no article, no quote, no images) prefers clean_text_fallback
    — the original API tweet text — over the HTML block's own get_text(),
    which carries name/handle/stat chrome that needs stripping. Only the
    primary tweet has API text available; quoted tweets always go through
    chrome-stripping."""
    raw_text = _own_text(block, exclude_id=exclude_id)
    article_href = _find_article_href(block, exclude_id=exclude_id)

    if article_href:
        article_url = f"https://x.com{article_href}"
        scraped_text, scraped_images = _scrape_article_page(article_url)
        if scraped_text:
            return {"text": scraped_text, "scraped": True, "image_urls": scraped_images}
        print(f"Article scrape failed for {article_url}, falling back to teaser text")

    image_urls = _find_content_images(block, exclude_id=exclude_id)

    if len(raw_text) > ARTICLE_LEN_THRESHOLD:
        # X inlined a full article body directly into this block's own status
        # page — no separate fetch needed, but it's scraped content, not a
        # plain tweet, so still clean the chrome off it.
        handle = _extract_handle(block)
        return {"text": _strip_chrome(raw_text, handle), "scraped": True, "image_urls": image_urls}

    if clean_text_fallback:
        return _finalize_short_text(clean_text_fallback, image_urls)

    handle = _extract_handle(block)
    return _finalize_short_text(_strip_chrome(raw_text, handle), image_urls)


# ---------------------------------------------------------------------------
# Image description (OpenAI first, Anthropic fallback). Bytes only ever live
# in memory — never written to disk or the DB.
# ---------------------------------------------------------------------------

def _guess_mime(url, content_type):
    if content_type and content_type.startswith("image/"):
        return content_type.split(";")[0].strip()
    lower = url.lower()
    if ".png" in lower:
        return "image/png"
    if ".gif" in lower:
        return "image/gif"
    if ".webp" in lower:
        return "image/webp"
    return "image/jpeg"


def _download_image_bytes(url):
    resp = httpx.get(url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    return resp.content, _guess_mime(url, resp.headers.get("content-type"))


def _describe_image_openai(image_bytes, mime):
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    b64 = base64.b64encode(image_bytes).decode()
    resp = client.chat.completions.create(
        model=OPENAI_VISION_MODEL,
        max_tokens=500,
        messages=[
            {"role": "system", "content": VISION_INSTRUCTION},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image per the instructions."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            },
        ],
    )
    return resp.choices[0].message.content.strip()


def _describe_image_anthropic(image_bytes, mime):
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    b64 = base64.b64encode(image_bytes).decode()
    resp = client.messages.create(
        model=ANTHROPIC_VISION_MODEL,
        max_tokens=500,
        system=VISION_INSTRUCTION,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": "Describe this image per the instructions."},
                ],
            }
        ],
    )
    return next(b.text for b in resp.content if b.type == "text").strip()


def _describe_image(url):
    """Returns (description, error). description is None if both providers failed."""
    try:
        image_bytes, mime = _download_image_bytes(url)
    except Exception as e:
        return None, f"download failed: {type(e).__name__}: {e}"

    openai_error = None
    if os.getenv("OPENAI_API_KEY"):
        try:
            return _describe_image_openai(image_bytes, mime), None
        except Exception as e:
            openai_error = f"{type(e).__name__}: {e}"
    else:
        openai_error = "OPENAI_API_KEY not set"

    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return _describe_image_anthropic(image_bytes, mime), None
        except Exception as e:
            return None, f"openai failed ({openai_error}); anthropic failed ({type(e).__name__}: {e})"

    return None, f"openai failed ({openai_error}); anthropic not configured (ANTHROPIC_API_KEY not set)"


def _describe_and_append_images(image_urls):
    """Returns (appended_text, image_processing_status). Descriptions are
    always appended at the end of full_content — never spliced inline."""
    image_urls = list(dict.fromkeys(image_urls))  # dedupe, preserve order
    if not image_urls:
        return "", "no_images_found"

    descriptions = []
    failures = 0
    for url in image_urls:
        description, error = _describe_image(url)
        if description:
            descriptions.append(description)
        else:
            failures += 1
            print(f"Image description failed for {url}: {error}")

    if not descriptions:
        return "", "images_fetch_failed"

    status = "images_partially_appended" if failures else "images_appended_successfully"
    return "\n\n".join(descriptions), status


# ---------------------------------------------------------------------------
# Top-level enrichment: fetch a tweet's own status page and build the final
# full_content / content_source / image_processing_status / quoted_tweet_id.
# ---------------------------------------------------------------------------

def _enrich_via_html(tweet_url, tweet_id, api_text):
    """Returns None on hard failure (page unreachable or unexpected shape) —
    caller falls back to the bare API tweet text."""
    resp = httpx.get(tweet_url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    primary_block = soup.find("article", attrs={"data-tweet-id": tweet_id})
    if primary_block is None:
        return None

    quoted_block = primary_block.find("article", attrs={"data-tweet-id": True})
    quoted_id = quoted_block.get("data-tweet-id") if quoted_block else None

    primary = _process_block(primary_block, exclude_id=quoted_id, clean_text_fallback=api_text)
    full_content = primary["text"]
    scraped_any = primary["scraped"]
    image_urls = list(primary["image_urls"])

    if quoted_block is not None:
        quoted = _process_block(quoted_block, exclude_id=None)
        handle = _extract_handle(quoted_block)
        quoted_label = f"@{handle}" if handle else "the quoted tweet"
        full_content = f"{full_content}\n\n---\nQuoted {quoted_label}:\n{quoted['text']}"
        scraped_any = scraped_any or quoted["scraped"]
        image_urls.extend(quoted["image_urls"])

    appended, image_status = _describe_and_append_images(image_urls)
    if appended:
        full_content = f"{full_content}\n\n{appended}"

    return {
        "full_content": full_content.strip(),
        "content_source": "api_scraped_article" if scraped_any else "api_text",
        "image_processing_status": image_status,
        "quoted_tweet_id": quoted_id,
    }


def enrich_tweet_content(api_text, author_username, tweet_id):
    """Public entry point used by fetch_bookmarks() and the one-time
    migration script. Given the bare API tweet text plus enough to build the
    tweet's own status URL, returns
    (full_content, content_source, image_processing_status, quoted_tweet_id).
    Falls back to the bare API text if the HTML page can't be fetched/parsed."""
    api_text = api_text or ""

    if author_username:
        tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"
        try:
            enriched = _enrich_via_html(tweet_url, tweet_id, api_text)
        except Exception as e:
            print(f"HTML enrichment failed for {tweet_url}: {e}")
            enriched = None
    else:
        enriched = None

    if enriched:
        return (
            enriched["full_content"],
            enriched["content_source"],
            enriched["image_processing_status"],
            enriched["quoted_tweet_id"],
        )

    content_source = "api_teaser_only" if BARE_LINK_RE.match(api_text.strip()) else "api_text"
    return api_text, content_source, "no_images_found", None


def backfill_missing_images(author_username, tweet_id, existing_full_content):
    """For a bookmark synced before the image-description pipeline existed:
    fetch the tweet's own status page, find any content images ON THE
    PRIMARY TWEET ONLY (never the quoted tweet's — these bookmarks predate
    quote-tweet handling entirely, so there's no established quoted-tweet
    section in existing_full_content to append into), describe them, and
    append. Doesn't touch anything else — no article/quote-tweet detection,
    no text changes beyond the append.

    Returns (new_full_content, image_processing_status), or None if no
    fetchable images were found (page unreachable, or nothing there
    anymore) — caller should leave the row untouched in that case."""
    if not author_username:
        return None

    tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"
    try:
        resp = httpx.get(tweet_url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"Fetch failed for {tweet_url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    primary_block = soup.find("article", attrs={"data-tweet-id": tweet_id})
    if primary_block is None:
        return None

    quoted_block = primary_block.find("article", attrs={"data-tweet-id": True})
    quoted_id = quoted_block.get("data-tweet-id") if quoted_block else None

    image_urls = _find_content_images(primary_block, exclude_id=quoted_id)
    if not image_urls:
        return None

    appended, image_status = _describe_and_append_images(image_urls)
    if not appended:
        return None

    new_content = f"{existing_full_content}\n\n{appended}".strip()
    return new_content, image_status


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

            full_content, content_source, image_processing_status, quoted_tweet_id = enrich_tweet_content(
                tweet.get("text", ""), author_username, tweet_id
            )

            page_results.append(
                {
                    "tweet_id": tweet_id,
                    "author_username": author_username,
                    "author_name": author.get("name"),
                    "full_content": full_content,
                    "content_source": content_source,
                    "image_processing_status": image_processing_status,
                    "quoted_tweet_id": quoted_tweet_id,
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
