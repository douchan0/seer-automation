#!/usr/bin/env python3
"""Confirm the objects a *vision* pass selected, by hovering exactly those points.

The grid scanner (tools/scan_machines.py) is mechanical: it misses small widgets and
costs minutes per room.  A vision pass over one screenshot finds the interactive
objects in seconds, but it cannot read their names.  This tool closes the loop:
the model names the candidates and their rough positions, and each one is confirmed
here with a hover (a Flash tooltip) so the name in the map is *observed*, not guessed.

    # 1. dump the current frame
    PYTHONPATH=.pydeps python3 tools/vision_probe.py --capture data/machines/shot.png

    # 2. look at it, then probe the points you believe are interactive
    PYTHONPATH=.pydeps python3 tools/vision_probe.py --points "150,300 700,420 1200,900"

    # 3. or pass a JSON list with labels you guessed, to compare guess vs tooltip
    PYTHONPATH=.pydeps python3 tools/vision_probe.py --json '[{"px":[150,300],"guess":"宝箱"}]'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.live import LiveSession  # noqa: E402
from seerauto.skills.act import _dismiss_dialogs, ensure_world  # noqa: E402
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.world.model import GameWorldState  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", default=None, help="write the current frame here and exit")
    ap.add_argument("--points", default=None, help='game_px points: "x,y x,y ..."')
    ap.add_argument("--json", default=None, help='[{"px":[x,y],"guess":"..."}] or a file path')
    ap.add_argument("--settle", type=float, default=0.9)
    ap.add_argument("--clean", action="store_true", help="close panels/dialogs first")
    args = ap.parse_args()

    session = LiveSession(dry_run=False, enable_input=True).open()
    try:
        if args.capture:
            os.makedirs(os.path.dirname(args.capture) or ".", exist_ok=True)
            session.export_png(args.capture)
            print(f"wrote {args.capture}  window={session.window_raw}")
            return 0
        if args.clean:
            ctx = SkillContext(session=session, world=GameWorldState(), dry_run=False,
                               allow_input=True, budget=Budget(max_duration_s=30, max_actions=6))
            ensure_world(ctx)
            _dismiss_dialogs(ctx, limit=3)
            time.sleep(0.6)

        items = []
        if args.points:
            for chunk in args.points.replace(";", " ").split():
                x, y = chunk.split(",")
                items.append({"px": [int(x), int(y)], "guess": ""})
        if args.json:
            raw = args.json
            if os.path.exists(raw):
                with open(raw, encoding="utf-8") as fh:
                    items.extend(json.load(fh))
            else:
                items.extend(json.loads(raw))
        if not items:
            print("nothing to probe; pass --points or --json", file=sys.stderr)
            return 2

        results = []
        for item in items:
            x, y = item["px"]
            probe = session.hover_px(x, y, settle_s=args.settle)
            text = probe.get("text", "")
            results.append({"px": [x, y], "guess": item.get("guess", ""), "tooltip": text})
            mark = "OK " if text else "-- "
            print(f"{mark}({x:4d},{y:4d}) guess={item.get('guess','')!r:16s} -> {text!r}")
        print(json.dumps(results, ensure_ascii=False))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
