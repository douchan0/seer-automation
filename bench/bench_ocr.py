#!/usr/bin/env python3
"""bench_ocr.py — Vision OCR cost matrix, separating process spawn from OCR.

Three things are measured, all on real game frames:

  1. In-process Vision OCR via `native/bin/bench_capture ocr`, which decodes the
     PNG once outside the timed region and then times ONLY the
     VNImageRequestHandler.perform call. This is the OCR cost a resident daemon
     actually pays.
  2. The same OCR through the existing one-shot CLI `./scripts/seerctl ocr`,
     which additionally pays process spawn + PNG decode on every call. This is
     the cost the current tooling pays.
  3. `./scripts/seerctl pos` — a CLI invocation that does no image work at all,
     used as a direct control for pure process-spawn overhead.

spawn_overhead = CLI_ocr - (in_process_decode + in_process_ocr)

Run with:  python3 bench/bench_ocr.py     (no .pydeps needed)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats as st  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_BIN = os.path.join(REPO, "native", "bin", "bench_capture")
GAME = os.path.join(REPO, "artifacts", "game.png")
BATTLE = os.path.join(REPO, "artifacts", "battle.png")
SEERCTL = os.path.join(REPO, "scripts", "seerctl")

ROI_900x400 = "700,200,900,400"

# (label, file, level, rect-or-None, langs)
MATRIX = [
    ("game_full_accurate_zh+en", GAME, "accurate", None, "zh-Hans,en-US"),
    ("game_full_fast_zh+en", GAME, "fast", None, "zh-Hans,en-US"),
    ("game_full_accurate_zh", GAME, "accurate", None, "zh-Hans"),
    ("game_full_fast_zh", GAME, "fast", None, "zh-Hans"),
    ("game_roi900x400_accurate_zh+en", GAME, "accurate", ROI_900x400, "zh-Hans,en-US"),
    ("game_roi900x400_fast_zh+en", GAME, "fast", ROI_900x400, "zh-Hans,en-US"),
    ("battle_full_accurate_zh+en", BATTLE, "accurate", None, "zh-Hans,en-US"),
    ("battle_full_fast_zh+en", BATTLE, "fast", None, "zh-Hans,en-US"),
    ("battle_roi_accurate_zh+en", BATTLE, "accurate", "100,150,600,300", "zh-Hans,en-US"),
]


def run_inproc(file, level, rect, langs, iters, warmup=1):
    cmd = [BENCH_BIN, "ocr", "--file", file, "--level", level,
           "--langs", langs, "--iters", str(iters), "--warmup", str(warmup)]
    if rect:
        cmd += ["--rect", rect]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"error": f"bench_capture ocr failed rc={proc.returncode}",
                "stderr": proc.stderr[-2000:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"bad json: {exc}", "stdout": proc.stdout[:500]}


def time_cli(cmd, iters, warmup=1):
    """Wall-clock the whole CLI process, including spawn + decode + OCR."""
    samples = []
    last_stdout = ""
    for _ in range(warmup):
        subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        samples.append((time.perf_counter_ns() - t0) / 1e6)
        last_stdout = proc.stdout
    return samples, last_stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=8, help="in-process OCR iterations")
    ap.add_argument("--cli-iters", type=int, default=6, help="one-shot CLI iterations")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    load_start = st.load_info()
    out = {
        "mode": "ocr",
        "load_start": load_start,
        "env": st.env_info(),
        "iters": args.iters,
        "cli_iters": args.cli_iters,
        "bin": BENCH_BIN,
        "inproc": [],
        "cli": {},
    }

    # ---------- 1. in-process Vision OCR matrix ----------
    for label, file, level, rect, langs in MATRIX:
        rec = run_inproc(file, level, rect, langs, args.iters)
        rec["label"] = label
        rec["level"] = level
        rec["rect"] = rect
        rec["langs"] = langs
        rec["file"] = file
        out["inproc"].append(rec)
        if not args.json:
            if "error" in rec:
                print(f"  [FAIL] {label}: {rec['error']}", file=sys.stderr)
            else:
                print(f"  [ok] {label}: n={rec.get('ocr_n')} "
                      f"mean={rec.get('ocr_mean', float('nan')):.1f}ms "
                      f"items={rec.get('items')}", file=sys.stderr)

    # ---------- 2. one-shot CLI baseline (spawn + decode + OCR) ----------
    if os.path.exists(SEERCTL):
        samples, stdout = time_cli([SEERCTL, "ocr", GAME, "0.3"], args.cli_iters)
        try:
            items = len(json.loads(stdout)) if stdout.strip() else None
        except json.JSONDecodeError:
            items = None
        rec = {
            "label": "seerctl_cli_ocr_game_full_accurate_zh+en",
            "cmd": [SEERCTL, "ocr", GAME, "0.3"],
            "note": "existing one-shot CLI: process spawn + PNG decode + Vision .accurate",
            "items": items,
            "samples_ms": samples,
        }
        rec.update(st.summarize(samples))
        out["cli"]["seerctl_ocr_game"] = rec
        if not args.json:
            print(f"  [ok] seerctl ocr game.png: n={rec['n']} mean={rec['mean']:.1f}ms "
                  f"items={items}", file=sys.stderr)

        # pure spawn control: a CLI call that does no image work
        spawn_samples, _ = time_cli([SEERCTL, "pos"], max(4, args.cli_iters))
        srec = {
            "label": "seerctl_cli_pos_spawn_control",
            "cmd": [SEERCTL, "pos"],
            "note": "control: CLI invocation that does no image work — isolates process spawn + binary init",
            "samples_ms": spawn_samples,
        }
        srec.update(st.summarize(spawn_samples))
        out["cli"]["seerctl_pos"] = srec
        if not args.json:
            print(f"  [ok] seerctl pos (spawn control): n={srec['n']} mean={srec['mean']:.1f}ms",
                  file=sys.stderr)

    # ---------- 3. decomposition ----------
    # in-process full-frame accurate zh+en: decode + ocr
    base = next((r for r in out["inproc"] if r.get("label") == "game_full_accurate_zh+en"), None)
    cli = out["cli"].get("seerctl_ocr_game")
    spawn = out["cli"].get("seerctl_pos")
    decomp = {}
    if base and "error" not in base and cli and spawn:
        decode = base.get("decode_ms")
        ocr = base.get("ocr_mean")
        decomp = {
            "cli_total_mean_ms": cli["mean"],
            "spawn_control_mean_ms": spawn["mean"],
            "inproc_decode_ms": decode,
            "inproc_ocr_mean_ms": ocr,
            "decode_plus_ocr_ms": (decode or 0) + (ocr or 0),
            "spawn_overhead_derived_ms": cli["mean"] - ((decode or 0) + (ocr or 0)),
            "spawn_control_ratio_of_cli": st.ratio(spawn["mean"], cli["mean"]),
            "ocr_share_of_cli": st.ratio(ocr, cli["mean"]),
            "note": ("spawn_overhead_derived subtracts in-process decode+OCR from the "
                     "measured CLI total; spawn_control is the independent control"),
        }
    out["decomposition"] = decomp

    # ---------- 4. ratios that survive a loaded machine ----------
    by = {r.get("label"): r for r in out["inproc"] if "error" not in r}
    ratios = {}
    pairs = [
        ("game_full_accurate_zh+en", "game_full_fast_zh+en"),
        ("game_full_accurate_zh+en", "game_roi900x400_accurate_zh+en"),
        ("game_full_accurate_zh+en", "game_full_accurate_zh"),
        ("battle_full_accurate_zh+en", "battle_full_fast_zh+en"),
    ]
    for a, b in pairs:
        if a in by and b in by and by[b].get("ocr_mean"):
            ratios[f"{a}_over_{b}"] = st.ratio(by[a]["ocr_mean"], by[b]["ocr_mean"])
    out["ratios"] = ratios
    out["load_end"] = st.load_info()

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"\nVision OCR — in-process (timed around VNImageRequestHandler.perform only)")
    print(f"load avg {load_start['loadavg_1m']:.2f} on {load_start['ncpu']} cores")
    rows = []
    for r in out["inproc"]:
        if "error" in r:
            rows.append([r["label"], "ERR", "-", "-", "-", "-", "-", r["error"][:30]])
        else:
            im = r.get("ocr_image") or {}
            rows.append([r["label"], r.get("ocr_n", 0), r.get("ocr_mean"), r.get("ocr_p50"),
                         r.get("ocr_p95"), r.get("items", 0),
                         f"{im.get('w')}x{im.get('h')}", ""])
    st.print_table(rows, ["config", "n", "mean ms", "p50", "p95", "items", "image", ""],
                   aligns=["l", "r", "r", "r", "r", "r", "l", "l"])

    if out["cli"]:
        print("\nOne-shot CLI (includes process spawn + PNG decode)")
        rows = [[k, v.get("n", 0), v.get("mean"), v.get("p50"), v.get("p95"), v.get("items")]
                for k, v in out["cli"].items()]
        st.print_table(rows, ["cli", "n", "mean ms", "p50", "p95", "items"],
                       aligns=["l", "r", "r", "r", "r", "r"])

    if decomp:
        print("\nDecomposition (why the CLI is slow)")
        for k, v in decomp.items():
            if k == "note":
                continue
            print(f"  {k:34s} {v if isinstance(v, str) else round(v, 2)}")
    if ratios:
        print("\nRatios (load-robust)")
        for k, v in ratios.items():
            print(f"  {k:60s} {v:.2f}x")


if __name__ == "__main__":
    main()
