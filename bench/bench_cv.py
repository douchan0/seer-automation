#!/usr/bin/env python3
"""bench_cv.py — OpenCV cost matrix for the candidate vision primitives.

Measures, on real game frames from artifacts/, the individual cost of every
OpenCV operation the hot loop might use:

  * colour conversion
  * full-frame vs downsampled pixel diff (change detection)
  * binary threshold
  * template matching at three scales (full frame / ROI / small ROI),
    in both colour and grayscale
  * the "state fingerprint" primitive: mean-abs-diff of a small patch

cv2/numpy live only in ./.pydeps, so this must be run with
    PYTHONPATH=.pydeps python3 bench/bench_cv.py

Every number printed is measured on this machine in this run. Load average is
recorded with the results because a loaded machine inflates everything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats as st  # noqa: E402

try:
    import cv2
    import numpy as np
except ImportError:
    print(json.dumps({
        "mode": "cv",
        "error": "cv2/numpy not importable — run with PYTHONPATH=.pydeps",
        "python": sys.executable,
    }))
    sys.exit(2)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.join(REPO, "artifacts", "game.png")
BATTLE = os.path.join(REPO, "artifacts", "battle.png")

# ROI used throughout: a plausible "skill bar + hp bar" band of the main frame.
GAME_ROI = (700, 200, 900, 400)      # x, y, w, h in game.png pixels
SMALL_ROI = (760, 240, 300, 200)     # smaller sub-region of the same frame
TPL = 60                             # template edge length


def imread_unicode(path):
    """cv2.imread breaks on non-ASCII paths; imdecode does not."""
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"cannot decode {path}")
    return img


def crop(img, rect):
    x, y, w, h = rect
    h_img, w_img = img.shape[:2]
    x2, y2 = min(x + w, w_img), min(y + h, h_img)
    return img[y:y2, x:x2]


def timeit(fn, iters, warmup=2):
    """Return (samples_ms, last_result). Never swallows an exception."""
    for _ in range(warmup):
        fn()
    samples = []
    last = None
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        last = fn()
        samples.append((time.perf_counter_ns() - t0) / 1e6)
    return samples, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=12, help="iterations for cheap ops")
    ap.add_argument("--heavy-iters", type=int, default=6, help="iterations for expensive ops")
    ap.add_argument("--json", action="store_true", help="print JSON only")
    args = ap.parse_args()

    load_start = st.load_info()

    game = imread_unicode(GAME)
    battle = imread_unicode(BATTLE)
    game_gray = cv2.cvtColor(game, cv2.COLOR_BGR2GRAY)
    game_roi = crop(game, GAME_ROI)
    game_roi_gray = cv2.cvtColor(game_roi, cv2.COLOR_BGR2GRAY)
    small_roi = crop(game, SMALL_ROI)
    small_roi_gray = cv2.cvtColor(small_roi, cv2.COLOR_BGR2GRAY)

    # Template is a real 60x60 crop from inside the ROI (a known UI element).
    tpl_color = crop(game_roi, (200, 100, TPL, TPL))
    tpl_gray = cv2.cvtColor(tpl_color, cv2.COLOR_BGR2GRAY)

    # A second frame for diff tests: shift the real frame by a few pixels, which
    # produces a realistic non-trivial diff without inventing pixel data.
    game_shift = np.roll(game, shift=(3, 5), axis=(0, 1))
    game_gray_shift = cv2.cvtColor(game_shift, cv2.COLOR_BGR2GRAY)
    game_small = cv2.resize(game, (229, 142), interpolation=cv2.INTER_AREA)
    game_small_shift = cv2.resize(game_shift, (229, 142), interpolation=cv2.INTER_AREA)

    results = []

    def bench(name, fn, iters, note, category, extra=None):
        try:
            samples, last = timeit(fn, iters)
        except Exception as exc:  # noqa: BLE001 - report, never hide
            results.append({
                "name": name, "category": category, "error": repr(exc), "note": note,
            })
            return
        rec = {
            "name": name,
            "category": category,
            "note": note,
            "samples_ms": samples,
        }
        rec.update(st.summarize(samples))
        if extra is not None:
            rec["extra"] = extra
        results.append(rec)

    # ---------------- colour conversion ----------------
    bench("cvtColor_BGR2GRAY_full", lambda: cv2.cvtColor(game, cv2.COLOR_BGR2GRAY),
          args.iters, f"full frame {game.shape[1]}x{game.shape[0]}", "convert")
    bench("cvtColor_BGR2GRAY_roi900x400", lambda: cv2.cvtColor(game_roi, cv2.COLOR_BGR2GRAY),
          args.iters, f"ROI {GAME_ROI[2]}x{GAME_ROI[3]}", "convert")
    bench("cvtColor_BGR2GRAY_roi300x200", lambda: cv2.cvtColor(small_roi, cv2.COLOR_BGR2GRAY),
          args.iters, f"ROI {SMALL_ROI[2]}x{SMALL_ROI[3]}", "convert")

    # ---------------- resize / downsample ----------------
    bench("resize_full_to_229x142", lambda: cv2.resize(game, (229, 142), interpolation=cv2.INTER_AREA),
          args.iters, "full frame -> 229x142 (1/10 linear)", "resize")
    bench("resize_full_to_half", lambda: cv2.resize(game, (game.shape[1] // 2, game.shape[0] // 2),
                                                    interpolation=cv2.INTER_AREA),
          args.iters, "full frame -> 1/2 linear", "resize")

    # ---------------- pixel diff / change detection ----------------
    bench("absdiff_full_color", lambda: cv2.absdiff(game, game_shift),
          args.iters, "full frame colour absdiff", "diff")
    bench("absdiff_full_gray", lambda: cv2.absdiff(game_gray, game_gray_shift),
          args.iters, "full frame grayscale absdiff", "diff")
    bench("absdiff_downsampled_229x142",
          lambda: cv2.absdiff(game_small, game_small_shift),
          args.iters, "absdiff on pre-downsampled 229x142", "diff")

    def downsampled_change():
        a = cv2.resize(game, (229, 142), interpolation=cv2.INTER_AREA)
        b = cv2.resize(game_shift, (229, 142), interpolation=cv2.INTER_AREA)
        d = cv2.absdiff(a, b)
        return float(d.mean())

    bench("change_detect_downsample_composed", downsampled_change,
          args.iters, "resize x2 + absdiff + mean: the whole downsampled change detector", "diff")

    bench("mean_abs_diff_numpy_full",
          lambda: float(np.abs(game.astype(np.int16) - game_shift.astype(np.int16)).mean()),
          args.iters, "pure-numpy full-frame mean abs diff (no cv2)", "diff")

    # ---------------- threshold ----------------
    bench("threshold_gray_full", lambda: cv2.threshold(game_gray, 128, 255, cv2.THRESH_BINARY),
          args.iters, "binary threshold on full gray frame", "threshold")
    bench("countNonZero_roi_mask",
          lambda: cv2.countNonZero(cv2.threshold(cv2.cvtColor(game_roi, cv2.COLOR_BGR2GRAY),
                                                 128, 255, cv2.THRESH_BINARY)[1]),
          args.iters, "threshold + countNonZero on 900x400 ROI", "threshold")

    # ---------------- state fingerprint primitive ----------------
    patch_a = crop(game, (900, 300, 100, 40)).astype(np.int16)
    patch_b = crop(game_shift, (900, 300, 100, 40)).astype(np.int16)

    def fingerprint_patch():
        return float(np.abs(patch_a - patch_b).mean())

    bench("fingerprint_patch_40x100x3", fingerprint_patch,
          args.iters, "mean abs diff of a 40x100x3 patch — the state-fingerprint primitive", "fingerprint")

    def fingerprint_3patches():
        tot = 0.0
        for (px, py) in ((900, 300), (1200, 320), (1500, 340)):
            a = crop(game, (px, py, 100, 40)).astype(np.int16)
            b = crop(game_shift, (px, py, 100, 40)).astype(np.int16)
            tot += float(np.abs(a - b).mean())
        return tot / 3.0

    bench("fingerprint_3patches_40x100x3", fingerprint_3patches,
          args.iters, "three fingerprint patches + mean — a multi-region state fingerprint", "fingerprint")

    # ---------------- template matching ----------------
    def tm_gray_full():
        return cv2.matchTemplate(game_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)

    def tm_color_full():
        return cv2.matchTemplate(game, tpl_color, cv2.TM_CCOEFF_NORMED)

    def tm_gray_roi():
        return cv2.matchTemplate(game_roi_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)

    def tm_color_roi():
        return cv2.matchTemplate(game_roi, tpl_color, cv2.TM_CCOEFF_NORMED)

    def tm_gray_small():
        return cv2.matchTemplate(small_roi_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)

    bench("matchTemplate_gray_full_2298x1428", tm_gray_full, args.heavy_iters,
          "60x60 template, TM_CCOEFF_NORMED, full grayscale frame", "template")
    bench("matchTemplate_color_full_2298x1428", tm_color_full, args.heavy_iters,
          "60x60 template, TM_CCOEFF_NORMED, full colour frame", "template")
    bench("matchTemplate_gray_roi900x400", tm_gray_roi, args.heavy_iters,
          "60x60 template, 900x400 grayscale ROI", "template")
    bench("matchTemplate_color_roi900x400", tm_color_roi, args.heavy_iters,
          "60x60 template, 900x400 colour ROI", "template")
    bench("matchTemplate_gray_roi300x200", tm_gray_small, args.heavy_iters,
          "60x60 template, 300x200 grayscale ROI", "template")

    # ---------------- battle.png (the smaller real frame) ----------------
    battle_gray = cv2.cvtColor(battle, cv2.COLOR_BGR2GRAY)
    bench("battle_cvtColor_gray", lambda: cv2.cvtColor(battle, cv2.COLOR_BGR2GRAY),
          args.iters, f"battle.png {battle.shape[1]}x{battle.shape[0]}", "convert")
    bench("battle_matchTemplate_gray_full", lambda: cv2.matchTemplate(battle_gray, tpl_gray,
                                                                     cv2.TM_CCOEFF_NORMED),
          args.heavy_iters, f"battle.png {battle.shape[1]}x{battle.shape[0]} full gray template match",
          "template")

    load_end = st.load_info()

    out = {
        "mode": "cv",
        "opencv_version": cv2.__version__,
        "numpy_version": np.__version__,
        "cv2_num_threads": cv2.getNumThreads(),
        "python": sys.executable,
        "pythonpath_note": "run with PYTHONPATH=.pydeps",
        "images": {
            "game": {"path": GAME, "w": int(game.shape[1]), "h": int(game.shape[0])},
            "battle": {"path": BATTLE, "w": int(battle.shape[1]), "h": int(battle.shape[0])},
        },
        "rois": {"game_roi": GAME_ROI, "small_roi": SMALL_ROI, "template": TPL},
        "iters": args.iters,
        "heavy_iters": args.heavy_iters,
        "load_start": load_start,
        "load_end": load_end,
        "env": st.env_info(),
        "results": results,
    }

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"\nOpenCV {cv2.__version__} / numpy {np.__version__} / threads={cv2.getNumThreads()}")
    print(f"game.png {game.shape[1]}x{game.shape[0]}   battle.png {battle.shape[1]}x{battle.shape[0]}")
    print(f"load avg {load_start['loadavg_1m']:.2f} on {load_start['ncpu']} cores "
          f"({load_start['load_per_core_1m']:.2f}/core)")
    rows = []
    for rec in results:
        if "error" in rec:
            rows.append([rec["name"], "ERROR", "-", "-", "-", rec["error"][:40]])
        else:
            rows.append([rec["name"], rec["n"], rec["mean"], rec["p50"], rec["p95"], rec["note"]])
    st.print_table(rows, ["operation", "n", "mean ms", "p50 ms", "p95 ms", "note"],
                   aligns=["l", "r", "r", "r", "r", "l"])
    print(json.dumps({"load_start": load_start}, indent=None))


if __name__ == "__main__":
    main()
