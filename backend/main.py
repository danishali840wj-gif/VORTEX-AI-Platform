"""VORTEX AI backend: auth, subscription keys, admin API, signals, paper trades.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "vortex.db"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
ENGINE_TOKEN = os.environ.get("ENGINE_TOKEN", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days

DURATIONS = {"30d": 30, "90d": 90, "1y": 365}

app = FastAPI(title="VORTEX AI Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- database
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
    status TEXT NOT NULL DEFAULT 'unused',   -- unused | used | revoked
    used_by INTEGER,
    used_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sub_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,                      -- activated | deactivated | blocked | unblocked
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    signal TEXT NOT NULL,                     -- BUY | SELL | WAIT
    price REAL NOT NULL,
    sl REAL,
    tp REAL,
    reason TEXT NOT NULL DEFAULT '',
    indicators TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    lots REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open | closed
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def startup():
    if not (ADMIN_TOKEN and ENGINE_TOKEN and SECRET_KEY):
        raise RuntimeError("Set ADMIN_TOKEN, ENGINE_TOKEN and SECRET_KEY environment variables (see .env.example).")
    with db() as c:
        c.executescript(SCHEMA)
        c.execute(
            "INSERT OR IGNORE INTO paper_account (id, balance, updated_at) VALUES (1, 1000.0, ?)",
            (now_iso(),),
        )


# ---------------------------------------------------------------- auth helpers
def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def make_token(user_id: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"uid": user_id, "exp": int(time.time()) + TOKEN_TTL}).encode()).decode()
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_token(token: str) -> Optional[int]:
    try:
        payload, sig = token.split(".", 1)
        good = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if data["exp"] < time.time():
            return None
        return int(data["uid"])
    except Exception:
        return None


def current_user(authorization: Optional[str] = Header(None)) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Login required")
    uid = read_token(authorization[7:])
    if uid is None:
        raise HTTPException(401, "Invalid or expired token")
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        raise HTTPException(401, "User not found")
    if user["blocked"]:
        raise HTTPException(403, "Account blocked. Contact admin.")
    return user


def sub_active(user: sqlite3.Row) -> bool:
    exp = user["sub_expires_at"]
    return bool(exp) and datetime.fromisoformat(exp) > datetime.now(timezone.utc)


def active_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if not sub_active(user):
        raise HTTPException(402, "Subscription required. Message admin to buy a key.")
    return user


def admin_only(x_admin_token: Optional[str] = Header(None)):
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "Admin token invalid")


def engine_only(x_engine_token: Optional[str] = Header(None)):
    if not x_engine_token or not hmac.compare_digest(x_engine_token, ENGINE_TOKEN):
        raise HTTPException(401, "Engine token invalid")


def log_history(c, user_id: int, event: str, detail: str = ""):
    c.execute(
        "INSERT INTO sub_history (user_id, event, detail, created_at) VALUES (?,?,?,?)",
        (user_id, event, detail, now_iso()),
    )


# ---------------------------------------------------------------- models
class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=6, max_length=200)
    name: str = Field(default="", max_length=100)


class LoginIn(BaseModel):
    email: str
    password: str


class ActivateIn(BaseModel):
    key: str


class GenerateKeysIn(BaseModel):
    duration: str = "30d"
    count: int = Field(default=1, ge=1, le=100)
    note: str = ""


class SignalIn(BaseModel):
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    signal: str
    price: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    reason: str = ""
    indicators: dict = {}


class PaperOpenIn(BaseModel):
    side: str
    entry: float
    sl: float
    tp: float
    lots: float


class PaperCloseIn(BaseModel):
    exit_price: float
    exit_reason: str
    pnl: float


# ---------------------------------------------------------------- user routes
@app.get("/api/health")
def health():
    return {"ok": True, "time": now_iso()}


@app.post("/api/auth/register")
def register(body: RegisterIn):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Invalid email")
    salt = secrets.token_hex(16)
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "Email already registered")
        cur = c.execute(
            "INSERT INTO users (email, name, pw_hash, pw_salt, created_at) VALUES (?,?,?,?,?)",
            (email, body.name.strip(), hash_pw(body.password, salt), salt, now_iso()),
        )
        uid = cur.lastrowid
    return {"token": make_token(uid), "email": email, "name": body.name.strip()}


@app.post("/api/auth/login")
def login(body: LoginIn):
    email = body.email.strip().lower()
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not hmac.compare_digest(user["pw_hash"], hash_pw(body.password, user["pw_salt"])):
        raise HTTPException(401, "Wrong email or password")
    if user["blocked"]:
        raise HTTPException(403, "Account blocked. Contact admin.")
    return {"token": make_token(user["id"]), "email": user["email"], "name": user["name"]}


@app.get("/api/me")
def me(user: sqlite3.Row = Depends(current_user)):
    return {
        "email": user["email"],
        "name": user["name"],
        "subscription_active": sub_active(user),
        "expires_at": user["sub_expires_at"],
    }


@app.post("/api/subscription/activate")
def activate(body: ActivateIn, user: sqlite3.Row = Depends(current_user)):
    key = body.key.strip().upper()
    with db() as c:
        row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "Invalid key")
        if row["status"] != "unused":
            raise HTTPException(409, "Key already used or revoked")
        # extend from current expiry if still active, else from now
        base = datetime.now(timezone.utc)
        if user["sub_expires_at"]:
            cur_exp = datetime.fromisoformat(user["sub_expires_at"])
            if cur_exp > base:
                base = cur_exp
        new_exp = (base + timedelta(days=row["days"])).isoformat(timespec="seconds")
        c.execute("UPDATE keys SET status='used', used_by=?, used_at=? WHERE id=?", (user["id"], now_iso(), row["id"]))
        c.execute("UPDATE users SET sub_expires_at=? WHERE id=?", (new_exp, user["id"]))
        log_history(c, user["id"], "activated", f"key {key} ({row['duration']}) -> expires {new_exp}")
    return {"subscription_active": True, "expires_at": new_exp}


@app.get("/api/signals/latest")
def latest_signal(user: sqlite3.Row = Depends(active_user)):
    with db() as c:
        row = c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"signal": None, "message": "Engine has not pushed any signal yet."}
    d = dict(row)
    d["indicators"] = json.loads(d["indicators"] or "{}")
    return {"signal": d}


@app.get("/api/signals/history")
def signal_history(limit: int = 50, user: sqlite3.Row = Depends(active_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (min(limit, 200),)).fetchall()
    return {"signals": [dict(r) for r in rows]}


@app.get("/api/paper/summary")
def paper_summary(user: sqlite3.Row = Depends(active_user)):
    with db() as c:
        acct = c.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        open_t = c.execute("SELECT * FROM paper_trades WHERE status='open' ORDER BY id DESC").fetchall()
        closed = c.execute("SELECT * FROM paper_trades WHERE status='closed' ORDER BY id DESC LIMIT 100").fetchall()
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    total_pnl = sum((t["pnl"] or 0) for t in closed)
    return {
        "balance": acct["balance"],
        "open_trades": [dict(t) for t in open_t],
        "closed_trades": [dict(t) for t in closed],
        "stats": {
            "closed_count": len(closed),
            "win_rate": round(100 * len(wins) / len(closed), 2) if closed else None,
            "total_pnl": round(total_pnl, 2),
        },
    }


# ---------------------------------------------------------------- engine routes
@app.post("/api/signals/push", dependencies=[Depends(engine_only)])
def push_signal(body: SignalIn):
    if body.signal not in ("BUY", "SELL", "WAIT"):
        raise HTTPException(400, "signal must be BUY, SELL or WAIT")
    with db() as c:
        c.execute(
            "INSERT INTO signals (symbol, timeframe, signal, price, sl, tp, reason, indicators, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (body.symbol, body.timeframe, body.signal, body.price, body.sl, body.tp, body.reason, json.dumps(body.indicators), now_iso()),
        )
    return {"ok": True}


@app.post("/api/paper/open", dependencies=[Depends(engine_only)])
def paper_open(body: PaperOpenIn):
    if body.side not in ("BUY", "SELL"):
        raise HTTPException(400, "side must be BUY or SELL")
    with db() as c:
        cur = c.execute(
            "INSERT INTO paper_trades (side, entry, sl, tp, lots, opened_at) VALUES (?,?,?,?,?,?)",
            (body.side, body.entry, body.sl, body.tp, body.lots, now_iso()),
        )
    return {"id": cur.lastrowid}


@app.post("/api/paper/close/{trade_id}", dependencies=[Depends(engine_only)])
def paper_close(trade_id: int, body: PaperCloseIn):
    with db() as c:
        t = c.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        if not t or t["status"] != "open":
            raise HTTPException(404, "Open trade not found")
        c.execute(
            "UPDATE paper_trades SET status='closed', exit_price=?, exit_reason=?, pnl=?, closed_at=? WHERE id=?",
            (body.exit_price, body.exit_reason, body.pnl, now_iso(), trade_id),
        )
        c.execute("UPDATE paper_account SET balance = balance + ?, updated_at=? WHERE id=1", (body.pnl, now_iso()))
    return {"ok": True}


@app.get("/api/paper/state", dependencies=[Depends(engine_only)])
def paper_state():
    with db() as c:
        acct = c.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        open_t = c.execute("SELECT * FROM paper_trades WHERE status='open'").fetchall()
    return {"balance": acct["balance"], "open_trades": [dict(t) for t in open_t]}


# ---------------------------------------------------------------- admin routes
def _key_string() -> str:
    raw = secrets.token_hex(8).upper()
    return "VTX-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


@app.post("/api/admin/keys/generate", dependencies=[Depends(admin_only)])
def admin_generate(body: GenerateKeysIn):
    if body.duration not in DURATIONS:
        raise HTTPException(400, f"duration must be one of {list(DURATIONS)}")
    out = []
    with db() as c:
        for _ in range(body.count):
            k = _key_string()
            c.execute(
                "INSERT INTO keys (key, duration, days, note, created_at) VALUES (?,?,?,?,?)",
                (k, body.duration, DURATIONS[body.duration], body.note, now_iso()),
            )
            out.append(k)
    return {"keys": out, "duration": body.duration}


@app.get("/api/admin/keys", dependencies=[Depends(admin_only)])
def admin_keys():
    with db() as c:
        rows = c.execute(
            "SELECT k.*, u.email AS used_by_email FROM keys k LEFT JOIN users u ON u.id=k.used_by ORDER BY k.id DESC LIMIT 500"
        ).fetchall()
    return {"keys": [dict(r) for r in rows]}


@app.post("/api/admin/keys/{key_id}/revoke", dependencies=[Depends(admin_only)])
def admin_revoke_key(key_id: int):
    with db() as c:
        row = c.execute("SELECT * FROM keys WHERE id=?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Key not found")
        if row["status"] == "used":
            raise HTTPException(409, "Key already used; deactivate the user's subscription instead")
        c.execute("UPDATE keys SET status='revoked' WHERE id=?", (key_id,))
    return {"ok": True}


@app.get("/api/admin/users", dependencies=[Depends(admin_only)])
def admin_users():
    with db() as c:
        rows = c.execute("SELECT id, email, name, blocked, sub_expires_at, created_at FROM users ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["subscription_active"] = bool(d["sub_expires_at"]) and datetime.fromisoformat(d["sub_expires_at"]) > datetime.now(timezone.utc)
        out.append(d)
    return {"users": out}


@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(admin_only)])
def admin_block(user_id: int):
    return _set_blocked(user_id, 1)


@app.post("/api/admin/users/{user_id}/unblock", dependencies=[Depends(admin_only)])
def admin_unblock(user_id: int):
    return _set_blocked(user_id, 0)


def _set_blocked(user_id: int, value: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "User not found")
        c.execute("UPDATE users SET blocked=? WHERE id=?", (value, user_id))
        log_history(c, user_id, "blocked" if value else "unblocked")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/deactivate", dependencies=[Depends(admin_only)])
def admin_deactivate(user_id: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "User not found")
        c.execute("UPDATE users SET sub_expires_at=NULL WHERE id=?", (user_id,))
        log_history(c, user_id, "deactivated")
    return {"ok": True}


@app.get("/api/admin/history", dependencies=[Depends(admin_only)])
def admin_history(limit: int = 200):
    with db() as c:
        rows = c.execute(
            "SELECT h.*, u.email FROM sub_history h LEFT JOIN users u ON u.id=h.user_id ORDER BY h.id DESC LIMIT ?",
            (min(limit, 1000),),
        ).fetchall()
    return {"history": [dict(r) for r in rows]}


# ---------------------------------------------------------------- static pages
@app.get("/admin")
def admin_page():
    return FileResponse(BASE_DIR / "static" / "admin.html")
