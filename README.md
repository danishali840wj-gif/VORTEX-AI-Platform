# VORTEX AI Platform

AI-based gold (XAU/USD) analysis and **paper-trading** platform with an Android app and a manual subscription-key system.

> Paper trading only. No broker connection, no real-money trading. Past/backtest results do not guarantee future profit.

## Structure

| Folder | What it is |
|---|---|
| `backend/` | FastAPI + SQLite: register/login, subscription keys, admin API, signals, paper trades |
| `backend/static/admin.html` | Admin panel (open `/admin`): generate keys (30d/90d/1y), users, block/unblock, deactivate, history |
| `engine/` | AI engine: `vortex/` (risk manager, paper account, DB - from your Stage 1 ZIP, unchanged), `indicators.py` + `strategy.py` (BUY/SELL/WAIT), `run_engine.py` (runner) |
| `app/` | Android app (Capacitor wrapper around `app/www`) |
| `.github/workflows/build-apk.yml` | Builds the APK in GitHub Actions |

## Subscription flow (no payment gateway)

1. Customer messages admin -> pays via UPI manually.
2. Admin opens `/admin`, generates a key (30d / 90d / 1y).
3. Customer enters the key in the app -> backend verifies, binds key to the account, sets expiry.
4. A key works once only. Admin can revoke unused keys, block users, deactivate subscriptions.

## Run locally

```bash
cp .env.example .env        # then edit secrets
cd backend && pip install -r requirements.txt
export $(grep -v '^#' ../.env | xargs)
uvicorn main:app --host 0.0.0.0 --port 8000
```

Admin panel: `http://localhost:8000/admin` (enter your `ADMIN_TOKEN`).

Engine (second terminal):

```bash
cd engine && pip install -r requirements.txt
export $(grep -v '^#' ../.env | xargs)
python run_engine.py
```

## Build the APK

1. Set your backend URL in `app/www/config.js` (`API_BASE`).
2. Push to GitHub -> Actions -> **Build VORTEX AI APK** -> download `VORTEX-AI-debug-apk`.

## Status

- `engine/vortex/` is your Stage 1 code (risk 1.7% / reward 5% / daily loss limit 5% / max 2 open trades / 20x leverage cap / emergency stop). Its 15 original tests pass.
- The Stage 1 ZIP had no strategy, so `engine/strategy.py` is a clean baseline (EMA/RSI/ADX/ATR + 15m confirmation). The original VORTEX V3 signal logic is still to be merged in.
- Run engine tests: `cd engine && python -m unittest discover -s tests -t .`
- Never commit `.env`, API keys or passwords.
