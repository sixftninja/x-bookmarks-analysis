import json
import re
from app.pipeline.llm import complete

BATCH_SIZE = 25

# Fixed tag vocabulary. Unlike categories (still free-form), tags are never
# invented mid-run — the model must pick from this list (plus whatever's
# already been used on real bookmarks, passed in as known_tags) or fall back
# to RESERVED_FALLBACK_TAG. New tags only ever get added deliberately, via
# the add_tag_to_bookmark MCP tool after a human approves one.
SEED_TAGS = [
    "Model Capabilities",
    "Model Releases",
    "Model Benchmarks & Evals",
    "Model Training & Fine-tuning",
    "Agent Harness & Orchestration",
    "Agent Memory Architecture",
    "Multi-Agent Systems",
    "AI Safety & Alignment",
    "AI Interpretability",
    "Prompt Engineering",
    "Context Engineering & RAG",
    "Open Source AI",
    "Cloud Hosting & Inference Costs",
    "AI Infrastructure & Hardware",
    "Developer Tools & Coding Agents",
    "Enterprise AI Strategy",
    "Enterprise AI Adoption",
    "AI Startups & Funding",
    "AI Business Strategy",
    "AI Economics & Scaling",
    "Robotics & Embodied AI",
    "AI in Science & Healthcare",
    "AI Policy & Regulation",
    "AI Ethics & Society",
    "Space & Advanced Computing",
    "Future of Work",
    "AI Education & Learning Resources",
    "Personal Productivity",
    "Political Commentary",
    "Personal Content",
]

RESERVED_FALLBACK_TAG = "Uncategorized"

BASE_SYSTEM_PROMPT = """You are analyzing a collection of bookmarked posts from X (Twitter). Your job is to:
1. Identify meaningful, specific categories that reflect the actual content themes
2. Assign each post to exactly one category
3. Assign each post one or more tags from a fixed list (see below)
4. Write a ~128 word summary for each post capturing its key insight or argument

Category rules:
- Create 5-15 categories maximum, depending on content diversity
- Category names must be specific (e.g. "AI Safety Research", "Startup GTM Strategy") not generic (e.g. "Interesting", "Tech", "Other")
- Group posts by their broad underlying subject/topic, not by surface wording. Two posts can use completely different vocabulary and examples and still belong in the same category if they're fundamentally about the same thing (e.g. a post about LangGraph agent loops and a post about Claude Code subagent orchestration are both "AI Agent Harness Design", even though they don't share terminology)

Summary rules:
- Summaries must capture what the post actually argues or reveals, not just describe it

Output rules:
- Return ONLY a valid JSON array. No markdown, no explanation, no code fences."""

TAGS_INSTRUCTION = (
    "\n\nTag rules:\n"
    "- Tags are a FIXED list: {tags}\n"
    "- Do not invent new tags under any circumstances, even if none fit well\n"
    "- A post can and often should have multiple tags when it genuinely spans more than one "
    "(e.g. a post about running an open-source agent harness cheaply on cloud infra is "
    '"Open Source AI", "Cloud Hosting & Inference Costs", AND "Agent Harness & Orchestration" all at once)\n'
    f'- Only if a post genuinely fits none of the listed tags, use exactly ["{RESERVED_FALLBACK_TAG}"] and nothing else'
)

INCREMENTAL_EXTRA = "\n- You have these existing categories: {categories}. Assign to existing categories where possible. Only create a new category if the content genuinely doesn't fit any existing one."


def _build_system_prompt(known_categories, tag_vocabulary):
    system_prompt = BASE_SYSTEM_PROMPT + TAGS_INSTRUCTION.format(tags=tag_vocabulary)
    if known_categories:
        system_prompt += INCREMENTAL_EXTRA.format(categories=known_categories)
    return system_prompt


def categorize_bookmarks(bookmarks, existing_categories=None, known_tags=None, on_batch_complete=None):
    known_categories = list(existing_categories or [])
    tag_vocabulary = sorted(set(SEED_TAGS) | set(known_tags or []))

    categorized = []
    batches = [bookmarks[i:i + BATCH_SIZE] for i in range(0, len(bookmarks), BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        system_prompt = _build_system_prompt(known_categories, tag_vocabulary)
        if not on_batch_complete:
            print(f"Categorizing batch {idx}/{len(batches)} ({len(batch)} posts)...")
        batch_results, usage = _categorize_batch(system_prompt, batch)
        categorized.extend(batch_results)

        # Feed categories invented in this batch back into the running list so
        # the NEXT batch in this same run can reuse them instead of
        # reinventing a differently-worded near-duplicate. Tags don't need
        # this treatment — the vocabulary is fixed for the whole run.
        for item in batch_results:
            if isinstance(item, dict) and item.get("category") and item["category"] not in known_categories:
                known_categories.append(item["category"])

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
        tags = item.get("tags")
        if not isinstance(tags, list) or not tags:
            tags = [RESERVED_FALLBACK_TAG]
        if original and item.get("category") and item.get("summary"):
            merged = {
                **original,
                "category": item["category"],
                "summary": item["summary"],
                "tags": json.dumps(tags),
            }
            final.append(merged)
        else:
            skipped += 1

    if skipped:
        print(f"\nWARNING: {skipped} items skipped due to missing category/summary in LLM response")

    return final


def _categorize_batch(system_prompt, batch):
    posts = [
        {"tweet_id": b["tweet_id"], "content": b["full_content"], "author": b.get("author_username", "")}
        for b in batch
    ]
    user_prompt = (
        "Categorize these bookmarked posts. Return a JSON array where each object has exactly these keys:\n"
        "- tweet_id (string, unchanged from input)\n"
        "- category (string)\n"
        "- tags (array of strings, from the fixed tag list)\n"
        "- summary (string, ~128 words)\n\n"
        f"Posts:\n{json.dumps(posts, indent=2)}"
    )

    messages = [{"role": "user", "content": user_prompt}]
    text, input_tokens, output_tokens = complete(system_prompt, messages)
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    results, final_usage = _parse_json_response(system_prompt, messages, text.strip(), usage=usage)
    return results, final_usage


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_json_response(system_prompt, messages, raw, usage=None, retry=False):
    try:
        parsed = json.loads(_CODE_FENCE_RE.sub("", raw).strip())
        if not isinstance(parsed, list):
            raise json.JSONDecodeError("Expected a JSON array", raw, 0)
        return parsed, usage or {}
    except json.JSONDecodeError:
        if retry:
            print(f"ERROR: Could not parse JSON after retry. Raw response:\n{raw}")
            return [], usage or {}

        print("JSON parse failed, retrying with fix-up prompt...")
        fix_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "The response above is not valid JSON. Please return ONLY the valid JSON array with no other text."},
        ]
        fixed_text, fix_input, fix_output = complete(system_prompt, fix_messages)
        retry_usage = {
            "input_tokens": (usage or {}).get("input_tokens", 0) + fix_input,
            "output_tokens": (usage or {}).get("output_tokens", 0) + fix_output,
        }
        return _parse_json_response(system_prompt, messages, fixed_text.strip(), usage=retry_usage, retry=True)
