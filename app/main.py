from fastapi import FastAPI
from app.routes import query, sync
from app.db import init_db
import os

app = FastAPI(title="X Bookmarks Analysis API")


@app.on_event("startup")
async def startup():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    init_db(db_path)


app.include_router(query.router)
app.include_router(sync.router)
