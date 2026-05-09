import os
import time
from dotenv import load_dotenv

load_dotenv()

from app.pipeline.fetch import fetch_bookmarks
from app.pipeline.categorize import categorize_bookmarks
from app.pipeline.llm import PROVIDER, MODEL
from app.db import init_db, get_existing_tweet_ids, insert_bookmarks, get_categories, log_sync

_input_price = os.getenv("LLM_INPUT_PRICE_MTOK")
_output_price = os.getenv("LLM_OUTPUT_PRICE_MTOK")
INPUT_PRICE_PER_MTOK = float(_input_price) if _input_price else None
OUTPUT_PRICE_PER_MTOK = float(_output_price) if _output_price else None


def fmt_time(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}m"


def main():
    db = os.getenv("DATABASE_URL", "./bookmarks.db")
    overall_start = time.time()

    print("Initializing database...")
    init_db(db)
    existing = get_existing_tweet_ids(db)
    print(f"Existing bookmarks in DB: {len(existing)}\n")

    print("Fetching bookmarks from X API...")
    fetch_start = time.time()
    tweets = fetch_bookmarks(existing_tweet_ids=existing)
    fetch_elapsed = time.time() - fetch_start
    print(f"\nFetched {len(tweets)} new bookmarks in {fmt_time(fetch_elapsed)}\n")

    if not tweets:
        print("No new bookmarks to categorize.")
        log_sync(0, "success", None, db)
        return

    cats = get_categories(db)
    batch_size = 25
    total_batches = (len(tweets) + batch_size - 1) // batch_size

    total_input_tokens = 0
    total_output_tokens = 0
    categorize_start = time.time()

    print(f"Categorizing {len(tweets)} bookmarks in {total_batches} batches of {batch_size}...\n")

    def on_batch_complete(batch_num, total, usage):
        nonlocal total_input_tokens, total_output_tokens
        total_input_tokens += usage.get("input_tokens", 0)
        total_output_tokens += usage.get("output_tokens", 0)

        elapsed = time.time() - categorize_start
        avg = elapsed / batch_num
        eta = (total - batch_num) * avg

        bar_width = 28
        filled = int(bar_width * batch_num / total)
        bar = "█" * filled + "░" * (bar_width - filled)
        pct = int(100 * batch_num / total)

        cost_str = ""
        if INPUT_PRICE_PER_MTOK is not None and OUTPUT_PRICE_PER_MTOK is not None:
            cost = (
                total_input_tokens / 1_000_000 * INPUT_PRICE_PER_MTOK
                + total_output_tokens / 1_000_000 * OUTPUT_PRICE_PER_MTOK
            )
            cost_str = f"  cost ${cost:.4f}"

        print(
            f"\r  [{bar}] {pct:3d}%  batch {batch_num}/{total}"
            f"  elapsed {fmt_time(elapsed)}"
            f"  eta {fmt_time(eta)}"
            f"  tokens {total_input_tokens + total_output_tokens:,}"
            f"{cost_str}",
            end="",
            flush=True,
        )

    categorized = categorize_bookmarks(tweets, existing_categories=cats, on_batch_complete=on_batch_complete)
    print()  # newline after progress line

    categorize_elapsed = time.time() - categorize_start
    count = insert_bookmarks(categorized, db)
    log_sync(count, "success", None, db)

    total_elapsed = time.time() - overall_start
    total_tokens = total_input_tokens + total_output_tokens

    if INPUT_PRICE_PER_MTOK is not None and OUTPUT_PRICE_PER_MTOK is not None:
        total_cost = (
            total_input_tokens / 1_000_000 * INPUT_PRICE_PER_MTOK
            + total_output_tokens / 1_000_000 * OUTPUT_PRICE_PER_MTOK
        )
        cost_line = f"│ Estimated cost     ${total_cost:<14.4f} │"
        pricing_note = f"  * Pricing: {PROVIDER}/{MODEL}\n    ${INPUT_PRICE_PER_MTOK}/MTok input · ${OUTPUT_PRICE_PER_MTOK}/MTok output"
    else:
        cost_line = "│ Estimated cost     (set LLM_*_PRICE) │"
        pricing_note = f"  * Model: {PROVIDER}/{MODEL}\n    Set LLM_INPUT_PRICE_MTOK and LLM_OUTPUT_PRICE_MTOK to display cost."

    print(f"""
┌─────────────────────────────────────┐
│              Summary                │
├─────────────────────────────────────┤
│ Bookmarks stored   {count:<19} │
│ Total time         {fmt_time(total_elapsed):<19} │
│   Fetch            {fmt_time(fetch_elapsed):<19} │
│   Categorize       {fmt_time(categorize_elapsed):<19} │
├─────────────────────────────────────┤
│ Input tokens       {total_input_tokens:<19,} │
│ Output tokens      {total_output_tokens:<19,} │
│ Total tokens       {total_tokens:<19,} │
{cost_line}
└─────────────────────────────────────┘
{pricing_note}
""")


if __name__ == "__main__":
    main()
