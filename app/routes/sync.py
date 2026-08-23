from fastapi import APIRouter, Header, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()


async def _run_pipeline():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    loop = asyncio.get_event_loop()

    from app.pipeline.auth import get_valid_access_token
    from app.pipeline.fetch import fetch_bookmarks
    from app.pipeline.categorize import categorize_bookmarks
    from app.db import (
        get_existing_tweet_ids,
        get_categories,
        get_all_tags,
        insert_bookmarks,
        log_sync,
    )

    await loop.run_in_executor(None, get_valid_access_token, db_path)

    existing_ids = await loop.run_in_executor(None, get_existing_tweet_ids, db_path)
    new_tweets = await loop.run_in_executor(
        None, lambda: fetch_bookmarks(existing_tweet_ids=existing_ids, db_path=db_path)
    )

    if not new_tweets:
        await loop.run_in_executor(None, log_sync, 0, "success", None, db_path)
        return {"status": "ok", "new_bookmarks": 0, "message": "No new bookmarks found"}

    # Rows with a real content_for_summary need the LLM; rows already
    # resolved deterministically (nothing substantial anywhere) skip it
    # entirely — see resolve_bookmark_rows / RESERVED_THIN_CONTENT_CATEGORY.
    needs_categorization = [t for t in new_tweets if "category" not in t]
    already_resolved = [t for t in new_tweets if "category" in t]

    existing_cats = await loop.run_in_executor(None, get_categories, db_path)
    known_tags = await loop.run_in_executor(None, get_all_tags, db_path)
    categorized = (
        await loop.run_in_executor(
            None, lambda: categorize_bookmarks(needs_categorization, existing_categories=existing_cats, known_tags=known_tags)
        )
        if needs_categorization else []
    )
    all_final = categorized + already_resolved

    count = await loop.run_in_executor(
        None, lambda: insert_bookmarks(all_final, db_path)
    )
    await loop.run_in_executor(None, log_sync, count, "success", None, db_path)

    categories_used = list({b["category"] for b in all_final})
    return {"status": "ok", "new_bookmarks": count, "categories_used": categories_used}


@router.post("/sync/upload-db")
async def upload_db(file: UploadFile = File(...), x_sync_secret: str = Header(default=None)):
    expected = os.getenv("SYNC_SECRET", "")
    if not x_sync_secret or x_sync_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Sync-Secret header")
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    content = await file.read()
    with open(db_path, "wb") as f:
        f.write(content)
    return {"status": "ok", "path": db_path, "size_bytes": len(content)}


@router.post("/sync")
async def trigger_sync(x_sync_secret: str = Header(default=None)):
    expected = os.getenv("SYNC_SECRET", "")
    if not x_sync_secret or x_sync_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Sync-Secret header")

    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    try:
        result = await _run_pipeline()
        return result
    except Exception as e:
        from app.db import log_sync
        log_sync(0, "error", str(e), db_path)
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})
