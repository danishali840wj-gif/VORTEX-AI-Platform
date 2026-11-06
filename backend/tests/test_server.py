"""End-to-end tests: start the REAL backend on a random port with a temp database and call it over HTTP."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

import server

ADMIN, ENGINE = "admin-test-token", "engine-test-token"


def call(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and r.headers.get("Content-Type", "").startswith("application/json") else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {}), e.headers


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        server.configure(os.path.join(cls.tmp, "t.db"), ADMIN, ENGINE, "secret-test-key", "danishali840", "danishali840")
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    # helpers
    def adm(self):
        return {"X-Admin-Token": ADMIN}

    def eng(self):
        return {"X-Engine-Token": ENGINE}

    _ph = [9000000000]

    def register(self, username, pw="secret123", name="T"):
        self._ph[0] += 1
        s, j, _ = call(self.base, "POST", "/api/auth/register", {"username": username, "phone": str(self._ph[0]), "password": pw, "name": name})
        self.assertEqual(s, 200, j)
        return {"Authorization": "Bearer " + j["token"]}

    def new_key(self, duration="30d"):
        s, j, _ = call(self.base, "POST", "/api/admin/keys/generate", {"duration": duration, "count": 1}, self.adm())
        self.assertEqual(s, 200, j)
        return j["keys"][0]

    # tests
    def test_health_admin_page_cors_and_404(self):
        self.assertEqual(call(self.base, "GET", "/api/health")[0], 200)
        s, raw, h = call(self.base, "GET", "/admin")
        self.assertEqual(s, 200)
        self.assertIn(b"VORTEX", raw)
        s, _, h = call(self.base, "OPTIONS", "/api/me")
        self.assertEqual(s, 204)
        self.assertEqual(h["Access-Control-Allow-Origin"], "*")
        self.assertIn("Authorization", h["Access-Control-Allow-Headers"])
        self.assertEqual(call(self.base, "GET", "/nope")[0], 404)
        self.assertEqual(call(self.base, "POST", "/api/health")[0], 405)

    def test_register_login_validation(self):
        R = lambda **b: call(self.base, "POST", "/api/auth/register", b)[0]
        self.register("user_a1")
        self.assertEqual(R(username="user_a1", phone="9111111111", password="secret123"), 409)      # username taken
        self.assertEqual(R(username="newone", phone=str(self._ph[0]), password="secret123"), 409)  # phone taken (a1's number)
        self.assertEqual(R(username="bad name!", phone="9222222222", password="secret123"), 400)
        self.assertEqual(R(username="okname", phone="12ab", password="secret123"), 400)
        self.assertEqual(R(username="okname", password="secret123"), 400)                      # phone required
        self.assertEqual(R(username="okname", phone="9333333333", password="123"), 400)
        self.assertEqual(call(self.base, "POST", "/api/auth/login", {"username": "user_a1", "password": "wrong-pass"})[0], 401)
        s, j, _ = call(self.base, "POST", "/api/auth/login", {"username": "USER_A1", "password": "secret123"})
        self.assertEqual(s, 200)
        self.assertIn("token", j)
        self.assertFalse(j["is_admin"])
        # garbage / non-object body
        req = urllib.request.Request(self.base + "/api/auth/login", data=b"not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(call(self.base, "POST", "/api/auth/login", [1, 2])[0], 400)

    def test_locked_until_key_then_active_and_key_single_use(self):
        u1 = self.register("lock")
        s, j, _ = call(self.base, "GET", "/api/me", headers=u1)
        self.assertFalse(j["subscription_active"])
        for path in ("/api/signals/latest", "/api/paper/summary", "/api/signals/history"):
            self.assertEqual(call(self.base, "GET", path, headers=u1)[0], 402, path)

        key = self.new_key("30d")
        self.assertEqual(call(self.base, "POST", "/api/subscription/activate", {"key": "VTX-WRONG"}, u1)[0], 404)
        s, j, _ = call(self.base, "POST", "/api/subscription/activate", {"key": key.lower()}, u1)  # case-insensitive
        self.assertEqual(s, 200, j)
        exp = datetime.fromisoformat(j["expires_at"])
        days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
        self.assertTrue(29.9 < days < 30.1, days)
        for path in ("/api/signals/latest", "/api/paper/summary", "/api/signals/history"):
            self.assertEqual(call(self.base, "GET", path, headers=u1)[0], 200, path)

        # key can never be reused, not even by another user
        u2 = self.register("other")
        self.assertEqual(call(self.base, "POST", "/api/subscription/activate", {"key": key}, u2)[0], 409)
        self.assertEqual(call(self.base, "POST", "/api/subscription/activate", {"key": key}, u1)[0], 409)

        # second key extends from current expiry
        key2 = self.new_key("90d")
        s, j, _ = call(self.base, "POST", "/api/subscription/activate", {"key": key2}, u1)
        days2 = (datetime.fromisoformat(j["expires_at"]) - datetime.now(timezone.utc)).total_seconds() / 86400
        self.assertTrue(119.9 < days2 < 120.1, days2)

    def test_durations_no_7_day(self):
        self.assertEqual(call(self.base, "POST", "/api/admin/keys/generate", {"duration": "7d"}, self.adm())[0], 400)
        for d, n in (("30d", 30), ("90d", 90), ("1y", 365)):
            u = self.register(f"dur_{d}")
            s, j, _ = call(self.base, "POST", "/api/subscription/activate", {"key": self.new_key(d)}, u)
            days = (datetime.fromisoformat(j["expires_at"]) - datetime.now(timezone.utc)).total_seconds() / 86400
            self.assertTrue(n - 0.1 < days < n + 0.1, (d, days))

    def test_admin_and_engine_auth_required(self):
        self.assertEqual(call(self.base, "GET", "/api/admin/users")[0], 401)
        self.assertEqual(call(self.base, "GET", "/api/admin/users", headers={"X-Admin-Token": "wrong"})[0], 401)
        self.assertEqual(call(self.base, "GET", "/api/admin/users", headers={"X-Engine-Token": ENGINE})[0], 401)
        sig = {"signal": "BUY", "price": 2000.5}
        self.assertEqual(call(self.base, "POST", "/api/signals/push", sig)[0], 401)
        self.assertEqual(call(self.base, "POST", "/api/signals/push", sig, self.adm())[0], 401)
        self.assertEqual(call(self.base, "POST", "/api/paper/sync", {}, headers={"X-Engine-Token": "x"})[0], 401)

    def test_admin_key_controls_block_deactivate_history(self):
        u = self.register("ctl")
        key = self.new_key("30d")
        call(self.base, "POST", "/api/subscription/activate", {"key": key}, u)

        # unused key can be revoked, then cannot be used
        k2 = self.new_key("30d")
        _, keys, _ = call(self.base, "GET", "/api/admin/keys", headers=self.adm())
        kid = next(k["id"] for k in keys["keys"] if k["key"] == k2)
        self.assertEqual(call(self.base, "POST", f"/api/admin/keys/{kid}/revoke", headers=self.adm())[0], 200)
        u_other = self.register("rev")
        self.assertEqual(call(self.base, "POST", "/api/subscription/activate", {"key": k2}, u_other)[0], 409)
        # used key cannot be revoked
        used_id = next(k["id"] for k in keys["keys"] if k["key"] == key)
        self.assertEqual(call(self.base, "POST", f"/api/admin/keys/{used_id}/revoke", headers=self.adm())[0], 409)
        self.assertEqual(next(k for k in keys["keys"] if k["key"] == key)["used_by_username"], "ctl")

        _, users, _ = call(self.base, "GET", "/api/admin/users", headers=self.adm())
        uid = next(x["id"] for x in users["users"] if x["username"] == "ctl")
        self.assertTrue(next(x for x in users["users"] if x["id"] == uid)["subscription_active"])

        # block -> 403 everywhere, even on login; unblock -> works again
        self.assertEqual(call(self.base, "POST", f"/api/admin/users/{uid}/block", headers=self.adm())[0], 200)
        self.assertEqual(call(self.base, "GET", "/api/me", headers=u)[0], 403)
        self.assertEqual(call(self.base, "POST", "/api/auth/login", {"username": "ctl", "password": "secret123"})[0], 403)
        self.assertEqual(call(self.base, "POST", f"/api/admin/users/{uid}/unblock", headers=self.adm())[0], 200)
        self.assertEqual(call(self.base, "GET", "/api/signals/latest", headers=u)[0], 200)

        # deactivate -> locked again
        self.assertEqual(call(self.base, "POST", f"/api/admin/users/{uid}/deactivate", headers=self.adm())[0], 200)
        self.assertEqual(call(self.base, "GET", "/api/signals/latest", headers=u)[0], 402)
        self.assertEqual(call(self.base, "POST", "/api/admin/users/999999/block", headers=self.adm())[0], 404)

        _, hist, _ = call(self.base, "GET", "/api/admin/history", headers=self.adm())
        events = [h["event"] for h in hist["history"] if h["username"] == "ctl"]
        for ev in ("activated", "blocked", "unblocked", "deactivated"):
            self.assertIn(ev, events)

    def test_signal_and_paper_flow_with_real_engine_code(self):
        u = self.register("flow")
        call(self.base, "POST", "/api/subscription/activate", {"key": self.new_key("1y")}, u)

        # bad pushes are rejected
        self.assertEqual(call(self.base, "POST", "/api/signals/push", {"signal": "HOLD", "price": 1}, self.eng())[0], 400)
        self.assertEqual(call(self.base, "POST", "/api/signals/push", {"signal": "BUY", "price": "x"}, self.eng())[0], 400)
        self.assertEqual(call(self.base, "POST", "/api/paper/sync", {"balance": 1}, self.eng())[0], 400)

        # drive the REAL engine against this REAL server
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engine"))
        import numpy as np
        import pandas as pd
        import run_engine
        from vortex.db import connect
        from vortex.paper import PaperAccount

        run_engine.BACKEND_URL, run_engine.ENGINE_TOKEN = self.base, ENGINE
        rng = np.random.default_rng(3)
        n = 400
        idx = pd.date_range("2026-01-05 00:00", periods=n, freq="5min")
        close = 2000 + np.cumsum(rng.normal(0.05, 1.2, n))
        df = pd.DataFrame({"Close": close}, index=idx)
        df["Open"] = df["Close"].shift(1).fillna(df["Close"])
        df["High"] = df[["Open", "Close"]].max(axis=1) + rng.uniform(0.1, 0.8, n)
        df["Low"] = df[["Open", "Close"]].min(axis=1) - rng.uniform(0.1, 0.8, n)
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, 1000.0)
        acc.set_trading(True)
        for i in range(130, n):
            run_engine.process_candle(conn, acc, df.iloc[:i])   # real HTTP pushes, no fake

        s, j, _ = call(self.base, "GET", "/api/signals/latest", headers=u)
        self.assertEqual(s, 200)
        self.assertIn(j["signal"]["signal"], ("BUY", "SELL", "WAIT"))
        self.assertIn("rsi14", j["signal"]["indicators"])
        s, hist, _ = call(self.base, "GET", "/api/signals/history?limit=5", headers=u)
        self.assertEqual(len(hist["signals"]), 5)

        s, p, _ = call(self.base, "GET", "/api/paper/summary", headers=u)
        self.assertEqual(s, 200)
        self.assertAlmostEqual(p["balance"], round(acc.balance, 2), places=2)
        self.assertEqual(p["stats"]["closed_count"], acc.stats()["trades"])
        self.assertIn("updated_at", p)
        # fields the Android app reads
        for t in p["open_trades"]:
            for k in ("side", "entry", "sl", "tp"):
                self.assertIn(k, t)
        for t in p["closed_trades"]:
            for k in ("side", "exit_reason", "pnl"):
                self.assertIn(k, t)


if __name__ == "__main__":
    unittest.main()


class ExtraTests(BackendTests):
    def test_admin_login_and_always_active(self):
        s, j, _ = call(self.base, "POST", "/api/admin/login", {"username": "danishali840", "password": "danishali840"})
        self.assertEqual(s, 200)
        self.assertEqual(j["admin_token"], ADMIN)
        s, j, _ = call(self.base, "POST", "/api/auth/login", {"username": "danishali840", "password": "danishali840"})
        h = {"Authorization": "Bearer " + j["token"]}
        self.assertTrue(j["is_admin"])
        self.assertTrue(call(self.base, "GET", "/api/me", headers=h)[1]["subscription_active"])
        self.assertEqual(call(self.base, "GET", "/api/signals/latest", headers=h)[0], 200)   # no key needed
        # normal user is not admin
        self.register("plainuser")
        self.assertEqual(call(self.base, "POST", "/api/admin/login", {"username": "plainuser", "password": "secret123"})[0], 403)
        # admin cannot be blocked from the panel
        users = call(self.base, "GET", "/api/admin/users", headers=self.adm())[1]["users"]
        aid = next(u["id"] for u in users if u["username"] == "danishali840")
        self.assertEqual(call(self.base, "POST", f"/api/admin/users/{aid}/block", headers=self.adm())[0], 400)

    def test_login_throttle(self):
        self.register("thr")
        for _ in range(8):
            self.assertEqual(call(self.base, "POST", "/api/auth/login", {"username": "thr", "password": "nope-nope"})[0], 401)
        self.assertEqual(call(self.base, "POST", "/api/auth/login", {"username": "thr", "password": "secret123"})[0], 429)
        server._FAILS.clear()

    def test_ai_requires_subscription_and_answers(self):
        h = self.register("aiuser")
        self.assertEqual(call(self.base, "GET", "/api/ai/analysis", headers=h)[0], 402)
        call(self.base, "POST", "/api/subscription/activate", {"key": self.new_key()}, h)
        call(self.base, "POST", "/api/signals/push", {"signal": "BUY", "price": 3900.5, "sl": 3890, "tp": 3950,
             "indicators": {"rsi14": 72, "adx14": 30, "ema20": 3899, "ema50": 3880}, "reason": "trend up"}, self.eng())
        s, j, _ = call(self.base, "GET", "/api/ai/analysis", headers=h)
        self.assertEqual(s, 200)
        self.assertIn("BUY", j["text"])
        self.assertIn("overbought", j["text"])
        s, j, _ = call(self.base, "POST", "/api/ai/chat", {"message": "risk kya hai?"}, h)
        self.assertEqual(s, 200)
        self.assertIn("Risk", j["text"])
        self.assertEqual(call(self.base, "POST", "/api/ai/chat", {"message": ""}, h)[0], 400)


class MigrationTest(unittest.TestCase):
    def test_old_email_db_is_migrated_with_backup(self):
        import sqlite3
        d = tempfile.mkdtemp()
        path = os.path.join(d, "old.db")
        c = sqlite3.connect(path)
        c.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '',
            pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0, sub_expires_at TEXT, created_at TEXT NOT NULL);
            INSERT INTO users (email, name, pw_hash, pw_salt, created_at) VALUES ('Old@x.com','Old','h','s','2026-01-01');""")
        c.commit(); c.close()
        server.configure(path, ADMIN, ENGINE, "k", "", "")
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("SELECT username, phone, is_admin FROM users").fetchall(), [("old@x.com", "", 0)])
        self.assertTrue(os.path.exists(path + ".bak-before-username"))
