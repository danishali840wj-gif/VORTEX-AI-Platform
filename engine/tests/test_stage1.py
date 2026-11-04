import unittest

from vortex.db import connect
from vortex.paper import PaperAccount
from vortex.risk import RiskConfig, RiskManager

T0 = 1_800_000_000 - (1_800_000_000 % 86400)  # a UTC midnight
PRICE = 4000.0


def new_account(**risk_kwargs):
    conn = connect(":memory:")
    acc = PaperAccount.create(conn, 10_000.0, risk=RiskConfig(**risk_kwargs), ts=T0)
    acc.set_trading(True, ts=T0)
    return acc


class RiskMathTests(unittest.TestCase):
    def test_position_size_risks_exactly_1_7_percent(self):
        units, risk = RiskManager().position_size(10_000, PRICE, 10)
        self.assertAlmostEqual(units, 17.0)
        self.assertAlmostEqual(risk, 170.0)  # 1.7% of 10,000

    def test_take_profit_is_5_percent_of_account(self):
        rm = RiskManager()
        units, _ = rm.position_size(10_000, PRICE, 10)
        sl, tp = rm.levels("BUY", PRICE, 10)
        self.assertAlmostEqual(PRICE - sl, 10)
        self.assertAlmostEqual((tp - PRICE) * units, 500.0)  # 5% of 10,000

    def test_leverage_cap_lowers_size_never_raises_risk(self):
        units, risk = RiskManager().position_size(10_000, PRICE, 1)
        self.assertAlmostEqual(units, 50.0)  # 10,000 * 20 / 4000
        self.assertLess(risk, 170.0)

    def test_too_wide_stop_gives_zero_size(self):
        units, _ = RiskManager().position_size(10, PRICE, 5000)
        self.assertEqual(units, 0.0)

    def test_config_rejects_dangerous_values(self):
        with self.assertRaises(ValueError):
            RiskConfig(risk_per_trade_pct=10)
        with self.assertRaises(ValueError):
            RiskConfig(daily_loss_limit_pct=50)
        with self.assertRaises(ValueError):
            RiskConfig(max_open_trades=0)


class TradingTests(unittest.TestCase):
    def test_trading_is_off_by_default(self):
        conn = connect(":memory:")
        acc = PaperAccount.create(conn, 10_000.0, ts=T0)
        tid, reason = acc.open_trade("BUY", PRICE, 10, ts=T0)
        self.assertIsNone(tid)
        self.assertEqual(reason, "trading is OFF")

    def test_buy_hits_stop_loss(self):
        acc = new_account()
        tid, _ = acc.open_trade("BUY", PRICE, 10, ts=T0)
        closed = acc.on_candle(high=4005, low=3989, close=3992, ts=T0 + 300)
        self.assertEqual(closed, [tid])
        self.assertAlmostEqual(acc.balance, 9830.0)  # -1.7%

    def test_buy_hits_take_profit(self):
        acc = new_account()
        acc.open_trade("BUY", PRICE, 10, ts=T0)
        acc.on_candle(high=4040, low=3995, close=4038, ts=T0 + 300)
        self.assertAlmostEqual(acc.balance, 10_500.0)  # +5%

    def test_sell_side_works(self):
        acc = new_account()
        acc.open_trade("SELL", PRICE, 10, ts=T0)
        acc.on_candle(high=4004, low=3960, close=3965, ts=T0 + 300)
        self.assertAlmostEqual(acc.balance, 10_500.0)

    def test_sl_and_tp_in_same_candle_counts_as_loss(self):
        acc = new_account()
        acc.open_trade("BUY", PRICE, 10, ts=T0)
        acc.on_candle(high=4050, low=3980, close=4000, ts=T0 + 300)
        self.assertAlmostEqual(acc.balance, 9830.0)

    def test_max_open_trades_enforced(self):
        acc = new_account(max_open_trades=1)
        self.assertIsNotNone(acc.open_trade("BUY", PRICE, 10, ts=T0)[0])
        tid, reason = acc.open_trade("BUY", PRICE, 10, ts=T0)
        self.assertIsNone(tid)
        self.assertEqual(reason, "max open trades reached")

    def test_daily_loss_limit_blocks_then_resets_next_day(self):
        acc = new_account(daily_loss_limit_pct=3.0)
        for i in range(2):  # two losses = about -3.37%
            acc.open_trade("BUY", PRICE, 10, ts=T0 + i * 600)
            acc.on_candle(4001, 3980, 3985, ts=T0 + i * 600 + 300)
        tid, reason = acc.open_trade("BUY", PRICE, 10, ts=T0 + 1500)
        self.assertIsNone(tid)
        self.assertEqual(reason, "daily loss limit reached")
        tid, _ = acc.open_trade("BUY", PRICE, 10, ts=T0 + 86400 + 60)
        self.assertIsNotNone(tid)

    def test_daily_limit_force_closes_open_trade(self):
        acc = new_account(daily_loss_limit_pct=1.0)
        acc.open_trade("BUY", PRICE, 10, ts=T0)  # 17 units
        closed = acc.on_candle(high=4000, low=3994, close=3994, ts=T0 + 300)
        self.assertEqual(len(closed), 1)  # floating -102 > 1% limit of 100
        self.assertEqual(acc.open_trades(), [])
        self.assertAlmostEqual(acc.balance, 10_000 - 17 * 6)

    def test_emergency_stop_closes_all_and_locks(self):
        acc = new_account()
        acc.open_trade("BUY", PRICE, 10, ts=T0)
        acc.open_trade("SELL", PRICE, 10, ts=T0)
        closed = acc.emergency_stop(price=4002, ts=T0 + 60)
        self.assertEqual(len(closed), 2)
        self.assertEqual(acc.open_trades(), [])
        self.assertFalse(acc.set_trading(True, ts=T0 + 70))  # still locked
        self.assertEqual(acc.open_trade("BUY", PRICE, 10, ts=T0 + 80)[1],
                         "emergency stop is active")
        acc.reset_emergency(ts=T0 + 90)
        self.assertTrue(acc.set_trading(True, ts=T0 + 91))
        self.assertIsNotNone(acc.open_trade("BUY", PRICE, 10, ts=T0 + 100)[0])

    def test_stats(self):
        acc = new_account()
        acc.open_trade("BUY", PRICE, 10, ts=T0)
        acc.on_candle(4040, 3995, 4038, ts=T0 + 300)   # win +500
        acc.open_trade("BUY", PRICE, 10, ts=T0 + 600)
        acc.on_candle(4001, 3980, 3985, ts=T0 + 900)   # loss
        s = acc.stats()
        self.assertEqual(s["trades"], 2)
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertGreater(s["profit_factor"], 1)
        self.assertGreater(s["max_drawdown_pct"], 0)


if __name__ == "__main__":
    unittest.main()
