"""VORTEX engine runner.

Every NEW closed 5-minute candle of gold:
  1. feed it to the paper account (closes trades whose SL/TP was touched, enforces daily loss limit)
  2. run the strategy -> BUY / SELL / WAIT
  3. if BUY/SELL, ask the risk manager to open a paper trade (all limits enforced in vortex/risk.py)
  4. push the signal + a snapshot of the paper account to the backend (the Android app reads these)

Paper trading ONLY: virtual money, no broker connection.

Usage:  python run_engine.py            (checks every 60 seconds)
        python run_engine.py --once     (single check)
Env:    BACKEND_URL, ENGINE_TOKEN, VORTEX_START_BALANCE (default 1000), VORTEX_ENGINE_DB (default engine.db)
"""
import os
import sys
import time
from datetime import datetime

import pandas as pd

from strategy import decide
from vortex.db import connect
from vortex.paper import PaperAccount
from vortex.risk import RiskConfig

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
ENGINE_TOKEN = os.environ.get("ENGINE_TOKEN", "")
SYMBOL = os.environ.get("VORTEX_YF_SYMBOL", "GC=F")  # initial dev data source (gold futures)
START_BALANCE = float(os.environ.get("VORTEX_START_BALANCE", "1000"))
ENGINE_DB = os.environ.get("VORTEX_ENGINE_DB", "engine.db")
CANDLE_SECONDS = 300


def fetch_candles() -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(SYMBOL, period="5d", interval="5m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("No market data returned (market closed or network issue)")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def api(path: str, payload: dict):
    import httpx

    r = httpx.post(BACKEND_URL + path, headers={"X-Engine-Token": ENGINE_TOKEN}, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def open_account():
    conn = connect(ENGINE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS engine_state (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    row = conn.execute("SELECT id FROM accounts WHERE mode='paper' ORDER BY id LIMIT 1").fetchone()
    if row:
        return conn, PaperAccount(conn, row["id"])
    acc = PaperAccount.create(conn, START_BALANCE, risk=RiskConfig())
    acc.set_trading(True)  # paper mode only: virtual money, so trading starts ON
    return conn, acc


def get_state(conn, key, default=None):
    row = conn.execute("SELECT v FROM engine_state WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_state(conn, key, value):
    conn.execute("INSERT INTO engine_state (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
    conn.commit()


def snapshot(conn, acc) -> dict:
    acct = acc._account()
    open_t = conn.execute("SELECT * FROM trades WHERE account_id=? AND status='open' ORDER BY id DESC", (acc.id,)).fetchall()
    closed = conn.execute(
        "SELECT * FROM trades WHERE account_id=? AND status='closed' ORDER BY closed_at DESC, id DESC LIMIT 100", (acc.id,)
    ).fetchall()
    st = acc.stats()
    return {
        "balance": round(acc.balance, 2),
        "trading_enabled": bool(acct["trading_enabled"]),
        "emergency_stopped": bool(acct["emergency_stopped"]),
        "open_trades": [
            {"id": t["id"], "side": t["side"], "entry": t["entry_price"], "sl": round(t["sl_price"], 2),
             "tp": round(t["tp_price"], 2), "units": t["units"], "opened_at": t["opened_at"]} for t in open_t
        ],
        "closed_trades": [
            {"id": t["id"], "side": t["side"], "entry": t["entry_price"], "exit_price": t["exit_price"],
             "exit_reason": t["close_reason"], "pnl": round(t["pnl"], 2), "closed_at": t["closed_at"]} for t in closed
        ],
        "stats": {
            "closed_count": st["trades"],
            "win_rate": round(st["win_rate_pct"], 2) if st["trades"] else None,
            "total_pnl": round(st["total_pnl"], 2),
            "profit_factor": round(st["profit_factor"], 2) if st["profit_factor"] is not None else None,
            "max_drawdown_pct": round(st["max_drawdown_pct"], 2),
        },
    }


def process_candle(conn, acc, closed_df: pd.DataFrame, push=api) -> dict:
    """Handle ONE new closed candle. Returns a small dict describing what happened (used by tests)."""
    last = closed_df.iloc[-1]
    ts = int(pd.Timestamp(closed_df.index[-1]).timestamp()) + CANDLE_SECONDS

    closed_ids = acc.on_candle(float(last["High"]), float(last["Low"]), float(last["Close"]), ts)
    d = decide(closed_df)

    sl = tp = None
    opened = None
    reason = ""
    if d.signal in ("BUY", "SELL") and d.sl_distance:
        sl, tp = acc.risk.levels(d.signal, d.price, d.sl_distance)
        opened, reason = acc.open_trade(d.signal, d.price, d.sl_distance, ts)

    try:
        push("/api/signals/push", {
            "symbol": "XAUUSD", "timeframe": "M5", "signal": d.signal, "price": round(d.price, 2),
            "sl": round(sl, 2) if sl else None, "tp": round(tp, 2) if tp else None,
            "reason": d.reason + (f" | trade: {reason}" if reason and not opened else ""), "indicators": d.indicators,
        })
        push("/api/paper/sync", snapshot(conn, acc))
    except Exception as e:  # keep engine alive if backend is down; next candle pushes again
        print("  push failed:", e)

    return {"signal": d.signal, "closed": closed_ids, "opened": opened, "reason": reason}


def cycle(conn, acc):
    df = fetch_candles()
    closed_df = df.iloc[:-1]  # drop the still-forming candle
    candle_ts = int(pd.Timestamp(closed_df.index[-1]).timestamp())
    if str(candle_ts) == get_state(conn, "last_candle_ts"):
        return  # already processed this candle
    res = process_candle(conn, acc, closed_df)
    set_state(conn, "last_candle_ts", candle_ts)
    print(f"{datetime.now():%H:%M:%S} signal={res['signal']} closed={res['closed']} opened={res['opened']} {res['reason']}")


def main():
    if not ENGINE_TOKEN:
        sys.exit("Set ENGINE_TOKEN (same value as backend).")
    conn, acc = open_account()
    once = "--once" in sys.argv
    while True:
        try:
            cycle(conn, acc)
        except Exception as e:  # keep the engine alive
            print("cycle error:", e)
        if once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
