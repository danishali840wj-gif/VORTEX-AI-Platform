"""Risk rules for paper trading (demo capital, no real money)."""
from dataclasses import dataclass, field

RISK_PER_TRADE = 0.01       # 1% of balance
DAILY_MAX_LOSS = 0.10       # 10% of day-start balance
DAILY_PROFIT_TARGET = 0.03  # 3% of day-start balance
COOLDOWN_AFTER_LOSSES = 3   # consecutive losses
COOLDOWN_CANDLES = 12
OUNCES_PER_LOT = 100        # XAUUSD: 1 lot = 100 oz, $1 move = $100/lot


def lot_size(balance: float, entry: float, sl: float) -> float:
    risk_usd = balance * RISK_PER_TRADE
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.0
    lots = risk_usd / (dist * OUNCES_PER_LOT)
    return round(max(lots, 0.01), 2)


def pnl_usd(side: str, entry: float, exit_price: float, lots: float) -> float:
    move = (exit_price - entry) if side == "BUY" else (entry - exit_price)
    return round(move * lots * OUNCES_PER_LOT, 2)


@dataclass
class DayState:
    day: str = ""
    start_balance: float = 0.0
    realized: float = 0.0
    consecutive_losses: int = 0
    cooldown_left: int = 0
    notes: list = field(default_factory=list)

    def roll_day(self, day: str, balance: float):
        if self.day != day:
            self.day, self.start_balance, self.realized = day, balance, 0.0
            self.consecutive_losses, self.cooldown_left = 0, 0

    def can_trade(self) -> tuple:
        if self.cooldown_left > 0:
            return False, f"cooldown {self.cooldown_left} candles left"
        if self.start_balance and self.realized <= -DAILY_MAX_LOSS * self.start_balance:
            return False, "daily max loss reached"
        if self.start_balance and self.realized >= DAILY_PROFIT_TARGET * self.start_balance:
            return False, "daily profit target reached"
        return True, ""

    def on_candle(self):
        if self.cooldown_left > 0:
            self.cooldown_left -= 1

    def on_close(self, pnl: float):
        self.realized += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= COOLDOWN_AFTER_LOSSES:
                self.cooldown_left = COOLDOWN_CANDLES
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0
