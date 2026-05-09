"""
Export all bookmarks to bookmarks_export.json.
Upload this file to any AI chatbot to search and discuss your bookmarks.
"""
import json
import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

db = os.getenv("DATABASE_URL", "./bookmarks.db")

with sqlite3.connect(db) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT tweet_id, author_username, author_name, category,
                  summary, full_content, tweet_url, bookmarked_at
           FROM bookmarks ORDER BY category, bookmarked_at DESC"""
    ).fetchall()

bookmarks = [dict(row) for row in rows]

with open("bookmarks_export.json", "w") as f:
    json.dump(bookmarks, f, indent=2)

print(f"Exported {len(bookmarks)} bookmarks to bookmarks_export.json")
