#!/usr/bin/env python3
"""
APEX CAPITAL INTELLIGENCE — Cloud Server
Deploy to Railway or Render. Zero external dependencies.

Configuration via environment variables (set in Railway dashboard):
  PORT               Auto-set by Railway — do not change
  API_KEY            Secret key your Mac uses to push data  (e.g. some-long-random-string)
  USERS              JSON string: {"jack":"password","guest":"viewonly"}
  PASSWORD_REQUIRED  "true" or "false"  (default: true)
"""

import hashlib
import hmac
import json
from typing import Optional
import os
import secrets
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Config from environment variables ───────────────────────
API_KEY            = os.environ.get("API_KEY", "change-this-secret-key")
PASSWORD_REQUIRED  = os.environ.get("PASSWORD_REQUIRED", "true").lower() != "false"
PORT               = int(os.environ.get("PORT", 5050))

_users_raw = os.environ.get("USERS", '{"jack":"REDACTED_DASHBOARD_PASSWORD","guest":"REDACTED_GUEST_PASSWORD"}')
try:
    USERS: dict = json.loads(_users_raw)
except Exception:
    USERS = {"jack": "REDACTED_DASHBOARD_PASSWORD"}

# ── In-memory store ──────────────────────────────────────────
# Populated by POST /api/push from the Mac sync script.
_data_store = {}
_data_pushed_at = None  # type: Optional[str]
_sessions = {}  # type: dict

# HTML file (in same folder as this script)
HTML_FILE = Path(__file__).parent / "apex_live_dashboard.html"

# ── Helpers ──────────────────────────────────────────────────
def get_session_token(cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "apex_session":
            return v.strip()
    return None

def is_authenticated(cookie_header: Optional[str]) -> bool:
    if not PASSWORD_REQUIRED:
        return True
    token = get_session_token(cookie_header)
    return token is not None and token in _sessions

def data_age_str() -> str:
    if not _data_pushed_at:
        return "No data pushed yet"
    try:
        pushed = datetime.fromisoformat(_data_pushed_at.replace("Z", "+00:00"))
        age_min = int((datetime.now(timezone.utc) - pushed).total_seconds() / 60)
        if age_min < 60:
            return f"{age_min}m ago"
        h, m = divmod(age_min, 60)
        return f"{h}h {m}m ago"
    except Exception:
        return _data_pushed_at

# ── Login page ───────────────────────────────────────────────
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apex Capital — Login</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#08080f;color:#e0e0e0;font-family:'SF Mono','Fira Code','Courier New',monospace;
       min-height:100vh;display:flex;align-items:center;justify-content:center}
  .box{width:100%;max-width:380px;padding:40px;background:#10101a;border:1px solid #1e1e30;border-radius:12px}
  .logo{font-size:20px;font-weight:700;letter-spacing:2px;color:#00FF88;text-transform:uppercase;
        text-align:center;margin-bottom:6px}
  .logo span{color:#fff}
  .sub{text-align:center;color:#888;font-size:11px;letter-spacing:1px;margin-bottom:32px}
  label{display:block;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888;margin-bottom:6px}
  input{width:100%;background:#14141f;border:1px solid #1e1e30;border-radius:6px;
        color:#e0e0e0;font-family:inherit;font-size:13px;padding:10px 12px;outline:none;
        transition:border-color .2s}
  input:focus{border-color:#00FF88}
  .field{margin-bottom:16px}
  button{width:100%;background:rgba(0,255,136,.15);border:1px solid rgba(0,255,136,.4);
         color:#00FF88;font-family:inherit;font-size:13px;font-weight:700;letter-spacing:1px;
         padding:12px;border-radius:6px;cursor:pointer;text-transform:uppercase;
         transition:background .2s;margin-top:8px}
  button:hover{background:rgba(0,255,136,.25)}
  .err{color:#FF4444;font-size:11px;text-align:center;margin-top:14px}
  .footer{text-align:center;color:#444;font-size:10px;margin-top:24px}
</style>
</head>
<body>
<div class="box">
  <div class="logo">Apex <span>Capital</span></div>
  <div class="sub">Intelligence Command Center</div>
  <form method="POST" action="/login">
    <div class="field"><label>Username</label><input name="username" type="text" autofocus autocomplete="username"></div>
    <div class="field"><label>Password</label><input name="password" type="password" autocomplete="current-password"></div>
    <button type="submit">Enter</button>
    ERRBLOCK
  </form>
  <div class="footer">Private &amp; Confidential · Apex Capital Intelligence</div>
</div>
</body>
</html>"""

# ── HTTP Handler ─────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        try:
            ts  = datetime.now().strftime("%H:%M:%S")
            msg = args[0] if args else ""
            if isinstance(msg, str) and " " in msg:
                path   = msg.split()[1]
                status = args[1] if len(args) > 1 else ""
                print(f"  [{ts}] {status} {path}")
        except Exception:
            pass

    def cookies(self):
        return self.headers.get("Cookie", "")

    def authed(self):
        return is_authenticated(self.cookies())

    def send_bytes(self, body: bytes, ctype: str, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, default=str).encode(), "application/json", status)

    def send_html(self, html: str, status=200, extra=None):
        self.send_bytes(html.encode(), "text/html; charset=utf-8", status, extra)

    def redirect(self, loc: str):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/login":
            self.send_html(LOGIN_PAGE.replace("ERRBLOCK", ""))
            return

        if path == "/logout":
            token = get_session_token(self.cookies())
            if token in _sessions:
                del _sessions[token]
            self.send_html(LOGIN_PAGE.replace("ERRBLOCK", ""),
                           extra={"Set-Cookie": "apex_session=; Max-Age=0; Path=/"})
            return

        if not self.authed():
            self.redirect("/login") if not path.startswith("/api") else self.send_json({"error": "Unauthorized"}, 401)
            return

        # Dashboard HTML
        if path == "/":
            try:
                self.send_bytes(HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self.send_html("<h1 style='font-family:monospace;color:#fff;background:#000;padding:40px'>apex_live_dashboard.html not found in same folder as server_cloud.py</h1>", 404)
            return

        # ── API endpoints ─────────────────────────────────────
        if path == "/api/all":
            self.send_json({
                "server_time":   datetime.now(timezone.utc).isoformat(),
                "data_pushed_at": _data_pushed_at,
                "data_age":      data_age_str(),
                "data":          _data_store,
                "file_ages":     {},   # not applicable for cloud — data is pushed, not local
            })
            return

        if path == "/api/health":
            self.send_json({
                "status":        "ok",
                "server_time":   datetime.now(timezone.utc).isoformat(),
                "data_pushed_at": _data_pushed_at,
                "data_age":      data_age_str(),
                "active_sessions": len(_sessions),
            })
            return

        if path.startswith("/api/"):
            key = path[5:]
            val = _data_store.get(key)
            if val is None:
                self.send_json({"error": f"Key '{key}' not found. Push data first."}, 404)
            else:
                self.send_json({"data": val, "pushed_at": _data_pushed_at})
            return

        self.send_html("<h1>404</h1>", 404)

    # ── POST ──────────────────────────────────────────────────
    def do_POST(self):
        global _data_store, _data_pushed_at
        path = urlparse(self.path).path.rstrip("/")

        # Login form
        if path == "/login":
            length   = int(self.headers.get("Content-Length", 0))
            body     = self.rfile.read(length).decode()
            params   = parse_qs(body)
            username = params.get("username", [""])[0].strip()
            password = params.get("password", [""])[0]
            expected = USERS.get(username)
            if expected and hmac.compare_digest(expected, password):
                token = secrets.token_hex(32)
                _sessions[token] = username
                print(f"  ✓ Login: {username}")
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"apex_session={token}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
            else:
                print(f"  ✗ Failed login: '{username}'")
                err = '<div class="err">Invalid username or password.</div>'
                self.send_html(LOGIN_PAGE.replace("ERRBLOCK", err))
            return

        # Data push from Mac sync script
        if path == "/api/push":
            auth_key = self.headers.get("X-API-Key", "")
            if not hmac.compare_digest(API_KEY, auth_key):
                self.send_json({"error": "Invalid API key"}, 403)
                print("  ✗ Push rejected: bad API key")
                return
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode()
            try:
                payload = json.loads(body)
                _data_store    = payload.get("data", {})
                _data_pushed_at = datetime.now(timezone.utc).isoformat()
                keys = list(_data_store.keys())
                print(f"  ✓ Data pushed: {len(keys)} keys — {', '.join(keys[:6])}{'...' if len(keys)>6 else ''}")
                self.send_json({"status": "ok", "keys": len(keys), "pushed_at": _data_pushed_at})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
            return

        self.send_html("<h1>404</h1>", 404)


# ── Entry point ──────────────────────────────────────────────
def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print()
    print("=" * 60)
    print("  APEX CAPITAL — Cloud Server")
    print("=" * 60)
    print(f"  Listening on port: {PORT}")
    print(f"  Password required: {PASSWORD_REQUIRED}")
    print(f"  Users configured:  {list(USERS.keys())}")
    print(f"  API key set:       {'(custom)' if API_KEY != 'change-this-secret-key' else '⚠ CHANGE THIS in Railway env vars!'}")
    print(f"  Dashboard file:    {HTML_FILE}")
    print()
    print("  Waiting for data push from Mac sync script...")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()

if __name__ == "__main__":
    main()
