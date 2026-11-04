"""VORTEX engine runner: fetch gold M5 candles, decide, push signal, run paper trading.

Paper trading ONLY. No broker connection, no real money.
Usage:  python run_engine.py            (loops every 60 seconds)
        python run_engine.py --once     (single cycle)
Env: BACKEND_URL, ENGINE_TOKEN
"""
import os
import sys
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

from risk import DayState, lot_size, pnl_usd
from strategy import decide

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
ENGINE_TOKEN = os.environ.get("ENGINE_TOKEN", "")
SYMBOL = os.environ.get("VORTEX_YF_SYMBOL", "GC=F")  # initial dev data source (gold futures)
HEADERS = {"X-Engine-Token": ENGINE_TOKEN}


def fetch_candles() -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(SYMBOL, period="5d", interval="5m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("No market data returned (market closed or network issue)")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def api(method: str, path: str, **kw):
    r = httpx.request(method, BACKEND_URL + path, headers=HEADERS, timeout=20, **kw)
    r.raise_for_status()
    return r.json()


def check_open_trades(state: dict, last: pd.Series, day: DayState):
    for t in state["open_trades"]:
        hit_sl = last["Low"] <= t["sl"] if t["side"] == "BUY" else last["High"] >= t["sl"]
        hit_tp = last["High"] >= t["tp"] if t["side"] == "BUY" else last["Low"] <= t["tp"]
        if not (hit_sl or hit_tp):
            continue
        # if both hit inside one candle, assume SL first (conservative)
        reason, px = ("SL", t["sl"]) if hit_sl else ("TP", t["tp"])
        pnl = pnl_usd(t["side"], t["entry"], px, t["lots"])
        api("POST", f"/api/paper/close/{t['id']}", json={"exit_price": px, "exit_reason": reason, "pnl": pnl})
        day.on_close(pnl)
        print(f"  closed trade #{t['id']} {t['side']} by {reason}, pnl {pnl}")


def cycle(day: DayState):
    df = fetch_candles()
    closed = df.iloc[:-1]  # drop the still-forming candle
    last = closed.iloc[-1]
    state = api("GET", "/api/paper/state")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day.roll_day(today, state["balance"])
    day.on_candle()

    check_open_trades(state, last, day)
    state = api("GET", "/api/paper/state")

    d = decide(closed)
    api("POST", "/api/signals/push", json={
        "symbol": "XAUUSD", "timeframe": "M5", "signal": d.signal, "price": d.price,
        "sl": d.sl, "tp": d.tp, "reason": d.reason, "indicators": d.indicators,
    })
    print(f"{datetime.now():%H:%M:%S} signal={d.signal} price={d.price} reason={d.reason}")

    if d.signal in ("BUY", "SELL") and not state["open_trades"]:
        ok, why = day.can_trade()
        if not ok:
            print(f"  skip trade: {why}")
            return
        lots = lot_size(state["balance"], d.price, d.sl)
        res = api("POST", "/api/paper/open", json={
            "side": d.signal, "entry": d.price, "sl": d.sl, "tp": d.tp, "lots": lots})
        print(f"  opened paper trade #{res['id']} {d.signal} lots={lots}")


def main():
    if not ENGINE_TOKEN:
        sys.exit("Set ENGINE_TOKEN (same value as backend).")
    once = "--once" in sys.argv
    day = DayState()
    while True:
        try:
            cycle(day)
        except Exception as e:  # keep the engine alive
            print("cycle error:", e)
        if once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
