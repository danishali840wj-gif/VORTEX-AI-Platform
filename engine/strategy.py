"""Signal strategy (BUY / SELL / WAIT) for XAU/USD on M5 with 15-minute confirmation.

NOTE: This is a clean baseline built from the indicator set Danish described
(EMA 20/50, RSI 14, ATR 14, ADX, 15m EMA 20/50 confirmation). The ZIP
(vortex_stage1) had only risk/paper/db code, no strategy, so the original VORTEX V3
signal logic is still missing here. Do not treat this as a proven profitable strategy.
"""
from dataclasses import dataclass
from typing import Optional

from indicators import add_indicators

ADX_MIN = 20.0
SL_ATR_MULT = 1.0  # stop distance = 1 x ATR; TP/size are set by vortex.risk.RiskManager


@dataclass
class Decision:
    signal: str  # BUY | SELL | WAIT
    price: float
    sl: Optional[float]
    tp: Optional[float]
    reason: str
    indicators: dict
    sl_distance: Optional[float] = None  # price distance of the stop; RiskManager turns it into SL/TP/size


def resample_15m(candles: list) -> list:
    """Group M5 candles into 15-minute candles (UTC, aligned to :00/:15/:30/:45)."""
    out, cur_bin = [], None
    for c in candles:
        b = int(c["t"]) // 900 * 900
        if b != cur_bin:
            out.append({"t": b, "Open": c["Open"], "High": c["High"], "Low": c["Low"], "Close": c["Close"]})
            cur_bin = b
        else:
            o = out[-1]
            o["High"] = max(o["High"], c["High"])
            o["Low"] = min(o["Low"], c["Low"])
            o["Close"] = c["Close"]
    return out


def decide(df5: list) -> Decision:
    """Decide on the LAST fully closed M5 candle of df5 (needs ~100+ rows)."""
    d5 = add_indicators(df5)
    d15 = add_indicators(resample_15m(df5))
    if len(d5) < 60 or len(d15) < 60:
        return Decision("WAIT", float(df5[-1]["Close"]), None, None, "Not enough data", {})

    r = d5[-1]
    r15 = d15[-2] if len(d15) > 1 else d15[-1]  # last closed 15m candle
    price = float(r["Close"])
    ind = {
        "ema20": round(float(r["ema20"]), 2),
        "ema50": round(float(r["ema50"]), 2),
        "rsi14": round(float(r["rsi14"]), 2),
        "atr14": round(float(r["atr14"]), 2),
        "adx14": round(float(r["adx14"]), 2),
        "ema20_15m": round(float(r15["ema20"]), 2),
        "ema50_15m": round(float(r15["ema50"]), 2),
    }
    atr_v = float(r["atr14"])

    if r["adx14"] < ADX_MIN:
        return Decision("WAIT", price, None, None, f"ADX {ind['adx14']} below {ADX_MIN} (no trend)", ind)

    up5 = r["ema20"] > r["ema50"] and price > r["ema20"]
    up15 = r15["ema20"] > r15["ema50"]
    dn5 = r["ema20"] < r["ema50"] and price < r["ema20"]
    dn15 = r15["ema20"] < r15["ema50"]

    if up5 and up15 and 50 <= r["rsi14"] <= 70:
        return Decision("BUY", price, None, None, "M5 and 15m uptrend, RSI 50-70, ADX trend OK", ind,
                        sl_distance=round(SL_ATR_MULT * atr_v, 2))
    if dn5 and dn15 and 30 <= r["rsi14"] <= 50:
        return Decision("SELL", price, None, None, "M5 and 15m downtrend, RSI 30-50, ADX trend OK", ind,
                        sl_distance=round(SL_ATR_MULT * atr_v, 2))
    return Decision("WAIT", price, None, None, "Trend/RSI conditions not aligned", ind)
