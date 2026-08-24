# ResearchScout

Fetches, categorizes, and searches X bookmarks — plus any article, paper, or link added manually — using an LLM. Every entry gets a category, tags from a fixed vocabulary, and a summary. X "teaser" posts get their full body scraped; quote-tweets become their own linked entries; PDF links resolve to a real abstract.

Runs on Railway, exposed to Claude and ChatGPT via an MCP connector (Streamable HTTP, `/mcp`, requires an `X-MCP-Secret` header). Query, edit, tag, and add sources conversationally from any device.

Share a link and ask the connected AI to add it — `add_source(url, notes)` fetches, categorizes, and summarizes it. `get_unreviewed()` surfaces new entries for triage; `mark_reviewed(id, notes)` records the conclusion, appending across sessions rather than overwriting.
