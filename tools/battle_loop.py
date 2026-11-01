#!/usr/bin/env python3
"""Round 4: drive one real battle with the local vision loop, and measure it.

This is deliberately NOT an LLM loop: state comes from a local template match on
a warm ScreenCaptureKit stream, and the click coordinate is derived from the
match itself rather than hard-coded.

What it proves (or disproves) per run:
  * capture     - warm `latest()` instead of a 160 ms subprocess grab
  * detect      - ROI template match instead of a 550 ms full-frame OCR
  * decide      - fixed policy: "the fight button is visible -> attack"
  * input       - the click lands where the template was actually seen
  * wait        - wait for the frame to change (seq bump), not a blind sleep
  * confirm     - the button must reappear before the next action

Safety: refuses to act if the game window is not frontmost, and stops on the
turn/time budget. It never clicks anywhere it did not just see.

Usage:
    PYTHONPATH=.pydeps python3 tools/battle_loop.py --max-turns 30 --max-seconds 300
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "native", "bin", "seerdx")
TPL = os.path.join(ROOT, "seerauto", "templates", "live", "live_fight_btn.png")
SEERCTL = os.path.join(ROOT, "scripts", "seerctl")

# search only the bottom-right action pad: ~1/6 of the pixels of a full-frame match
ROI = (1600, 1050, 2400, 1460)


def frontmost() -> bool:
    import json as _j
    import subprocess as _sp
    try:
        out = _sp.run([SEERCTL, "wl"], capture_output=True, text=True, timeout=5).stdout
        for w in _j.loads(out):
            if w["layer"] == 0:
                return w["owner"] == "SeerGame"
    except Exception:
        return False
    return False


def pct(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--max-seconds", type=float, default=300.0)
    ap.add_argument("--threshold", type=float, default=0.80)
    ap.add_argument("--target", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(BIN):
        print(f"daemon missing: {BIN} (run tools/build_seerdx.sh)")
        return 2
    tpl = cv2.imread(TPL)
    if tpl is None:
        print(f"template missing: {TPL}")
        return 2
    th, tw = tpl.shape[:2]
    x0, y0, x1, y1 = ROI

    client = C.DaemonClient(BIN, args=(["--target", args.target] if args.target else []),
                            timeout_s=30.0)
    win = client.win()
    geo = G.WindowGeometry(origin_pt=G.Point(float(win["x"]), float(win["y"])),
                           size_pt=G.Size(float(win["w"]), float(win["h"])),
                           scale=G.RETINA_SCALE)
    backend = C.DaemonCapture(client, geo)
    if not backend.warm:
        print("no warm session; refusing to run a timing-sensitive loop on the slow path")
        return 1

    # warm: bring the game forward and make sure a click will reach it
    client.send("activate")
    time.sleep(0.6)
    if not frontmost():
        print("REFUSING: game window is not frontmost; a click would land elsewhere")
        backend.close()
        client.close()
        return 1

    print(f"window {win['w']}x{win['h']} pt -> {geo.pixel_size.w:.0f}x{geo.pixel_size.h:.0f} px")
    print(f"template {tw}x{th}  ROI {ROI}  threshold {args.threshold}")
    print(f"budget: {args.max_turns} turns / {args.max_seconds:.0f}s")
    print()

    stages = {k: [] for k in ("capture", "detect", "decide", "input", "wait", "confirm")}
    turns = 0
    end_reason = "max_turns"
    started = time.perf_counter()
    last_seq = None
    detections = []

    try:
        while turns < args.max_turns and (time.perf_counter() - started) < args.max_seconds:
            # ---- capture -------------------------------------------------- #
            t = time.perf_counter()
            frame = backend.latest()
            stages["capture"].append((time.perf_counter() - t) * 1000)

            # ---- detect (ROI only) ---------------------------------------- #
            t = time.perf_counter()
            roi = frame.array[y0:y1, x0:x1]
            res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            stages["detect"].append((time.perf_counter() - t) * 1000)
            detections.append(float(score))

            # ---- decide --------------------------------------------------- #
            t = time.perf_counter()
            found = score >= args.threshold
            stages["decide"].append((time.perf_counter() - t) * 1000)
            if not found:
                end_reason = "fight button gone (battle over or a dialog appeared)"
                break

            # click the centre of what we actually matched, not a fixed guess
            cx = x0 + loc[0] + tw / 2
            cy = y0 + loc[1] + th / 2

            # ---- input ---------------------------------------------------- #
            t = time.perf_counter()
            gp = geo.global_pt_from_game_px(cx, cy)
            client.send("click", x=gp.x, y=gp.y, n=1)
            stages["input"].append((time.perf_counter() - t) * 1000)
            turns += 1

            # ---- wait for the frame to actually change -------------------- #
            t = time.perf_counter()
            deadline = time.perf_counter() + 5.0
            changed = False
            base_seq = frame.seq
            while time.perf_counter() < deadline:
                f2 = backend.latest()
                if f2.seq != base_seq:
                    changed = True
                    break
                time.sleep(0.004)
            stages["wait"].append((time.perf_counter() - t) * 1000)

            # ---- confirm: the button is still there for the next turn ----- #
            t = time.perf_counter()
            f3 = backend.latest()
            roi3 = f3.array[y0:y1, x0:x1]
            s3 = float(cv2.matchTemplate(roi3, tpl, cv2.TM_CCOEFF_NORMED).max())
            stages["confirm"].append((time.perf_counter() - t) * 1000)

            if not args.quiet:
                print(f"turn {turns:2d}  score={score:.3f}  click=({cx:.0f},{cy:.0f})  "
                      f"changed={changed}  next_score={s3:.3f}  "
                      f"cap={stages['capture'][-1]:.2f} det={stages['detect'][-1]:.2f} "
                      f"wait={stages['wait'][-1]:.1f}")
            last_seq = f3.seq

            if s3 < args.threshold:
                end_reason = "fight button gone after the click (battle over)"
                break
        else:
            end_reason = "budget reached"
    except KeyboardInterrupt:
        end_reason = "interrupted"
    finally:
        print()
        print(f"stopped: {end_reason} after {turns} turns "
              f"({time.perf_counter() - started:.1f}s)")
        total = []
        for k, v in stages.items():
            if v:
                print(f"  {k:<8} n={len(v):<4} mean={statistics.mean(v):8.2f} "
                      f"p50={pct(v, .5):8.2f} p95={pct(v, .95):8.2f}")
        # per-turn closed loop = sum of the stage means for one action
        if turns:
            per_turn = sum(statistics.mean(v) for v in stages.values() if v)
            print(f"  {'TOTAL':<8} per-turn closed loop ~{per_turn:.1f} ms "
                  f"(capture+detect+decide+input+wait+confirm)")
        if detections:
            print(f"  match score: min={min(detections):.3f} mean={statistics.mean(detections):.3f} "
                  f"max={max(detections):.3f}")

        # final look: what is on screen now?
        try:
            shot = "/tmp/battle_end.png"
            client.send("shot", path=shot)
            items = client.send("ocr", path=shot, min=0.3).get("items", [])
            texts = [i["text"] for i in items]
            print(f"  screen after battle ({len(texts)} boxes): {' | '.join(texts[:14])}")
        except Exception as exc:  # noqa: BLE001
            print(f"  final OCR failed: {exc}")
            texts = []

        backend.close()
        client.close()

        if args.json:
            json.dump({"turns": turns, "end_reason": end_reason,
                       "stages_ms": stages, "match_scores": detections,
                       "final_texts": texts}, open(args.json, "w", encoding="utf-8"), indent=1)
            print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
