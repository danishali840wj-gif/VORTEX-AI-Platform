"""MT5 broker bridge via MetaApi (cloud REST). Places the engine's signals as REAL orders on an MT5 account.

Safety:
  * MT5_MODE = off (default) | demo | live.  off -> nothing is ever sent to a broker.
  * live additionally needs MT5_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK, otherwise it stays off.
  * The paper account's risk rules (max open trades, daily loss limit, emergency stop, trading ON/OFF)
    still gate every order; this module only mirrors a trade that already passed them.
  * Lot size comes from the REAL account balance (1.7% risk), capped by MT5_MAX_LOT.
  * SL and TP are sent WITH the order, so the broker closes the trade even if this engine is offline.
Env: METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION (default new-york), MT5_SYMBOL (default XAUUSD), MT5_MAX_LOT (default 0.10)
"""
import json
import math
import os
import urllib.request

LIVE_CONFIRM = "I_ACCEPT_REAL_MONEY_RISK"
OUNCES_PER_LOT = 100.0  # XAUUSD: 1 standard lot = 100 oz


def _urlopen_json(method, url, token, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"auth-token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class MetaApiBroker:
    def __init__(self, token, account_id, mode, region="new-york", symbol="XAUUSD", max_lot=0.10, http=_urlopen_json):
        self.token, self.account_id, self.mode = token, account_id, mode
        self.symbol, self.max_lot, self._http = symbol, max_lot, http
        self.base = f"https://mt-client-api-v1.{region}.agiliumtrade.ai/users/current/accounts/{account_id}"

    @classmethod
    def from_env(cls, env=None, http=_urlopen_json):
        """Return a broker, or None when MT5 trading is switched off / not configured."""
        env = os.environ if env is None else env
        mode = env.get("MT5_MODE", "off").strip().lower()
        if mode not in ("demo", "live"):
            return None
        if mode == "live" and env.get("MT5_LIVE_CONFIRM") != LIVE_CONFIRM:
            print("MT5_MODE=live ignored: MT5_LIVE_CONFIRM is not set -> staying OFF")
            return None
        token, acc = env.get("METAAPI_TOKEN", ""), env.get("METAAPI_ACCOUNT_ID", "")
        if not token or not acc:
            print("MT5 mode is on but METAAPI_TOKEN / METAAPI_ACCOUNT_ID are missing -> staying OFF")
            return None
        return cls(token, acc, mode, env.get("METAAPI_REGION", "new-york"), env.get("MT5_SYMBOL", "XAUUSD"),
                   float(env.get("MT5_MAX_LOT", "0.10")), http)

    def account_info(self):
        return self._http("GET", self.base + "/account-information", self.token)

    def sizing_balance(self, info):
        """Balance in USD for risk sizing (cent accounts report US cents)."""
        bal = float(info["balance"])
        return bal / 100.0 if str(info.get("currency", "")).upper() in ("USC", "EUC", "GBC") else bal

    def lots_for(self, risk, info, price, sl_distance):
        """Lot size from the real balance via the same RiskManager used for paper trades; 0.0 = too small."""
        units, _ = risk.position_size(self.sizing_balance(info), price, sl_distance)
        lots = math.floor(units / OUNCES_PER_LOT / 0.01 + 1e-9) * 0.01
        return round(min(lots, self.max_lot), 2)

    def place(self, side, lots, sl, tp, comment="VORTEX"):
        body = {"actionType": "ORDER_TYPE_BUY" if side == "BUY" else "ORDER_TYPE_SELL", "symbol": self.symbol,
                "volume": lots, "stopLoss": round(sl, 2), "takeProfit": round(tp, 2), "comment": comment}
        return self._http("POST", self.base + "/trade", self.token, body)
