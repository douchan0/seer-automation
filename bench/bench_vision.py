#!/usr/bin/env python3
"""bench_vision.py — the hot-loop primitives, measured as complete decisions.

bench_cv.py measures individual OpenCV operations. This file answers the
question that actually matters for the game loop:

    "which state-detection strategy fits inside a 16.7 ms frame budget?"

So each strategy is measured end-to-end as one decision (pixels in -> verdict
out), including the numpy handoff from a raw BGRA buffer, and each is compared
against the 60 Hz budget.

Candidate strategies, cheapest first:
  A. fingerprint      — mean-abs-diff of a few small patches
  B. downsampled diff — shrink the frame, diff it, count changed pixels
  C. ROI template     — matchTemplate on a small region only
  D. full-frame match — matchTemplate on the whole frame (expected: too slow)
  E. conditional      — A gates C: only template-match the ROI when it changed

Also measures the raw-buffer -> numpy handoff, which every strategy pays.

Run with:  PYTHONPATH=.pydeps python3 bench/bench_vision.py
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
    print(json.dumps({"mode": "vision",
                      "error": "cv2/numpy not importable — run with PYTHONPATH=.pydeps"}))
    sys.exit(2)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.join(REPO, "artifacts", "game.png")

FRAME_BUDGET_MS = 1000.0 / 60.0  # 16.67 ms

# Regions a real client would watch (fractions of the 2298x1428 reference frame).
FINGERPRINT_PATCHES = [  # x, y, w, h
    (900, 300, 100, 40),    # skill bar strip
    (1200, 320, 100, 40),   # secondary bar
    (1500, 340, 100, 40),   # status strip
]
ROI_SMALL = (760, 240, 300, 200)
ROI_MEDIUM = (700, 200, 900, 400)
TPL_SIZE = 60
DOWNSAMPLE = (229, 142)


def imread_unicode(path):
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"cannot decode {path}")
    return img


def crop(img, rect):
    x, y, w, h = rect
    return img[y:y + h, x:x + w]


def timeit(fn, iters, warmup=2):
    for _ in range(warmup):
        fn()
    samples, last = [], None
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        last = fn()
        samples.append((time.perf_counter_ns() - t0) / 1e6)
    return samples, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=15)
    ap.add_argument("--heavy-iters", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    load_start = st.load_info()
    frame = imread_unicode(GAME)
    h, w = frame.shape[:2]
    frame2 = np.roll(frame, shift=(2, 3), axis=(0, 1))  # "next frame"
    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    frame2_gray = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)

    # templates + ROI caches
    roi_small = crop(frame, ROI_SMALL)
    roi_small_gray = cv2.cvtColor(roi_small, cv2.COLOR_BGR2GRAY)
    roi_med = crop(frame, ROI_MEDIUM)
    roi_med_gray = cv2.cvtColor(roi_med, cv2.COLOR_BGR2GRAY)
    tpl_color = crop(roi_med, (200, 100, TPL_SIZE, TPL_SIZE))
    tpl_gray = cv2.cvtColor(tpl_color, cv2.COLOR_BGR2GRAY)

    # A raw BGRA buffer as the capture layer would hand it over (13.1 MB).
    raw_bgra = np.ascontiguousarray(
        cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)).tobytes()
    raw_bytes = len(raw_bgra)

    results = []

    def bench(name, fn, iters, note, budget_ok=None):
        try:
            samples, _ = timeit(fn, iters)
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "error": repr(exc), "note": note})
            return
        rec = {"name": name, "note": note, "samples_ms": samples}
        rec.update(st.summarize(samples))
        rec["fits_60hz"] = rec["p95"] < FRAME_BUDGET_MS
        rec["p95_headroom_x"] = st.ratio(FRAME_BUDGET_MS, rec["p95"])
        results.append(rec)

    # ---------------------------------------------------------------- handoff
    def handoff_copy():
        buf = np.frombuffer(raw_bgra, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(buf[:, :, :3])

    def handoff_view_only():
        buf = np.frombuffer(raw_bgra, dtype=np.uint8).reshape(h, w, 4)
        return buf[:, :, :3]

    bench("handoff_frombuffer_view_only", handoff_view_only, args.iters,
          f"np.frombuffer + reshape + BGR slice of a {raw_bytes/1024/1024:.1f} MB BGRA buffer (zero-copy view)")
    bench("handoff_frombuffer_plus_copy", handoff_copy, args.iters,
          "same, plus ascontiguousarray to own the pixels (one 13 MB copy)")

    # ---------------------------------------------------------------- A: fingerprint
    def fingerprint_1():
        a = crop(frame, FINGERPRINT_PATCHES[0]).astype(np.int16)
        b = crop(frame2, FINGERPRINT_PATCHES[0]).astype(np.int16)
        return float(np.abs(a - b).mean())

    def fingerprint_3():
        tot = 0.0
        for r in FINGERPRINT_PATCHES:
            a = crop(frame, r).astype(np.int16)
            b = crop(frame2, r).astype(np.int16)
            tot += float(np.abs(a - b).mean())
        return tot / len(FINGERPRINT_PATCHES)

    def fingerprint_3_precast():
        # realistic production shape: patches pre-extracted once per frame,
        # diffed as int16 (avoids re-slicing + re-casting in the loop)
        tot = 0.0
        for r in FINGERPRINT_PATCHES:
            a = crop(frame, r)
            b = crop(frame2, r)
            tot += float(cv2.absdiff(a, b).mean())
        return tot / len(FINGERPRINT_PATCHES)

    bench("A_fingerprint_1patch", fingerprint_1, args.iters,
          "mean abs diff of ONE 100x40x3 patch (int16 cast path)")
    bench("A_fingerprint_3patches", fingerprint_3, args.iters,
          "mean abs diff of THREE 100x40x3 patches (int16 cast path)")
    bench("A_fingerprint_3patches_cv2absdiff", fingerprint_3_precast, args.iters,
          "three patches diffed with cv2.absdiff instead of int16 numpy")

    # ---------------------------------------------------------------- B: downsampled diff
    small_a = cv2.resize(frame, DOWNSAMPLE, interpolation=cv2.INTER_AREA)
    small_b = cv2.resize(frame2, DOWNSAMPLE, interpolation=cv2.INTER_AREA)

    def downsample_only():
        return cv2.resize(frame, DOWNSAMPLE, interpolation=cv2.INTER_AREA)

    def downsample_diff():
        return float(cv2.absdiff(small_a, small_b).mean())

    def downsample_diff_composed():
        a = cv2.resize(frame, DOWNSAMPLE, interpolation=cv2.INTER_AREA)
        b = cv2.resize(frame2, DOWNSAMPLE, interpolation=cv2.INTER_AREA)
        return float(cv2.absdiff(a, b).mean())

    def downsample_gray_diff():
        a = cv2.resize(frame, DOWNSAMPLE, interpolation=cv2.INTER_AREA)
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        b = cv2.resize(frame2, DOWNSAMPLE, interpolation=cv2.INTER_AREA)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        return int(cv2.countNonZero(cv2.absdiff(ga, gb) > 8))

    bench("B_downsample_only", downsample_only, args.iters,
          f"resize full frame to {DOWNSAMPLE[0]}x{DOWNSAMPLE[1]}")
    bench("B_downsample_diff_precomputed", downsample_diff, args.iters,
          "absdiff + mean on already-downsampled buffers (capture layer did the resize)")
    bench("B_downsample_diff_composed", downsample_diff_composed, args.iters,
          "resize x2 + absdiff + mean: the complete downsampled change detector")
    bench("B_downsample_gray_changed_pixels", downsample_gray_diff, args.iters,
          "resize x2 + gray + absdiff + threshold + countNonZero: 'how many pixels changed'")

    # ---------------------------------------------------------------- C: ROI template
    def roi_small_match():
        return cv2.matchTemplate(roi_small_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)

    def roi_med_match():
        return cv2.matchTemplate(roi_med_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)

    bench("C_roi_template_300x200", roi_small_match, args.heavy_iters,
          "matchTemplate 60x60 on a 300x200 gray ROI")
    bench("C_roi_template_900x400", roi_med_match, args.heavy_iters,
          "matchTemplate 60x60 on a 900x400 gray ROI")

    # ---------------------------------------------------------------- D: full frame
    bench("D_fullframe_template_gray", lambda: cv2.matchTemplate(frame_gray, tpl_gray,
                                                                cv2.TM_CCOEFF_NORMED),
          args.heavy_iters, f"matchTemplate 60x60 on the full {w}x{h} gray frame (expected: too slow)")

    # ---------------------------------------------------------------- E: conditional
    THRESH = 2.0

    def conditional_pipeline():
        """A gates C: fingerprint first, template-match the ROI only if changed."""
        changed = fingerprint_3_precast() > THRESH
        if changed:
            res = cv2.matchTemplate(roi_med_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
            return float(res.max())
        return None

    bench("E_conditional_fingerprint_then_roi_template", conditional_pipeline, args.iters,
          f"fingerprint(3 patches) -> if diff>{THRESH} -> ROI template match. The production shape.")

    # ---------------------------------------------------------------- summary
    load_end = st.load_info()
    out = {
        "mode": "vision",
        "frame": {"path": GAME, "w": int(w), "h": int(h)},
        "raw_bgra_bytes": raw_bytes,
        "frame_budget_ms_60hz": FRAME_BUDGET_MS,
        "downsample": DOWNSAMPLE,
        "fingerprint_patches": FINGERPRINT_PATCHES,
        "rois": {"small": ROI_SMALL, "medium": ROI_MEDIUM, "template": TPL_SIZE},
        "cv2_version": cv2.__version__,
        "numpy_version": np.__version__,
        "cv2_num_threads": cv2.getNumThreads(),
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

    print(f"\nHot-loop primitives — frame {w}x{h}, 60 Hz budget = {FRAME_BUDGET_MS:.2f} ms")
    print(f"load avg {load_start['loadavg_1m']:.2f} on {load_start['ncpu']} cores")
    rows = []
    for r in results:
        if "error" in r:
            rows.append([r["name"], "ERR", "-", "-", "-", "-", r["error"][:30]])
        else:
            rows.append([r["name"], r["n"], r["mean"], r["p50"], r["p95"],
                         "yes" if r["fits_60hz"] else "NO", r["note"][:52]])
    st.print_table(rows, ["strategy / primitive", "n", "mean ms", "p50", "p95", "60Hz?", "note"],
                   aligns=["l", "r", "r", "r", "r", "r", "l"])

    fit = [r["name"] for r in results if r.get("fits_60hz")]
    nofit = [r["name"] for r in results if r.get("fits_60hz") is False]
    print(f"\nfits in one 60 Hz frame (p95 < {FRAME_BUDGET_MS:.1f} ms): {len(fit)}")
    for n in fit:
        print(f"   OK   {n}")
    print(f"does NOT fit: {len(nofit)}")
    for n in nofit:
        print(f"   NO   {n}")


if __name__ == "__main__":
    main()
