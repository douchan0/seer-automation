#!/usr/bin/env python3
"""Background-input probe: can the client be driven *without* taking the desktop?

The overnight run must not fight the person using the Mac.  The experimental lane
(`LiveSession(background=True)` -> `clickpid` -> `CGEvent.postToPid`) is supposed to
touch only the game process.  This probe is the only honest way to find out whether
that lane reaches the Flash/PepperFlash playfield, because a `{"ok": true}` reply
from the daemon proves nothing about whether the game received the click.

Probe contract (docs: AGENTS.md 5.6):

    reversible      -> open the SPT panel, then close it again; nothing is spent
    state change    -> the SPT panel text must appear, then disappear
    no focus change -> the frontmost app is identical before and after
    no cursor move  -> the real pointer coordinate is identical before and after
    no stray keys   -> the probe never sends key/type events at all

All five must hold for `BACKGROUND_INPUT_LIVE_PASS`.  Anything else is
`BACKGROUND_INPUT_BLOCKED`, and the caller must stop driving the game rather than
silently fall back to foreground HID input.

    PYTHONPATH=.pydeps python3 tools/bg_probe.py
    PYTHONPATH=.pydeps python3 tools/bg_probe.py --json data/overnight/bg_probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.live import LiveSession  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAME_DIR = os.path.join(ROOT, "data", "overnight", "frames")

#: Measured on the 1200x800 client (docs/state.md, Round 5).  Both are game_px.
SPT_BADGE = (2151.0, 347.0)
SPT_CLOSE = (2095.0, 346.0)
#: Text that only exists once the SPT panel is actually on screen.
SPT_MARKERS = ("SPT", "帕诺", "卡兰", "太空站左翼", "勇者之塔")
#: Text that belongs to the world HUD and survives the panel opening; used to prove
#: the panel closed again instead of the whole client dying.
WORLD_MARKERS = ("公屏聊天", "卫兵", "当前回合", "综合待命中")


def frontmost() -> dict:
    """Frontmost app, read without the Automation permission osascript needs."""

    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                             timeout=5).stdout.strip().split()[0]
        info = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                              capture_output=True, text=True, timeout=5).stdout
        name = ""
        for part in info.split('"'):
            if part and "=" not in part and not part.startswith("ASN"):
                name = part if not name else name
        if "=" in info:
            name = info.split("=", 1)[1].strip().strip('"')
        return {"asn": asn, "name": name}
    except Exception as exc:  # noqa: BLE001 - reporting, never fatal
        return {"error": str(exc)}


def cursor(session: LiveSession) -> dict:
    return session.client.send("cursorpos") or {}


def texts(session: LiveSession) -> list[str]:
    try:
        return [str(i["text"]) for i in session.ocr(min_conf=0.25)]
    except Exception:  # noqa: BLE001
        return []


def has(items: list[str], markers) -> bool:
    joined = " ".join(items)
    return any(m in joined for m in markers)


def shot(session: LiveSession, tag: str) -> str:
    os.makedirs(FRAME_DIR, exist_ok=True)
    path = os.path.join(FRAME_DIR, f"bgprobe_{tag}.png")
    session.client.send("shot", path=path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(ROOT, "data", "overnight",
                                                   "bg_probe.json"))
    args = ap.parse_args()
    evidence: dict = {"started_at": datetime.now().astimezone().isoformat(
        timespec="seconds"), "checks": {}, "frames": {}}

    session = LiveSession(dry_run=False, enable_input=True, background=True).open()
    try:
        # 1. baseline: what does the person see, and where is their pointer?
        before_front = frontmost()
        before_cursor = cursor(session)
        before_texts = texts(session)
        evidence["before"] = {"front": before_front, "cursor": before_cursor,
                              "texts": before_texts,
                              "frame": shot(session, "01_before")}
        evidence["checks"]["world_before"] = has(before_texts, WORLD_MARKERS)
        evidence["checks"]["spt_absent_before"] = not has(before_texts, SPT_MARKERS)

        # 2. the one reversible action under test
        session.click_px(*SPT_BADGE, "bg probe: open SPT panel")
        time.sleep(2.0)
        mid_texts = texts(session)
        evidence["mid"] = {"texts": mid_texts, "frame": shot(session, "02_panel")}
        evidence["checks"]["spt_opened"] = has(mid_texts, SPT_MARKERS)

        # 3. undo, so the client is left exactly as it was found
        session.click_px(*SPT_CLOSE, "bg probe: close SPT panel")
        time.sleep(1.8)
        after_texts = texts(session)
        evidence["after"] = {"texts": after_texts, "frame": shot(session, "03_after")}
        evidence["checks"]["spt_closed_again"] = (
            not has(after_texts, SPT_MARKERS) and has(after_texts, WORLD_MARKERS))

        # 4. the two invariants that make this lane acceptable at all
        after_front = frontmost()
        after_cursor = cursor(session)
        evidence["after"]["front"] = after_front
        evidence["after"]["cursor"] = after_cursor
        evidence["checks"]["frontmost_unchanged"] = (
            before_front.get("asn") == after_front.get("asn")
            and before_front.get("name") == after_front.get("name"))
        evidence["checks"]["cursor_unchanged"] = (
            abs(float(before_cursor.get("x", -1)) - float(after_cursor.get("x", -2))) < 1.0
            and abs(float(before_cursor.get("y", -1)) - float(after_cursor.get("y", -2))) < 1.0)
        evidence["checks"]["no_key_events_sent"] = True
    finally:
        session.close()

    checks = evidence["checks"]
    passed = all([
        checks.get("world_before"),
        checks.get("spt_absent_before"),
        checks.get("spt_opened"),
        checks.get("spt_closed_again"),
        checks.get("frontmost_unchanged"),
        checks.get("cursor_unchanged"),
    ])
    evidence["verdict"] = "BACKGROUND_INPUT_LIVE_PASS" if passed else "BACKGROUND_INPUT_BLOCKED"
    evidence["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"verdict": evidence["verdict"], "checks": checks,
                      "json": args.json}, ensure_ascii=False, indent=1))
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
