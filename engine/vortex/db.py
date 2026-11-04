"""VORTEX AI - database layer (SQLite).

Stage 1 only needs `accounts`, `trades` and `events`. The `users` and
`activation_keys` tables are created now so later stages (auth, subscription)
can plug in without a schema change.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    is_blocked INTEGER NOT NULL DEFAULT 0,
    subscription_expires_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activation_keys (
    key TEXT PRIMARY KEY,
    duration_days INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    used_by INTEGER REFERENCES users(id),
    used_at INTEGER
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    mode TEXT NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper', 'live')),
    balance REAL NOT NULL,
    start_balance REAL NOT NULL,
    trading_enabled INTEGER NOT NULL DEFAULT 0,
    emergency_stopped INTEGER NOT NULL DEFAULT 0,
    risk_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    units REAL NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    risk_amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at INTEGER NOT NULL,
    closed_at INTEGER,
    exit_price REAL,
    pnl REAL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_account_status ON trades(account_id, status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""


def connect(path: str = "vortex.db") -> sqlite3.Connection:
    """Open (and initialise) the database. Use ':memory:' for tests."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
