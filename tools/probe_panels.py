#!/usr/bin/env python3
"""Find a UI panel by clicking candidate points and reading what appears.

The 经验分配器 is the one route this account has to turn the 积累经验 pool into
levels without fighting, and `docs/MACHINE_MAP.md` records it as **NOT FOUND** after
190 hover points in 发明室 plus every visited room.  The remaining leads are specific
and cheap:

    1. the 精灵背包 **top** icon row (y≈450..560 game_px) — never successfully scanned
    2. the post-battle 战报/结算 screen (in the guide video the panel appears right
       after a battle ends)
    3. rooms never entered: 动力室, 精灵中心

This tool walks an explicit list of points, OCRs after each click, and stops the
moment one of the wanted keywords shows up.  It is a *search*, so it must be bounded
and reversible:

    * every click is followed by an OCR of the panel it opened, and by an `Esc`
      attempt to close it (a stray panel left open would confuse the next step)
    * a point budget and a wall-clock budget are both mandatory caps
    * it never guesses a coordinate: the caller passes them (or uses --grid with an
      explicit region), and the coordinates come from `docs/state.md` or a scan

    # probe the bag's top icon row, 64 game_px apart, 1 click per point
    PYTHONPATH=.pydeps python3 tools/probe_panels.py \
        --wanted 经验分配器,积累经验 --region 900,440,2350,580 --step 64

    # probe the exact points already suspected
    PYTHONPATH=.pydeps python3 tools/probe_panels.py \
        --points 1080,505 --points 1300,505 --wanted 经验分配器

Every probe is written to data/overnight/panel_probe.jsonl, including the misses: the
list of "what this button is not" is what stops the next session repeating it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.live import LiveSession  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "overnight")
PROBE_LOG = os.path.join(OUT_DIR, "panel_probe.jsonl")
FRAME_DIR = os.path.join(OUT_DIR, "frames")

DEFAULT_WANTED = ("经验分配器", "积累经验")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def parse_points(raw: list[str]) -> list[tuple[float, float]]:
    out = []
    for item in raw or []:
        x, _, y = item.partition(",")
        out.append((float(x), float(y)))
    return out


def grid_points(region: tuple[float, float, float, float], step: float):
    x0, y0, x1, y1 = region
    x = x0
    while x <= x1:
        y = y0
        while y <= y1:
            yield (x, y)
            y += step
        x += step


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", action="append", default=None,
                    help="game_px x,y — repeatable. Explicit points beat a grid.")
    ap.add_argument("--region", default=None,
                    help="x0,y0,x1,y1 game_px region to sweep with --step")
    ap.add_argument("--step", type=float, default=64.0)
    ap.add_argument("--wanted", default=",".join(DEFAULT_WANTED),
                    help="comma separated OCR substrings that count as a hit")
    ap.add_argument("--max-clicks", type=int, default=40)
    ap.add_argument("--max-seconds", type=float, default=300.0)
    ap.add_argument("--settle", type=float, default=1.4)
    ap.add_argument("--dry-run", action="store_true", help="do not click at all")
    ap.add_argument("--out", default=PROBE_LOG)
    args = ap.parse_args()

    wanted = tuple(w.strip() for w in args.wanted.split(",") if w.strip())
    points = parse_points(args.points)
    if not points and args.region:
        x0, y0, x1, y1 = (float(v) for v in args.region.split(","))
        points = list(grid_points((x0, y0, x1, y1), args.step))
    if not points:
        ap.error("no points: pass --points or --region")

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(FRAME_DIR, exist_ok=True)
    session = LiveSession(dry_run=args.dry_run,
                          enable_input=not args.dry_run).open()
    started = time.time()
    hits: list[dict] = []
    clicks = 0
    try:
        for point in points:
            if clicks >= args.max_clicks:
                log(f"stop: click budget {args.max_clicks} reached")
                break
            if time.time() - started > args.max_seconds:
                log(f"stop: time budget {args.max_seconds:.0f}s reached")
                break
            before = [str(i["text"]) for i in session.ocr(min_conf=0.25)]
            if not args.dry_run:
                try:
                    session.click_px(point[0], point[1], f"probe panel at {point}")
                    clicks += 1
                except Exception as exc:  # noqa: BLE001
                    log(f"click refused at {point}: {exc}")
                    continue
            time.sleep(args.settle)
            after = [str(i["text"]) for i in session.ocr(min_conf=0.25)]
            joined = " ".join(after)
            found = [w for w in wanted if w in joined]
            record = {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "point": list(point),
                "wanted_hit": found,
                "texts_before": before[:12],
                "texts_after": after[:20],
                "new_texts": [t for t in after if t not in before][:12],
            }
            if found:
                path = os.path.join(FRAME_DIR,
                                    f"panel_hit_{int(point[0])}_{int(point[1])}.png")
                session.client.send("shot", path=path)
                record["frame"] = path
                hits.append(record)
                log(f"HIT at {point}: {found} -> {path}")
            else:
                log(f"miss at {point}: {record['new_texts'][:4]}")
            with open(args.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            # close whatever opened, so the next probe starts from the same screen
            if not args.dry_run and record["new_texts"]:
                try:
                    session.client.send("key", code=53)  # Esc
                    time.sleep(0.6)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    log(f"done: {clicks} clicks, {len(points)} points, {len(hits)} hit(s)")
    if hits:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
    return 0 if hits else 10


if __name__ == "__main__":
    raise SystemExit(main())
