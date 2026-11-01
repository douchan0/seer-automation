#!/usr/bin/env python3
"""Human-in-the-loop control surface for the XIN计划 client.

Every command is a *single, deliberate* step so a human (or the Lead agent) can
look before acting. This is deliberately not an automation loop: Round 4 is
verified one step at a time.

Commands
    look                      capture + OCR, print text with game_px coordinates
    shot [path]               save one PNG of the game window
    clickpx X Y               click at game_px (window-local device pixels)
    clicktext TEXT            click the OCR box containing TEXT (first match)
    hover TEXT                move the mouse over an OCR match without clicking
    budget                    report loop timing of look (capture+OCR)

Coordinates are always game_px = window-local device pixels of the current
window (scale 2.0 on a Retina display), which is the same space the engine and
docs/state.md use.

Safety: nothing here clicks without being told to. `clickpx`/`clicktext` print
the window geometry they are about to click into and refuse if the window moved
between capture and click.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "native", "bin", "seerdx")


def open_client(args):
    daemon_args = ["--target", args.target] if args.target else []
    return C.DaemonClient(BIN, args=daemon_args, timeout_s=30.0)


def geometry_of(client) -> G.WindowGeometry:
    w = client.win()
    return G.WindowGeometry(
        origin_pt=G.Point(float(w["x"]), float(w["y"])),
        size_pt=G.Size(float(w["w"]), float(w["h"])),
        scale=G.RETINA_SCALE,
    )


def look(client, geo, *, min_conf=0.3, shot_path="/tmp/ctl_shot.png"):
    t0 = time.perf_counter()
    client.send("shot", path=shot_path)
    t_shot = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    items = client.send("ocr", path=shot_path, min=min_conf).get("items", [])
    t_ocr = (time.perf_counter() - t1) * 1000
    w = geo.pixel_size.w
    h = geo.pixel_size.h
    rows = []
    for it in items:
        cx = (it["x"] + it["w"] / 2) * w
        cy = (1 - (it["y"] + it["h"] / 2)) * h
        rows.append({"text": it["text"], "conf": it["conf"], "px": (cx, cy)})
    rows.sort(key=lambda r: (r["px"][1], r["px"][0]))
    return rows, t_shot, t_ocr, (w, h), shot_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--target", default=None)
    ap.add_argument("--min", type=float, default=0.3)
    ap.add_argument("--dry-run", action="store_true", help="never send input")
    args = ap.parse_args()

    client = open_client(args)
    try:
        geo = geometry_of(client)

        if args.cmd == "budget":
            rows, ts, to, size, _ = look(client, geo, min_conf=args.min)
            print(f"window {size[0]:.0f}x{size[1]:.0f} px | shot {ts:.1f} ms | "
                  f"ocr {to:.1f} ms | total {ts + to:.1f} ms | {len(rows)} boxes")
            return 0

        if args.cmd == "shot":
            path = args.rest[0] if args.rest else "/tmp/ctl_shot.png"
            r = client.send("shot", path=path)
            print(f"{r.get('path')} {r.get('w')}x{r.get('h')}")
            return 0

        if args.cmd == "look":
            rows, ts, to, size, path = look(client, geo, min_conf=args.min)
            print(f"# window {size[0]:.0f}x{size[1]:.0f} px  shot {ts:.1f} ms  "
                  f"ocr {to:.1f} ms  -> {path}")
            for r in rows:
                x, y = r["px"]
                print(f"({x:6.0f},{y:6.0f})  conf={r['conf']:.2f}  {r['text']}")
            return 0

        if args.cmd == "clicktext":
            needle = " ".join(args.rest)
            rows, ts, to, size, _ = look(client, geo, min_conf=args.min)
            hits = [r for r in rows if needle in r["text"]]
            if not hits:
                print(f"NOT FOUND: {needle!r}  ({len(rows)} boxes seen)")
                return 1
            r = hits[0]
            return _click(client, geo, r["px"], r["text"], args.dry_run)

        if args.cmd == "hover":
            needle = " ".join(args.rest)
            rows, *_ = look(client, geo, min_conf=args.min)
            hits = [r for r in rows if needle in r["text"]]
            if not hits:
                print(f"NOT FOUND: {needle!r}")
                return 1
            _gp = geo.global_pt_from_game_px(hits[0]["px"][0], hits[0]["px"][1])
            gx, gy = _gp.x, _gp.y
            if not args.dry_run:
                client.send("move", x=gx, y=gy)
            print(f"hover {hits[0]['text']!r} at global ({gx:.0f},{gy:.0f})")
            return 0

        if args.cmd == "type":
            text = " ".join(args.rest)
            if not ensure_frontmost(client):
                print("REFUSING: could not bring the game window to the front")
                return 1
            client.send("type", text=text)
            print(f"typed {text!r}")
            return 0

        if args.cmd == "key":
            if not ensure_frontmost(client):
                print("REFUSING: could not bring the game window to the front")
                return 1
            code = int(args.rest[0])
            client.send("key", code=code)
            print(f"key {code}")
            return 0

        if args.cmd == "clickpx":
            px = G.Point(float(args.rest[0]), float(args.rest[1]))
            return _click(client, geo, px, f"raw {args.rest}", args.dry_run)

        print(f"unknown command {args.cmd!r}")
        return 2
    finally:
        client.close()


def ensure_frontmost(client, tries=12) -> bool:
    """Bring the game window to the front; a click lands on whatever is on top,
    and ScreenCaptureKit happily captures an occluded window, so without this a
    capture-then-click loop silently clicks the wrong app."""
    import json as _json, subprocess as _sp
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ctl = os.path.join(root, "scripts", "seerctl")
    for _ in range(tries):
        client.send("activate")
        time.sleep(0.25)
        try:
            out = _sp.run([ctl, "wl"], capture_output=True, text=True, timeout=5).stdout
            ws = _json.loads(out)
        except Exception:
            time.sleep(0.2)
            continue
        top = next((w for w in ws if w["layer"] == 0), None)
        if top and top["owner"] == "SeerGame":
            return True
    return False


def _click(client, geo, px_point, label, dry_run) -> int:
    # accept either a Point or an (x, y) tuple
    if hasattr(px_point, "x"):
        px_x, px_y = float(px_point.x), float(px_point.y)
    else:
        px_x, px_y = float(px_point[0]), float(px_point[1])
    _gp = geo.global_pt_from_game_px(px_x, px_y)
    gx, gy = _gp.x, _gp.y
    # Re-read the window right before acting: never click into a window that moved.
    now = geometry_of(client)
    moved = (abs(now.origin_pt.x - geo.origin_pt.x) > 2
             or abs(now.origin_pt.y - geo.origin_pt.y) > 2
             or abs(now.size_pt.w - geo.size_pt.w) > 2
             or abs(now.size_pt.h - geo.size_pt.h) > 2)
    if moved:
        print(f"REFUSING: window moved between capture and click "
              f"({geo.origin_pt}->{now.origin_pt}, {geo.size_pt}->{now.size_pt})")
        return 1
    print(f"click {label!r} game_px=({px_x:.0f},{px_y:.0f}) "
          f"-> global_pt=({gx:.1f},{gy:.1f}) window={geo.origin_pt} {geo.size_pt}")
    if dry_run:
        print("(dry run: not sent)")
        return 0
    if not ensure_frontmost(client):
        print("REFUSING: could not bring the game window to the front")
        return 1
    client.send("click", x=gx, y=gy, n=1)
    print("sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
