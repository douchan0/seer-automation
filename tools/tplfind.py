#!/usr/bin/env python3
"""tplfind — offline template lookup on a saved live frame (no game input).

Why: the bottom bar icons drift ~100 px between scenes (AGENTS §6 G6), so a click
must be aimed at the match on *this* frame, not at a coordinate table.  This tool
answers "where is template X in frame Y" so `tools/ctl.py clickpx` can be given a
measured point instead of a guessed one.  It is pure offline image math.

    python3 tools/tplfind.py artifacts/night_20261109/01_before_dismiss.png \
        seerauto/templates/live/live_petbag_icon.png --roi 300,1300,2100,1450

ROI is in frame px (the 2400x1600 capture space, same as game_px).  Prints the best
match centre and the top-N peaks; exits 1 when the best score is under --min.
"""
from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np


def peaks(res: np.ndarray, n: int, min_score: float) -> list[tuple[float, int, int]]:
    out: list[tuple[float, int, int]] = []
    work = res.copy()
    for _ in range(n):
        _, best, _, loc = cv2.minMaxLoc(work)
        if best < min_score:
            break
        out.append((float(best), int(loc[0]), int(loc[1])))
        x0 = max(0, loc[0] - 12)
        y0 = max(0, loc[1] - 12)
        work[y0:loc[1] + 13, x0:loc[0] + 13] = -1.0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frame")
    ap.add_argument("template", nargs="+")
    ap.add_argument("--roi", default=None, help="x0,y0,x1,y1 in frame px")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--min", type=float, default=0.55)
    args = ap.parse_args()

    frame = cv2.imread(args.frame)
    if frame is None:
        print(f"cannot read frame {args.frame}", file=sys.stderr)
        return 2
    h, w = frame.shape[:2]
    if args.roi:
        x0, y0, x1, y1 = (int(v) for v in args.roi.split(","))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
    else:
        x0, y0, x1, y1 = 0, 0, w, h
    sub = frame[y0:y1, x0:x1]
    worst = 1.0
    for idx, path in enumerate(args.template):
        tpl = cv2.imread(path)
        if tpl is None:
            print(f"cannot read template {path}", file=sys.stderr)
            return 2
        th, tw = tpl.shape[:2]
        if sub.shape[0] < th or sub.shape[1] < tw:
            print(f"{os.path.basename(path)}: template {tw}x{th} larger than ROI")
            worst = 0.0
            continue
        res = cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED)
        info = []
        for score, lx, ly in peaks(res, args.top, args.min):
            cx = x0 + lx + tw / 2.0
            cy = y0 + ly + th / 2.0
            info.append(f"({cx:6.1f},{cy:6.1f}) score={score:.3f}")
            worst = score if idx == 0 and score < worst else worst
        print(f"{os.path.basename(path)} [{tw}x{th}] roi=({x0},{y0},{x1},{y1}): "
              + (" | ".join(info) if info else f"no match >= {args.min}"))
    return 0 if worst >= args.min else 1


if __name__ == "__main__":
    raise SystemExit(main())
