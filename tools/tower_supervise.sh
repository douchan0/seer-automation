#!/bin/bash
# tools/tower_supervise.sh — restart the tower grinder until the hard deadline.
#
# Why: on 2026-11-07 the grinder produced a verified PROGRESS (floor 1 -> 2,
# 1710 XP) but then could not engage on the next floor and its in-run RECOVER
# cannot work from *inside* the tower (no bottom bar, no star map).  A fresh
# process re-enters the cycle through the INSIDE path and keeps producing, so an
# external restart loop turns "one floor per run" into continuous production.
#
# Stops on: the absolute deadline, /tmp/seerauto.stop, or --max-runs.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONPATH=.pydeps
DEADLINE="${DEADLINE:-2026-11-07T06:50:00+08:00}"
RUN_MINUTES="${RUN_MINUTES:-12}"
MAX_RUNS="${MAX_RUNS:-200}"
LOG="data/overnight/20261106_dual_mode/tower_grind.log"
OUT="data/overnight/20261106_dual_mode/tower_supervise.log"

say() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$OUT"; }

run=0
while [ "$run" -lt "$MAX_RUNS" ]; do
    run=$((run + 1))
    now=$(date +%s)
    if [ -e /tmp/seerauto.stop ]; then say "stop flag present -> exiting after $((run-1)) run(s)"; break; fi
    say "run $run: tower_grind --minutes $RUN_MINUTES (deadline $DEADLINE)"
    python3 tools/tower_grind.py --preset 1 --minutes "$RUN_MINUTES" \
        --deadline-at "$DEADLINE" --allow-input --auto-recover --max-recovers 1 \
        >> "$LOG" 2>&1
    code=$?
    say "run $run: exit=$code"
    if [ "$code" -eq 0 ]; then say "clean finish -> stopping loop"; break; fi
    sleep 4
done
say "supervisor done after $run run(s) at $(date '+%H:%M:%S')"
