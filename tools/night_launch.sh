#!/bin/bash
# tools/night_launch.sh — the single command that starts the overnight live run.
#
# It exists so the 23:10 start is one auditable action instead of a hand-typed
# sequence at the end of a long day, and so the preflight is never skipped:
#
#   1. read-only checks (window identity, no startup popup, who is frontmost)
#   2. refuse to start if a master pause exists (the user may have said 暂停)
#   3. hand the night to tools/supervise.py with an ABSOLUTE deadline
#
# Nothing here clicks.  The supervisor does, and only after this script has said the
# machine is in a state where that is allowed.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONPATH=.pydeps
DEADLINE="${1:-2026-11-06T06:30:00+08:00}"
LOG="data/overnight/supervise_long.log"
mkdir -p data/overnight/frames

say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

say "=== night launch; deadline $DEADLINE ==="

if [ -e /tmp/seerauto.master.pause ]; then
    say "REFUSED: /tmp/seerauto.master.pause exists — the user asked to stay paused"
    exit 7
fi

if ! ./scripts/seerctl wl 2>/dev/null | grep -q SeerGame; then
    say "no game window; launching the client"
    open -a /Applications/SeerGame.app
    sleep 20
fi

say "windows:"; ./scripts/seerctl wl 2>/dev/null | head -c 400; say ""

if ! python3 tools/fix_startup.py --check >/tmp/seerauto_startup.txt 2>&1; then
    say "startup popup is up -> clearing it"
    python3 tools/fix_startup.py >>"$LOG" 2>&1 || {
        say "FAILED to clear the startup popup; not starting"; exit 8; }
fi

say "frontmost: $(lsappinfo info -only name "$(lsappinfo front)" 2>/dev/null | tail -1)"

python3 tools/overnight_snapshot.py --label launch >>"$LOG" 2>&1
say "engine: $ROOT"
say "stated goal graph:"; python3 tools/explain_goal.py 2>&1 | head -4 | tee -a "$LOG"

say "handing over to tools/supervise.py"
exec python3 tools/supervise.py \
    --deadline-at "$DEADLINE" \
    --batch-minutes "${BATCH_MINUTES:-20}" \
    --batch-battles "${BATCH_BATTLES:-12}" \
    --max-no-work "${MAX_NO_WORK:-2}" \
    --max-failures "${MAX_FAILURES:-3}" 2>&1 | tee -a "$LOG"
