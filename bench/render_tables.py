#!/usr/bin/env python3
"""render_tables.py — emit markdown tables straight from a results JSON.

This exists so that docs/PERFORMANCE_BASELINE.md can never contain a number
that was hand-transcribed from a benchmark run. Every table in the report is
pasted verbatim from this tool's output, which reads bench/results/*.json.

    python3 bench/render_tables.py bench/results/latest.json
    python3 bench/render_tables.py bench/results/latest.json --section loops
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats as st  # noqa: E402


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def f(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def section_env(d):
    e = d.get("env", {})
    ls = d.get("load_summary", {})
    ll = d.get("load_log", [])
    first = ll[0] if ll else {}
    rows = [
        ["label", d.get("label", "?")],
        ["started (UTC)", d.get("started_utc", "?")],
        ["platform", e.get("platform", "?")],
        ["machine", e.get("machine", "?")],
        ["python", e.get("python_version", "?")],
        ["ncpu", first.get("ncpu", "?")],
        ["load avg 1m (start)", f(first.get("loadavg_1m"), 2)],
        ["load avg 1m (peak)", f(ls.get("max_loadavg_1m"), 2)],
        ["load per core (peak)", f(ls.get("max_load_per_core"), 2)],
        ["contended", ls.get("contended")],
        ["native binary", d.get("build", {}).get("bin", "?")],
        ["rebuilt this run", d.get("build", {}).get("built")],
    ]
    return md_table(["field", "value"], rows)


def section_headline(d):
    h = d.get("headline", {})
    rows = []
    for r in h.get("rows", []):
        if "error" in r:
            rows.append([r["label"], "ERROR", "—", "—", "—", "—", r["error"][:40]])
        else:
            rows.append([r["label"], r["n"], f(r["mean"]), f(r["p50"]), f(r["p95"]),
                         f(r["min"]), r["kind"]])
    out = [md_table(["backend", "n", "mean ms", "p50 ms", "p95 ms", "min ms", "class"], rows)]

    sm = h.get("stream_meta") or {}
    if sm:
        srows = []
        if sm.get("achieved_fps"):
            srows.append(["achieved fps (animated content)", f(sm["achieved_fps"], 1)])
        if sm.get("inter_frame_mean"):
            srows.append(["inter-frame delta mean", f(sm["inter_frame_mean"]) + " ms"])
            srows.append(["inter-frame delta p50", f(sm["inter_frame_p50"]) + " ms"])
            srows.append(["inter-frame delta p95", f(sm["inter_frame_p95"]) + " ms"])
        if sm.get("start_capture_ms"):
            srows.append(["startCapture() one-time", f(sm["start_capture_ms"], 1) + " ms"])
        if sm.get("frames_delivered"):
            srows.append(["frames delivered", sm["frames_delivered"]])
        if sm.get("static_screen_inter_frame_mean"):
            srows.append(["STATIC screen frame interval",
                          f(sm["static_screen_inter_frame_mean"], 1) + " ms"])
            srows.append(["STATIC screen frames in budget", sm.get("static_screen_frames")])
        stl = sm.get("read_latest_staleness")
        if stl:
            srows.append(["read-latest staleness mean", f(stl.get("mean")) + " ms"])
            srows.append(["read-latest staleness p50", f(stl.get("p50")) + " ms"])
            srows.append(["read-latest staleness p95", f(stl.get("p95")) + " ms"])
            srows.append(["read-latest staleness max", f(stl.get("max")) + " ms"])
        if sm.get("read_latest_frame_bytes"):
            srows.append(["read-latest frame size",
                          f(sm["read_latest_frame_bytes"] / 1024 / 1024) + " MB"])
        if sm.get("read_latest_rss_delta_mb") is not None:
            srows.append(["RSS delta for stream", f(sm["read_latest_rss_delta_mb"], 1) + " MB"])
        if srows:
            out.append("\n" + md_table(["SCStream metric", "value"], srows))

    ratios = h.get("ratios") or {}
    if ratios:
        out.append("\n" + md_table(["comparison", "speed-up"],
                                   [[k, f(v, 1) + "x"] for k, v in ratios.items()]))
    return "\n".join(out)


def section_loops(d):
    h = d.get("headline", {})
    out = []
    for key, title in (("loops", "Generic-window loops"),
                       ("game_content_loops", "Loops on real game content")):
        items = h.get(key) or []
        if not items:
            continue
        rows = []
        for r in items:
            if "error" in r:
                rows.append([r["label"], "ERROR", "—", "—", "—", "—", r["error"][:30]])
            else:
                rows.append([r["label"], r["n"], f(r["mean"]), f(r["p50"]), f(r["p95"]),
                             f(r.get("ocr_items_mean"), 1)])
        out.append(f"\n**{title}**\n")
        out.append(md_table(["loop", "n", "mean ms", "p50 ms", "p95 ms", "OCR items"], rows))
    gs = h.get("game_content_speedup")
    if gs:
        out.append(f"\nCapture-path speed-up at identical OCR: **{f(gs['speedup'])}x** "
                   f"({f(gs['prod_ms'], 0)} ms → {f(gs['inproc_ms'], 0)} ms)")
    return "\n".join(out)


def section_bottleneck(d):
    h = d.get("headline", {})
    rank = h.get("bottleneck_ranking") or []
    if not rank:
        return "_no bottleneck data_"
    rows = [[i, b["stage"], f(b["mean_ms"]), f(b["share_pct"], 1), b["source"]]
            for i, b in enumerate(rank, 1)]
    out = [f"source leg: {h.get('bottleneck_source_leg', '?')}\n",
           md_table(["#", "stage", "mean ms", "share %", "source"], rows),
           f"\nTotal: **{f(h.get('bottleneck_total_ms'))} ms** per production loop iteration."]
    comp = h.get("bottleneck_components") or {}
    if comp:
        crows = [[k, f(v) if isinstance(v, float) else str(v)]
                 for k, v in comp.items() if k != "note"]
        out.append("\n" + md_table(["component", "value"], crows))
    return "\n".join(out)


def section_ocr(d):
    ocr = d.get("legs", {}).get("ocr", {})
    if not ocr or "error" in ocr:
        return "_no OCR data: " + str((ocr or {}).get("error", "missing")) + "_"
    rows = []
    for r in ocr.get("inproc", []):
        if "error" in r:
            rows.append([r["label"], "ERROR", "—", "—", "—", "—", "—", r["error"][:30]])
            continue
        im = r.get("ocr_image") or {}
        rows.append([r["label"], r.get("ocr_n"), f(r.get("ocr_mean")), f(r.get("ocr_p50")),
                     f(r.get("ocr_p95")), r.get("items"),
                     f"{im.get('w')}x{im.get('h')}",
                     f(r.get("cold_first_call_ms"), 1)])
    out = [md_table(["config", "n", "mean ms", "p50 ms", "p95 ms", "items", "image",
                     "cold 1st call ms"], rows)]
    cli = ocr.get("cli") or {}
    if cli:
        crows = [[k, v.get("n"), f(v.get("mean")), f(v.get("p50")), f(v.get("p95")), v.get("items")]
                 for k, v in cli.items()]
        out.append("\n" + md_table(["one-shot CLI (spawn+decode+OCR)", "n", "mean ms", "p50",
                                    "p95", "items"], crows))
    dec = ocr.get("decomposition") or {}
    if dec:
        out.append("\n" + md_table(["decomposition", "ms"],
                                   [[k, f(v) if isinstance(v, float) else str(v)]
                                    for k, v in dec.items() if k != "note"]))
    ratios = ocr.get("ratios") or {}
    if ratios:
        out.append("\n" + md_table(["OCR ratio", "value"],
                                   [[k, f(v) + "x"] for k, v in ratios.items()]))
    return "\n".join(out)


def section_cv(d):
    cv = d.get("legs", {}).get("cv", {})
    if not cv or "error" in cv:
        return "_no CV data: " + str((cv or {}).get("error", "missing")) + "_"
    rows = []
    for r in cv.get("results", []):
        if "error" in r:
            rows.append([r["name"], "ERROR", "—", "—", "—", r.get("note", "")[:40]])
            continue
        rows.append([r["name"], r["n"], f(r["mean"]), f(r["p50"]), f(r["p95"]),
                     r.get("note", "")[:60]])
    return md_table(["operation", "n", "mean ms", "p50 ms", "p95 ms", "note"], rows)


def section_vision(d):
    v = d.get("legs", {}).get("vision", {})
    if not v or "error" in v:
        return "_no vision data: " + str((v or {}).get("error", "missing")) + "_"
    rows = []
    for r in v.get("results", []):
        if "error" in r:
            rows.append([r["name"], "ERROR", "—", "—", "—", "—", r.get("note", "")[:40]])
            continue
        rows.append([r["name"], r["n"], f(r["mean"]), f(r["p50"]), f(r["p95"]),
                     "yes" if r.get("fits_60hz") else "NO", r.get("note", "")[:60]])
    return md_table(["strategy / primitive", "n", "mean ms", "p50 ms", "p95 ms",
                     f"fits {f(v.get('frame_budget_ms_60hz'), 1)} ms", "note"], rows)


def section_raw(d):
    """Every raw backend record, for the appendix."""
    rows = []
    for legname in ("capture", "capture_game_content"):
        leg = d.get("legs", {}).get(legname, {})
        for r in leg.get("results", []):
            if "error" in r:
                rows.append([legname, r["backend"], "ERROR", "—", "—", "—", "—",
                             r["error"][:40]])
            else:
                rows.append([legname, r["backend"], r.get("n"), f(r.get("mean")),
                             f(r.get("p50")), f(r.get("p95")), f(r.get("max")),
                             (r.get("meta", {}).get("note") or "")[:70]])
    return md_table(["leg", "backend", "n", "mean ms", "p50 ms", "p95 ms", "max ms", "note"],
                    rows)


SECTIONS = {
    "env": section_env,
    "headline": section_headline,
    "loops": section_loops,
    "bottleneck": section_bottleneck,
    "ocr": section_ocr,
    "cv": section_cv,
    "vision": section_vision,
    "raw": section_raw,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--section", default="all", choices=["all"] + list(SECTIONS))
    args = ap.parse_args()
    with open(args.json_path, encoding="utf-8") as fh:
        d = json.load(fh)
    names = list(SECTIONS) if args.section == "all" else [args.section]
    for n in names:
        print(f"\n<!-- ===== {n} ===== -->\n")
        print(SECTIONS[n](d))


if __name__ == "__main__":
    main()
