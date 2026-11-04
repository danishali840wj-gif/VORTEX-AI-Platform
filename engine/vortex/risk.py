"""VORTEX AI - risk manager.

All limits are enforced here, in one place, so no other module can open a
trade that skips them.

Interpretation of the plan's numbers (account-based, not price-based):
  * 1.7% = the most the account may lose if a trade hits its Stop Loss.
  * 5%   = the profit the account makes if the trade hits its Take Profit.
  * The strategy supplies the stop distance in price (e.g. from ATR); position
    size is then chosen so that stop distance == 1.7% of balance, and the
    take-profit sits at stop_distance * (5 / 1.7).
"""
import json
import math
from dataclasses import dataclass, asdict

# Hard ceilings: even a user-edited config cannot go beyond these.
MAX_RISK_PER_TRADE_PCT = 5.0
MAX_DAILY_LOSS_LIMIT_PCT = 20.0


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.7
    reward_per_trade_pct: float = 5.0
    daily_loss_limit_pct: float = 5.0
    max_open_trades: int = 2
    max_leverage: float = 20.0
    unit_step: float = 0.01      # smallest tradable size (ounces for XAU/USD)
    min_units: float = 0.01

    def __post_init__(self):
        if not 0 < self.risk_per_trade_pct <= MAX_RISK_PER_TRADE_PCT:
            raise ValueError(
                f"risk_per_trade_pct must be in (0, {MAX_RISK_PER_TRADE_PCT}]")
        if self.reward_per_trade_pct <= 0:
            raise ValueError("reward_per_trade_pct must be > 0")
        if not 0 < self.daily_loss_limit_pct <= MAX_DAILY_LOSS_LIMIT_PCT:
            raise ValueError(
                f"daily_loss_limit_pct must be in (0, {MAX_DAILY_LOSS_LIMIT_PCT}]")
        if self.max_open_trades < 1:
            raise ValueError("max_open_trades must be >= 1")
        if self.max_leverage <= 0:
            raise ValueError("max_leverage must be > 0")
        if self.unit_step <= 0 or self.min_units <= 0:
            raise ValueError("unit_step and min_units must be > 0")

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "RiskConfig":
        return cls(**json.loads(text))


class RiskManager:
    def __init__(self, cfg: RiskConfig | None = None):
        self.cfg = cfg or RiskConfig()

    @property
    def reward_to_risk(self) -> float:
        return self.cfg.reward_per_trade_pct / self.cfg.risk_per_trade_pct

    def levels(self, side: str, entry: float, sl_distance: float):
        """Return (sl_price, tp_price) for the given stop distance."""
        if sl_distance <= 0:
            raise ValueError("sl_distance must be > 0")
        tp_distance = sl_distance * self.reward_to_risk
        if side == "BUY":
            return entry - sl_distance, entry + tp_distance
        if side == "SELL":
            return entry + sl_distance, entry - tp_distance
        raise ValueError("side must be BUY or SELL")

    def position_size(self, balance: float, entry: float, sl_distance: float):
        """Return (units, risk_amount).

        Size is rounded DOWN, and capped by max leverage, so the real risk is
        never above risk_per_trade_pct. Returns (0.0, 0.0) if too small.
        """
        if balance <= 0 or entry <= 0 or sl_distance <= 0:
            return 0.0, 0.0
        units = (balance * self.cfg.risk_per_trade_pct / 100.0) / sl_distance
        units = min(units, balance * self.cfg.max_leverage / entry)
        step = self.cfg.unit_step
        units = round(math.floor(units / step + 1e-9) * step, 8)
        if units < self.cfg.min_units:
            return 0.0, 0.0
        return units, units * sl_distance

    def daily_limit_hit(self, day_start_balance: float, today_pnl: float) -> bool:
        limit = day_start_balance * self.cfg.daily_loss_limit_pct / 100.0
        return today_pnl <= -limit

    def check_can_open(self, *, emergency_stopped: bool, trading_enabled: bool,
                       open_count: int, day_start_balance: float,
                       today_pnl: float):
        """Return (allowed, reason). Every new trade must pass this."""
        if emergency_stopped:
            return False, "emergency stop is active"
        if not trading_enabled:
            return False, "trading is OFF"
        if open_count >= self.cfg.max_open_trades:
            return False, "max open trades reached"
        if self.daily_limit_hit(day_start_balance, today_pnl):
            return False, "daily loss limit reached"
        return True, "ok"
