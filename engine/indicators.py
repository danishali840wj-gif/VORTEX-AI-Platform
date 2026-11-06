"""Indicators: EMA, RSI(14), ATR(14), ADX(14) using Wilder smoothing where standard.

Pure Python (no pandas/numpy needed - runs on plain Termux). Candles are a list of dicts:
    {"t": unix_seconds, "Open": float, "High": float, "Low": float, "Close": float}
Every function returns a list of floats with the same length as the input.
Math is identical to the earlier pandas version (ewm adjust=False).
"""


def _ewm(values, alpha):
    """Exponential moving average like pandas ewm(adjust=False); None = missing value (skipped)."""
    out, prev = [], None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else prev + alpha * (v - prev)
        out.append(prev)
    return out


def ema(values, period):
    return _ewm(values, 2.0 / (period + 1))


def _wilder(values, period):
    return _ewm(values, 1.0 / period)


def rsi(close, period=14):
    gain, loss = [None], [None]
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        gain.append(max(d, 0.0))
        loss.append(max(-d, 0.0))
    ag, al = _wilder(gain, period), _wilder(loss, period)
    out = []
    for g, l in zip(ag, al):
        if g is None or l is None or l == 0:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + g / l))
    return out


def true_range(candles):
    out = []
    for i, c in enumerate(candles):
        tr = c["High"] - c["Low"]
        if i > 0:
            pc = candles[i - 1]["Close"]
            tr = max(tr, abs(c["High"] - pc), abs(c["Low"] - pc))
        out.append(tr)
    return out


def atr(candles, period=14):
    return _wilder(true_range(candles), period)


def adx(candles, period=14):
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(candles)):
        up = candles[i]["High"] - candles[i - 1]["High"]
        down = -(candles[i]["Low"] - candles[i - 1]["Low"])
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    tr_s = _wilder(true_range(candles), period)
    p_s, m_s = _wilder(plus_dm, period), _wilder(minus_dm, period)
    dx = []
    for t, p, m in zip(tr_s, p_s, m_s):
        pdi = 100 * p / t if t else float("nan")
        mdi = 100 * m / t if t else float("nan")
        s = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / s if s and s == s else 0.0)
    return _wilder(dx, period)


def add_indicators(candles):
    """Return a list of dicts: the candle plus ema20, ema50, rsi14, atr14, adx14."""
    close = [c["Close"] for c in candles]
    e20, e50, r, a, x = ema(close, 20), ema(close, 50), rsi(close, 14), atr(candles, 14), adx(candles, 14)
    return [dict(c, ema20=e20[i], ema50=e50[i], rsi14=r[i], atr14=a[i], adx14=x[i]) for i, c in enumerate(candles)]
