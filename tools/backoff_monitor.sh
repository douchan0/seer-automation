#!/usr/bin/env bash
# Wait for the game backend to come back, then relaunch the client and report.
# The backend (119.45.28.34:3000) times out while the version endpoint answers;
# the client cannot load without it.  This just watches and retries — it never
# sends game input by itself.
set -u
BACKEND="http://119.45.28.34:3000/file"
LOG="/tmp/seerauto_backend_watch.log"
for i in $(seq 1 120); do
  code=$(curl -sS -m 6 -o /dev/null -w "%{http_code}" "$BACKEND" 2>/dev/null)
  [ -z "$code" ] && code=000
  echo "$(date +%H:%M:%S) attempt=$i backend=$code" >> "$LOG"
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "$(date +%H:%M:%S) BACKEND UP ($code) — relaunching client" >> "$LOG"
    pkill -f SeerGame 2>/dev/null
    sleep 6
    open -a /Applications/SeerGame.app
    sleep 45
    echo "$(date +%H:%M:%S) client relaunched" >> "$LOG"
    exit 0
  fi
  sleep 120
done
echo "$(date +%H:%M:%S) gave up after 120 attempts" >> "$LOG"
