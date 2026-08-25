# ResearchScout

Fetches, categorizes, and searches X bookmarks — plus any article, paper, or link added manually — using an LLM. Every entry gets a category, tags, and a summary; teaser posts get scraped in full, quote-tweets become linked entries, PDF links resolve to an abstract.

Runs on Railway, exposed to Claude and ChatGPT via an MCP connector (Streamable HTTP `/mcp`, secured by a shared secret in a header or query param). Query, edit, tag, and add sources conversationally from any device.

## Tools

| Tool | Function |
|---|---|
| `get_bookmark_stats` | Bookmark count per category. |
| `get_categories` | All category names, alphabetically. |
| `get_bookmarks_by_category` | Bookmarks in one category. |
| `search_bookmarks` | Keyword search across summaries and post text. |
| `get_recent_bookmarks` | Most recently added bookmarks. |
| `get_sync_status` | Last sync time, bookmarks added, total count. |
| `rename_category` | Rename a category everywhere. |
| `move_bookmarks` | Move bookmarks to a category. |
| `merge_categories` | Merge one category into another. |
| `delete_bookmarks` | Permanently delete bookmarks by id. |
| `delete_category` | Permanently delete a whole category. |
| `trigger_sync` | Fetch and categorize new X bookmarks. |
| `edit_bookmark` | Edit a bookmark's category or summary. |
| `get_bookmark_by_tweet_id` | Get one bookmark by exact id. |
| `get_bookmarks_by_author` | Bookmarks from one author or source. |
| `get_authors` | Every author/source with bookmark counts. |
| `get_bookmarks_by_tag` | Bookmarks carrying one or more tags. |
| `add_tag_to_bookmark` | Add a tag to a bookmark. |
| `get_full_content` | Live full-text fetch for one entry. |
| `add_source` | Add and categorize an external link. |
| `mark_reviewed` | Mark an entry reviewed, with notes. |
| `get_unreviewed` | Entries not yet reviewed, newest first. |
