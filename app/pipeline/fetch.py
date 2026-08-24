import json
import os
import re
import time
import httpx
import trafilatura
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from app.pipeline.auth import get_valid_access_token
from app.pipeline.categorize import (
    NOT_ENOUGH_CONTENT_CATEGORY,
    NOT_ENOUGH_CONTENT_TAG,
    NOT_ENOUGH_CONTENT_SUMMARY,
)

load_dotenv()

BASE_URL = "https://api.x.com/2"

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
# A row's own text (and separately, whatever a link in it resolves to) gets
# classified against these two bars — see resolve_bookmark_rows / _resolve_tweet.
MIN_CATEGORIZE_WORDS = 40   # below this, with nothing rescuing it: not worth an LLM call at all
MIN_SUMMARIZE_WORDS = 300   # at/above this: substantial enough to actually compress into a summary
MAX_QUOTE_DEPTH = 3  # OP -> QP -> QQP; anything QQP itself quotes is skipped
ARTICLE_HREF_RE = re.compile(r"^/i/article/\d+")
BARE_LINK_RE = re.compile(r"^https://t\.co/\w+$")
BARE_URL_ONLY_RE = re.compile(r"^(https?://|www\.)?[\w.-]+\.[a-z]{2,}(/\S*)?$", re.IGNORECASE)
HANDLE_HREF_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})$")
# A quote-tweet's own permalink/date anchor, e.g. href="/staysaasy/status/2042063369432183238"
# — the only place its tweet_id is still exposed since X dropped the
# data-tweet-id attribute (see _extract_quoted_identity).
STATUS_HREF_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)")
ARTICLE_LEN_THRESHOLD = 1500  # chars — signals a tweet's own status page already inlines a full article body

# Chrome around a tweet block's own text (author name/handle header, date,
# "Article" label, trailing engagement-stat numbers) — stripped only when we
# have no clean API text to use instead (i.e. for quoted tweets, and for a
# primary tweet whose own status page inlined a full article body).
_MONTH_ABBR_RE = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z]*\.? \d{1,2}$")
_STAT_LINE_RE = re.compile(r"^[\d.,]+[KM]?$")
_TIME_LINE_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)?\s*[·\-]")

OPENAI_TEXT_MODEL = "gpt-4o"
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
# a tweet's own status page, and a quote-tweet is a nested <article> element
# inside the primary tweet's own block. X identifies the primary tweet via
# itemid="https://x.com/i/status/<id>" on its own <article> — the nested
# quote-tweet <article> carries no such id at all, so its identity has to be
# read off its own permalink/date anchor instead (see
# _extract_quoted_identity). Any <article> found via block.find("article")
# — i.e. scoped to block's own descendants, which correctly excludes block
# itself — is unambiguously a nested quote, never an unrelated reply or
# "more from this author" block; those render as page-level siblings, not
# nested inside the primary tweet's own subtree.
# ---------------------------------------------------------------------------

def _own_text(block):
    """get_text() of a block with any nested quote-tweet subtree stripped out.
    Operates on an independent re-parsed copy so the caller's own tree
    (block, and whatever it's nested inside) is untouched."""
    copy = BeautifulSoup(str(block), "lxml")
    root = copy.find("article")
    if root is None:
        return copy.get_text(separator="\n", strip=True)
    nested = root.find("article")
    if nested:
        nested.decompose()
    return root.get_text(separator="\n", strip=True)


def _find_article_href(block):
    copy = BeautifulSoup(str(block), "lxml")
    root = copy.find("article")
    if root is None:
        return None
    nested = root.find("article")
    if nested:
        nested.decompose()
    a = root.find("a", href=ARTICLE_HREF_RE)
    return a.get("href") if a else None


def _extract_handle(block):
    for a in block.find_all("a", href=True):
        m = HANDLE_HREF_RE.match(a["href"])
        if m:
            return m.group(1)
    return None


def _extract_quoted_identity(quoted_block):
    """Returns (handle, tweet_id) for a nested quote-tweet block, read off
    its own permalink/date anchor — X no longer exposes a stable id
    attribute on nested articles, so this anchor is the only remaining
    source for either value. Returns (None, None) if not found."""
    a = quoted_block.find("a", href=STATUS_HREF_RE)
    if not a:
        return None, None
    m = STATUS_HREF_RE.match(a["href"])
    return (m.group(1), m.group(2)) if m else (None, None)


_X_DOMAIN_PREFIXES = ("https://x.com", "http://x.com", "https://twitter.com", "http://twitter.com")


def _is_internal_x_href(href):
    """True for hrefs that stay within x.com/twitter.com itself — profile
    links, status permalinks, hashtags — never a link the post is actually
    pointing readers to."""
    return href.startswith("/") or href.startswith(_X_DOMAIN_PREFIXES)


def _extract_first_href(block):
    """Finds the first real external link's href within a block's own
    subtree (nested quote-tweet content excluded) — reads actual anchor
    hrefs rather than the block's flattened display text, since X visually
    truncates long URLs in tweet text with an ellipsis while the href
    itself always carries the real, untruncated URL. Prefers an expanded
    href over its t.co redirect wrapper when both are present for the same
    link (X commonly renders both), since the expanded one saves a
    redirect hop. Returns None if block has no matching anchor at all —
    callers should fall back to text-based extraction in that case."""
    copy = BeautifulSoup(str(block), "lxml")
    root = copy.find("article")
    if root is None:
        return None
    nested = root.find("article")
    if nested:
        nested.decompose()

    candidates = [
        a["href"] for a in root.find_all("a", href=True)
        if a["href"].startswith("http") and not _is_internal_x_href(a["href"])
    ]
    non_tco = [h for h in candidates if "t.co/" not in h]
    return (non_tco or candidates or [None])[0]


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
    """General-purpose "download this URL and extract its readable article
    text" helper — has no idea what site it's looking at. Used both as a
    fallback when the BeautifulSoup parse of X's own native Article pages
    comes back empty, and as the main path for any other external site."""
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
    Returns text, or None."""
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
            if full_text:
                return full_text
    except Exception as e:
        print(f"Article page parse failed for {article_url}: {e}")

    return _fetch_article_text_trafilatura(article_url)


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


def _resolve_external_link(url):
    """Given a URL (scheme included), returns its resolved text content, or
    None if nothing could be extracted. PDFs are checked first: a research
    paper yields just its abstract, anything else a short explanatory
    placeholder. Non-PDF links are scraped generically via trafilatura — no
    attempt to detect a paper's HTML landing page by URL shape; downstream
    summarization is left to recognize that on its own. Shared by both the
    sync-time pipeline and the on-demand get_full_content path."""
    pdf_bytes = _fetch_pdf_bytes_if_pdf(url)
    if pdf_bytes:
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

    return _fetch_article_text_trafilatura(url)


def _finalize_short_text(text, block=None):
    """For short (non-article) block text: look for a URL anywhere in it —
    not just when the block is nothing else — and try to resolve what it
    points to (a PDF's abstract, or a generic external article) rather than
    leaving a link as dead text. Prefers reading the real href straight off
    block's own anchors (untruncated) over scanning text, when block is
    given — falls back to text-scanning otherwise (or if the block has no
    matching anchor)."""
    url = (_extract_first_href(block) if block is not None else None) or _extract_first_url(text)
    if not url:
        return {"text": text, "scraped": False}

    normalized_url = url if url.startswith("http") else f"https://{url}"
    resolved = _resolve_external_link(normalized_url)
    if not resolved:
        return {"text": text, "scraped": False}

    if is_bare_url_only(text.strip()):
        return {"text": resolved, "scraped": True}
    return {"text": f"{text}\n\n---\nLinked content:\n{resolved}", "scraped": True}


def _process_block(block, clean_text_fallback=None):
    """Returns {"text", "scraped"} for one tweet block (primary or quoted).
    Follows an /i/article/ link if present; otherwise, for the common case
    (no article, no quote) prefers clean_text_fallback — the original API
    tweet text — over the HTML block's own get_text(), which carries
    name/handle/stat chrome that needs stripping. Only the primary tweet has
    API text available; quoted tweets always go through chrome-stripping."""
    raw_text = _own_text(block)
    article_href = _find_article_href(block)

    if article_href:
        article_url = f"https://x.com{article_href}"
        scraped_text = _scrape_article_page(article_url)
        if scraped_text:
            return {"text": scraped_text, "scraped": True}
        print(f"Article scrape failed for {article_url}, falling back to teaser text")

    if len(raw_text) > ARTICLE_LEN_THRESHOLD:
        # X inlined a full article body directly into this block's own status
        # page — no separate fetch needed, but it's scraped content, not a
        # plain tweet, so still clean the chrome off it.
        handle = _extract_handle(block)
        return {"text": _strip_chrome(raw_text, handle), "scraped": True}

    if clean_text_fallback:
        return _finalize_short_text(clean_text_fallback, block=block)

    handle = _extract_handle(block)
    return _finalize_short_text(_strip_chrome(raw_text, handle), block=block)


# ---------------------------------------------------------------------------
# Top-level enrichment: fetch a tweet's own status page and build the final
# full_content / content_source / embedded_quote_tweet_id.
# ---------------------------------------------------------------------------

def _enrich_via_html(tweet_url, tweet_id, api_text):
    """Returns None on hard failure (page unreachable or unexpected shape) —
    caller falls back to the bare API tweet text."""
    resp = httpx.get(tweet_url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    primary_block = soup.find("article", attrs={"itemid": f"https://x.com/i/status/{tweet_id}"})
    if primary_block is None:
        return None

    quoted_block = primary_block.find("article")
    quoted_handle, quoted_id = _extract_quoted_identity(quoted_block) if quoted_block else (None, None)

    primary = _process_block(primary_block, clean_text_fallback=api_text)
    full_content = primary["text"]
    scraped_any = primary["scraped"]

    if quoted_block is not None:
        quoted = _process_block(quoted_block)
        quoted_label = f"@{quoted_handle}" if quoted_handle else "the quoted tweet"
        full_content = f"{full_content}\n\n---\nQuoted {quoted_label}:\n{quoted['text']}"
        scraped_any = scraped_any or quoted["scraped"]

    return {
        "full_content": full_content.strip(),
        "content_source": "api_scraped_article" if scraped_any else "api_text",
        "embedded_quote_tweet_id": quoted_id,
    }


def enrich_tweet_content(api_text, author_username, tweet_id):
    """Public entry point used by the one-time migration scripts and the
    on-demand get_full_content MCP tool. Given the bare API tweet text plus
    enough to build the tweet's own status URL, returns (full_content,
    content_source, embedded_quote_tweet_id). Falls back to the bare API
    text if the HTML page can't be fetched/parsed.

    embedded_quote_tweet_id is NOT the quoted_from_tweet_id database column
    — it's discovered fresh from this call's own live HTML parse (X's own
    DOM nests a quote tweet's <article> inside its quoter's), unrelated to
    and computed independently of anything already in the database."""
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
            enriched["embedded_quote_tweet_id"],
        )

    content_source = "api_teaser_only" if BARE_LINK_RE.match(api_text.strip()) else "api_text"
    return api_text, content_source, None


# ---------------------------------------------------------------------------
# Sync-time content resolution: decides, per tweet (recursively for anything
# it quotes), how much a row's own text (and separately, whatever a link in
# it points to) is worth — nothing (skip the LLM entirely), enough to
# categorize/tag but not summarize independently, or enough to summarize for
# real. A quoted tweet is never merged into its quoter's text — it becomes
# its own row, up to 3 levels deep (OP -> QP -> QQP). Images are never
# touched here — image description doesn't exist anywhere in this app.
# ---------------------------------------------------------------------------

def _extract_first_url(text):
    """Finds the first whitespace-delimited token that's a URL — reuses
    is_bare_url_only per-token rather than a fresh regex, since that's
    already validated safe against both scheme (https://...) and schemeless
    (arxiv.org/pdf/...) links without false-positiving on ordinary prose."""
    for token in (text or "").split():
        candidate = token.rstrip(").,;!?\"'")
        if is_bare_url_only(candidate):
            return candidate
    return None


def _resolve_tweet(tweet_id, author_username, api_text=None, depth=1, max_depth=MAX_QUOTE_DEPTH):
    """Fetches this tweet's own status page and resolves it. Returns None if
    unreachable (deleted, protected, etc) — caller decides what to do about
    that (the top-level bookmark falls back to bare API text; a quoted
    tweet found unreachable is simply dropped, no row for it).

    Returns a dict: tweet_id, author_username, post_text (set whenever this
    row isn't going through real summarization — the verbatim own text,
    plus any resolved link content appended), content_for_summary (text to
    hand the categorizer — None means nothing substantial anywhere, LLM
    skipped entirely for this row), force_summary_sentinel (True when
    content_for_summary is only there for category/tags — the LLM's summary
    output for this row gets discarded and replaced with
    NOT_ENOUGH_CONTENT_SUMMARY), and quoted_child (a nested dict of the same
    shape for whatever this tweet quotes, or None)."""
    tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"
    try:
        resp = httpx.get(tweet_url, timeout=30, follow_redirects=True, headers=BROWSER_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"Fetch failed for {tweet_url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    primary_block = soup.find("article", attrs={"itemid": f"https://x.com/i/status/{tweet_id}"})
    if primary_block is None:
        return None

    quoted_block = primary_block.find("article")
    quoted_handle, quoted_id = _extract_quoted_identity(quoted_block) if quoted_block else (None, None)

    handle = _extract_handle(primary_block)
    html_own_text = _strip_chrome(_own_text(primary_block), handle)

    if len(html_own_text) > ARTICLE_LEN_THRESHOLD:
        # X inlined a full article body directly into this tweet's own
        # status page — this is exactly the original bug this whole
        # pipeline was built to catch: the bookmarks API's own text field
        # (api_text) only ever gives a bare teaser link for these, never
        # the real body. Must check this BEFORE preferring api_text, not
        # after, or that whole class of post silently regresses.
        own_text = html_own_text
    elif api_text:
        # Ordinary tweet — api_text is only ever given for the top-level
        # bookmarked tweet (OP), and it's cleaner than HTML-derived text
        # (no chrome-strip residue risk). Quoted tweets (QP/QQP) have no
        # API text of their own; always chrome-stripped HTML in that case.
        own_text = api_text
    else:
        own_text = html_own_text

    own_word_count = len(own_text.split())

    # A link is chased for every post, regardless of its own length — a
    # 250-word post with a link at the end still deserves the linked
    # content folded into its summary, not just its own words. Prefer the
    # real href straight off the page's own anchors (untruncated) over
    # scanning own_text, which may be X's visually-truncated display text.
    external_text = None
    external_word_count = 0
    url = _extract_first_href(primary_block) or _extract_first_url(own_text)
    if url:
        normalized_url = url if url.startswith("http") else f"https://{url}"
        try:
            external_text = _resolve_external_link(normalized_url)
        except Exception as e:
            print(f"Link resolution failed for {normalized_url}: {e}")
        if external_text:
            external_word_count = len(external_text.split())

    if own_word_count >= MIN_SUMMARIZE_WORDS or external_word_count >= MIN_SUMMARIZE_WORDS:
        # Enough for a real summary — combine own text with whatever the
        # link resolved to, whenever there is one, regardless of which side
        # actually cleared the bar.
        post_text = None
        content_for_summary = (
            f"{own_text}\n\n---\nLinked content:\n{external_text}" if external_text else own_text
        )
        force_summary_sentinel = False
    elif own_word_count >= MIN_CATEGORIZE_WORDS or MIN_CATEGORIZE_WORDS <= external_word_count < MIN_SUMMARIZE_WORDS:
        # Enough to categorize/tag for real, not enough to summarize on its
        # own — store verbatim (own text, plus a short linked snippet
        # appended if that's what pushed it into this band), and mark the
        # LLM's own summary attempt to be discarded after the fact.
        if external_text:
            post_text = (
                external_text if is_bare_url_only(own_text.strip())
                else f"{own_text}\n\n---\nLinked content:\n{external_text}"
            )
        else:
            post_text = own_text
        content_for_summary = post_text
        force_summary_sentinel = True
    else:
        # Nothing substantial anywhere — own text is thin, and there's
        # either no link or the link didn't resolve to anything worthwhile.
        post_text = own_text
        content_for_summary = None
        force_summary_sentinel = False

    quoted_child = None
    if quoted_block is not None and depth < max_depth and quoted_handle and quoted_id:
        quoted_child = _resolve_tweet(quoted_id, quoted_handle, depth=depth + 1, max_depth=max_depth)
        # quoted_child is None if that tweet's own status page is
        # unreachable — dropped silently, no row for it, per design.

    return {
        "tweet_id": tweet_id,
        "author_username": author_username,
        "post_text": post_text,
        "content_for_summary": content_for_summary,
        "force_summary_sentinel": force_summary_sentinel,
        "quoted_child": quoted_child,
    }


def _flatten_resolved_tree(resolved, quoted_from=None):
    """Turns the nested quoted_child structure into a flat list of row
    dicts, each carrying quoted_from_tweet_id pointing at whichever row
    quoted it (None for the top-level bookmark itself)."""
    row = {
        "tweet_id": resolved["tweet_id"],
        "author_username": resolved["author_username"],
        "post_text": resolved["post_text"],
        "content_for_summary": resolved["content_for_summary"],
        "force_summary_sentinel": resolved["force_summary_sentinel"],
        "quoted_from_tweet_id": quoted_from,
    }
    rows = [row]
    if resolved.get("quoted_child"):
        rows.extend(_flatten_resolved_tree(resolved["quoted_child"], quoted_from=resolved["tweet_id"]))
    return rows


def resolve_bookmark_rows(tweet_id, author_username, api_text):
    """Top-level entry point used by fetch_bookmarks(): resolves a newly
    bookmarked tweet — and, recursively, anything it quotes up to
    MAX_QUOTE_DEPTH — into a flat list of row-ready dicts. The bookmarked
    tweet itself (OP) is always the first entry, even if its own status
    page turns out to be unreachable (falls back to the bare API text
    rather than being dropped, with no link-chasing — just a word-count
    check against the bare text); any quoted tweet found unreachable is
    simply absent from the list, not present as a broken row."""
    resolved = None
    if author_username:
        try:
            resolved = _resolve_tweet(tweet_id, author_username, api_text=api_text)
        except Exception as e:
            print(f"HTML resolution failed for {tweet_id}: {e}")

    if resolved is None:
        api_text = api_text or ""
        word_count = len(api_text.split())
        if word_count >= MIN_SUMMARIZE_WORDS:
            post_text, content_for_summary, force_summary_sentinel = None, api_text, False
        elif word_count >= MIN_CATEGORIZE_WORDS:
            post_text, content_for_summary, force_summary_sentinel = api_text, api_text, True
        else:
            post_text, content_for_summary, force_summary_sentinel = api_text, None, False
        resolved = {
            "tweet_id": tweet_id,
            "author_username": author_username,
            "post_text": post_text,
            "content_for_summary": content_for_summary,
            "force_summary_sentinel": force_summary_sentinel,
            "quoted_child": None,
        }

    return _flatten_resolved_tree(resolved)


def build_bookmark_dict(row, is_op, author_name=None, media_urls=None, bookmarked_at=None):
    """Turns one resolve_bookmark_rows() row into an insert/update-ready
    bookmark dict — category/summary/tags either filled in deterministically
    (nothing substantial anywhere for this row) or left absent for the
    caller to run through categorize_bookmarks() next.

    author_name/media_urls/bookmarked_at only ever apply to the OP (a
    derived quote row has none of these — same as fetch_bookmarks() already
    treats them for a freshly-discovered QP/QQP, since a quote row was never
    itself returned by the bookmarks API). Shared by fetch_bookmarks() (has
    these live from the API response for the OP) and the one-time backfill
    script (re-processing existing rows, which already has these values
    stored from the row's original sync and passes none in here — it
    updates them separately, or not at all, rather than through this dict)."""
    row_author = row["author_username"]
    content_for_summary = row["content_for_summary"]

    bookmark = {
        "tweet_id": row["tweet_id"],
        "author_username": row_author,
        "author_name": author_name if is_op else None,
        "post_text": row["post_text"],
        "quoted_from_tweet_id": row["quoted_from_tweet_id"],
        "media_urls": media_urls if is_op else None,
        "tweet_url": (
            f"https://x.com/{row_author}/status/{row['tweet_id']}"
            if row_author else None
        ),
        "bookmarked_at": bookmarked_at if is_op else None,
    }

    if content_for_summary is None:
        # Nothing substantial anywhere for this row — decided
        # deterministically (word count), so skip the LLM call entirely
        # rather than asking it to summarize nothing.
        bookmark["category"] = NOT_ENOUGH_CONTENT_CATEGORY
        bookmark["summary"] = NOT_ENOUGH_CONTENT_SUMMARY
        bookmark["tags"] = json.dumps([NOT_ENOUGH_CONTENT_TAG])
    else:
        # category/summary/tags deliberately absent — the caller runs this
        # through categorize_bookmarks() next.
        bookmark["content_for_summary"] = content_for_summary
        if row["force_summary_sentinel"]:
            bookmark["_force_summary_sentinel"] = True

    return bookmark


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
            raw_text = tweet.get("text", "")

            # A single bookmarked tweet can resolve to several rows: itself,
            # plus anything it quotes (up to MAX_QUOTE_DEPTH), each a fully
            # independent bookmark with its own summary/category/tags.
            for row in resolve_bookmark_rows(tweet_id, author_username, raw_text):
                is_op = row["tweet_id"] == tweet_id
                bookmark = build_bookmark_dict(
                    row, is_op,
                    author_name=author.get("name"),
                    media_urls=_extract_media_urls(tweet.get("entities")),
                    bookmarked_at=tweet.get("created_at"),
                )
                page_results.append(bookmark)

        # Dedupe within this run: the same tweet can surface twice (e.g. two
        # different OPs quoting the same post in this page, or in an
        # earlier one). DB-level INSERT OR IGNORE would catch it either
        # way, but this avoids wasting an LLM call summarizing it twice.
        seen_in_run = {r["tweet_id"] for r in results}
        new_in_page = []
        for t in page_results:
            if t["tweet_id"] in existing_tweet_ids or t["tweet_id"] in seen_in_run:
                continue
            seen_in_run.add(t["tweet_id"])
            new_in_page.append(t)
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
