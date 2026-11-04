"""VORTEX AI - paper trading engine (virtual money, real rules).

Every trade goes through RiskManager.check_can_open(), so the same checks
will apply unchanged when a live broker is plugged in later.
"""
import time

from .risk import RiskConfig, RiskManager

DAY = 86400  # trading day = UTC day


def _day_start(ts: int) -> int:
    return ts - ts % DAY


class PaperAccount:
    def __init__(self, conn, account_id: int):
        self.conn = conn
        self.id = account_id
        row = self._account()
        if row is None:
            raise KeyError(f"account {account_id} not found")
        self.risk = RiskManager(RiskConfig.from_json(row["risk_json"]))

    # ---------- creation ----------
    @classmethod
    def create(cls, conn, start_balance: float = 10_000.0, user_id=None,
               risk: RiskConfig | None = None, ts: int | None = None):
        if start_balance <= 0:
            raise ValueError("start_balance must be > 0")
        risk = risk or RiskConfig()
        ts = ts or int(time.time())
        cur = conn.execute(
            "INSERT INTO accounts (user_id, mode, balance, start_balance, "
            "risk_json, created_at) VALUES (?, 'paper', ?, ?, ?, ?)",
            (user_id, start_balance, start_balance, risk.to_json(), ts))
        conn.commit()
        return cls(conn, cur.lastrowid)

    # ---------- state helpers ----------
    def _account(self):
        return self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (self.id,)).fetchone()

    @property
    def balance(self) -> float:
        return self._account()["balance"]

    def open_trades(self):
        return self.conn.execute(
            "SELECT * FROM trades WHERE account_id = ? AND status = 'open' "
            "ORDER BY id", (self.id,)).fetchall()

    def _log(self, kind: str, detail: str, ts: int):
        self.conn.execute(
            "INSERT INTO events (account_id, ts, kind, detail) VALUES (?,?,?,?)",
            (self.id, ts, kind, detail))

    def _realized_today(self, ts: int) -> float:
        start = _day_start(ts)
        return self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE account_id = ? "
            "AND status = 'closed' AND closed_at >= ? AND closed_at < ?",
            (self.id, start, start + DAY)).fetchone()[0]

    @staticmethod
    def _pnl(trade, price: float) -> float:
        diff = price - trade["entry_price"]
        if trade["side"] == "SELL":
            diff = -diff
        return diff * trade["units"]

    def floating_pnl(self, price: float) -> float:
        return sum(self._pnl(t, price) for t in self.open_trades())

    def today_pnl(self, price: float, ts: int) -> float:
        return self._realized_today(ts) + self.floating_pnl(price)

    # ---------- controls ----------
    def set_trading(self, enabled: bool, ts: int | None = None) -> bool:
        """Manual trading ON/OFF. Cannot switch ON while emergency stop is active."""
        ts = ts or int(time.time())
        if enabled and self._account()["emergency_stopped"]:
            return False
        self.conn.execute("UPDATE accounts SET trading_enabled = ? WHERE id = ?",
                          (1 if enabled else 0, self.id))
        self._log("trading_on" if enabled else "trading_off", "", ts)
        self.conn.commit()
        return True

    def emergency_stop(self, price: float, ts: int | None = None,
                       reason: str = "manual"):
        """Close everything at `price`, switch trading OFF and lock it."""
        ts = ts or int(time.time())
        closed = [self._close(t, price, "emergency_stop", ts)
                  for t in self.open_trades()]
        self.conn.execute(
            "UPDATE accounts SET emergency_stopped = 1, trading_enabled = 0 "
            "WHERE id = ?", (self.id,))
        self._log("emergency_stop", reason, ts)
        self.conn.commit()
        return closed

    def reset_emergency(self, ts: int | None = None):
        """Unlock after an emergency stop. Trading stays OFF until set_trading(True)."""
        ts = ts or int(time.time())
        self.conn.execute(
            "UPDATE accounts SET emergency_stopped = 0 WHERE id = ?", (self.id,))
        self._log("emergency_reset", "", ts)
        self.conn.commit()

    # ---------- trading ----------
    def open_trade(self, side: str, price: float, sl_distance: float,
                   ts: int | None = None, symbol: str = "XAU/USD"):
        """Try to open a trade. Returns (trade_id or None, reason)."""
        ts = ts or int(time.time())
        side = side.upper()
        if side not in ("BUY", "SELL"):
            return None, "side must be BUY or SELL"
        if price <= 0 or sl_distance <= 0:
            return None, "price and sl_distance must be > 0"

        acc = self._account()
        realized = self._realized_today(ts)
        ok, reason = self.risk.check_can_open(
            emergency_stopped=bool(acc["emergency_stopped"]),
            trading_enabled=bool(acc["trading_enabled"]),
            open_count=len(self.open_trades()),
            day_start_balance=acc["balance"] - realized,
            today_pnl=realized + self.floating_pnl(price))
        if not ok:
            self._log("trade_blocked", reason, ts)
            self.conn.commit()
            return None, reason

        units, risk_amount = self.risk.position_size(
            acc["balance"], price, sl_distance)
        if units <= 0:
            self._log("trade_blocked", "size too small", ts)
            self.conn.commit()
            return None, "position size too small (stop too wide for balance)"

        sl, tp = self.risk.levels(side, price, sl_distance)
        cur = self.conn.execute(
            "INSERT INTO trades (account_id, symbol, side, units, entry_price, "
            "sl_price, tp_price, risk_amount, opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.id, symbol, side, units, price, sl, tp, risk_amount, ts))
        self._log("trade_opened", f"{side} {units} @ {price}", ts)
        self.conn.commit()
        return cur.lastrowid, "opened"

    def _close(self, trade, exit_price: float, reason: str, ts: int) -> int:
        pnl = self._pnl(trade, exit_price)
        self.conn.execute(
            "UPDATE trades SET status = 'closed', closed_at = ?, exit_price = ?, "
            "pnl = ?, close_reason = ? WHERE id = ?",
            (ts, exit_price, pnl, reason, trade["id"]))
        self.conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE id = ?",
            (pnl, self.id))
        self._log("trade_closed", f"#{trade['id']} {reason} pnl={pnl:.2f}", ts)
        return trade["id"]

    def on_candle(self, high: float, low: float, close: float,
                  ts: int | None = None):
        """Feed one finished candle. Closes trades whose SL/TP was touched.

        If one candle touches both SL and TP we cannot know which came first,
        so we assume the SL (the pessimistic case).
        Also enforces the daily loss limit: if today's loss (realized +
        floating at `close`) reaches the limit, all open trades are closed.
        Returns the list of closed trade ids.
        """
        ts = ts or int(time.time())
        closed = []
        for t in self.open_trades():
            if t["side"] == "BUY":
                sl_hit, tp_hit = low <= t["sl_price"], high >= t["tp_price"]
            else:
                sl_hit, tp_hit = high >= t["sl_price"], low <= t["tp_price"]
            if sl_hit:
                closed.append(self._close(t, t["sl_price"], "stop_loss", ts))
            elif tp_hit:
                closed.append(self._close(t, t["tp_price"], "take_profit", ts))

        remaining = self.open_trades()
        if remaining:
            realized = self._realized_today(ts)
            day_start_balance = self.balance - realized
            floating = sum(self._pnl(t, close) for t in remaining)
            if self.risk.daily_limit_hit(day_start_balance, realized + floating):
                for t in remaining:
                    closed.append(self._close(t, close, "daily_limit", ts))
                self._log("daily_limit", "all trades closed", ts)
        self.conn.commit()
        return closed

    # ---------- reporting ----------
    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT pnl FROM trades WHERE account_id = ? AND status = 'closed' "
            "ORDER BY closed_at, id", (self.id,)).fetchall()
        pnls = [r["pnl"] for r in rows]
        start = self._account()["start_balance"]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        equity = peak = start
        max_dd = 0.0
        for p in pnls:
            equity += p
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)

        return {
            "balance": self.balance,
            "trades": len(pnls),
            "win_rate_pct": (len(wins) / len(pnls) * 100.0) if pnls else 0.0,
            "total_pnl": sum(pnls),
            "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
            "max_drawdown_pct": max_dd,
        }
