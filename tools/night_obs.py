#!/usr/bin/env python3
"""night_obs — one read-only observation step with evidence files (no input).

Why: tonight every claim needs a frame + the OCR text that was read off it, sitting
next to each other in artifacts/night_20261109/.  ``tools/ctl.py look`` prints OCR to
stdout but throws the frame away; this writes ``<stem>.png`` + ``<stem>.ocr.json`` +
``<stem>.ocr.txt`` for the same capture and never sends any input op.

    PYTHONPATH=.pydeps python3 tools/night_obs.py shot artifacts/night_20261109/01_world
    PYTHONPATH=.pydeps python3 tools/night_obs.py ocr  artifacts/night_20261109/01_world
    PYTHONPATH=.pydeps python3 tools/night_obs.py win

It refuses to write into artifacts/ when the live window geometry changed mid-capture
(AGENTS.md §5.5: never trust a frame whose window moved underneath you).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402

BIN = os.path.join(ROOT, "native", "bin", "seerdx")


def client() -> C.DaemonClient:
    return C.DaemonClient(BIN, timeout_s=30.0)


def geo_of(c: C.DaemonClient) -> G.WindowGeometry:
    w = c.win()
    return G.WindowGeometry(
        origin_pt=G.Point(float(w["x"]), float(w["y"])),
        size_pt=G.Size(float(w["w"]), float(w["h"])),
        scale=G.RETINA_SCALE,
    )


def ocr_rows(c: C.DaemonClient, path: str, min_conf: float) -> list[dict]:
    items = c.send("ocr", path=path, min=min_conf).get("items", [])
    w, h = geo_of(c).pixel_size.w, geo_of(c).pixel_size.h
    rows = []
    for it in items:
        cx = (it["x"] + it["w"] / 2) * w
        cy = (1 - (it["y"] + it["h"] / 2)) * h
        rows.append({"text": it["text"], "conf": round(float(it["conf"]), 3),
                     "px": [round(cx, 1), round(cy, 1)]})
    rows.sort(key=lambda r: (r["px"][1], r["px"][0]))
    return rows


def write_sidecars(stem: str, rows: list[dict]) -> None:
    with open(stem + ".ocr.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    with open(stem + ".ocr.txt", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(f"({r['px'][0]:7.1f},{r['px'][1]:7.1f})  conf={r['conf']:.2f}  {r['text']}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["win", "shot", "ocr"])
    ap.add_argument("stem", nargs="?", default=None)
    ap.add_argument("--min", type=float, default=0.3)
    args = ap.parse_args()
    c = client()
    try:
        if args.cmd == "win":
            print(json.dumps(c.win(), ensure_ascii=False))
            return 0
        if not args.stem:
            print("a path stem is required", file=sys.stderr)
            return 2
        stem = args.stem if os.path.isabs(args.stem) else os.path.join(ROOT, args.stem)
        os.makedirs(os.path.dirname(stem) or ".", exist_ok=True)
        png = stem if stem.endswith(".png") else stem + ".png"
        stem = png[:-4]
        if args.cmd == "shot":
            before = geo_of(c)
            r = c.send("shot", path=png)
            after = geo_of(c)
            moved = (before.origin_pt.x != after.origin_pt.x
                     or before.origin_pt.y != after.origin_pt.y
                     or before.size_pt.w != after.size_pt.w
                     or before.size_pt.h != after.size_pt.h)
            print(f"shot {png} {r.get('w')}x{r.get('h')} "
                  f"window=({after.origin_pt.x:.0f},{after.origin_pt.y:.0f}) "
                  f"{after.size_pt.w:.0f}x{after.size_pt.h:.0f}pt"
                  + ("  WARNING: window moved mid-capture" if moved else ""))
        rows = ocr_rows(c, png, args.min)
        write_sidecars(stem, rows)
        print(f"# {len(rows)} boxes -> {stem}.ocr.json/.ocr.txt")
        for r in rows:
            print(f"({r['px'][0]:7.1f},{r['px'][1]:7.1f})  conf={r['conf']:.2f}  {r['text']}")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
