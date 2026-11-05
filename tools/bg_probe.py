#!/usr/bin/env python3
"""Background-input probe: can the client be driven *without* taking the desktop?

The overnight run must not fight the person using the Mac.  The experimental lane
(`LiveSession(background=True)` -> `clickpid` -> `CGEvent.postToPid`) is supposed to
touch only the game process, and a `{"ok": true}` reply from the daemon proves nothing
about whether the game received the click.

An independent review found the first version of this probe guilty of exactly the
mistake it was written to prevent — a verdict stronger than its evidence:

  * `frontmost_unchanged` compared two values that were both `None` when `lsappinfo`
    failed, so the check passed while measuring nothing;
  * every non-world precondition (client on the server list, wrong window size, dropped
    OCR) was reported as `BACKGROUND_INPUT_BLOCKED`, which claims something specific
    about PepperFlash that those situations do not establish;
  * the "foreground arm" was a screenshot taken by hand, in no JSON, from no run.

So the probe now runs **both arms through the same code** and reports three states:

    BACKGROUND_INPUT_LIVE_PASS   the PID click did open the panel
    BACKGROUND_INPUT_BLOCKED     the PID click did NOT, while the same coordinates
                                 through the foreground lane DID — the only evidence
                                 that justifies the word "blocked"
    PROBE_INCONCLUSIVE           anything else: not in the world, no panel, no A/B.
                                 Says nothing about the input lane.

    PYTHONPATH=.pydeps python3 tools/bg_probe.py            # both arms
    PYTHONPATH=.pydeps python3 tools/bg_probe.py --lane background --check

Exit codes: 0 PASS, 3 BLOCKED, 4 INCONCLUSIVE.
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

#: Measured on the 1200x800 client (docs/state.md).  Both are game_px.
SPT_BADGE = (2151.0, 347.0)
SPT_CLOSE = (2095.0, 346.0)
#: Text that only the opened panel contains.  Verified live 2026-11-05: the panel OCRs
#: 「星际挑战终端 / SPT 挑战 / 共73个入口 / 帕诺星系 / 卡兰星系 / 太空站左翼 / 蘑菇怪 /
#: 击败后解锁交易行资格」.  星系 names alone are NOT markers: they appear on the map and
#: in chat too, so they would both miss a real panel and fake one.
SPT_MARKERS = ("星际挑战终端", "SPT 挑战", "73个", "太空站左翼", "击败后解锁交易行资格")
#: Arrival markers that the server-list screen does not carry.  「公屏聊天」／
#: 「综合待命中」 are visible behind the server panel, so they cannot be used here.
WORLD_MARKERS = ("卫兵", "已连接公屏聊天")


def frontmost() -> dict:
    """Frontmost app, read without the Automation permission `osascript` needs."""

    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                             timeout=5).stdout.strip().split()[0]
        info = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                              capture_output=True, text=True, timeout=5).stdout
        name = info.split("=", 1)[1].strip().strip('"') if "=" in info else ""
        return {"asn": asn or None, "name": name or None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


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
    try:
        session.client.send("shot", path=path)
        return path
    except Exception as exc:  # noqa: BLE001
        return f"<shot failed: {exc}>"


def run_arm(lane: str, *, check_only: bool) -> dict:
    """Open the SPT panel through one input lane and record what happened.

    The foreground arm *must* raise the window (that is what "foreground" means, and
    `LiveSession.click_px` refuses with NotFrontmost otherwise).  The background arm
    must not: raising the window there would invalidate the very invariant it measures.
    """

    background = lane == "background"
    session = LiveSession(dry_run=False, enable_input=True, background=background).open()
    arm: dict = {"lane": lane}
    try:
        if not background and not check_only:
            session.client.send("activate")
            time.sleep(0.8)
            arm["activated"] = True
        arm["front_before"] = frontmost()
        arm["cursor_before"] = session.client.send("cursorpos") or {}
        arm["texts_before"] = texts(session)
        arm["frame_before"] = shot(session, f"{lane}_01_before")
        arm["world_before"] = has(arm["texts_before"], WORLD_MARKERS)
        arm["spt_absent_before"] = not has(arm["texts_before"], SPT_MARKERS)
        if check_only:
            return arm
        try:
            session.click_px(*SPT_BADGE, f"{lane} probe: open SPT panel")
            arm["click_error"] = ""
        except Exception as exc:  # noqa: BLE001
            arm["click_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(2.0)
        arm["texts_mid"] = texts(session)
        arm["frame_mid"] = shot(session, f"{lane}_02_panel")
        arm["spt_opened"] = has(arm["texts_mid"], SPT_MARKERS)
        arm["unexpected_change"] = sorted(
            set(arm["texts_mid"]) - set(arm["texts_before"]) - set(SPT_MARKERS))
        # put the client back exactly as found
        closed = not arm["spt_opened"]
        if arm["spt_opened"]:
            try:
                session.click_px(*SPT_CLOSE, f"{lane} probe: close SPT panel")
                time.sleep(1.8)
                arm["texts_after"] = texts(session)
                arm["frame_after"] = shot(session, f"{lane}_03_after")
                closed = not has(arm["texts_after"], SPT_MARKERS)
            except Exception as exc:  # noqa: BLE001
                arm["close_error"] = f"{type(exc).__name__}: {exc}"
        arm["panel_closed"] = closed
        # Dirty = the probe changed the screen and could not undo it.  A panel that
        # opened and stayed open is exactly that, and the next live run would then
        # start from a state this tool created.
        arm["left_dirty"] = bool(arm["spt_opened"]) and not closed

        arm["front_after"] = frontmost()
        arm["cursor_after"] = session.client.send("cursorpos") or {}
        # Measure first, compare second: two missing readings are not "unchanged".
        arm["front_measured"] = bool(arm["front_before"].get("asn")
                                     and arm["front_after"].get("asn"))
        arm["frontmost_unchanged"] = (
            arm["front_measured"]
            and arm["front_before"].get("asn") == arm["front_after"].get("asn"))
        cb, ca = arm["cursor_before"], arm["cursor_after"]
        arm["cursor_measured"] = cb.get("x") is not None and ca.get("x") is not None
        arm["cursor_unchanged"] = (
            arm["cursor_measured"]
            and abs(float(cb["x"]) - float(ca["x"])) < 1.0
            and abs(float(cb.get("y", 0)) - float(ca.get("y", 0))) < 1.0)
        return arm
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def verdict_for(arms: dict) -> tuple[str, list[str], str]:
    """Three states, and the words have to be earned."""

    bg, fg = arms.get("background") or {}, arms.get("foreground") or {}
    failed: list[str] = []
    if not bg:
        return ("PROBE_INCONCLUSIVE", ["no background arm"], "nothing was measured")
    if "spt_opened" not in bg:
        return ("PROBE_INCONCLUSIVE", ["no click was performed (--check)"],
                "observation only; nothing was tested")
    if not bg.get("world_before") or not bg.get("spt_absent_before"):
        return ("PROBE_INCONCLUSIVE", ["not in the world with the panel closed"],
                "the probe needs a clean world screen before it can attribute a change "
                "to the input lane")
    if bg.get("click_error"):
        return ("PROBE_INCONCLUSIVE", [f"click refused: {bg['click_error']}"],
                "the background click never left the process")
    if bg.get("spt_opened"):
        return ("BACKGROUND_INPUT_LIVE_PASS", [], "the PID click opened the panel")
    if bg.get("left_dirty"):
        return ("PROBE_INCONCLUSIVE", ["panel state unknown after the probe"],
                "cannot tell whether the click landed because the screen was left dirty")
    # Background did nothing.  That is only evidence if the same coordinates *do* work.
    if not fg:
        return ("PROBE_INCONCLUSIVE", ["no foreground control arm"],
                "a negative result without a positive control proves nothing")
    if not fg.get("world_before") or not fg.get("spt_absent_before"):
        return ("PROBE_INCONCLUSIVE", ["control arm did not start from a clean screen"],
                "the control arm did not begin from the same state as the test arm")
    if "spt_opened" not in fg or not fg.get("spt_opened"):
        return ("PROBE_INCONCLUSIVE", ["foreground click also failed"],
                "the coordinates themselves are wrong, not the input lane")
    if not fg.get("panel_closed"):
        failed.append("control arm left the panel open")
    return ("BACKGROUND_INPUT_BLOCKED", failed,
            "identical coordinates: the PID lane did nothing, the HID lane opened the "
            "panel. The lane is safe (no focus, no cursor) but cannot reach this "
            "client's PepperFlash canvas.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", choices=["background", "foreground", "both"], default="both")
    ap.add_argument("--check", action="store_true", help="observe only, never click")
    ap.add_argument("--json", default=None,
                    help="defaults to bg_probe.json, or bg_probe.check.json with --check")
    args = ap.parse_args()
    if args.json is None:
        name = "bg_probe.check.json" if args.check else "bg_probe.json"
        args.json = os.path.join(ROOT, "data", "overnight", name)

    arms: dict = {}
    lanes = ["background", "foreground"] if args.lane == "both" else [args.lane]
    for lane in lanes:
        arms[lane] = run_arm(lane, check_only=args.check)
        print(f"[{lane}] opened={arms[lane].get('spt_opened')} "
              f"front_unchanged={arms[lane].get('frontmost_unchanged')} "
              f"cursor_unchanged={arms[lane].get('cursor_unchanged')}", flush=True)
    verdict, failed, basis = verdict_for(arms)
    evidence = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "arms": arms,
        "verdict": verdict,
        "verdict_basis": basis,
        "failed_checks": failed,
        "coordinates": {"spt_badge": list(SPT_BADGE), "spt_close": list(SPT_CLOSE)},
        "note": "frames live in data/overnight/frames/ (gitignored); the OCR text that "
                "matters is embedded here so the verdict survives a clone.",
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    if (not args.check and verdict == "PROBE_INCONCLUSIVE"
            and os.path.exists(args.json) and not os.environ.get("BG_PROBE_FORCE")):
        # Never let a weaker run erase a stronger piece of evidence.
        print(f"refusing to overwrite {args.json} with an INCONCLUSIVE result "
              "(set BG_PROBE_FORCE=1 to override)")
        print(json.dumps({"verdict": verdict, "basis": basis, "failed": failed},
                         ensure_ascii=False, indent=1))
        return 4
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"verdict": verdict, "basis": basis, "failed": failed,
                      "json": args.json}, ensure_ascii=False, indent=1))
    return {"BACKGROUND_INPUT_LIVE_PASS": 0, "BACKGROUND_INPUT_BLOCKED": 3}.get(verdict, 4)


if __name__ == "__main__":
    raise SystemExit(main())
