import json
import os
import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-20250514"
BATCH_SIZE = 25

BASE_SYSTEM_PROMPT = """You are analyzing a collection of bookmarked posts from X (Twitter). Your job is to:
1. Identify meaningful, specific categories that reflect the actual content themes
2. Assign each post to exactly one category
3. Write a ~128 word summary for each post capturing its key insight or argument

Rules:
- Create 5-15 categories maximum, depending on content diversity
- Category names must be specific (e.g. "AI Safety Research", "Startup GTM Strategy") not generic (e.g. "Interesting", "Tech", "Other")
- Summaries must capture what the post actually argues or reveals, not just describe it
- Return ONLY a valid JSON array. No markdown, no explanation, no code fences."""

INCREMENTAL_EXTRA = "\n- You have these existing categories: {categories}. Assign to existing categories where possible. Only create a new category if the content genuinely doesn't fit any existing one."


def categorize_bookmarks(bookmarks, existing_categories=None, on_batch_complete=None):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    system_prompt = BASE_SYSTEM_PROMPT
    if existing_categories:
        system_prompt += INCREMENTAL_EXTRA.format(categories=existing_categories)

    categorized = []
    batches = [bookmarks[i:i + BATCH_SIZE] for i in range(0, len(bookmarks), BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        if not on_batch_complete:
            print(f"Categorizing batch {idx}/{len(batches)} ({len(batch)} posts)...")
        batch_results, usage = _categorize_batch(client, system_prompt, batch)
        categorized.extend(batch_results)
        if on_batch_complete:
            on_batch_complete(idx, len(batches), usage)

    tweet_map = {b["tweet_id"]: b for b in bookmarks}
    final = []
    skipped = 0
    for item in categorized:
        if not isinstance(item, dict):
            skipped += 1
            continue
        original = tweet_map.get(item.get("tweet_id", ""))
        if original and item.get("category") and item.get("summary"):
            merged = {**original, "category": item["category"], "summary": item["summary"]}
            final.append(merged)
        else:
            skipped += 1

    if skipped:
        print(f"\nWARNING: {skipped} items skipped due to missing category/summary in Claude response")

    return final


def _categorize_batch(client, system_prompt, batch):
    posts = [
        {"tweet_id": b["tweet_id"], "content": b["full_content"], "author": b.get("author_username", "")}
        for b in batch
    ]
    user_prompt = (
        "Categorize these bookmarked posts. Return a JSON array where each object has exactly these keys:\n"
        "- tweet_id (string, unchanged from input)\n"
        "- category (string)\n"
        "- summary (string, ~128 words)\n\n"
        f"Posts:\n{json.dumps(posts, indent=2)}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=8096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    raw = response.content[0].text.strip()
    results, final_usage = _parse_json_response(client, system_prompt, user_prompt, raw, usage=usage)
    return results, final_usage


def _parse_json_response(client, system_prompt, user_prompt, raw, usage=None, retry=False):
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise json.JSONDecodeError("Expected a JSON array", raw, 0)
        return parsed, usage or {}
    except json.JSONDecodeError:
        if retry:
            print(f"ERROR: Could not parse JSON after retry. Raw response:\n{raw}")
            return [], usage or {}

        print("JSON parse failed, retrying with fix-up prompt...")
        fix_response = client.messages.create(
            model=MODEL,
            max_tokens=8096,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "The response above is not valid JSON. Please return ONLY the valid JSON array with no other text.",
                },
            ],
        )
        retry_usage = {
            "input_tokens": (usage or {}).get("input_tokens", 0) + fix_response.usage.input_tokens,
            "output_tokens": (usage or {}).get("output_tokens", 0) + fix_response.usage.output_tokens,
        }
        fixed = fix_response.content[0].text.strip()
        return _parse_json_response(client, system_prompt, user_prompt, fixed, usage=retry_usage, retry=True)
