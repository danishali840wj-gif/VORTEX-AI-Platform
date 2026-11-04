#!/usr/bin/env bash
# Stops the VORTEX backend and engine started by run.sh
cd "$(dirname "$0")"
for name in backend engine; do
  f="data/$name.pid"
  if [ -f "$f" ]; then
    pid="$(cat "$f")"
    kill "$pid" 2>/dev/null && [ "$1" != "quiet" ] && echo "stopped $name ($pid)"
    rm -f "$f"
  fi
done
exit 0
