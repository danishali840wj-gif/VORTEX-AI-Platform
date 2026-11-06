"""VORTEX engine runner.

Every NEW closed 5-minute candle of gold:
  1. feed it to the paper account (closes trades whose SL/TP was touched, enforces daily loss limit)
  2. run the strategy -> BUY / SELL / WAIT
  3. if BUY/SELL, ask the risk manager to open a paper trade (all limits enforced in vortex/risk.py)
  4. push the signal + a snapshot of the paper account to the backend (the Android app reads these)

Default: virtual paper trades. If MT5_MODE=demo|live (see vortex/mt5.py), each approved trade is ALSO sent to MT5.

Usage:  python run_engine.py            (checks every 60 seconds)
        python run_engine.py --once     (single check)
Env:    BACKEND_URL, ENGINE_TOKEN, VORTEX_START_BALANCE (default 1000), VORTEX_ENGINE_DB (default engine.db)
"""
import os
import sys
import time
from datetime import datetime

from strategy import decide
from vortex.db import connect
from vortex.mt5 import MetaApiBroker
from vortex.paper import PaperAccount
from vortex.risk import RiskConfig

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
ENGINE_TOKEN = os.environ.get("ENGINE_TOKEN", "")
SYMBOL = os.environ.get("VORTEX_YF_SYMBOL", "GC=F")  # initial dev data source: Yahoo gold futures
START_BALANCE = float(os.environ.get("VORTEX_START_BALANCE", "1000"))
ENGINE_DB = os.environ.get("VORTEX_ENGINE_DB", "engine.db")
CANDLE_SECONDS = 300


def parse_yahoo_chart(payload: dict) -> list:
    """Turn Yahoo's chart JSON into a list of OHLC candle dicts (t = unix seconds, UTC), dropping empty candles."""
    result = payload["chart"]["result"][0]
    q = result["indicators"]["quote"][0]
    out = []
    for t, o, h, l, c in zip(result["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, l, c):
            continue
        out.append({"t": int(t), "Open": float(o), "High": float(h), "Low": float(l), "Close": float(c)})
    if not out:
        raise RuntimeError("No market data returned (market closed?)")
    return out


def fetch_candles() -> list:
    import json
    import urllib.request

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?interval=5m&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (VORTEX engine)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return parse_yahoo_chart(json.loads(r.read()))


def api(path: str, payload: dict):
    import json
    import urllib.request

    req = urllib.request.Request(
        BACKEND_URL + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Engine-Token": ENGINE_TOKEN})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


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


def snapshot(conn, acc, broker=None, broker_info=None) -> dict:
    acct = acc._account()
    open_t = conn.execute("SELECT * FROM trades WHERE account_id=? AND status='open' ORDER BY id DESC", (acc.id,)).fetchall()
    closed = conn.execute(
        "SELECT * FROM trades WHERE account_id=? AND status='closed' ORDER BY closed_at DESC, id DESC LIMIT 100", (acc.id,)
    ).fetchall()
    st = acc.stats()
    return {
        "mode": broker.mode.upper() if broker else "VIRTUAL",
        "broker_balance": round(float(broker_info["balance"]), 2) if broker_info else None,
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


def process_candle(conn, acc, closed_df: list, push=api, broker=None) -> dict:
    """Handle ONE new closed candle. Returns a small dict describing what happened (used by tests)."""
    last = closed_df[-1]
    ts = int(last["t"]) + CANDLE_SECONDS

    closed_ids = acc.on_candle(float(last["High"]), float(last["Low"]), float(last["Close"]), ts)
    d = decide(closed_df)

    sl = tp = None
    opened = None
    broker_info = None
    reason = ""
    if d.signal in ("BUY", "SELL") and d.sl_distance:
        sl, tp = acc.risk.levels(d.signal, d.price, d.sl_distance)
        opened, reason = acc.open_trade(d.signal, d.price, d.sl_distance, ts)
        if opened and broker:  # the trade passed every risk rule -> mirror it as a REAL order on MT5
            try:
                info = broker.account_info()
                broker_info = info
                lots = broker.lots_for(acc.risk, info, d.price, d.sl_distance)
                if lots < 0.01:
                    reason = "MT5: balance too small for 0.01 lot - order NOT sent"
                else:
                    broker.place(d.signal, lots, sl, tp)
                    reason = f"MT5 {broker.mode}: {d.signal} {lots} lot sent"
            except Exception as e:
                reason = f"MT5 order FAILED: {e}"
                print("  ", reason)

    try:
        push("/api/signals/push", {
            "symbol": "XAUUSD", "timeframe": "M5", "signal": d.signal, "price": round(d.price, 2),
            "sl": round(sl, 2) if sl else None, "tp": round(tp, 2) if tp else None,
            "reason": d.reason + (f" | {reason}" if reason and (not opened or broker) else ""), "indicators": d.indicators,
        })
        if broker and broker_info is None:
            try:
                broker_info = broker.account_info()
            except Exception:
                pass
        push("/api/paper/sync", snapshot(conn, acc, broker, broker_info))
    except Exception as e:  # keep engine alive if backend is down; next candle pushes again
        print("  push failed:", e)

    return {"signal": d.signal, "closed": closed_ids, "opened": opened, "reason": reason}


def cycle(conn, acc):
    df = fetch_candles()
    closed_df = df[:-1]  # drop the still-forming candle
    candle_ts = int(closed_df[-1]["t"])
    if str(candle_ts) == get_state(conn, "last_candle_ts"):
        return  # already processed this candle
    res = process_candle(conn, acc, closed_df, broker=BROKER)
    set_state(conn, "last_candle_ts", candle_ts)
    print(f"{datetime.now():%H:%M:%S} signal={res['signal']} closed={res['closed']} opened={res['opened']} {res['reason']}")


BROKER = MetaApiBroker.from_env()


def main():
    if not ENGINE_TOKEN:
        sys.exit("Set ENGINE_TOKEN (same value as backend).")
    print("MT5 mode:", BROKER.mode.upper() if BROKER else "OFF (virtual paper trades only)")
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
