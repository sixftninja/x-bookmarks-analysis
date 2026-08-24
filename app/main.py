from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.routes import query, sync
from app.db import init_db
from app.mcp_server import mcp
import os

app = FastAPI(title="ResearchScout API")


@app.on_event("startup")
async def startup():
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    init_db(db_path)


# The MCP interface (unlike the read-only query routes) can edit, delete,
# and add sources — spending API keys via add_source/trigger_sync. Every
# /mcp request must carry the right secret in X-MCP-Secret. Fails closed:
# an unset MCP_AUTH_SECRET blocks every request rather than allowing them
# through, same as SYNC_SECRET's behavior in app/routes/sync.py.
@app.middleware("http")
async def mcp_auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        expected = os.getenv("MCP_AUTH_SECRET", "")
        provided = request.headers.get("X-MCP-Secret")
        if not provided or provided != expected:
            return JSONResponse(status_code=401, content={"error": "Invalid or missing X-MCP-Secret header"})
    return await call_next(request)


app.include_router(query.router)
app.include_router(sync.router)
app.mount("/mcp", mcp.sse_app())
