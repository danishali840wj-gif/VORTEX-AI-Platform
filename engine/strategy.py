"""Signal strategy (BUY / SELL / WAIT) for XAU/USD on M5 with 15-minute confirmation.

NOTE: This is a clean baseline built from the indicator set Danish described
(EMA 20/50, RSI 14, ATR 14, ADX, 15m EMA 20/50 confirmation). The original
VORTEX V3 logic from the old ZIP must replace/merge into `decide()` once the
ZIP is provided. Do not treat this as a proven profitable strategy.
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from indicators import add_indicators

ADX_MIN = 20.0
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0


@dataclass
class Decision:
    signal: str  # BUY | SELL | WAIT
    price: float
    sl: Optional[float]
    tp: Optional[float]
    reason: str
    indicators: dict


def resample_15m(df5: pd.DataFrame) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    return df5.resample("15min").agg(agg).dropna()


def decide(df5: pd.DataFrame) -> Decision:
    """Decide on the LAST fully closed M5 candle of df5 (needs ~100+ rows)."""
    d5 = add_indicators(df5)
    d15 = add_indicators(resample_15m(df5))
    if len(d5) < 60 or len(d15) < 60:
        return Decision("WAIT", float(df5["Close"].iloc[-1]), None, None, "Not enough data", {})

    r = d5.iloc[-1]
    r15 = d15.iloc[-2] if len(d15) > 1 else d15.iloc[-1]  # last closed 15m candle
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
        return Decision("BUY", price, round(price - SL_ATR_MULT * atr_v, 2), round(price + TP_ATR_MULT * atr_v, 2),
                        "M5 and 15m uptrend, RSI 50-70, ADX trend OK", ind)
    if dn5 and dn15 and 30 <= r["rsi14"] <= 50:
        return Decision("SELL", price, round(price + SL_ATR_MULT * atr_v, 2), round(price - TP_ATR_MULT * atr_v, 2),
                        "M5 and 15m downtrend, RSI 30-50, ADX trend OK", ind)
    return Decision("WAIT", price, None, None, "Trend/RSI conditions not aligned", ind)
