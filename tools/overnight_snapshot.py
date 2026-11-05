#!/usr/bin/env python3
"""Freeze the night's evidence into one timestamped directory.

The report has to answer "what actually happened" without asking the reader to trust a
chat log, so every phase boundary gets a snapshot: the goal graph's verdict, the last
run, the tower ledger, git state, and the world facts as SQLite holds them.

Nothing here touches the game (no click, no screenshot of the client is required — the
caller's own frames live in data/overnight/frames).

    PYTHONPATH=.pydeps python3 tools/overnight_snapshot.py --label 2310_live_start
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "data", "overnight", "snapshots")


def sh(cmd: list[str], cwd: str = ROOT) -> str:
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"<failed: {exc}>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="snapshot")
    args = ap.parse_args()
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    out_dir = os.path.join(BASE, f"{stamp}_{args.label}")
    os.makedirs(out_dir, exist_ok=True)

    def write(name: str, text: str) -> None:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    write("git.txt", sh(["git", "log", "--oneline", "-12"]) + "\n\n"
          + sh(["git", "status", "--short"]))
    write("goal_status.txt", sh([sys.executable, "-m", "seerauto", "goal", "status"]))
    write("explain_goal.txt", sh([sys.executable, "tools", "explain_goal.py"]))

    ledger = os.path.join(ROOT, "data", "overnight", "tower_ledger.jsonl")
    if os.path.exists(ledger):
        lines = open(ledger, encoding="utf-8").read().strip().splitlines()
        write("tower_ledger_tail.jsonl", "\n".join(lines[-40:]))
    state = os.path.join(ROOT, "data", "overnight", "tower_state.json")
    if os.path.exists(state):
        write("tower_state.json", open(state, encoding="utf-8").read())

    # World facts straight out of SQLite: the last observation, whole.
    try:
        from seerauto.world.store import WorldStore

        store = WorldStore(os.path.join(ROOT, "data", "goalrunner.sqlite3"))
        try:
            world = store.latest_world()
            run = store.latest_run()
            payload = {
                "run": {k: run.get(k) for k in
                         ("run_id", "status", "stop_reason", "stats", "budgets", "options")}
                if run else None,
                "world": None if world is None else world.summary(),
            }
            write("world.json", json.dumps(payload, ensure_ascii=False, indent=1))
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        write("world.json", f'{{"error": "{exc}"}}')

    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
