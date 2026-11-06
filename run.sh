#!/usr/bin/env bash
# VORTEX AI - one command setup + start (works on Termux and Linux).
#   bash run.sh        -> installs what is missing, creates .env (once), starts backend + engine
#   bash stop.sh       -> stops both
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
mkdir -p logs data

echo "== 1/4 Checking Python packages"
HAVE_PD=0
python -c "import numpy, pandas" 2>/dev/null && HAVE_PD=1
if [ "$HAVE_PD" = 0 ] && command -v pkg >/dev/null 2>&1; then   # Termux: prebuilt packages (no slow compile)
  pkg install -y python >/dev/null 2>&1 || true
  pkg install -y python-numpy 2>/dev/null || true
  pkg install -y tur-repo 2>/dev/null && pkg install -y python-pandas 2>/dev/null || true
  python -c "import numpy, pandas" 2>/dev/null && HAVE_PD=1
fi
if [ "$HAVE_PD" = 0 ] && ! command -v pkg >/dev/null 2>&1; then
  python -m pip install pandas numpy && HAVE_PD=1 || true
fi
if [ "$HAVE_PD" = 0 ]; then
  echo "   WARNING: pandas/numpy not available - backend (login/register/keys) will run, AI signal engine will be skipped"
fi

echo "== 2/4 Secrets (.env)"
if [ ! -f .env ]; then
  python - <<'PY'
import secrets
open(".env", "w").write(
    "ADMIN_TOKEN=%s\nENGINE_TOKEN=%s\nSECRET_KEY=%s\nDB_PATH=%s/data/vortex.db\nVORTEX_ENGINE_DB=%s/data/engine.db\n"
    "BACKEND_URL=http://127.0.0.1:8000\nPORT=8000\nVORTEX_START_BALANCE=1000\n"
    % (secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(32),
       __import__("os").getcwd(), __import__("os").getcwd()))
PY
  chmod 600 .env
  echo "   created new .env with random secrets"
else
  echo "   .env already exists - kept as is"
fi
# admin login for the app/admin panel (stored only in .env, which is never pushed to GitHub)
grep -q '^ADMIN_USER=.\+' .env || echo "   NOTE: ADMIN_USER / ADMIN_PASS not set in .env - admin login will not exist"
set -a; . ./.env; set +a

echo "== 3/4 Stopping old processes (if any)"
bash "$ROOT/stop.sh" quiet || true

command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true

echo "== 4/4 Starting backend + engine"
nohup python backend/server.py > logs/backend.log 2>&1 < /dev/null &
echo $! > data/backend.pid
sleep 2
if ! python - <<'PY'
import os, urllib.request, sys
try:
    urllib.request.urlopen("http://127.0.0.1:%s/api/health" % os.environ.get("PORT", "8000"), timeout=5)
except Exception as e:
    print("backend not reachable:", e); sys.exit(1)
PY
then
  echo "ERROR: backend did not start. Last log lines:"; tail -n 15 logs/backend.log; exit 1
fi
if [ "$HAVE_PD" = 1 ]; then
  cd engine
  nohup python run_engine.py > ../logs/engine.log 2>&1 < /dev/null &
  echo $! > ../data/engine.pid
  cd ..
else
  echo "   (engine skipped: no pandas)"
fi

echo
echo "=============================================="
echo " VORTEX is running"
echo " Admin panel : http://127.0.0.1:${PORT:-8000}/admin"
echo " Admin login : username $ADMIN_USER (password is in .env)"
echo " Logs        : logs/backend.log , logs/engine.log"
echo " Stop        : bash stop.sh"
echo "=============================================="
