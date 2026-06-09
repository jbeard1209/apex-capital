#!/usr/bin/env python3
"""
APEX CAPITAL INTELLIGENCE — Live Dashboard Server
Zero external dependencies — uses only Python built-ins.

Run:   python3 dashboard_server.py
Open:  http://localhost:5050
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
from typing import Optional
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ============================================================
#  ★ CONFIGURATION — edit these lines
# ============================================================

PASSWORD_REQUIRED = True          # Set False to make it fully public (no login)

# Add as many users as you like: "username": "password"
USERS = {
    "jack":  "REDACTED_DASHBOARD_PASSWORD",   # your personal login
    "guest": "REDACTED_GUEST_PASSWORD",       # share this with anyone you trust
}

PORT = 5050                       # change if this port is in use

# ============================================================

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "institutional-tracker" / "data"
HTML_FILE = BASE_DIR / "apex_live_dashboard.html"

# In-memory session store  {token: username}
_sessions = {}  # type: dict

# ── Data files ──────────────────────────────────────────────
DATA_FILES = {
    "state":             "state.json",
    "canary":            "canary_state.json",
    "canary_alert":      "canary_alert.json",
    "portfolio":         "review_recommendations.json",
    "cos":               "chief_of_staff_report.json",
    "pods":              "pod_allocations.json",
    "pod_pnl":           "pod_pnl_report.json",
    "agents":            "agent_registry.json",
    "morning":           "morning_briefing.json",
    "trade_review":      "trade_review_report.json",
    "manager_decisions": "manager_decisions.json",
    "research":          "research_team_report.json",
    "pipeline":          "pipeline_heartbeat.json",
    "etf_board":         "etf_board_meeting.json",
    "issue_tracker":     "issue_tracker.json",
    "hr_report":         "hr_report.json",
    "board_meeting":     "board_meeting_report.json",
    "premarket":         "premarket_alerts.json",
    "execution_quality": "execution_quality.json",
}

# ── Helpers ─────────────────────────────────────────────────
def load_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def file_age_minutes(path: Path):
    try:
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        return round(age / 60, 1)
    except Exception:
        return None

def build_all_response():
    result = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "data":        {},
        "file_ages":   {},
    }
    for key, filename in DATA_FILES.items():
        p = DATA_DIR / filename
        result["data"][key]      = load_json(p)
        result["file_ages"][key] = file_age_minutes(p)
    agents_dir = BASE_DIR / "institutional-tracker" / "agents"
    if agents_dir.exists():
        result["agent_files"] = sorted(p.name for p in agents_dir.glob("cos_*.py"))

    # Teaching Division
    result["data"]["teaching_lesson"]   = load_json(DATA_DIR / "teaching_lesson_today.json")
    result["data"]["teaching_progress"] = load_json(DATA_DIR / "teaching_progress.json")
    result["data"]["ml_live_picks"]     = load_json(DATA_DIR / "ml_live_picks_log.json")

    return result

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

# ── Login page HTML ─────────────────────────────────────────
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
  .err{color:#FF4444;font-size:11px;text-align:center;margin-top:14px;display:ERRVIS}
  .footer{text-align:center;color:#444;font-size:10px;margin-top:24px}
</style>
</head>
<body>
<div class="box">
  <div class="logo">Apex <span>Capital</span></div>
  <div class="sub">Intelligence Command Center</div>
  <form method="POST" action="/login">
    <div class="field"><label>Username</label><input name="username" type="text" autocomplete="username" autofocus></div>
    <div class="field"><label>Password</label><input name="password" type="password" autocomplete="current-password"></div>
    <button type="submit">Enter</button>
    <div class="err" style="display:ERRVIS">ERRTEXT</div>
  </form>
  <div class="footer">Private &amp; Confidential · Apex Capital Intelligence</div>
</div>
</body>
</html>"""

# ── HTTP Handler ────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            # args[0] is the request line for normal requests (e.g. "GET / HTTP/1.1")
            # but can be an int when called from log_error — handle both safely
            first = args[0] if args else ""
            if isinstance(first, str) and " " in first:
                path = first.split()[1]
                if not path.startswith("/api"):
                    print(f"  [{ts}] {path}")
        except Exception:
            pass

    # ── helpers ──────────────────────────────────────────────
    def cookies(self):
        return self.headers.get("Cookie", "")

    def authed(self):
        return is_authenticated(self.cookies())

    def send_bytes(self, body: bytes, content_type: str, status=200, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_bytes(body, "application/json", status)

    def send_html(self, html: str, status=200, extra_headers=None):
        self.send_bytes(html.encode(), "text/html; charset=utf-8", status, extra_headers)

    def redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        # Login page
        if path == "/login":
            page = LOGIN_PAGE.replace("display:ERRVIS", "display:none").replace("ERRTEXT", "")
            self.send_html(page)
            return

        # Logout
        if path == "/logout":
            token = get_session_token(self.cookies())
            if token and token in _sessions:
                del _sessions[token]
            self.send_html(
                LOGIN_PAGE.replace("display:ERRVIS","display:none").replace("ERRTEXT",""),
                extra_headers={"Set-Cookie": "apex_session=; Max-Age=0; Path=/"}
            )
            return

        # Require auth for everything else
        if not self.authed():
            if path.startswith("/api"):
                self.send_json({"error": "Unauthorized"}, 401)
            else:
                self.redirect("/login")
            return

        # Dashboard
        if path in ("", "/"):
            try:
                body = HTML_FILE.read_bytes()
                self.send_bytes(body, "text/html; charset=utf-8")
            except FileNotFoundError:
                self.send_html("<h1>apex_live_dashboard.html not found</h1>", 404)
            return

        # API
        if path == "/api/all":
            self.send_json(build_all_response())
            return

        if path == "/api/health":
            ages = {k: file_age_minutes(DATA_DIR / v) for k, v in DATA_FILES.items()}
            self.send_json({"status": "ok", "server_time": datetime.now(timezone.utc).isoformat(), "file_ages": ages})
            return

        if path.startswith("/api/"):
            key = path[5:]
            if key not in DATA_FILES:
                self.send_json({"error": f"Unknown key '{key}'"}, 404)
            else:
                p = DATA_DIR / DATA_FILES[key]
                data = load_json(p)
                if data is None:
                    self.send_json({"error": "not found"}, 404)
                else:
                    self.send_json({"data": data, "age_minutes": file_age_minutes(p)})
            return

        self.send_html("<h1>404</h1>", 404)

    # ── POST (login form) ─────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode()
            params = parse_qs(body)
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
                print(f"  ✗ Failed login attempt: '{username}'")
                page = LOGIN_PAGE \
                    .replace("display:ERRVIS", "display:block") \
                    .replace("ERRTEXT", "Invalid username or password.")
                self.send_html(page, 200)
        else:
            self.send_html("<h1>404</h1>", 404)


# ── Entry point ─────────────────────────────────────────────
def main():
    for try_port in [PORT, PORT+1, PORT+2, 8080, 8888]:
        try:
            server = HTTPServer(("127.0.0.1", try_port), Handler)
            used_port = try_port
            break
        except OSError:
            continue
    else:
        print("ERROR: Could not bind to any port. Check if something else is using ports 5050–5052, 8080, 8888.")
        sys.exit(1)

    url = f"http://localhost:{used_port}"

    print()
    print("=" * 60)
    print("  APEX CAPITAL INTELLIGENCE — Live Dashboard")
    print("=" * 60)
    print(f"  Local URL:    {url}")
    print(f"  Password:     {'ENABLED' if PASSWORD_REQUIRED else 'DISABLED (public)'}")
    if PASSWORD_REQUIRED:
        for user in USERS:
            print(f"    • {user}")
    print(f"  Data dir:     {DATA_DIR}")
    print(f"  Auto-refresh: every 30 seconds")
    print()
    print("  To make it PUBLIC → see START.md for ngrok instructions")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    print()

    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
