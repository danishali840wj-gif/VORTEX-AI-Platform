"""VORTEX AI backend - pure Python standard library (no pip install needed).

Auth, subscription keys, admin API, signals, paper-account snapshot, admin panel page.

Run:  python server.py            (reads ADMIN_TOKEN, ENGINE_TOKEN, SECRET_KEY, DB_PATH, PORT from env)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).parent
DURATIONS = {"30d": 30, "90d": 90, "1y": 365}
TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days
MAX_BODY = 1_000_000

CONFIG = {"db_path": str(BASE_DIR / "vortex.db"), "admin_token": "", "engine_token": "", "secret": ""}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    pw_salt TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    sub_expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    duration TEXT NOT NULL,
    days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unused',
    used_by INTEGER,
    used_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sub_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    signal TEXT NOT NULL,
    price REAL NOT NULL,
    sl REAL,
    tp REAL,
    reason TEXT NOT NULL DEFAULT '',
    indicators TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class HTTPError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(CONFIG["db_path"], timeout=15, isolation_level=None)  # autocommit; tx() for multi-step
    conn.row_factory = sqlite3.Row
    return conn


class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK, so multi-step changes (e.g. key activation) are atomic."""

    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, et, ev, tb):
        self.c.execute("ROLLBACK" if et else "COMMIT")
        return False


def init_db():
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


# ---------------------------------------------------------------- auth helpers
def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def make_token(uid):
    payload = base64.urlsafe_b64encode(json.dumps({"uid": uid, "exp": int(time.time()) + TOKEN_TTL}).encode()).decode()
    sig = hmac.new(CONFIG["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_token(token):
    try:
        payload, sig = token.split(".", 1)
        good = hmac.new(CONFIG["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return int(data["uid"]) if data["exp"] >= time.time() else None
    except Exception:
        return None


def sub_active(user):
    exp = user["sub_expires_at"]
    return bool(exp) and datetime.fromisoformat(exp) > datetime.now(timezone.utc)


def log_history(c, user_id, event, detail=""):
    c.execute("INSERT INTO sub_history (user_id, event, detail, created_at) VALUES (?,?,?,?)",
              (user_id, event, detail, now_iso()))


def need(body, field, typ=str, min_len=0, max_len=500):
    v = body.get(field)
    if not isinstance(v, typ) or isinstance(v, bool) and typ is not bool:
        raise HTTPError(400, f"'{field}' is required")
    if typ is str and not (min_len <= len(v) <= max_len):
        raise HTTPError(400, f"'{field}' must be {min_len}-{max_len} characters")
    return v


# ---------------------------------------------------------------- request object
class Req:
    def __init__(self, headers, body, query, match):
        self.headers, self.body, self.query, self.match = headers, body, query, match
        self.user = None

    def header(self, name):
        return self.headers.get(name)


def _bearer_user(req, require_active):
    auth = req.header("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPError(401, "Login required")
    uid = read_token(auth[7:])
    if uid is None:
        raise HTTPError(401, "Invalid or expired token")
    conn = connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not user:
        raise HTTPError(401, "User not found")
    if user["blocked"]:
        raise HTTPError(403, "Account blocked. Contact admin.")
    if require_active and not sub_active(user):
        raise HTTPError(402, "Subscription required. Message admin to buy a key.")
    req.user = user


def _token_header(req, header, expected, label):
    got = req.header(header) or ""
    if not expected or not hmac.compare_digest(got, expected):
        raise HTTPError(401, f"{label} token invalid")


def authorize(req, level):
    if level == "user":
        _bearer_user(req, False)
    elif level == "active":
        _bearer_user(req, True)
    elif level == "admin":
        _token_header(req, "X-Admin-Token", CONFIG["admin_token"], "Admin")
    elif level == "engine":
        _token_header(req, "X-Engine-Token", CONFIG["engine_token"], "Engine")


# ---------------------------------------------------------------- handlers
ROUTES = []


def route(method, pattern, auth=None):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), auth, fn))
        return fn
    return deco


@route("GET", "/api/health")
def h_health(req):
    return 200, {"ok": True, "time": now_iso()}


@route("POST", "/api/auth/register")
def h_register(req):
    email = need(req.body, "email", str, 5, 200).strip().lower()
    password = need(req.body, "password", str, 6, 200)
    name = req.body.get("name", "")
    if not isinstance(name, str) or len(name) > 100:
        raise HTTPError(400, "'name' too long")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPError(400, "Invalid email")
    salt = secrets.token_hex(16)
    conn = connect()
    try:
        try:
            cur = conn.execute("INSERT INTO users (email, name, pw_hash, pw_salt, created_at) VALUES (?,?,?,?,?)",
                               (email, name.strip(), hash_pw(password, salt), salt, now_iso()))
        except sqlite3.IntegrityError:
            raise HTTPError(409, "Email already registered")
        uid = cur.lastrowid
    finally:
        conn.close()
    return 200, {"token": make_token(uid), "email": email, "name": name.strip()}


@route("POST", "/api/auth/login")
def h_login(req):
    email = need(req.body, "email").strip().lower()
    password = need(req.body, "password", str, 0, 200)
    conn = connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    finally:
        conn.close()
    if not user or not hmac.compare_digest(user["pw_hash"], hash_pw(password, user["pw_salt"])):
        raise HTTPError(401, "Wrong email or password")
    if user["blocked"]:
        raise HTTPError(403, "Account blocked. Contact admin.")
    return 200, {"token": make_token(user["id"]), "email": user["email"], "name": user["name"]}


@route("GET", "/api/me", auth="user")
def h_me(req):
    u = req.user
    return 200, {"email": u["email"], "name": u["name"], "subscription_active": sub_active(u), "expires_at": u["sub_expires_at"]}


@route("POST", "/api/subscription/activate", auth="user")
def h_activate(req):
    key = need(req.body, "key", str, 1, 100).strip().upper()
    conn = connect()
    try:
        with tx(conn):
            row = conn.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
            if not row:
                raise HTTPError(404, "Invalid key")
            if row["status"] != "unused":
                raise HTTPError(409, "Key already used or revoked")
            user = conn.execute("SELECT * FROM users WHERE id=?", (req.user["id"],)).fetchone()
            base = datetime.now(timezone.utc)
            if user["sub_expires_at"]:
                cur_exp = datetime.fromisoformat(user["sub_expires_at"])
                if cur_exp > base:
                    base = cur_exp
            new_exp = (base + timedelta(days=row["days"])).isoformat(timespec="seconds")
            conn.execute("UPDATE keys SET status='used', used_by=?, used_at=? WHERE id=?", (user["id"], now_iso(), row["id"]))
            conn.execute("UPDATE users SET sub_expires_at=? WHERE id=?", (new_exp, user["id"]))
            log_history(conn, user["id"], "activated", f"key {key} ({row['duration']}) -> expires {new_exp}")
    finally:
        conn.close()
    return 200, {"subscription_active": True, "expires_at": new_exp}


@route("GET", "/api/signals/latest", auth="active")
def h_latest(req):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return 200, {"signal": None, "message": "Engine has not pushed any signal yet."}
    d = dict(row)
    d["indicators"] = json.loads(d["indicators"] or "{}")
    return 200, {"signal": d}


@route("GET", "/api/signals/history", auth="active")
def h_sig_hist(req):
    try:
        limit = max(1, min(int(req.query.get("limit", ["50"])[0]), 200))
    except ValueError:
        raise HTTPError(400, "limit must be a number")
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return 200, {"signals": [dict(r) for r in rows]}


@route("GET", "/api/paper/summary", auth="active")
def h_paper_summary(req):
    conn = connect()
    try:
        row = conn.execute("SELECT data, updated_at FROM paper_snapshot WHERE id=1").fetchone()
    finally:
        conn.close()
    if not row:
        return 200, {"balance": 0, "open_trades": [], "closed_trades": [],
                     "stats": {"closed_count": 0, "win_rate": None, "total_pnl": 0}, "updated_at": None}
    d = json.loads(row["data"])
    d["updated_at"] = row["updated_at"]
    return 200, d


@route("POST", "/api/signals/push", auth="engine")
def h_push(req):
    b = req.body
    sig = need(b, "signal")
    if sig not in ("BUY", "SELL", "WAIT"):
        raise HTTPError(400, "signal must be BUY, SELL or WAIT")
    price = b.get("price")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        raise HTTPError(400, "'price' must be a number")
    for k in ("sl", "tp"):
        if b.get(k) is not None and (not isinstance(b[k], (int, float)) or isinstance(b[k], bool)):
            raise HTTPError(400, f"'{k}' must be a number or null")
    ind = b.get("indicators", {})
    if not isinstance(ind, dict):
        raise HTTPError(400, "'indicators' must be an object")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO signals (symbol, timeframe, signal, price, sl, tp, reason, indicators, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(b.get("symbol", "XAUUSD"))[:20], str(b.get("timeframe", "M5"))[:10], sig, price, b.get("sl"), b.get("tp"),
             str(b.get("reason", ""))[:500], json.dumps(ind), now_iso()))
        # keep the table small
        conn.execute("DELETE FROM signals WHERE id <= (SELECT MAX(id) FROM signals) - 5000")
    finally:
        conn.close()
    return 200, {"ok": True}


@route("POST", "/api/paper/sync", auth="engine")
def h_sync(req):
    for k in ("balance", "open_trades", "closed_trades", "stats"):
        if k not in req.body:
            raise HTTPError(400, f"missing '{k}'")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO paper_snapshot (id, data, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (json.dumps(req.body), now_iso()))
    finally:
        conn.close()
    return 200, {"ok": True}


# ---- admin
def _key_string():
    raw = secrets.token_hex(8).upper()
    return "VTX-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


@route("POST", "/api/admin/keys/generate", auth="admin")
def h_gen(req):
    duration = req.body.get("duration", "30d")
    if duration not in DURATIONS:
        raise HTTPError(400, f"duration must be one of {list(DURATIONS)}")
    count = req.body.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise HTTPError(400, "count must be 1-100")
    note = str(req.body.get("note", ""))[:200]
    out = []
    conn = connect()
    try:
        with tx(conn):
            for _ in range(count):
                k = _key_string()
                conn.execute("INSERT INTO keys (key, duration, days, note, created_at) VALUES (?,?,?,?,?)",
                             (k, duration, DURATIONS[duration], note, now_iso()))
                out.append(k)
    finally:
        conn.close()
    return 200, {"keys": out, "duration": duration}


@route("GET", "/api/admin/keys", auth="admin")
def h_keys(req):
    conn = connect()
    try:
        rows = conn.execute("SELECT k.*, u.email AS used_by_email FROM keys k LEFT JOIN users u ON u.id=k.used_by "
                            "ORDER BY k.id DESC LIMIT 500").fetchall()
    finally:
        conn.close()
    return 200, {"keys": [dict(r) for r in rows]}


@route("POST", r"/api/admin/keys/(\d+)/revoke", auth="admin")
def h_revoke(req):
    kid = int(req.match.group(1))
    conn = connect()
    try:
        with tx(conn):
            row = conn.execute("SELECT * FROM keys WHERE id=?", (kid,)).fetchone()
            if not row:
                raise HTTPError(404, "Key not found")
            if row["status"] == "used":
                raise HTTPError(409, "Key already used; deactivate the user's subscription instead")
            conn.execute("UPDATE keys SET status='revoked' WHERE id=?", (kid,))
    finally:
        conn.close()
    return 200, {"ok": True}


@route("GET", "/api/admin/users", auth="admin")
def h_users(req):
    conn = connect()
    try:
        rows = conn.execute("SELECT id, email, name, blocked, sub_expires_at, created_at FROM users ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["subscription_active"] = bool(d["sub_expires_at"]) and datetime.fromisoformat(d["sub_expires_at"]) > datetime.now(timezone.utc)
        out.append(d)
    return 200, {"users": out}


def _user_action(uid, fn_sql, params, event):
    conn = connect()
    try:
        with tx(conn):
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                raise HTTPError(404, "User not found")
            conn.execute(fn_sql, params)
            log_history(conn, uid, event)
    finally:
        conn.close()
    return 200, {"ok": True}


@route("POST", r"/api/admin/users/(\d+)/block", auth="admin")
def h_block(req):
    uid = int(req.match.group(1))
    return _user_action(uid, "UPDATE users SET blocked=1 WHERE id=?", (uid,), "blocked")


@route("POST", r"/api/admin/users/(\d+)/unblock", auth="admin")
def h_unblock(req):
    uid = int(req.match.group(1))
    return _user_action(uid, "UPDATE users SET blocked=0 WHERE id=?", (uid,), "unblocked")


@route("POST", r"/api/admin/users/(\d+)/deactivate", auth="admin")
def h_deactivate(req):
    uid = int(req.match.group(1))
    return _user_action(uid, "UPDATE users SET sub_expires_at=NULL WHERE id=?", (uid,), "deactivated")


@route("GET", "/api/admin/history", auth="admin")
def h_history(req):
    try:
        limit = max(1, min(int(req.query.get("limit", ["200"])[0]), 1000))
    except ValueError:
        raise HTTPError(400, "limit must be a number")
    conn = connect()
    try:
        rows = conn.execute("SELECT h.*, u.email FROM sub_history h LEFT JOIN users u ON u.id=h.user_id "
                            "ORDER BY h.id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return 200, {"history": [dict(r) for r in rows]}


# ---------------------------------------------------------------- HTTP server
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Token, X-Engine-Token",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "VortexServer/1.0"

    def log_message(self, fmt, *args):  # quiet; do not log tokens
        pass

    def _send(self, status, payload=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, raw=b"")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            url = urlparse(self.path)
            if method == "GET" and url.path == "/admin":
                return self._send(200, raw=(BASE_DIR / "static" / "admin.html").read_bytes(), ctype="text/html; charset=utf-8")
            if method == "GET" and url.path == "/":
                return self._send(200, raw=b"VORTEX AI backend is running. Admin panel: /admin", ctype="text/plain; charset=utf-8")
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise HTTPError(413, "Body too large")
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise HTTPError(400, "Body must be valid JSON")
                    if not isinstance(body, dict):
                        raise HTTPError(400, "Body must be a JSON object")
            path_found = False
            for m, rx, auth, fn in ROUTES:
                match = rx.match(url.path)
                if not match:
                    continue
                path_found = True
                if m != method:
                    continue
                req = Req(self.headers, body, parse_qs(url.query), match)
                authorize(req, auth)
                status, payload = fn(req)
                return self._send(status, payload)
            raise HTTPError(405 if path_found else 404, "Method not allowed" if path_found else "Not found")
        except HTTPError as e:
            self._send(e.status, {"detail": e.detail})
        except Exception as e:  # never leak internals
            sys.stderr.write(f"internal error: {type(e).__name__}: {e}\n")
            self._send(500, {"detail": "Internal server error"})


def configure(db_path=None, admin_token=None, engine_token=None, secret=None):
    if db_path:
        CONFIG["db_path"] = db_path
    for k, v in (("admin_token", admin_token), ("engine_token", engine_token), ("secret", secret)):
        if v is not None:
            CONFIG[k] = v
    if not (CONFIG["admin_token"] and CONFIG["engine_token"] and CONFIG["secret"]):
        raise RuntimeError("Set ADMIN_TOKEN, ENGINE_TOKEN and SECRET_KEY (see .env.example).")
    init_db()


def make_server(host="0.0.0.0", port=8000):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    configure(os.environ.get("DB_PATH"), os.environ.get("ADMIN_TOKEN", ""), os.environ.get("ENGINE_TOKEN", ""),
              os.environ.get("SECRET_KEY", ""))
    port = int(os.environ.get("PORT", "8000"))
    srv = make_server("0.0.0.0", port)
    print(f"VORTEX backend running on port {port}  (admin panel: /admin)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
