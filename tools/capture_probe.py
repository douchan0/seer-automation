#!/usr/bin/env python3
"""Read-only capture probe: is this frame from the daemon, and is it *content*?

Written for the 2026-11-07 background-capture bug: `tower_grind` under `nohup`
reported a constant dark frame `mean_rgb=(61,107,150)` while a foreground
`LiveSession.frame_bgr()` showed the normal tower picture.  This tool prints, per
frame, everything needed to tell a real frame from a placeholder:

    backend, warm-session flag, source file, size, seq, age_ms,
    mean_rgb, per-channel std, and `uniform` (placeholder suspicion)

It never clicks, never moves the pointer, never sends input: `LiveSession` is opened
with ``dry_run=True, enable_input=False``.  It DOES open one warm SCK stream, so do
not run it at the same time as another automation process on the same window unless
you are deliberately reproducing the contention (that contention is itself the bug:
a second warm stream on one window can deliver one solid frame and then stall).

    # foreground
    PYTHONPATH=.pydeps python3 tools/capture_probe.py --frames 3

    # background (the failing case): the log must show backend=daemon and a real picture
    nohup env PYTHONPATH=.pydeps python3 tools/capture_probe.py --frames 3 \
        > /tmp/capture_probe_bg.log 2>&1 < /dev/null & disown; sleep 6; cat /tmp/capture_probe_bg.log

Exit codes: 0 all frames non-uniform, 5 placeholder/unavailable, 1 other error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto.errors import CaptureUnavailable, WindowNotFound  # noqa: E402


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only daemon capture probe")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--backend", default="daemon",
                    choices=["daemon", "screencapture"])
    ap.add_argument("--binary", default=None)
    args = ap.parse_args(argv)

    try:
        from seerauto.live import LiveSession
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": f"cannot import LiveSession: {exc}",
              "kind": "ImportError"})
        return 1

    session = LiveSession(binary=args.binary, dry_run=True, enable_input=False)
    try:
        session.open()
        backend = session.backend
        emit({"kind": "session", "backend": getattr(backend, "name", "?"),
              "warm": bool(getattr(backend, "warm", False)),
              "daemon_binary": getattr(session.client, "describe", lambda: "?")(
              ) if session.client else None,
              "window": dict(session.window_raw),
              "pixel_size": list(session.pixel_size)})
    except (CaptureUnavailable, WindowNotFound) as exc:
        emit({"ok": False, "kind": type(exc).__name__, "error": str(exc),
              "hint": ("daemon unavailable / no game window. If this is the "
                       "background process, check that the daemon binary is "
                       "executable and that the game window is on screen.")})
        return 5 if isinstance(exc, CaptureUnavailable) else 1
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "kind": type(exc).__name__, "error": str(exc)})
        return 1

    code = 0
    try:
        for index in range(max(1, args.frames)):
            t0 = time.perf_counter()
            try:
                frame = session.backend.latest()
            except CaptureUnavailable as exc:
                emit({"ok": False, "kind": "CaptureUnavailable", "frame": index,
                      "error": str(exc),
                      "hint": "this is the placeholder/stall case: the daemon's "
                              "warm stream is not delivering the game picture. Do "
                              "not treat the frame as real."})
                code = 5
                break
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            stats = C.frame_stats(frame.array)
            emit({
                "ok": not stats["uniform"],
                "frame": index,
                "backend": frame.meta.get("backend"),
                "warm": frame.meta.get("warm_session"),
                "from_daemon": frame.meta.get("backend") == "daemon",
                "source_path": frame.meta.get("source_path"),
                "seq": frame.seq,
                "age_ms": round(frame.age_ms(), 1),
                "size": [frame.width, frame.height],
                "mean_rgb": stats["mean_rgb"],
                "std": stats["std"],
                "uniform": stats["uniform"],
                "read_ms": round(elapsed_ms, 1),
            })
            if index + 1 < args.frames:
                time.sleep(max(0.0, args.interval))
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "kind": type(exc).__name__, "error": str(exc)})
        code = 1
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
