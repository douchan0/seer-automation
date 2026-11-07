#!/usr/bin/env python3
"""Local, model-free tower acceptance runner (task 2026-11-07: fixes A/B/C).

This is the live harness for the three user-reported defects:

A. **active skill choice** - the battle is fought by ``TowerBattleLocal``: it reads
   the move slots and clicks a chosen move locally, inside the 10 s countdown.  The
   per-round timings (``t_round_ui/t_decision/t_click``) are recorded so a win can
   be audited instead of trusted.  No external model is called on this path.
B. **timely settlement** - ``TowerSettlement`` reads the current popup, captures the
   verbatim ``XIN精灵获得经验：N`` line, then clicks exactly ONE confirm and waits
   for the screen to change; the visible->click latency is recorded per popup.
C. **current-frame targeting** - ``GuardianFinder`` locates the guardian in the frame
   taken *now*, excluding the platform, our own orange pet and the player sprite
   (whose name label is OCR-visible).  When it finds nothing it refuses to click -
   no fixed-coordinate sweeping.

One daemon client for the whole run (single capture instance), cooperative pause and
stop files, per-battle budget, append-only ledger.

    PYTHONPATH=.pydeps python3 tools/tower_live.py --battles 3 --allow-input
    PYTHONPATH=.pydeps python3 tools/tower_live.py --dry-run

Pause: touch /tmp/seerauto.master.pause    Stop: touch /tmp/seerauto.stop
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402
from seerauto.skills.tower import (  # noqa: E402
    GuardianFinder,
    TowerBattleLocal,
    TowerSettlement,
    XP_RE,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "native", "bin", "seerdx")
DATA = os.path.join(ROOT, "data", "overnight", "20261106_dual_mode")
LEDGER = os.path.join(DATA, "tower_live.jsonl")
FRAMES = os.path.join(DATA, "live")
SHOT = "/tmp/tower_live_frame.png"
PAUSE = "/tmp/seerauto.master.pause"
STOP = "/tmp/seerauto.stop"
FLOOR_ROI = (1127, 1191, 1325, 1314)          # calibrated 2026-11-07 (two-digit safe)
FLOOR_RE = re.compile(r"^0*(?P<floor>\d{1,2})$")
OVERLAY_MARKERS = ("米米号", "登船日期", "SPT战绩", "精灵信息", "精灵王之战")


def now_iso() -> str:
    import datetime

    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(event: dict[str, Any]) -> None:
    event.setdefault("ts", now_iso())
    os.makedirs(DATA, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class DaemonSession:
    """Minimal LiveSession-shaped adapter over the native capture daemon."""

    def __init__(self, client: C.DaemonClient, geo: G.WindowGeometry, *,
                 cache_s: float = 0.15) -> None:
        self.client = client
        self.geo = geo
        self.cache_s = cache_s
        self._cache: tuple[float, list[dict[str, Any]]] | None = None
        self.pixel_size = (geo.pixel_size.w, geo.pixel_size.h)

    # -- capture --------------------------------------------------------- #
    def shot(self, path: str = SHOT) -> str:
        self.client.send("shot", path=path)
        return path

    def latest(self):
        path = self.shot(SHOT)
        frame = cv2.imread(path, cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError(f"cannot read the captured frame {path}")
        return frame

    def ocr(self, min_conf: float = 0.3) -> list[dict[str, Any]]:
        t = time.monotonic()
        if self._cache is not None and t - self._cache[0] <= self.cache_s:
            return self._cache[1]
        path = self.shot(SHOT)
        items = self.client.send("ocr", path=path, min=min_conf).get("items", [])
        w, h = self.pixel_size
        rows = []
        for it in items:
            cx = (it["x"] + it["w"] / 2) * w
            cy = (1 - (it["y"] + it["h"] / 2)) * h
            rows.append({"text": it["text"], "conf": it["conf"], "px": (cx, cy)})
        self._cache = (time.monotonic(), rows)
        return rows

    # -- input ----------------------------------------------------------- #
    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        gp = self.geo.global_pt_from_game_px(float(x), float(y))
        current = geometry_of(self.client)
        moved = (abs(current.origin_pt.x - self.geo.origin_pt.x) > 2
                 or abs(current.origin_pt.y - self.geo.origin_pt.y) > 2
                 or abs(current.size_pt.w - self.geo.size_pt.w) > 2
                 or abs(current.size_pt.h - self.geo.size_pt.h) > 2)
        if moved:
            raise RuntimeError("window moved between capture and click; refusing")
        self.client.send("click", x=gp.x, y=gp.y, n=1)
        self._cache = None
        return True


def geometry_of(client: C.DaemonClient) -> G.WindowGeometry:
    w = client.win()
    return G.WindowGeometry(
        origin_pt=G.Point(float(w["x"]), float(w["y"])),
        size_pt=G.Size(float(w["w"]), float(w["h"])),
        scale=G.RETINA_SCALE,
    )


def ensure_frontmost(client: C.DaemonClient, tries: int = 12) -> bool:
    client.send("activate")
    time.sleep(0.4)
    for _ in range(tries):
        try:
            w = client.win()
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
            continue
        if w.get("w"):
            return True
        time.sleep(0.2)
    return False


def read_floor(session: DaemonSession) -> int | None:
    x0, y0, x1, y1 = FLOOR_ROI
    path = session.shot(SHOT)
    items = session.client.send("ocr", path=path, min=0.15).get("items", [])
    w, h = session.pixel_size
    best = None
    for it in items:
        cx = (it["x"] + it["w"] / 2) * w
        cy = (1 - (it["y"] + it["h"] / 2)) * h
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            m = FLOOR_RE.match(str(it["text"]).strip())
            if m:
                best = int(m.group("floor"))
    return best


def stopped() -> str | None:
    if os.path.exists(STOP):
        return "stop flag"
    if os.path.exists(PAUSE):
        return "pause flag"
    return None


def one_battle(session: DaemonSession, finder: GuardianFinder,
               battle: TowerBattleLocal, settle: TowerSettlement,
               index: int) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": "battle", "index": index, "t_start": now_iso()}
    t0 = time.monotonic()

    items = session.ocr(min_conf=0.2)
    text = " ".join(str(i["text"]) for i in items)
    if sum(1 for m in OVERLAY_MARKERS if m in text) >= 2:
        row.update(outcome="BLOCKED", reason="a non-battle overlay is up; "
                   "close it and re-run (no blind clicking)")
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row

    floor_before = read_floor(session)
    row["floor_before"] = floor_before

    frame = session.latest()
    found = finder.find(frame, items)
    if found is None:
        row.update(outcome="BLOCKED",
                   reason="no guardian sprite found in the current frame; "
                          "refusing to sweep fixed coordinates")
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
    row["guardian"] = {"px": [round(found["px"][0], 1), round(found["px"][1], 1)],
                       "area": found["area"], "mean_rgb": found["mean_rgb"]}

    # engage: click the body twice (first walks, second interacts) - live-verified
    gx, gy = found["px"]
    try:
        session.click_px(gx, gy, reason="engage_1")
        time.sleep(1.2)
        session.click_px(gx, gy, reason="engage_2")
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="FAILED", reason=f"engage click failed: {exc}")
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
    row["t_engage"] = round(time.monotonic() - t0, 2)

    outcome = battle.run(session)
    row["battle"] = outcome.to_dict()
    row["t_battle_end"] = round(time.monotonic() - t0, 2)
    if outcome.unknown and not outcome.won and not outcome.defeat:
        row.update(outcome="BLOCKED", reason=outcome.reason)
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
    if outcome.defeat:
        row.update(outcome="FAILED", reason="defeat in the tower")
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row

    settle_result = settle.settle(session)
    row["settlement"] = settle_result.to_dict()
    row["t_settled"] = round(time.monotonic() - t0, 2)

    floor_after = read_floor(session)
    row["floor_after"] = floor_after
    xp_values = []
    for line in settle_result.xp_texts:
        m = XP_RE.search(line)
        if m:
            xp_values.append(int(m.group(1)))
    row["xp_values"] = xp_values

    advanced = (floor_before is not None and floor_after is not None
                and floor_after > floor_before)
    if advanced or xp_values:
        row["outcome"] = "PROGRESS"
        row["reason"] = ("floor advanced" if advanced else "") + \
                        ("; xp verbatim" if xp_values else "")
    else:
        row["outcome"] = "UNVERIFIED_ACTIVITY"
        row["reason"] = ("battle won but neither the floor digit nor a verbatim XP "
                         f"line proves it (floor {floor_before}->{floor_after})")
    row["elapsed_s"] = round(time.monotonic() - t0, 2)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--battles", type=int, default=3)
    ap.add_argument("--allow-input", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--target", default=None)
    ap.add_argument("--max-seconds", type=float, default=240.0,
                    help="hard cap for the whole run")
    args = ap.parse_args()

    client = C.DaemonClient(BIN, args=["--target", args.target] if args.target else [],
                            timeout_s=30.0)
    session = None
    try:
        geo = geometry_of(client)
        say(f"window {geo.origin_pt} {geo.size_pt} px={geo.pixel_size}")
        if args.dry_run:
            frame = None
            path = "/tmp/tower_live_dry.png"
            client.send("shot", path=path)
            frame = cv2.imread(path, cv2.IMREAD_COLOR)
            items = []
            w, h = geo.pixel_size.w, geo.pixel_size.h
            for it in client.send("ocr", path=path, min=0.2).get("items", []):
                cx = (it["x"] + it["w"] / 2) * w
                cy = (1 - (it["y"] + it["h"] / 2)) * h
                items.append({"text": it["text"], "conf": it["conf"], "px": (cx, cy)})
            found = GuardianFinder().find(frame, items) if frame is not None else None
            print(json.dumps({
                "dry_run": True, "starts_nothing": True,
                "floor": None,
                "guardian": None if found is None else {
                    "px": [round(found["px"][0], 1), round(found["px"][1], 1)],
                    "area": found["area"], "mean_rgb": found["mean_rgb"]},
                "ocr_lines": [i["text"] for i in items][:12],
                "plan": ["locate the guardian in THIS frame",
                         "click body twice (walk + interact)",
                         "TowerBattleLocal: read moves, click the chosen one before "
                         "the 10s countdown, record timings",
                         "TowerSettlement: capture verbatim XP, click ONE confirm at "
                         "a time, record latency",
                         "read the painted floor again and record PROGRESS or "
                         "UNVERIFIED_ACTIVITY"],
            }, ensure_ascii=False, indent=2))
            return 0
        if not args.allow_input:
            say("REFUSED: pass --allow-input to actually click")
            return 6
        if stopped():
            say(f"REFUSED: {stopped()}")
            return 7
        if not ensure_frontmost(client):
            say("REFUSED: the game window is not frontmost")
            return 8
        geo = geometry_of(client)
        session = DaemonSession(client, geo)
        finder = GuardianFinder()
        battle = TowerBattleLocal()
        settle = TowerSettlement()
        started = time.monotonic()
        done = 0
        failed = 0
        for index in range(1, args.battles + 1):
            if stopped():
                say(f"stopping: {stopped()}")
                break
            if time.monotonic() - started > args.max_seconds:
                say("stopping: --max-seconds budget")
                break
            row = one_battle(session, finder, battle, settle, index)
            log(row)
            done += 1
            if row["outcome"] not in ("PROGRESS",):
                failed += 1
            say(f"battle {index}: {row['outcome']} "
                f"({row.get('reason', '')[:90]})")
            if failed >= 2:
                say("stopping: two non-PROGRESS battles")
                break
        log({"kind": "session.end", "battles": done, "non_progress": failed,
             "elapsed_s": round(time.monotonic() - started, 1)})
        return 0 if failed == 0 else 3
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
