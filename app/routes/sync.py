from fastapi import APIRouter, Header, HTTPException
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

    existing_cats = await loop.run_in_executor(None, get_categories, db_path)
    categorized = await loop.run_in_executor(
        None, lambda: categorize_bookmarks(new_tweets, existing_categories=existing_cats)
    )

    count = await loop.run_in_executor(
        None, lambda: insert_bookmarks(categorized, db_path)
    )
    await loop.run_in_executor(None, log_sync, count, "success", None, db_path)

    categories_used = list({b["category"] for b in categorized})
    return {"status": "ok", "new_bookmarks": count, "categories_used": categories_used}


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
