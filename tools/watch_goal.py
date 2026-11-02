#!/usr/bin/env python3
"""Print the live progress of the newest goal run, safely and cheaply.

Read-only: opens the SQLite file, never touches the game.  Designed to be the
one command to run in a second shell while a long run is in progress.

    PYTHONPATH=.pydeps python3 tools/watch_goal.py            # once
    PYTHONPATH=.pydeps python3 tools/watch_goal.py --follow   # every 30 s
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.world.store import WorldStore  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "goalrunner.sqlite3")


def snapshot() -> str:
    store = WorldStore(DB)
    try:
        run = store.latest_run()
        if run is None:
            return "no run yet"
        nodes = store.node_states(run["run_id"])
        world = store.latest_world()
        lines = [
            f"[{time.strftime('%H:%M:%S')}] run {run['run_id']} {run['status']}"
            f"  stop={run.get('stop_reason')}",
            f"  stats {run.get('stats')}",
        ]
        if world is not None:
            s = world.summary()
            pet = s.get("active_pet") or {}
            hp = ""
            if pet.get("hp") is not None:
                hp = f" hp={pet.get('hp')}/{pet.get('hp_max')}"
            battle = s.get("battle_ready")
            lines.append(f"  scene={s.get('scene')} pet={pet.get('name')} Lv{pet.get('level')}"
                         f"{hp} xp_to_next={s.get('xp_to_next')} can_battle={battle}")
        for node_id, row in sorted(nodes.items()):
            lines.append(f"  node {node_id}: {row.get('status')} {row.get('proof') or ''}")
        sk = [e for e in store.events(run["run_id"], limit=40) if e["kind"].startswith("skill")]
        if sk:
            lines.append(f"  last skill: {sk[0]['ts'][11:]} {sk[0]['kind']} {sk[0]['message'][:90]}")
        return "\n".join(lines)
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    a = ap.parse_args()
    if not a.follow:
        print(snapshot())
        return 0
    try:
        while True:
            print(snapshot())
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
