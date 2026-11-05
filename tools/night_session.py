#!/usr/bin/env python3
"""Night session driver: prove (or disprove) the background input lane, then work.

The user's rule for tonight: from 23:30 the bot must not steal the pointer/focus.
The only way to do that on macOS is to post input events straight to the game
process (``CGEvent.postToPid``) — whether PepperFlash accepts them is an empirical
question, and this script answers it with a visible A/B instead of trusting a
return code (the daemon always answers ``ok``).

    # 1) A/B test only
    PYTHONPATH=.pydeps python3 tools/night_session.py --probe

    # 2) test, then grind in the lane that worked
    PYTHONPATH=.pydeps python3 tools/night_session.py --run --max-battles 40 --max-minutes 45

Everything is logged to ``data/night_session.log`` and a JSON verdict is written to
``data/night_session.json`` so the morning report can quote it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402
from seerauto.live import LiveSession, list_game_windows  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "data", "night_session.log")
RESULT = os.path.join(ROOT, "data", "night_session.json")
BAG_ICON = (1940, 1375)


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def ensure_window() -> bool:
    """Bring the client back if its window was closed — without raising the app."""

    if list_game_windows():
        return True
    log("no game window; relaunching the client with `open -a`")
    subprocess.run(["open", "-a", "/Applications/SeerGame.app"], check=False)
    for _ in range(20):
        time.sleep(2)
        if list_game_windows():
            log(f"window back: {[(w['w'], w['h'], w['aux']) for w in list_game_windows()]}")
            return True
    return False


def panel_open(session: LiveSession) -> bool:
    try:
        return any("等级" in str(i["text"]) for i in session.ocr(min_conf=0.2))
    except Exception:  # noqa: BLE001
        return False


def probe_lane(lane: str) -> dict:
    """Click the pet-bag icon through one lane and report whether the panel opened."""

    session = LiveSession(dry_run=False, enable_input=True,
                          background=(lane == "background")).open()
    try:
        if panel_open(session):
            # make sure we start closed so the click is meaningful
            session.click_px(2072, 312, "close bag")
            time.sleep(1.0)
        before = panel_open(session)
        session.click_px(*BAG_ICON, f"probe via {lane}")
        time.sleep(1.6)
        after = panel_open(session)
        verdict = {"lane": lane, "before": before, "after": after, "worked": after and not before}
        log(f"probe[{lane}] before={before} after={after} -> worked={verdict['worked']}")
        return verdict
    finally:
        session.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="only run the A/B test")
    ap.add_argument("--run", action="store_true", help="run the goal after the test")
    ap.add_argument("--max-battles", type=int, default=40)
    ap.add_argument("--max-minutes", type=float, default=45.0)
    ap.add_argument("--planner", default="deterministic")
    ap.add_argument("--skip-probe", action="store_true")
    args = ap.parse_args()

    log("=" * 60)
    log("night session start")
    if not ensure_window():
        log("ABORT: no game window")
        return 3

    verdicts = {}
    if not args.skip_probe:
        # Foreground first (control), background second (the question).
        for lane in ("foreground", "background"):
            try:
                verdicts[lane] = probe_lane(lane)
            except Exception as exc:  # noqa: BLE001
                verdicts[lane] = {"lane": lane, "error": f"{type(exc).__name__}: {exc}"}
                log(f"probe[{lane}] ERROR {exc}")
            time.sleep(0.8)

    bg_ok = bool((verdicts.get("background") or {}).get("worked"))
    fg_ok = bool((verdicts.get("foreground") or {}).get("worked"))
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "probes": verdicts,
        "background_lane_works": bg_ok,
        "foreground_lane_works": fg_ok,
        "chosen_lane": "background" if bg_ok else ("foreground" if fg_ok else "none"),
    }
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    log(f"verdict: {payload}")

    if args.probe and not args.run:
        return 0 if (bg_ok or fg_ok) else 4
    if not (bg_ok or fg_ok):
        log("ABORT: neither input lane reached the client")
        return 5

    cmd = [sys.executable, "-m", "seerauto", "goal", "start", "puni",
           "--planner", args.planner, "--allow-input",
           "--max-minutes", str(args.max_minutes), "--max-battles", str(args.max_battles)]
    if bg_ok:
        cmd.append("--background")
    log("running: " + " ".join(cmd))
    env = dict(os.environ, PYTHONPATH=".pydeps")
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    log(f"goal run exited {proc.returncode}")
    # Propagate the runner's own outcome code. A caller that only ever sees 0 cannot
    # tell verified progress apart from "planner refused" (PR #1 finding, kept).
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
