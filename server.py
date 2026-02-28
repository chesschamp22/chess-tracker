#!/usr/bin/env python3
"""
Chess Rating Tracker — Web Server
Serves chess-tracker.html, proxies USCF API, and fetches ChessKid data
by logging in with the user's credentials.

Local usage:
    pip install requests
    python3 server.py

Deploy to Render/Railway:
    See README.md
"""

import json
import os
import re
import time
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

PORT = int(os.environ.get("PORT", 8765))
MUIR_BASE = "https://ratings-api.uschess.org/api/v1"
HTML_FILE = Path(__file__).parent / "chess-tracker.html"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ── ChessKid session cache ─────────────────────────────────────────────────
# Keyed by username.lower() → { session, expiry, last_data }
_ck_sessions: dict = {}
_ck_lock = threading.Lock()
SESSION_TTL = 3600          # re-use session for 1 hour
DATA_CACHE_TTL = 300        # re-fetch stats every 5 min


# ── ChessKid login & fetch ─────────────────────────────────────────────────

def _ck_login(username: str, password: str):
    """Log into ChessKid and return a requests.Session with auth cookies."""
    if not HAS_REQUESTS:
        raise RuntimeError("requests library not installed — run: pip install requests")

    s = req_lib.Session()
    s.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.chesskid.com/login",
        "Origin": "https://www.chesskid.com",
    })

    # Step 1: GET login page to collect any CSRF token / cookies
    login_page = s.get("https://www.chesskid.com/login", timeout=10)
    login_page.raise_for_status()

    # Extract CSRF token if present (Chess.com family uses _token field)
    csrf = None
    m = re.search(r'"_token"\s*:\s*"([^"]+)"', login_page.text)
    if not m:
        m = re.search(r'name="_token"\s+value="([^"]+)"', login_page.text)
    if m:
        csrf = m.group(1)

    # Step 2: POST credentials
    payload = {
        "username": username,
        "password": password,
        "remember": "1",
    }
    if csrf:
        payload["_token"] = csrf

    # Try JSON login (Chess.com API style)
    resp = s.post(
        "https://www.chesskid.com/login",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
        allow_redirects=True,
    )

    # Fallback: form-encoded login
    if resp.status_code not in (200, 302) or "login" in resp.url:
        resp = s.post(
            "https://www.chesskid.com/login",
            data=payload,
            timeout=10,
            allow_redirects=True,
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"Login failed: HTTP {resp.status_code}")

    # Verify we got an auth cookie
    cookies = dict(s.cookies)
    has_auth = any(k in cookies for k in ("PHPSESSID", "chesskid_session", "_session", "remember_web"))
    if not has_auth and "login" in resp.url:
        raise RuntimeError("Login rejected — check username/password")

    return s


def _ck_fetch_stats(session, username: str) -> dict:
    """Fetch ChessKid stats after login. Returns structured dict."""

    endpoints = {
        "member_stats":  f"https://www.chesskid.com/callback/member/stats/{username}",
        "popup":         f"https://www.chesskid.com/callback/user/popup/{username}",
        "blitz_chart":   f"https://www.chesskid.com/callback/live/stats/{username}/chart?daysAgo=365&type=blitz",
        "rapid_chart":   f"https://www.chesskid.com/callback/live/stats/{username}/chart?daysAgo=365&type=rapid",
        "bullet_chart":  f"https://www.chesskid.com/callback/live/stats/{username}/chart?daysAgo=365&type=bullet",
        "tactics_chart": f"https://www.chesskid.com/callback/live/stats/{username}/chart?daysAgo=365&type=tactics",
        "daily_chart":   f"https://www.chesskid.com/callback/live/stats/{username}/chart?daysAgo=365&type=daily",
    }

    raw = {}
    for key, url in endpoints.items():
        try:
            r = session.get(url, timeout=10)
            if r.status_code == 200:
                raw[key] = r.json()
        except Exception as e:
            raw[key] = {"_error": str(e)}

    # Parse into clean format for the frontend
    stats = raw.get("member_stats", {})
    popup = raw.get("popup", {})

    def chart_to_series(data):
        """Convert chart API response to [{date, rating}] list."""
        if not data or "_error" in data:
            return []
        arr = data if isinstance(data, list) else data.get("chart", data.get("data", []))
        if not isinstance(arr, list):
            return []
        out = []
        for p in arr:
            if isinstance(p, dict):
                ts = p.get("timestamp") or p.get("date") or p.get("time")
                rating = p.get("rating") or p.get("value")
                if ts and rating:
                    # timestamps > 1e10 are already ms, else seconds
                    ms = int(ts) * 1000 if int(ts) < 1e10 else int(ts)
                    out.append({"date": ms, "rating": int(rating)})
        out.sort(key=lambda x: x["date"])
        return out

    result = {
        "username": username,
        "current": {
            "puzzles":     stats.get("tactics", {}).get("last", {}).get("rating"),
            "puzzlesBest": stats.get("tactics", {}).get("highest", {}).get("rating"),
            "blitz":       stats.get("blitz", {}).get("last", {}).get("rating"),
            "rapid":       stats.get("rapid", {}).get("last", {}).get("rating"),
            "bullet":      stats.get("bullet", {}).get("last", {}).get("rating"),
            "daily":       stats.get("daily", {}).get("last", {}).get("rating"),
        },
        "charts": {
            "puzzles": chart_to_series(raw.get("tactics_chart")),
            "blitz":   chart_to_series(raw.get("blitz_chart")),
            "rapid":   chart_to_series(raw.get("rapid_chart")),
            "bullet":  chart_to_series(raw.get("bullet_chart")),
            "daily":   chart_to_series(raw.get("daily_chart")),
        },
        "_raw_keys": list(raw.keys()),
    }

    # Fill in from popup if stats missed anything
    if popup and not result["current"]["puzzles"]:
        result["current"]["puzzles"] = popup.get("tactics_rating") or popup.get("puzzle_rush", {}).get("rating")

    return result


def get_chesskid_data(username: str, password: str) -> dict:
    """Login (or reuse cached session) and return ChessKid stats."""
    key = username.lower()
    now = time.time()

    with _ck_lock:
        cached = _ck_sessions.get(key)
        if cached:
            # Return cached data if fresh enough
            if now - cached["data_time"] < DATA_CACHE_TTL:
                return cached["last_data"]
            # Session still valid, just re-fetch stats
            if now - cached["session_time"] < SESSION_TTL:
                try:
                    data = _ck_fetch_stats(cached["session"], username)
                    cached["last_data"] = data
                    cached["data_time"] = now
                    return data
                except Exception:
                    pass  # fall through to re-login

        # New login
        try:
            session = _ck_login(username, password)
            data = _ck_fetch_stats(session, username)
            _ck_sessions[key] = {
                "session": session,
                "session_time": now,
                "last_data": data,
                "data_time": now,
            }
            return data
        except RuntimeError as e:
            return {"error": True, "reason": str(e)}
        except Exception as e:
            return {"error": True, "reason": f"Unexpected error: {e}"}


# ── HTTP Handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/uscf/"):
            self._proxy_uscf()
            return
        if self.path in ("/health", "/ping"):
            self._json(200, {"status": "ok", "requests_available": HAS_REQUESTS})
            return
        self._serve_html()

    def do_POST(self):
        if self.path == "/chesskid":
            self._handle_chesskid()
            return
        self._json(404, {"error": "Not found"})

    def _handle_chesskid(self):
        """POST /chesskid  body: {"username":"...", "password":"..."}"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            payload = json.loads(body)
            username = payload.get("username", "").strip()
            password = payload.get("password", "").strip()
            if not username or not password:
                self._json(400, {"error": True, "reason": "username and password required"})
                return
            data = get_chesskid_data(username, password)
            self._json(200, data)
        except json.JSONDecodeError:
            self._json(400, {"error": True, "reason": "Invalid JSON body"})
        except Exception as e:
            self._json(500, {"error": True, "reason": str(e)})

    def _proxy_uscf(self):
        muir_path = self.path[5:]
        url = f"{MUIR_BASE}{muir_path}"
        try:
            r = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 ChessTracker/1.0",
                "Accept": "application/json",
                "Referer": "https://ratings.uschess.org/",
                "Origin": "https://ratings.uschess.org",
            })
            with urllib.request.urlopen(r, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._json(e.code, {"error": True, "status": e.code, "reason": str(e.reason)})
        except Exception as e:
            self._json(502, {"error": True, "reason": str(e)})

    def _serve_html(self):
        if not HTML_FILE.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"chess-tracker.html not found next to server.py")
            return
        content = HTML_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not HAS_REQUESTS:
        print("⚠  'requests' not found. Install it first:")
        print("   pip install requests\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"✓ Chess Tracker running at http://localhost:{PORT}")
    print(f"  Open that URL in your browser.")
    print(f"  USCF proxy:     GET  /uscf/members/<id>")
    print(f"  ChessKid data:  POST /chesskid  {{username, password}}")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
