#!/usr/bin/env python3
"""Find interactive machines/NPCs in a scene by hovering a grid and reading tooltips.

Why this exists
---------------
The client names every hoverable object in a tooltip ("精灵恢复", "经验分配器", …).
Those names are the only machine-readable handle on objects whose artwork we have
never seen, and they are exactly how a human finds a device they cannot read.

Cost control
------------
A hover costs a pointer move + settle (~0.9 s) and, naively, two OCR passes (~1 s).
This scanner first diffs a small window around the cursor between "before move" and
"after settle" (~2 ms of numpy); OCR runs **only** when that window changed, i.e.
when a tooltip actually appeared.  That takes a 200-point sweep from ~7 minutes to
~2.

Usage
-----
    # scan the scene the client is in right now
    PYTHONPATH=.pydeps python3 tools/scan_machines.py --step 160 --out data/machines/now.json

    # scan after travelling (destination names live in seerauto.skills.act.MAP_DESTINATIONS)
    PYTHONPATH=.pydeps python3 tools/scan_machines.py --travel ship_bridge --step 160

Safety: read-only except the pointer.  Never clicks.  Refuses unless the game is
frontmost (a hover into another app would just be a stray pointer move, but the
tooltips would be nonsense).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from seerauto.live import LiveSession  # noqa: E402
from seerauto.skills.act import (  # noqa: E402
    MAP_DESTINATIONS,
    Travel,
    _dismiss_dialogs,
    ensure_world,
)
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.world.model import GameWorldState  # noqa: E402

#: Words that are part of the always-on HUD or of a panel, never a tooltip.
NOISE = ("公屏聊天", "XIN计划", "万圣", "石圣", "在线", "聊天", "SPT", "SET", "目",
         "hello", "系统：", "综合待命", "背包", "等级", "个体", "性格", "威力",
         "PP", "命中率", "体力", "攻击", "防御", "特攻", "特防", "速度", "升级所需",
         "获得时间", "威力", "抓", "瞪眼", "火花", "挑拨", "小蘑菇", "烈火猴",
         "满荡", "未保存", "号背包", "F4级", "%")

#: A machine/NPC tooltip is a short action noun (2-8 chars), no digits, no colon.
_ACTION_HINTS = ("器", "机", "台", "室", "舱", "塔", "门", "门", "站", "中心", "商店",
                 "商人", "银行", "仓库", "医院", "学院", "殿堂", "关", "券", "炉",
                 "传送", "恢复", "训练", "领取", "进入", "打开", "查看", "对话", "购买")


def looks_like_tooltip(text: str) -> bool:
    text = text.strip()
    if not 2 <= len(text) <= 8:
        return False
    if any(ch.isdigit() for ch in text):
        return False
    if any(n in text for n in NOISE):
        return False
    return any(h in text for h in _ACTION_HINTS)


def crop_around(frame: np.ndarray, x: float, y: float, w: int = 520, h: int = 200) -> np.ndarray:
    fh, fw = frame.shape[:2]
    x0 = max(0, int(x) - w // 2)
    y0 = max(0, int(y) - h // 2)
    return frame[y0:min(fh, y0 + h), x0:min(fw, x0 + w)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--travel", default=None, help="a MAP_DESTINATIONS key to travel to first")
    ap.add_argument("--step", type=int, default=160)
    ap.add_argument("--x0", type=int, default=150)
    ap.add_argument("--x1", type=int, default=2250)
    ap.add_argument("--y0", type=int, default=200)
    ap.add_argument("--y1", type=int, default=1340)
    ap.add_argument("--settle", type=float, default=0.9)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    session = LiveSession(dry_run=False, enable_input=True).open()
    if not session.ensure_frontmost():
        print("REFUSING: the game window is not frontmost", file=sys.stderr)
        return 3
    ctx = SkillContext(session=session, world=GameWorldState(), dry_run=False,
                       allow_input=True, budget=Budget(max_duration_s=600, max_actions=10))

    if args.travel:
        if args.travel not in MAP_DESTINATIONS:
            print(f"unknown destination {args.travel!r}; known: {sorted(MAP_DESTINATIONS)}",
                  file=sys.stderr)
            return 2
        ensure_world(ctx)
        _dismiss_dialogs(ctx, limit=3)
        result = Travel()(ctx, destination=args.travel)
        print(f"travel {args.travel}: {result.status.value} {result.proof[:100]}")
        if not result.ok:
            return 4
        time.sleep(1.2)

    hits: list[dict] = []
    probes = 0
    ocr_calls = 0
    started = time.perf_counter()
    xs = list(range(args.x0, args.x1 + 1, args.step))
    ys = list(range(args.y0, args.y1 + 1, args.step))
    total = len(xs) * len(ys)
    print(f"scanning {len(xs)}x{len(ys)} = {total} points, step {args.step}")

    for y in ys:
        for x in xs:
            probes += 1
            before = session.latest()
            region_before = crop_around(before, x, y)
            session._move_human(x, y)
            time.sleep(args.settle)
            after = session.latest()
            region_after = crop_around(after, x, y)
            if region_before.shape != region_after.shape:
                continue
            delta = float(np.abs(region_after.astype(np.int16)
                                 - region_before.astype(np.int16)).mean())
            if delta < 1.0:          # no tooltip appeared: skip the OCR entirely
                continue
            ocr_calls += 1
            # one OCR pass; keep only boxes near the pointer (a tooltip is drawn
            # above/beside the icon, never across the screen)
            near = []
            for i in session.ocr(min_conf=0.15):
                px = i["px"]
                if abs(px[0] - x) < 320 and abs(px[1] - y) < 220:
                    if looks_like_tooltip(str(i["text"])):
                        near.append(str(i["text"]))
            raw_near = sorted({str(i["text"]) for i in session.ocr(min_conf=0.15)
                               if abs(i["px"][0] - x) < 320 and abs(i["px"][1] - y) < 220})
            if near:
                hits.append({"px": [x, y], "text": sorted(set(near)),
                             "raw": raw_near, "delta": round(delta, 2)})
                print(f"  ({x},{y}) -> {sorted(set(near))}")
            elif raw_near:
                hits.append({"px": [x, y], "text": [], "raw": raw_near,
                             "delta": round(delta, 2)})
                print(f"  ({x},{y}) raw -> {raw_near[:6]}")
        if args.out:
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    elapsed = time.perf_counter() - started
    payload = {
        "travel": args.travel,
        "window": session.window_raw,
        "grid": {"x": [args.x0, args.x1], "y": [args.y0, args.y1], "step": args.step},
        "probes": probes,
        "ocr_calls": ocr_calls,
        "elapsed_s": round(elapsed, 1),
        "hits": hits,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
        print(f"wrote {args.out}")
    print(f"done: {probes} probes, {ocr_calls} OCR passes, {len(hits)} tooltips, "
          f"{elapsed:.0f}s")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
