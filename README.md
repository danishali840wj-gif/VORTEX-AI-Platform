# VORTEX AI Platform

AI-based gold (XAU/USD) analysis and **paper-trading** platform with an Android app and a manual subscription-key system.

> Paper trading only. No broker connection, no real-money trading. Past/backtest results do not guarantee future profit.

## Structure

| Folder | What it is |
|---|---|
| `backend/` | Pure-Python server (`server.py`) + SQLite: register/login, subscription keys, admin API, signals, paper trades |
| `backend/static/admin.html` | Admin panel (open `/admin`): generate keys (30d/90d/1y), users, block/unblock, deactivate, history |
| `engine/` | AI engine: `vortex/` (risk manager, paper account, DB - from your Stage 1 ZIP, unchanged), `indicators.py` + `strategy.py` (BUY/SELL/WAIT), `run_engine.py` (runner) |
| `app/` | Android app (Capacitor wrapper around `app/www`) |
| `.github/workflows/build-apk.yml` | Builds the APK in GitHub Actions |

## Subscription flow (no payment gateway)

1. Customer messages admin -> pays via UPI manually.
2. Admin opens `/admin`, generates a key (30d / 90d / 1y).
3. Customer enters the key in the app -> backend verifies, binds key to the account, sets expiry.
4. A key works once only. Admin can revoke unused keys, block users, deactivate subscriptions.

## Run (Termux or Linux) - one command

```bash
git clone https://github.com/danishali840wj-gif/VORTEX-AI-Platform
cd VORTEX-AI-Platform
bash run.sh
```

`run.sh` creates `.env` with random secrets (first time only), starts the backend and the engine, and prints your **admin token**.
Stop everything: `bash stop.sh`.

- Admin panel: `http://127.0.0.1:8000/admin` (paste the admin token).
- Backend is pure Python standard library (no pip needed). Engine is pure Python too (no pandas/numpy).
- Logs: `logs/backend.log`, `logs/engine.log`.

## Build the APK

Every push to `main` that touches `app/` builds the APK in GitHub Actions and publishes it under **Releases** (`VORTEX-AI.apk`).
The app talks to `http://127.0.0.1:8000` by default (backend on the same phone). To use another server: Login screen -> **Server: change URL**.

## Status

- `engine/vortex/` is your Stage 1 code (risk 1.7% / reward 5% / daily loss limit 5% / max 2 open trades / 20x leverage cap / emergency stop). Its 15 original tests pass.
- The Stage 1 ZIP had no strategy, so `engine/strategy.py` is a clean baseline (EMA/RSI/ADX/ATR + 15m confirmation). The original VORTEX V3 signal logic is still to be merged in.
- Tests: `cd engine && python -m unittest discover -s tests -t .` and `cd backend && python -m unittest discover -s tests -t .`
- Never commit `.env`, API keys or passwords.

## Real MT5 trading (MetaApi)
Default is OFF (virtual trades). To trade a real MT5 account: create a MetaApi account, add your MT5 account there,
put `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` and `MT5_MODE=demo` in `.env`, restart with `bash run.sh`.
Use a demo account first. `MT5_MODE=live` additionally needs `MT5_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`.
Every order passes the same risk rules (1.7% risk, max 2 open, 5% daily loss limit, emergency stop) and is sent with SL and TP.
