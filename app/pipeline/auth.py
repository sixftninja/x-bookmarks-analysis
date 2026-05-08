import sqlite3
import os
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://api.x.com/2/oauth2/token"


def get_valid_access_token(db_path=None):
    if db_path is None:
        db_path = os.getenv("DATABASE_URL", "./bookmarks.db")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT access_token, refresh_token, expires_at FROM oauth_tokens WHERE id = 1"
        ).fetchone()

    if row is None:
        raise RuntimeError("OAuth tokens not found. Run scripts/first_auth.py locally first.")

    access_token, refresh_token, expires_at_str = row
    expires_at = datetime.fromisoformat(expires_at_str)

    if datetime.now(timezone.utc) < expires_at:
        return access_token

    print("Access token expired, refreshing...")
    return _refresh_token(refresh_token, db_path)


def _refresh_token(refresh_token, db_path):
    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")

    response = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )

    if response.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {response.status_code} {response.text}")

    data = response.json()
    new_access_token = data["access_token"]
    new_refresh_token = data.get("refresh_token", refresh_token)
    expires_in = data.get("expires_in", 7200)

    from datetime import timedelta
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO oauth_tokens (id, access_token, refresh_token, expires_at, scope, updated_at)
               VALUES (1, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                   access_token = excluded.access_token,
                   refresh_token = excluded.refresh_token,
                   expires_at = excluded.expires_at,
                   updated_at = excluded.updated_at""",
            (new_access_token, new_refresh_token, expires_at, data.get("scope", "")),
        )
        conn.commit()

    print("Token refreshed successfully.")
    return new_access_token
