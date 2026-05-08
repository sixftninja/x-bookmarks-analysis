"""
One-time OAuth 2.0 PKCE flow to generate and store X API tokens.
Run this locally: python scripts/first_auth.py
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

REDIRECT_URI = "http://localhost:8080/callback"
AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
SCOPES = "tweet.read users.read bookmark.read offline.access"

DB_PATH = os.getenv("DATABASE_URL", "./bookmarks.db")

_auth_code = None
_state_received = None


def _generate_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                scope TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code, _state_received
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            _auth_code = params.get("code", [None])[0]
            _state_received = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful! You can close this tab.</h1>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress server logs


def _exchange_code(code, verifier):
    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("X_CLIENT_ID and X_CLIENT_SECRET must be set in .env")

    resp = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text}")
    return resp.json()


def _save_tokens(token_data):
    expires_in = token_data.get("expires_in", 7200)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO oauth_tokens (id, access_token, refresh_token, expires_at, scope, updated_at)
            VALUES (1, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at,
                scope = excluded.scope,
                updated_at = excluded.updated_at
        """, (
            token_data["access_token"],
            token_data["refresh_token"],
            expires_at,
            token_data.get("scope", ""),
        ))
        conn.commit()


def main():
    client_id = os.getenv("X_CLIENT_ID")
    if not client_id:
        print("ERROR: X_CLIENT_ID not set in .env")
        return

    _init_db()

    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"\nOpening browser for authorization...")
    print(f"If the browser doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for callback on http://localhost:8080/callback ...")
    while _auth_code is None:
        import time
        time.sleep(0.1)

    server.shutdown()

    if _state_received != state:
        print("ERROR: State mismatch — possible CSRF. Aborting.")
        return

    print("Authorization code received. Exchanging for tokens...")
    try:
        token_data = _exchange_code(_auth_code, verifier)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return

    _save_tokens(token_data)
    print(f"\nSuccess! Tokens saved to {DB_PATH}")
    print("Next steps:")
    print("  1. Run the first local sync to fetch and categorize your bookmarks")
    print("  2. Upload bookmarks.db to Railway (see README step 6)")


if __name__ == "__main__":
    main()
