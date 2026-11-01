#!/usr/bin/env python3
"""bench_all.py — run the whole performance matrix and write one JSON result.

    python3 bench/bench_all.py                # full run
    python3 bench/bench_all.py --quick        # smaller n, for smoke tests
    python3 bench/bench_all.py --label QUIESCED --skip-cv

Every leg writes its own measurements; nothing is estimated. The load average
is sampled before, during and after, because a latency number without its load
context is not reproducible and must not be published as a baseline.

Outputs:
    bench/results/<UTC timestamp>_<label>.json   (full record)
    bench/results/latest.json                    (copy of the newest run)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats as st  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
RESULTS = os.path.join(BENCH, "results")
NATIVE_SRC = os.path.join(REPO, "native", "bench_capture.swift")
NATIVE_BIN = os.path.join(REPO, "native", "bin", "bench_capture")
MODULE_CACHE = "/tmp/bench/mc"

SWIFT_FRAMEWORKS = [
    "-framework", "ScreenCaptureKit",
    "-framework", "CoreGraphics",
    "-framework", "Vision",
    "-framework", "AppKit",
    "-framework", "ImageIO",
    "-framework", "CoreVideo",
    "-framework", "QuartzCore",
]


# ------------------------------------------------------------------ build

def ensure_binary(force=False):
    """Compile native/bench_capture.swift if missing or older than the source."""
    src_mtime = os.path.getmtime(NATIVE_SRC) if os.path.exists(NATIVE_SRC) else 0
    bin_mtime = os.path.getmtime(NATIVE_BIN) if os.path.exists(NATIVE_BIN) else 0
    if not force and os.path.exists(NATIVE_BIN) and bin_mtime >= src_mtime:
        return {"built": False, "reason": "up to date", "bin": NATIVE_BIN}
    if not os.path.exists(NATIVE_SRC):
        raise SystemExit(f"missing Swift source: {NATIVE_SRC}")
    os.makedirs(MODULE_CACHE, exist_ok=True)
    os.makedirs(os.path.dirname(NATIVE_BIN), exist_ok=True)
    # The sandbox refuses the default ModuleCache, so point swiftc at /tmp.
    cmd = ["swiftc", "-O", "-module-cache-path", MODULE_CACHE,
           "-o", NATIVE_BIN, NATIVE_SRC] + SWIFT_FRAMEWORKS
    print(f"[build] {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise SystemExit(f"swiftc failed:\n{proc.stderr[-4000:]}")
    return {"built": True, "bin": NATIVE_BIN, "cmd": cmd}


# ------------------------------------------------------------------ legs

LOOP_BACKENDS = ",".join([
    "loop_prod_accurate", "loop_prod_fast",
    "loop_inproc_accurate", "loop_inproc_fast",
    "loop_inproc_accurate_roi", "loop_inproc_fast_roi",
])

# The game-content leg runs the capture-only backends AND the loops back to back
# on the same window, so the bottleneck decomposition never mixes measurements
# taken on different windows or far apart in time.
GAME_LEG_BACKENDS = ",".join([
    "windowlist_enum",
    "subprocess_screencapture_window_png",
    "sck_screenshot_warm_2x",
    "sck_screenshot_roi_2x",
    "sck_screenshot_warm_1x",
    "loop_prod_instrumented",
    "loop_inproc_instrumented",
]) + "," + LOOP_BACKENDS


def leg_capture(args, load_log, extra=None, name="capture"):
    cmd = [NATIVE_BIN, "capture",
           "--iters", str(args.capture_iters),
           "--loop-iters", str(args.loop_iters),
           "--warmup", "2"] + (extra or [])
    print(f"\n[{name}] {' '.join(cmd)}", file=sys.stderr)
    load_log.append({"at": f"{name}_start", **st.load_info()})
    out, err, rc = st.run_json(cmd, cwd=REPO, timeout=3600)
    load_log.append({"at": f"{name}_end", **st.load_info()})
    if out is None:
        return {"error": f"{name} leg failed rc={rc}", "stderr": err[-3000:]}
    out["_stderr_tail"] = err[-2000:]
    return out


def leg_script(name, script, extra, load_log):
    cmd = [sys.executable, os.path.join(BENCH, script), "--json"] + extra
    print(f"\n[{name}] {' '.join(cmd)}", file=sys.stderr)
    load_log.append({"at": f"{name}_start", **st.load_info()})
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO, ".pydeps") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=3600, env=env)
    load_log.append({"at": f"{name}_end", **st.load_info()})
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"error": f"{name} leg failed rc={proc.returncode}",
                "stderr": proc.stderr[-3000:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"{name} leg produced invalid JSON: {exc}",
                "stdout": proc.stdout[:1000], "stderr": proc.stderr[-2000:]}


# ------------------------------------------------------------------ analysis

def index_capture(cap):
    """backend name -> record."""
    if not cap or "results" not in cap:
        return {}
    return {r["backend"]: r for r in cap["results"]}


def build_headline(cap, ocr, cv, vision, cap_game=None):
    """The comparison the report is built around. All values measured."""
    be = index_capture(cap)
    beg = index_capture(cap_game)
    h = {"rows": [], "ratios": {}, "notes": []}

    def add(key, label, rec, kind, note=""):
        if not rec or "error" in rec or not rec.get("n"):
            h["rows"].append({"key": key, "label": label, "kind": kind,
                              "error": (rec or {}).get("error", "no data"), "note": note})
            return
        h["rows"].append({
            "key": key, "label": label, "kind": kind,
            "n": rec.get("n"), "mean": rec.get("mean"), "p50": rec.get("p50"),
            "p95": rec.get("p95"), "min": rec.get("min"), "max": rec.get("max"),
            "note": note,
        })

    add("baseline_shot", "screencapture subprocess (window region, PNG to disk)", 
        be.get("subprocess_screencapture_window_png"), "baseline",
        "process spawn + capture + PNG encode to disk; decode not included")
    add("baseline_shot_full", "screencapture subprocess (full screen, PNG to disk)",
        be.get("subprocess_screencapture_fullscreen_png"), "baseline")
    add("prod_shot", "PROD shot() = subprocess + PNG decode (current seerd path)",
        be.get("prod_shot_pipeline"), "baseline",
        "byte-for-byte scripts/seerd.swift shot()")
    add("sck_percall", "SCK SCScreenshotManager per call (cached filter, .best)",
        be.get("sck_screenshot_warm_2x"), "sck_percall")
    add("sck_percall_roi", "SCK SCScreenshotManager per call (ROI 300x200pt)",
        be.get("sck_screenshot_roi_2x"), "sck_percall")
    add("sck_stream_read", "SCK persistent stream: read latest frame (640x480)",
        be.get("sck_stream_read_latest"), "sck_stream",
        "preallocated buffer + memcpy, non-blocking")
    add("memcpy_game", "memcpy one 2298x1428 BGRA frame (13.1 MB)",
        be.get("memcpy_game_frame_bgra"), "sck_stream",
        "the handoff cost at real game resolution")

    # stream meta-derived rows (not latencies of a call, so kept separate)
    stream_meta = {}
    ss = be.get("sck_stream_steady_state_animated")
    if ss and "error" not in ss:
        stream_meta = {
            "achieved_fps": ss["meta"].get("achieved_fps"),
            "inter_frame_mean": ss.get("mean"),
            "inter_frame_p50": ss.get("p50"),
            "inter_frame_p95": ss.get("p95"),
            "start_capture_ms": ss["meta"].get("start_capture_ms"),
            "frames_delivered": ss["meta"].get("frames_delivered"),
        }
    static = be.get("sck_stream_steady_state_static")
    if static and "error" not in static:
        stream_meta["static_screen_inter_frame_mean"] = static.get("mean")
        stream_meta["static_screen_frames"] = static["meta"].get("frames_delivered")
    rl = be.get("sck_stream_read_latest")
    if rl and "error" not in rl:
        stream_meta["read_latest_staleness"] = rl["meta"].get("staleness_ms")
        stream_meta["read_latest_frame_bytes"] = rl["meta"].get("frame_bytes")
        stream_meta["read_latest_rss_delta_mb"] = (
            (rl["meta"].get("rss_mb_after") or 0) - (rl["meta"].get("rss_mb_before") or 0))
    h["stream_meta"] = stream_meta

    # ratios that survive a loaded machine
    def mean_of(k):
        r = be.get(k)
        if r and "error" not in r and r.get("mean"):
            return r["mean"]
        return None

    base = mean_of("prod_shot_pipeline")
    for k, label in [("sck_screenshot_warm_2x", "per-call SCK vs prod shot()"),
                     ("sck_stream_read_latest", "stream read-latest vs prod shot()"),
                     ("memcpy_game_frame_bgra", "game-res memcpy vs prod shot()")]:
        v = mean_of(k)
        if base and v:
            h["ratios"][label] = base / v

    # end-to-end loops
    h["loops"] = []
    for k, label in [
        ("loop_prod_accurate", "PROD loop: subprocess + Vision .accurate"),
        ("loop_prod_fast", "subprocess + Vision .fast"),
        ("loop_inproc_accurate", "in-process SCK + Vision .accurate"),
        ("loop_inproc_fast", "in-process SCK + Vision .fast"),
        ("loop_inproc_accurate_roi", "in-process SCK ROI + Vision .accurate"),
        ("loop_inproc_fast_roi", "in-process SCK ROI + Vision .fast"),
    ]:
        r = be.get(k)
        if r and "error" not in r and r.get("n"):
            h["loops"].append({"key": k, "label": label, "n": r["n"], "mean": r["mean"],
                               "p50": r["p50"], "p95": r["p95"],
                               "ocr_items_mean": r["meta"].get("ocr_items_mean")})
        else:
            h["loops"].append({"key": k, "label": label,
                               "error": (r or {}).get("error", "no data")})

    # Bottleneck ranking of the CURRENT production loop.
    #
    # Composed algebraically from measurements taken in the SAME leg, back to
    # back, on the SAME window, so the terms stay comparable even on a loaded
    # machine (which inflates everything by roughly the same factor):
    #
    #   capture_subprocess = subprocess_screencapture_window_png        (measured)
    #   capture_sck        = sck_screenshot_warm_2x                     (measured)
    #   ocr_fast           = loop_inproc_fast      - capture_sck        (same leg)
    #   ocr_accurate       = loop_inproc_accurate  - capture_sck        (same leg)
    #   decode             = (loop_prod_fast - capture_subprocess) - ocr_fast
    #
    # The last line works because the subprocess loop and the in-process loop
    # differ only by (PNG decode) once capture and OCR are subtracted.
    #
    # Preferred source: the game-content leg (real game screenshot content at
    # real game geometry). Falls back to the generic-window leg.
    def mean_in(src, k):
        r = src.get(k)
        if r and "error" not in r and r.get("mean"):
            return r["mean"]
        return None

    def bottleneck_from(src, source_label):
        cap_sub = mean_in(src, "subprocess_screencapture_window_png")
        cap_sck = mean_in(src, "sck_screenshot_warm_2x")
        lp_fast = mean_in(src, "loop_prod_fast")
        lp_acc = mean_in(src, "loop_prod_accurate")
        li_fast = mean_in(src, "loop_inproc_fast")
        li_acc = mean_in(src, "loop_inproc_accurate")
        enum_ms = mean_in(src, "windowlist_enum")

        ocr_fast = max(li_fast - cap_sck, 0.0) if (cap_sck and li_fast) else None
        ocr_acc = max(li_acc - cap_sck, 0.0) if (cap_sck and li_acc) else None
        decode_ms = (max((lp_fast - cap_sub) - ocr_fast, 0.0)
                     if (lp_fast and cap_sub and ocr_fast is not None) else None)

        rank = []
        if enum_ms is not None:
            rank.append({"stage": "window enumeration (CGWindowListCopyWindowInfo, seerd.findWin)",
                         "mean_ms": enum_ms, "source": "measured"})
        if cap_sub is not None:
            rank.append({"stage": "screencapture subprocess (spawn + capture + PNG write)",
                         "mean_ms": cap_sub, "source": "measured"})
        if decode_ms is not None:
            rank.append({"stage": "PNG decode (real pixel decode, lazy handle excluded)",
                         "mean_ms": decode_ms,
                         "source": "derived: (loop_prod_fast - capture) - ocr_fast"})
        if ocr_acc is not None:
            rank.append({"stage": "Vision OCR .accurate (full window)",
                         "mean_ms": ocr_acc,
                         "source": "derived: loop_inproc_accurate - capture"})
        rank.sort(key=lambda x: -x["mean_ms"])
        total = sum(x["mean_ms"] for x in rank)
        for x in rank:
            x["share_pct"] = (x["mean_ms"] / total * 100.0) if total else None
        return {
            "ranking": rank,
            "total_ms": total,
            "source_leg": source_label,
            "components": {
                "capture_subprocess_ms": cap_sub,
                "capture_sck_percall_ms": cap_sck,
                "ocr_fast_ms": ocr_fast,
                "ocr_accurate_ms": ocr_acc,
                "png_decode_ms": decode_ms,
                "window_enum_ms": enum_ms,
                "ocr_accurate_over_fast": st.ratio(ocr_acc, ocr_fast),
                "capture_share_of_prod_loop_pct": (
                    cap_sub / (cap_sub + (decode_ms or 0) + (ocr_acc or 0)) * 100.0
                    if cap_sub else None),
                "check_prod_loop_reconstructed_ms": (
                    (cap_sub + (decode_ms or 0) + (ocr_acc or 0)) if cap_sub is not None else None),
                "check_loop_prod_accurate_ms": lp_acc,
                "note": "all terms from the same leg; reconstruction should approximate loop_prod_accurate",
            },
        }

    if beg and len(beg) > 3:
        b = bottleneck_from(beg, "game-content leg (real screenshot, real geometry)")
    else:
        b = bottleneck_from(be, "generic-window leg")
    h["bottleneck_ranking_derived"] = b["ranking"]
    h["bottleneck_source_leg_derived"] = b["source_leg"]
    h["bottleneck_components_derived"] = b["components"]

    # PREFERRED ranking: the instrumented per-stage numbers. These are measured
    # inside each iteration, so there is no subtraction across backends and no
    # exposure to load drift. The derived ranking above is kept only as a
    # cross-check; it disagreed with its own inputs by 2.3x on 2026-11-01
    # (305.7 ms derived vs 134.6 ms measured for the same operation) and must
    # not be quoted.
    ins = h.get("instrumented_prod")
    if ins and ins.get("stages"):
        h["bottleneck_ranking"] = [
            {"stage": st2["stage"], "mean_ms": st2["mean_ms"],
             "p50_ms": st2["p50_ms"], "p95_ms": st2["p95_ms"],
             "share_pct": st2["share_pct"],
             "source": "measured inside each iteration (loop_prod_instrumented)"}
            for st2 in ins["stages"]
        ]
        h["bottleneck_total_ms"] = ins["total_ms"]
        h["bottleneck_source_leg"] = ("loop_prod_instrumented, game content, "
                                      f"n={ins['n']}")
        h["bottleneck_note"] = ("preferred: per-stage timestamps recorded inside the "
                                "same loop iterations")
    else:
        h["bottleneck_ranking"] = b["ranking"]
        h["bottleneck_total_ms"] = b["total_ms"]
        h["bottleneck_source_leg"] = b["source_leg"] + " (DERIVED — cross-check only)"
        h["bottleneck_note"] = "derived by subtraction; may disagree with measured rows"

    h["game_content_loops"] = []
    if cap_game and "target_window" in cap_game:
        h["game_content_target"] = cap_game["target_window"]
    for k, label in [
        ("loop_prod_accurate", "PROD loop (subprocess+decode+Vision .accurate) on game content"),
        ("loop_prod_fast", "subprocess+decode+Vision .fast on game content"),
        ("loop_inproc_accurate", "in-process SCK + Vision .accurate on game content"),
        ("loop_inproc_fast", "in-process SCK + Vision .fast on game content"),
        ("loop_inproc_accurate_roi", "in-process SCK ROI + Vision .accurate on game content"),
        ("loop_inproc_fast_roi", "in-process SCK ROI + Vision .fast on game content"),
    ]:
        r = beg.get(k)
        if r and "error" not in r and r.get("n"):
            h["game_content_loops"].append({
                "key": k, "label": label, "n": r["n"], "mean": r["mean"],
                "p50": r["p50"], "p95": r["p95"],
                "ocr_items_mean": r["meta"].get("ocr_items_mean")})
        else:
            h["game_content_loops"].append({"key": k, "label": label,
                                            "error": (r or {}).get("error", "no data")})
    # Definitive per-stage split, measured inside each iteration (no cross-backend
    # arithmetic, immune to load drift between backends).
    ip = beg.get("loop_prod_instrumented")
    ii = beg.get("loop_inproc_instrumented")
    if ip and "error" not in ip:
        stages = ip["meta"].get("stages") or {}
        total = ip.get("mean")
        rows = []
        for name, stt in stages.items():
            rows.append({"stage": name, "mean_ms": stt.get("mean"),
                         "p50_ms": stt.get("p50"), "p95_ms": stt.get("p95"),
                         "share_pct": (stt.get("mean") / total * 100.0) if total else None})
        rows.sort(key=lambda x: -(x["mean_ms"] or 0))
        h["instrumented_prod"] = {
            "total_ms": total, "n": ip.get("n"),
            "ocr_items_mean": ip["meta"].get("ocr_items_mean"),
            "stages": rows,
            "note": "capture / forced PNG decode / Vision OCR timed inside each iteration of the production path",
        }
    if ii and "error" not in ii:
        stages = ii["meta"].get("stages") or {}
        total = ii.get("mean")
        rows = []
        for name, stt in stages.items():
            rows.append({"stage": name, "mean_ms": stt.get("mean"),
                         "p50_ms": stt.get("p50"), "p95_ms": stt.get("p95"),
                         "share_pct": (stt.get("mean") / total * 100.0) if total else None})
        rows.sort(key=lambda x: -(x["mean_ms"] or 0))
        h["instrumented_inproc"] = {
            "total_ms": total, "n": ii.get("n"),
            "ocr_items_mean": ii["meta"].get("ocr_items_mean"),
            "stages": rows,
            "note": "SCK capture / Vision OCR timed inside each iteration of the in-process path",
        }
    if ip and ii and "error" not in ip and "error" not in ii and ip.get("mean") and ii.get("mean"):
        h["instrumented_speedup"] = {
            "prod_ms": ip["mean"], "inproc_ms": ii["mean"],
            "speedup": ip["mean"] / ii["mean"],
        }

    def comparability(a, b, label):
        """Two loops may only be compared if they OCRed the same content.

        The subprocess path captures the COMPOSITED screen region; SCK's
        desktopIndependentWindow captures the window's own backing store. On an
        occluded window those are different pixels, which changes the OCR item
        count and therefore the OCR cost. Detect that instead of publishing it.
        """
        if not a or not b or "error" in a or "error" in b:
            return None
        ia = a["meta"].get("ocr_items_mean")
        ib = b["meta"].get("ocr_items_mean")
        if ia is None or ib is None:
            return None
        hi = max(ia, ib, 1.0)
        drift = abs(ia - ib) / hi
        return {
            "pair": label,
            "ocr_items_prod": ia,
            "ocr_items_inproc": ib,
            "relative_item_drift": drift,
            "comparable": bool(drift <= 0.25),
            "reason": ("same content" if drift <= 0.25 else
                       "DIFFERENT PIXELS CAPTURED — subprocess grabs the composited "
                       "screen region (occluded), SCK grabs the window's own content. "
                       "Loop totals for these two paths are NOT comparable."),
        }

    gp = beg.get("loop_prod_accurate")
    gi = beg.get("loop_inproc_accurate")
    h["game_content_comparability"] = comparability(gp, gi, "game content: prod vs inproc (.accurate)")
    h["generic_comparability"] = comparability(be.get("loop_prod_accurate"),
                                               be.get("loop_inproc_accurate"),
                                               "generic window: prod vs inproc (.accurate)")
    if (gp and gi and "error" not in gp and "error" not in gi
            and gp.get("mean") and gi.get("mean")
            and (h["game_content_comparability"] or {}).get("comparable")):
        h["game_content_speedup"] = {
            "prod_ms": gp["mean"], "inproc_ms": gi["mean"],
            "speedup": gp["mean"] / gi["mean"],
            "note": "same OCR (.accurate), same content, same geometry - only the capture path differs",
        }

    return h


def build_vision_summary(cv, vision):
    s = {}
    if vision and "results" in vision:
        fits = [r["name"] for r in vision["results"] if r.get("fits_60hz")]
        nofit = [r["name"] for r in vision["results"] if r.get("fits_60hz") is False]
        s["vision_fits_60hz"] = fits
        s["vision_too_slow_for_60hz"] = nofit
    if cv and "results" in cv:
        by = {r["name"]: r for r in cv["results"] if "error" not in r}
        s["cv_cheap_ops"] = sorted(
            [{"name": n, "mean": r["mean"], "p95": r["p95"]} for n, r in by.items()],
            key=lambda x: x["mean"])[:8]
        s["cv_expensive_ops"] = sorted(
            [{"name": n, "mean": r["mean"], "p95": r["p95"]} for n, r in by.items()],
            key=lambda x: -x["mean"])[:5]
    if vision and "results" in vision:
        by = {r["name"]: r for r in vision["results"] if "error" not in r}
        s["hotloop_primitives"] = [
            {"name": n, "mean": r["mean"], "p50": r["p50"], "p95": r["p95"],
             "fits_60hz": r["fits_60hz"]}
            for n, r in by.items()
        ]
    return s


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="LOADED", help="tag for this run, e.g. LOADED / QUIESCED")
    ap.add_argument("--quick", action="store_true", help="smaller n for a smoke test")
    ap.add_argument("--skip-cv", action="store_true")
    ap.add_argument("--skip-ocr", action="store_true")
    ap.add_argument("--skip-vision", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--capture-iters", type=int, default=20)
    ap.add_argument("--loop-iters", type=int, default=8)
    ap.add_argument("--ocr-iters", type=int, default=8)
    ap.add_argument("--cli-iters", type=int, default=6)
    ap.add_argument("--cv-iters", type=int, default=12)
    ap.add_argument("--vision-iters", type=int, default=15)
    args = ap.parse_args()

    if args.quick:
        args.capture_iters = 5
        args.loop_iters = 3
        args.ocr_iters = 3
        args.cli_iters = 3
        args.cv_iters = 4
        args.vision_iters = 5

    os.makedirs(RESULTS, exist_ok=True)
    load_log = [{"at": "start", **st.load_info()}]

    print("=" * 78)
    print(f"  seer-puni-quest performance benchmark   label={args.label}")
    print("=" * 78)
    print(f"  host   : {st.env_info()['platform']}")
    print(f"  python : {sys.version.split()[0]}")
    l0 = load_log[0]
    print(f"  load   : {l0['loadavg_1m']:.2f} / {l0['ncpu']} cores "
          f"= {l0['load_per_core_1m']:.2f} per core"
          f"{'   *** CONTENDED — numbers provisional ***' if l0['contended'] else ''}")

    build = ensure_binary(force=args.rebuild)
    print(f"  native : {NATIVE_BIN} ({'rebuilt' if build['built'] else 'up to date'})")

    # ---- legs
    cap = leg_capture(args, load_log)
    # Second capture pass against a SYNTHETIC window showing the real game
    # screenshot at real game geometry (1149x714 pt @ (300,120) -> 2298x1428 px).
    # This is the only way to get capture+OCR loop numbers on real game CONTENT
    # while the game is not running. Labelled synthetic everywhere.
    cap_game = leg_capture(
        args, load_log,
        extra=["--image-window", "artifacts/game.png", "--only", GAME_LEG_BACKENDS],
        name="capture_game_content")
    ocr = {} if args.skip_ocr else leg_script(
        "ocr", "bench_ocr.py",
        ["--iters", str(args.ocr_iters), "--cli-iters", str(args.cli_iters)], load_log)
    cv = {} if args.skip_cv else leg_script(
        "cv", "bench_cv.py",
        ["--iters", str(args.cv_iters), "--heavy-iters", str(max(3, args.cv_iters // 2))],
        load_log)
    vision = {} if args.skip_vision else leg_script(
        "vision", "bench_vision.py",
        ["--iters", str(args.vision_iters),
         "--heavy-iters", str(max(3, args.vision_iters // 3))], load_log)

    load_log.append({"at": "end", **st.load_info()})

    headline = build_headline(cap, ocr, cv, vision, cap_game)
    vs = build_vision_summary(cv, vision)

    result = {
        "schema": 1,
        "label": args.label,
        "started_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "env": st.env_info(),
        "args": vars(args),
        "build": build,
        "load_log": load_log,
        "load_summary": {
            "max_loadavg_1m": max(x["loadavg_1m"] for x in load_log),
            "max_load_per_core": max(x["load_per_core_1m"] for x in load_log),
            "contended": any(x["contended"] for x in load_log),
        },
        "headline": headline,
        "vision_summary": vs,
        "legs": {
            "capture": cap,
            "capture_game_content": cap_game,
            "ocr": ocr,
            "cv": cv,
            "vision": vision,
        },
    }

    # Persist FIRST. The expensive measurements are done at this point; a
    # formatting bug in the report must never be able to throw them away
    # (that is exactly what happened on 2026-11-01: print_table raised
    # IndexError on the last step and the whole run's data was lost).
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(RESULTS, f"{stamp}_{args.label}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    latest = os.path.join(RESULTS, "latest.json")
    shutil.copyfile(out_path, latest)
    print(f"\n  results written -> {out_path}", file=sys.stderr)

    try:
        # ---------------------------------------------------------------- report
        print(f"\n{'=' * 78}\n  HEADLINE — capture backends ({args.label} run)\n{'=' * 78}")
        rows = []
        for r in headline["rows"]:
            if "error" in r:
                rows.append([r["label"][:46], "ERR", "-", "-", "-", r["error"][:24]])
            else:
                rows.append([r["label"][:46], r["n"], r["mean"], r["p50"], r["p95"], r["kind"]])
        st.print_table(rows, ["backend", "n", "mean ms", "p50", "p95", "class"],
                       aligns=["l", "r", "r", "r", "r", "l"])

        if headline["ratios"]:
            print("\n  Speed-up vs the current production shot() path:")
            for k, v in headline["ratios"].items():
                print(f"    {k:44s} {v:7.1f}x")

        sm = headline.get("stream_meta") or {}
        if sm:
            print("\n  Persistent SCStream:")
            if sm.get("achieved_fps"):
                print(f"    achieved fps                    {sm['achieved_fps']:.1f}")
            if sm.get("inter_frame_mean"):
                print(f"    inter-frame delta mean/p50/p95  {sm['inter_frame_mean']:.2f} / "
                      f"{sm['inter_frame_p50']:.2f} / {sm['inter_frame_p95']:.2f} ms")
            if sm.get("start_capture_ms"):
                print(f"    startCapture one-time           {sm['start_capture_ms']:.1f} ms")
            if sm.get("static_screen_inter_frame_mean"):
                print(f"    STATIC screen frame interval    "
                      f"{sm['static_screen_inter_frame_mean']:.1f} ms "
                      f"({sm.get('static_screen_frames')} frames) <- change-driven")
            stl = sm.get("read_latest_staleness")
            if stl:
                print(f"    read-latest staleness mean/p95  {stl['mean']:.2f} / {stl['p95']:.2f} ms")
            if sm.get("read_latest_rss_delta_mb") is not None:
                print(f"    RSS delta for stream buffers    {sm['read_latest_rss_delta_mb']:.1f} MB")

        if headline["loops"]:
            print(f"\n{'=' * 78}\n  END-TO-END LOOPS — capture + OCR, measured as one iteration\n{'=' * 78}")
            rows = []
            for r in headline["loops"]:
                if "error" in r:
                    rows.append([r["label"][:44], "ERR", "-", "-", "-", r["error"][:20]])
                else:
                    rows.append([r["label"][:44], r["n"], r["mean"], r["p50"], r["p95"],
                                 r.get("ocr_items_mean")])
            st.print_table(rows, ["loop", "n", "mean ms", "p50", "p95", "ocr items"],
                           aligns=["l", "r", "r", "r", "r", "r"])

        if headline.get("game_content_loops"):
            print(f"\n{'=' * 78}\n  END-TO-END LOOPS ON REAL GAME CONTENT\n"
                  f"  (synthetic window showing artifacts/game.png at 1149x714 pt @ (300,120)"
                  f" -> 2298x1428 px)\n{'=' * 78}")
            rows = []
            for r in headline["game_content_loops"]:
                if "error" in r:
                    rows.append([r["label"][:52], "ERR", "-", "-", "-", r["error"][:18]])
                else:
                    rows.append([r["label"][:52], r["n"], r["mean"], r["p50"], r["p95"],
                                 r.get("ocr_items_mean")])
            st.print_table(rows, ["loop (game content)", "n", "mean ms", "p50", "p95", "ocr items"],
                           aligns=["l", "r", "r", "r", "r", "r"])
            gs = headline.get("game_content_speedup")
            if gs:
                print(f"    capture-path speed-up at identical OCR: {gs['speedup']:.2f}x "
                      f"({gs['prod_ms']:.0f} ms -> {gs['inproc_ms']:.0f} ms)")

        for key, title in (("instrumented_prod", "PRODUCTION LOOP - per-stage, timed inside each iteration"),
                           ("instrumented_inproc", "IN-PROCESS LOOP - per-stage, timed inside each iteration")):
            ins = headline.get(key)
            if not ins:
                continue
            print(f"\n{'=' * 78}\n  {title}\n"
                  f"  total {f(ins['total_ms'])} ms (n={ins['n']}, OCR items {f(ins.get('ocr_items_mean'), 1)})\n{'=' * 78}")
            rows = [[s2["stage"], f(s2["mean_ms"]), f(s2["p50_ms"]), f(s2["p95_ms"]),
                     f(s2["share_pct"], 1)] for s2 in ins["stages"]]
            st.print_table(rows, ["stage", "mean ms", "p50 ms", "p95 ms", "share %"],
                           aligns=["l", "r", "r", "r", "r"])

        if headline["bottleneck_ranking"]:
            print(f"\n{'=' * 78}\n  BOTTLENECK RANKING - current production loop\n"
                  f"  source: {headline.get('bottleneck_source_leg', '?')}\n{'=' * 78}")
            rows = []
            for i, b in enumerate(headline["bottleneck_ranking"], 1):
                rows.append([i, b["stage"][:52], b["mean_ms"], b["share_pct"], b["source"][:30]])
            st.print_table(rows, ["#", "stage", "mean ms", "share %", "source"],
                           aligns=["r", "l", "r", "r", "l"])
            print(f"    total = {headline['bottleneck_total_ms']:.1f} ms per loop iteration")

        if vs.get("vision_fits_60hz") or vs.get("vision_too_slow_for_60hz"):
            print(f"\n{'=' * 78}\n  VISION HOT LOOP — what fits in a 16.7 ms frame\n{'=' * 78}")
            print(f"    fits     : {len(vs.get('vision_fits_60hz', []))}")
            for n in vs.get("vision_fits_60hz", []):
                print(f"       OK  {n}")
            print(f"    too slow : {len(vs.get('vision_too_slow_for_60hz', []))}")
            for n in vs.get("vision_too_slow_for_60hz", []):
                print(f"       NO  {n}")

        print(f"\n  results -> {out_path}")
        print(f"  latest  -> {latest}")
        if result["load_summary"]["contended"]:
            print(f"\n  *** LOADED RUN: peak load {result['load_summary']['max_loadavg_1m']:.1f} "
                  f"on {load_log[0]['ncpu']} cores. Absolute ms are inflated; "
                  f"treat as provisional and prefer the ratios. ***")

    except Exception as exc:  # noqa: BLE001 - never lose a completed run to a print bug
        import traceback
        print("\n  !! report rendering failed, results JSON is already on disk:", file=sys.stderr)
        traceback.print_exc()
    # exit code: 0 only if the core capture leg produced real data
    ok = bool(cap) and "results" in cap and any(
        r.get("n") for r in cap.get("results", []))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
