#!/usr/bin/env python3
"""night_hover — identify bottom-bar icons by reading their hover tooltips.

Why: icon artwork has no OCR text and the bar drifts between scenes (AGENTS §6 G6),
so the only reliable identification is the client's own tooltip (docs/UI_BUTTON_MAP.md).
This tool opens ONE warm LiveSession, walks the pointer over each candidate point,
and prints the tooltip text it produced.  It never clicks.

    PYTHONPATH=.pydeps python3 tools/night_hover.py --y 1375 530 670 810

A tooltip that appears only as a diff between two OCR passes is exactly what
``LiveSession.hover_px`` returns; empty text means "no tooltip seen", not "no button".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from seerauto.live import LiveSession  # noqa: E402


def text_of(it: dict) -> str:
    return str(it.get("text") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xs", nargs="+", type=float)
    ap.add_argument("--y", type=float, default=1375.0)
    ap.add_argument("--settle", type=float, default=1.3)
    ap.add_argument("--min-conf", type=float, default=0.15)
    ap.add_argument("--json", default=None)
    ap.add_argument("--near", type=float, default=300.0,
                    help="only report tooltip candidates within this many px of the point")
    args = ap.parse_args()

    flags = [p for p in ("/tmp/seerauto.master.pause", "/tmp/seerauto.stop",
                         "/tmp/seerauto.pause") if os.path.exists(p)]
    if flags:
        print(f"REFUSING: control flag(s) present: {flags}")
        return 20

    rows = []
    s = LiveSession(dry_run=False, enable_input=False)
    try:
        s.open()
        print(f"window {s.window_raw} px={s.pixel_size}")
        # Tooltips are drawn by the game itself: if another app owns the pointer's
        # target window the client never sees the move events and no tooltip fires.
        s.client.send("activate")
        time.sleep(0.6)
        print("frontmost ok:", s.ensure_frontmost())
        for x in args.xs:
            tip = s.hover_px(x, args.y, settle_s=args.settle, min_conf=args.min_conf)
            text = str(tip.get("text") or "").strip()
            near = []
            for it in tip.get("items") or []:
                ix, iy = (float(v) for v in it.get("px", (0, 0)))
                if abs(ix - x) <= args.near and abs(iy - args.y) <= args.near:
                    near.append(text_of(it))
            rows.append({"x": x, "y": args.y, "tip": text, "near": near})
            print(f"({x:7.1f},{args.y:7.1f})  ->  {text!r}   near={near!r}")
            time.sleep(0.2)
    finally:
        s.close()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
