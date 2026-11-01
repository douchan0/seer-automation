#!/usr/bin/env python3
"""Benchmark the native SCK daemon against the subprocess screencapture path.

Read-only: this script never clicks, types or focuses anything. It only asks the
game window what it looks like.

Usage:
    PYTHONPATH=.pydeps python3 bench/bench_daemon.py [--iters N] [--target NAME]

The interesting number is not "how fast is a screenshot" but "how long does the
engine wait to learn what is on screen", so the loop-level figures (Python
round trip through DaemonCapture.latest()) are reported first.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402


def pct(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def row(label, xs, n_note=""):
    if not xs:
        print(f"{label:<52} NO DATA")
        return
    print(
        f"{label:<52} n={len(xs):<4} mean={statistics.mean(xs):8.2f} "
        f"p50={pct(xs, 0.5):8.2f} p95={pct(xs, 0.95):8.2f} min={min(xs):8.2f} {n_note}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=60)
    ap.add_argument("--target", default=None, help="window owner substring (default: game)")
    ap.add_argument("--json", default=None, help="write results here")
    args = ap.parse_args()

    binary = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "native", "bin", "seerdx")
    if not os.path.exists(binary):
        print(f"daemon not built: {binary}\n  build with: tools/build_seerdx.sh")
        return 2

    daemon_args = ["--target", args.target] if args.target else []
    client = C.DaemonClient(binary, args=daemon_args, timeout_s=20.0)

    load = os.getloadavg()
    print(f"load avg: {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}  cpus: {os.cpu_count()}")
    print(f"daemon:   {binary} {' '.join(daemon_args)}")

    win = client.win()
    geo = G.WindowGeometry(
        origin_pt=G.Point(float(win["x"]), float(win["y"])),
        size_pt=G.Size(float(win["w"]), float(win["h"])),
        scale=G.RETINA_SCALE,
    )
    print(f"window:   id={win['id']} {win['w']}x{win['h']} pt @ ({win['x']},{win['y']})"
          f" -> {geo.pixel_size.w:.0f}x{geo.pixel_size.h:.0f} px")
    print()

    results = {"loadavg": list(load), "cpus": os.cpu_count(), "window": win,
               "daemon_args": daemon_args, "iters": args.iters}

    # ---- 1. warm session startup ---------------------------------------- #
    t0 = time.perf_counter()
    try:
        backend = C.DaemonCapture(client, geo)
    except C.CaptureUnavailable as exc:
        print(f"capture.start failed: {exc}")
        client.close()
        return 1
    startup_ms = (time.perf_counter() - t0) * 1000
    print(f"[1] warm session start (capture.start): {startup_ms:.1f} ms, warm={backend.warm}")
    results["session_start_ms"] = startup_ms

    if not backend.warm:
        print("    daemon could not open a warm stream; only one-shot grab is available")

    # ---- 2. engine-level latency: DaemonCapture.latest() ----------------- #
    if backend.warm:
        backend.latest()  # prime the decode cache
        lat, decoded, cached, seqs = [], [], [], set()
        for _ in range(args.iters):
            t0 = time.perf_counter()
            f = backend.latest()
            dt = (time.perf_counter() - t0) * 1000
            lat.append(dt)
            if f.meta.get("decode") == "cached":
                cached.append(dt)
            else:
                decoded.append(dt)
            seqs.add(f.seq)
        row("[2] engine latest()  (all polls)", lat)
        row("    latest() hitting decode cache", cached)
        row("    latest() doing a real decode", decoded)
        print(f"    distinct frames seen: {len(seqs)} over {args.iters} polls")
        results["latest_all_ms"] = lat
        results["latest_cached_ms"] = cached
        results["latest_decoded_ms"] = decoded
        results["distinct_frames"] = len(seqs)
        print()

        # ---- 3. roi path ------------------------------------------------- #
        roi = G.Rect(0, 0, 400, 300)
        roi_lat = []
        for _ in range(args.iters):
            t0 = time.perf_counter()
            backend.grab(roi)
            roi_lat.append((time.perf_counter() - t0) * 1000)
        row("[3] engine grab(ROI 400x300)", roi_lat, "(daemon crops natively)")
        results["roi_ms"] = roi_lat
        print()

    # ---- 4. old path: subprocess screencapture --------------------------- #
    sub = C.SubprocessScreencapture(geo)
    sub_lat = []
    for _ in range(min(args.iters, 20)):
        t0 = time.perf_counter()
        sub.grab()
        sub_lat.append((time.perf_counter() - t0) * 1000)
    row("[4] OLD SubprocessScreencapture.grab()", sub_lat, "(= scripts/seerd.shot path)")
    results["subprocess_ms"] = sub_lat
    print()

    # ---- summary --------------------------------------------------------- #
    if results.get("latest_all_ms"):
        old = statistics.mean(sub_lat)
        new = statistics.mean(results["latest_all_ms"])
        print(f"engine 'learn what is on screen':  {old:8.2f} ms  ->  {new:8.2f} ms   "
              f"({old / new:.1f}x)")
        if results["latest_cached_ms"]:
            print(f"  static screen (cache hit):       {statistics.mean(results['latest_cached_ms']):8.2f} ms")
        results["speedup_engine"] = old / new

    backend.close()
    client.close()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
