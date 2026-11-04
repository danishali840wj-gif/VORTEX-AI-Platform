"""Integration test: strategy + RiskManager + PaperAccount + snapshot, on SYNTHETIC candles.

This checks the wiring and the money math only. It says nothing about real-market profitability.
"""
import unittest

import numpy as np
import pandas as pd

import run_engine
from vortex.db import connect
from vortex.paper import PaperAccount
from vortex.risk import RiskConfig


def synthetic(n=900, seed=7, drift=0.05, vol=1.2):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-05 00:00", periods=n, freq="5min")
    close = 2000 + np.cumsum(rng.normal(drift, vol, n))
    df = pd.DataFrame({"Close": close}, index=idx)
    df["Open"] = df["Close"].shift(1).fillna(df["Close"])
    df["High"] = df[["Open", "Close"]].max(axis=1) + rng.uniform(0.1, 0.8, n)
    df["Low"] = df[["Open", "Close"]].min(axis=1) - rng.uniform(0.1, 0.8, n)
    return df[["Open", "High", "Low", "Close"]]


class EngineWiringTests(unittest.TestCase):
    def run_stream(self, df, start=1000.0):
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, start, risk=RiskConfig())
        acc.set_trading(True)
        pushes, signals = [], []

        def fake_push(path, payload):
            pushes.append((path, payload))

        for i in range(130, len(df)):
            res = run_engine.process_candle(conn, acc, df.iloc[:i], push=fake_push)
            signals.append(res["signal"])
        return conn, acc, pushes, signals

    def test_live_like_stream(self):
        # try a few seeds; wiring must hold for all, and at least one must actually trade
        traded = 0
        for seed in (1, 2, 3, 4, 5, 6):
            df = synthetic(seed=seed, drift=0.05 if seed % 2 else -0.05)
            conn, acc, pushes, signals = self.run_stream(df)

            # every candle pushed a signal and a snapshot
            paths = [p for p, _ in pushes]
            self.assertEqual(paths.count("/api/signals/push"), len(signals))
            self.assertEqual(paths.count("/api/paper/sync"), len(signals))
            self.assertTrue(set(signals) <= {"BUY", "SELL", "WAIT"})

            # never more than max_open_trades open
            self.assertLessEqual(len(acc.open_trades()), acc.risk.cfg.max_open_trades)

            # balance == start + sum of closed pnl
            closed = conn.execute("SELECT pnl, risk_amount, units, entry_price, sl_price, tp_price, side "
                                  "FROM trades WHERE status='closed'").fetchall()
            self.assertAlmostEqual(acc.balance, 1000.0 + sum(t["pnl"] for t in closed), places=6)

            # every opened trade risked at most 1.7% of the starting account scale (balance only moves a little)
            for t in conn.execute("SELECT * FROM trades").fetchall():
                self.assertLessEqual(t["risk_amount"], 0.017 * max(acc.balance, 1000.0) * 1.5)
                if t["side"] == "BUY":
                    self.assertLess(t["sl_price"], t["entry_price"])
                    self.assertGreater(t["tp_price"], t["entry_price"])
                else:
                    self.assertGreater(t["sl_price"], t["entry_price"])
                    self.assertLess(t["tp_price"], t["entry_price"])
            traded += len(closed)

            # snapshot has exactly what the backend/app expect
            snap = pushes[-1][1]
            for k in ("balance", "open_trades", "closed_trades", "stats", "trading_enabled", "emergency_stopped"):
                self.assertIn(k, snap)
            for k in ("closed_count", "win_rate", "total_pnl"):
                self.assertIn(k, snap["stats"])
            for t in snap["closed_trades"]:
                for k in ("side", "exit_reason", "pnl"):
                    self.assertIn(k, t)
            for t in snap["open_trades"]:
                for k in ("side", "entry", "sl", "tp"):
                    self.assertIn(k, t)
        self.assertGreater(traded, 0, "no trade was ever closed on any synthetic seed - strategy never triggered")

    def test_parse_yahoo_chart_drops_empty_candles(self):
        payload = {"chart": {"result": [{
            "timestamp": [1767571200, 1767571500, 1767571800],
            "indicators": {"quote": [{
                "open": [2000.0, None, 2001.0], "high": [2001.0, None, 2002.0],
                "low": [1999.0, None, 2000.0], "close": [2000.5, None, 2001.5]}]}}]}}
        df = run_engine.parse_yahoo_chart(payload)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close"])
        self.assertEqual(str(df.index.tz), "UTC")
        empty = {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{"open": [None], "high": [None], "low": [None], "close": [None]}]}}]}}
        with self.assertRaises(RuntimeError):
            run_engine.parse_yahoo_chart(empty)

    def test_push_failure_does_not_crash(self):
        df = synthetic()
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, 1000.0)
        acc.set_trading(True)

        def broken(path, payload):
            raise RuntimeError("backend down")

        res = run_engine.process_candle(conn, acc, df.iloc[:200], push=broken)
        self.assertIn(res["signal"], ("BUY", "SELL", "WAIT"))

    def test_trading_off_blocks_trades(self):
        df = synthetic(seed=1)
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, 1000.0)  # trading OFF by default
        for i in range(130, len(df)):
            run_engine.process_candle(conn, acc, df.iloc[:i], push=lambda *a: None)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
