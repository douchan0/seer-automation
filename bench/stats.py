"""Shared statistics + environment helpers for the benchmark suite.

Everything here is pure stdlib so it works without .pydeps.
"""
from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------- statistics

def percentile(sorted_xs, p):
    """Linear-interpolated percentile (same definition as the Swift side)."""
    if not sorted_xs:
        return float("nan")
    if len(sorted_xs) == 1:
        return float(sorted_xs[0])
    idx = p * (len(sorted_xs) - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    frac = idx - lo
    return sorted_xs[lo] * (1.0 - frac) + sorted_xs[hi] * frac


def summarize(xs):
    """n / mean / p50 / p95 / min / max, matching native/bench_capture.swift."""
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    return {
        "n": len(xs),
        "mean": sum(xs) / len(xs),
        "p50": percentile(s, 0.50),
        "p95": percentile(s, 0.95),
        "min": s[0],
        "max": s[-1],
    }


def ratio(a, b):
    """a/b, guarding against zero/None."""
    try:
        if not b:
            return None
        return a / b
    except (TypeError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------- environment

def load_info():
    """Load average + core count. A latency number without load context is not
    reproducible, so every run records this."""
    try:
        la1, la5, la15 = os.getloadavg()
    except OSError:
        la1 = la5 = la15 = float("nan")
    ncpu = os.cpu_count() or 1
    return {
        "loadavg_1m": la1,
        "loadavg_5m": la5,
        "loadavg_15m": la15,
        "ncpu": ncpu,
        "load_per_core_1m": la1 / ncpu,
        # anything above ~1.0/core means the machine is contended
        "contended": bool(la1 / ncpu > 1.0),
    }


def env_info():
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timestamp_local": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def run_json(cmd, cwd=None, timeout=1800):
    """Run a command, return (parsed_json_or_None, stderr_text, returncode)."""
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    out = None
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = None
    if isinstance(out, dict):
        out.setdefault("_harness", {})
        out["_harness"]["wall_ms"] = wall_ms
        out["_harness"]["cmd"] = cmd
        out["_harness"]["returncode"] = proc.returncode
    return out, proc.stderr, proc.returncode


# ---------------------------------------------------------------- formatting

def fmt(v, nd=1, width=8):
    if v is None:
        return f"{'-':>{width}}"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return f"{'-':>{width}}"
    if isinstance(v, float):
        return f"{v:>{width}.{nd}f}"
    return f"{v:>{width}}"


def print_table(rows, headers, aligns=None):
    """rows: list of tuples/lists of already-formatted-or-raw values."""
    # Defensive: rows may have fewer or more cells than headers. Padding and
    # truncating here means a table-formatting mistake can never crash a run
    # after the expensive measurements have already completed.
    ncol = len(headers)
    widths = [len(h) for h in headers]
    cells = []
    for r in rows:
        r = list(r)
        if len(r) < ncol:
            r = r + [""] * (ncol - len(r))
        elif len(r) > ncol:
            r = r[:ncol]
        rc = []
        for i, v in enumerate(r):
            s = v if isinstance(v, str) else fmt(v)
            rc.append(s)
            widths[i] = max(widths[i], len(s))
        cells.append(rc)
    aligns = aligns or ["l"] + ["r"] * (len(headers) - 1)
    sep = "+".join("-" * (w + 2) for w in widths)
    sep = "+" + sep + "+"

    def line(vals):
        out = []
        for i, v in enumerate(vals):
            if aligns[i] == "l":
                out.append(" " + v.ljust(widths[i]) + " ")
            else:
                out.append(" " + v.rjust(widths[i]) + " ")
        return "|" + "|".join(out) + "|"

    print(sep)
    print(line(list(headers)))
    print(sep.replace("-", "="))
    for rc in cells:
        print(line(rc))
    print(sep)
