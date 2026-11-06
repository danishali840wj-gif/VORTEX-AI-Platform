import unittest

import run_engine
from tests.test_engine_integration import synthetic
from vortex.db import connect
from vortex.mt5 import LIVE_CONFIRM, MetaApiBroker
from vortex.paper import PaperAccount
from vortex.risk import RiskManager


class FakeHttp:
    def __init__(self, balance=1000.0, currency="USD", fail=False):
        self.calls, self.balance, self.currency, self.fail = [], balance, currency, fail

    def __call__(self, method, url, token, payload=None, timeout=30):
        self.calls.append((method, url, token, payload))
        if self.fail and method == "POST":
            raise RuntimeError("broker down")
        if url.endswith("/account-information"):
            return {"balance": self.balance, "currency": self.currency}
        return {"orderId": "1"}


class Mt5Tests(unittest.TestCase):
    def test_off_by_default_and_live_needs_confirmation(self):
        self.assertIsNone(MetaApiBroker.from_env({}))
        base = {"METAAPI_TOKEN": "t", "METAAPI_ACCOUNT_ID": "a"}
        self.assertIsNone(MetaApiBroker.from_env({**base, "MT5_MODE": "live"}))
        self.assertIsNone(MetaApiBroker.from_env({"MT5_MODE": "demo"}))  # no token
        self.assertEqual(MetaApiBroker.from_env({**base, "MT5_MODE": "demo"}).mode, "demo")
        self.assertEqual(MetaApiBroker.from_env({**base, "MT5_MODE": "live", "MT5_LIVE_CONFIRM": LIVE_CONFIRM}).mode, "live")

    def test_lot_sizing_and_cap(self):
        b = MetaApiBroker("t", "a", "demo", max_lot=0.10, http=FakeHttp())
        risk = RiskManager()
        # $1000 * 1.7% = $17 risk, stop 5.0 -> 3.4 oz = 0.034 lot -> floor 0.03
        self.assertEqual(b.lots_for(risk, {"balance": 1000, "currency": "USD"}, 4000.0, 5.0), 0.03)
        # cent account reports cents: 100000 USC == $1000
        self.assertEqual(b.lots_for(risk, {"balance": 100000, "currency": "USC"}, 4000.0, 5.0), 0.03)
        # huge balance is capped by MT5_MAX_LOT
        self.assertEqual(b.lots_for(risk, {"balance": 1_000_000, "currency": "USD"}, 4000.0, 5.0), 0.10)
        # tiny balance -> too small
        self.assertLess(b.lots_for(risk, {"balance": 50, "currency": "USD"}, 4000.0, 5.0), 0.01)

    def _run(self, http, seed=1):
        df = synthetic(seed=seed, drift=0.05)
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, 1000.0)
        acc.set_trading(True)
        broker = MetaApiBroker("TOKEN", "ACC", "demo", http=http)
        pushes = []
        for i in range(130, len(df)):
            run_engine.process_candle(conn, acc, df[:i], push=lambda p, d: pushes.append((p, d)), broker=broker)
        return acc, pushes

    def test_orders_are_sent_with_sl_tp_and_never_exceed_open_limit(self):
        http = FakeHttp()
        acc, pushes = self._run(http)
        orders = [c for c in http.calls if c[0] == "POST"]
        self.assertGreater(len(orders), 0)
        for _, url, token, body in orders:
            self.assertTrue(url.endswith("/ACC/trade") or "/ACC/trade" in url)
            self.assertEqual(token, "TOKEN")
            self.assertIn(body["actionType"], ("ORDER_TYPE_BUY", "ORDER_TYPE_SELL"))
            self.assertGreaterEqual(body["volume"], 0.01)
            self.assertLessEqual(body["volume"], 0.10)
            if body["actionType"] == "ORDER_TYPE_BUY":
                self.assertLess(body["stopLoss"], body["takeProfit"])
            else:
                self.assertGreater(body["stopLoss"], body["takeProfit"])
        snap = [d for p, d in pushes if p == "/api/paper/sync"][-1]
        self.assertEqual(snap["mode"], "DEMO")
        self.assertEqual(snap["broker_balance"], 1000.0)

    def test_no_broker_means_no_calls_and_broker_failure_does_not_crash(self):
        http = FakeHttp(fail=True)
        acc, pushes = self._run(http)  # POST raises every time; engine must survive
        sigs = [d for p, d in pushes if p == "/api/signals/push"]
        self.assertTrue(any("MT5 order FAILED" in (s["reason"] or "") for s in sigs))

    def test_blocked_paper_trade_sends_no_order(self):
        http = FakeHttp()
        df = synthetic(seed=1)
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, 1000.0)  # trading OFF
        broker = MetaApiBroker("T", "A", "demo", http=http)
        for i in range(130, len(df)):
            run_engine.process_candle(conn, acc, df[:i], push=lambda *a: None, broker=broker)
        self.assertEqual([c for c in http.calls if c[0] == "POST"], [])


if __name__ == "__main__":
    unittest.main()
