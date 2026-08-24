from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.routes import query, sync
from app.db import init_db
from app.mcp_server import mcp
import os


@asynccontextmanager
async def lifespan(app):
    db_path = os.getenv("DATABASE_URL", "./bookmarks.db")
    init_db(db_path)
    # streamable_http_app()'s session manager needs its own lifespan run
    # for the duration of the app — mounting the app alone doesn't do this;
    # skipping it leaves the session manager never started.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="ResearchScout API", lifespan=lifespan)


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
# streamable_http_app()'s own internal route is already "/mcp" (FastMCP's
# default streamable_http_path) — mounted at root so the final path is
# exactly /mcp, not /mcp/mcp. This replaces the older SSE transport
# (mcp.sse_app(), which served /mcp/sse + /mcp/messages) now that both
# Claude's and ChatGPT's connector UIs ask for Streamable HTTP specifically.
app.mount("/", mcp.streamable_http_app())
