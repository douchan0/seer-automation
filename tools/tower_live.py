"""Bounded foreground validation for the model-free tower runner."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402
from seerauto.skills.tower import (  # noqa: E402
    BATTLE_MARKERS,
    OVERLAY_MARKERS,
    POPUP_ROI,
    VICTORY_MARKERS,
    XP_RE,
    GuardianFinder,
    TowerBattleLocal,
    TowerSettlement,
    clear_tower_platform,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "native", "bin", "seerdx")
DATA = os.path.join(ROOT, "data", "overnight", "20261107_codex_led")
LEDGER = os.path.join(DATA, "tower_live.jsonl")
PAUSE = "/tmp/seerauto.master.pause"
LOCAL_PAUSE = "/tmp/seerauto.pause"
STOP = "/tmp/seerauto.stop"
FLOOR_ROI = (1127, 1191, 1325, 1314)          # calibrated 2026-11-07
#: three digits: the audit found 100+ rejected by the old two-digit pattern
FLOOR_RE = re.compile(r"^(?P<floor>[0-9]{1,3})$")


class InputRefused(RuntimeError):
    """A dispatch was refused by the single-input boundary."""


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


# --------------------------------------------------------------------------- #
# single input boundary
# --------------------------------------------------------------------------- #

@dataclass
class ActionGuard:
    """Every dispatch re-checks the world; refusals are recorded, never bypassed."""

    window_id: int | None
    geometry: G.WindowGeometry
    pid: int | None = None
    pause_paths: tuple[str, ...] = (PAUSE, LOCAL_PAUSE)
    stop_paths: tuple[str, ...] = (STOP,)
    max_clicks: int = 400
    max_seconds: float = 900.0
    geometry_tolerance_pt: float = 2.0
    clock: Callable[[], float] = time.monotonic
    window_probe: Callable[[], dict[str, Any]] | None = None
    clicks: int = 0
    started: float = field(default_factory=time.monotonic)
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- individual checks ------------------------------------------------- #
    def _flag(self) -> str | None:
        for path in self.stop_paths:
            if os.path.exists(path):
                return f"stop flag {path}"
        for path in self.pause_paths:
            if os.path.exists(path):
                return f"pause flag {path}"
        return None

    def _budget(self) -> str | None:
        if self.clicks >= self.max_clicks:
            return f"click budget exhausted ({self.clicks}/{self.max_clicks})"
        elapsed = self.clock() - self.started
        if elapsed >= self.max_seconds:
            return f"time budget exhausted ({elapsed:.1f}s > {self.max_seconds:.1f}s)"
        return None

    def _identity(self) -> str | None:
        if self.window_probe is None:
            return "no window probe configured"
        try:
            win = self.window_probe()
        except Exception as exc:  # noqa: BLE001
            return f"window probe failed: {exc}"
        if not win:
            return "no game window"
        wid = win.get("id")
        if wid is None or self.window_id is None or wid != self.window_id:
            return "window identity changed or could not be verified"
        if self.pid is None or win.get("pid") != self.pid:
            return "window PID changed or could not be verified"
        if win.get("frontmost") is not True:
            return "the captured game window is not frontmost"
        try:
            dx = abs(float(win["x"]) - self.geometry.origin_pt.x)
            dy = abs(float(win["y"]) - self.geometry.origin_pt.y)
            dw = abs(float(win["w"]) - self.geometry.size_pt.w)
            dh = abs(float(win["h"]) - self.geometry.size_pt.h)
        except (KeyError, TypeError, ValueError) as exc:
            return f"window probe incomplete: {exc}"
        tol = self.geometry_tolerance_pt
        if max(dx, dy, dw, dh) > tol:
            return (f"window geometry moved (dx={dx:.1f} dy={dy:.1f} "
                    f"dw={dw:.1f} dh={dh:.1f} > {tol} pt)")
        return None

    def allow(self, reason: str) -> None:
        """Raise ``InputRefused`` unless every check passes."""

        # Recheck cooperative controls after the round-trip window probe.
        for name, check in (("flag", self._flag), ("budget", self._budget),
                            ("window", self._identity), ("flag", self._flag),
                            ("budget", self._budget)):
            why = check()
            if why:
                self.history.append({"ts": now_iso(), "kind": "input_refused",
                                     "check": name, "reason": why,
                                     "dispatch": reason, "clicks": self.clicks})
                raise InputRefused(f"{name}: {why}")
        self.clicks += 1
        self.history.append({"ts": now_iso(), "kind": "input_allowed",
                             "dispatch": reason, "clicks": self.clicks})


class DaemonSession:
    """Use a single warm stream and OCR bounded, changed regions."""

    def __init__(self, client: C.DaemonClient, geo: G.WindowGeometry, *,
                 guard: ActionGuard | None = None) -> None:
        self.client = client
        self.geo = geo
        self.guard = guard
        self.backend: C.DaemonCapture | None = None
        self._ocr_cache: dict[tuple, tuple[bytes, list[dict[str, Any]]]] = {}

    def latest(self):
        if self.backend is None:
            self.backend = C.DaemonCapture(self.client, self.geo)
            if not self.backend.warm:
                raise C.CaptureUnavailable("tower requires a warm game-window stream")
        frame = self.backend.latest().array
        if frame.shape[:2] != (1600, 2400):
            raise C.CaptureUnavailable("tower calibration requires a 2400×1600 frame")
        return frame

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
        else:
            self.client.close()

    def ocr(self, min_conf: float = 0.3, *, frame=None, roi=None) -> list[dict[str, Any]]:
        if frame is None:
            frame = self.latest()
        x0, y0, x1, y1 = roi or (0, 0, frame.shape[1], frame.shape[0])
        region = frame[y0:y1, x0:x1]
        # Every poll re-observes pixels. Identical text regions need no Vision call.
        signature = region[::3, ::3].tobytes()
        key = (roi, min_conf)
        cached = self._ocr_cache.get(key)
        if cached is not None and signature == cached[0]:
            return cached[1]
        with tempfile.NamedTemporaryFile(suffix=".png") as output:
            if not cv2.imwrite(output.name, region):
                raise C.CaptureUnavailable("cannot export tower OCR region")
            reply = self.client.send("ocr", path=output.name, min=min_conf)
        if reply.get("error") or not isinstance(reply.get("items"), list):
            raise C.DaemonError(f"tower OCR failed: {reply!r}")
        rows = [{"text": item["text"], "conf": item["conf"],
                 "px": (x0 + (item["x"] + item["w"] / 2) * (x1 - x0),
                        y0 + (1 - item["y"] - item["h"] / 2) * (y1 - y0))}
                for item in reply["items"]]
        self._ocr_cache[key] = (signature, rows)
        return rows

    def battle_items(self, min_conf: float = 0.2, *, frame=None) -> list[dict[str, Any]]:
        if frame is None:
            frame = self.latest()
        rows = []
        for roi in ((930, 270, 2090, 580), POPUP_ROI, (1050, 1150, 1600, 1450)):
            rows.extend(self.ocr(min_conf, frame=frame, roi=roi))
        return rows

    def settlement_items(self, min_conf: float = 0.2) -> list[dict[str, Any]]:
        return self.ocr(min_conf, roi=POPUP_ROI)

    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        if self.guard is None:
            raise InputRefused("no input guard configured")
        w, h = self.geo.pixel_size.w, self.geo.pixel_size.h
        if not (0 <= x < w and 0 <= y < h):
            raise InputRefused("click outside calibrated game pixels")
        self.guard.allow(reason)
        reply = self.client.send(
            "click.guarded", x=x / self.geo.scale, y=y / self.geo.scale,
            id=self.guard.window_id, pid=self.guard.pid,
            bounds=[self.geo.origin_pt.x, self.geo.origin_pt.y,
                    self.geo.size_pt.w, self.geo.size_pt.h])
        if reply.get("ok") is not True:
            self.guard.history.append({"ts": now_iso(), "kind": "input_refused",
                                       "check": "native", "dispatch": reason,
                                       "reason": str(reply.get("error", "no acknowledgement"))})
            return False
        self._ocr_cache.clear()
        return True


def parse_floor_text(text: str) -> int | None:
    match = FLOOR_RE.fullmatch(text.strip().lstrip("（(").rstrip("）)"))
    return int(match["floor"]) if match is not None else None


def floor_plate(items: Sequence[Mapping[str, Any]]) -> tuple[int, tuple[float, float]] | None:
    x0, y0, x1, y1 = FLOOR_ROI
    hits = [(parse_floor_text(str(it["text"])), tuple(it["px"])) for it in items
            if x0 <= it["px"][0] <= x1 and y0 <= it["px"][1] <= y1]
    hits = [(value, point) for value, point in hits if value is not None and value > 0]
    return hits[0] if len(hits) == 1 else None


def read_floor(session: DaemonSession, *, require_clear: bool = False) -> int | None:
    frame = session.latest() if require_clear else None
    if frame is not None and not clear_tower_platform(frame):
        return None
    plate = floor_plate(session.ocr(0.15, frame=frame, roi=FLOOR_ROI))
    return plate[0] if plate else None


def read_floor_stable(session: DaemonSession, *, tries: int = 3,
                      require_clear: bool = False) -> int | None:
    previous = None
    for _ in range(max(2, tries)):
        current = read_floor(session, require_clear=require_clear)
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(0.15)
    return None


def control_stop() -> str | None:
    return next((path for path in (STOP, PAUSE, LOCAL_PAUSE) if os.path.exists(path)), None)


def locate_guardian(session: DaemonSession, finder: GuardianFinder, expected_floor: int):
    frame = session.latest()
    if not clear_tower_platform(frame):
        return None, None
    items = session.ocr(0.2, frame=frame, roi=(180, 340, 2200, 1330))
    text = " ".join(it["text"] for it in items)
    if sum(marker in text for marker in OVERLAY_MARKERS) >= 2:
        return None, None
    plate = floor_plate(session.ocr(0.15, frame=frame, roi=FLOOR_ROI))
    if plate is None or plate[0] != expected_floor:
        return None, None
    return finder.locate(frame, items, *plate), finder.player_position(items)


def wait_after_engage(session: DaemonSession, finder: GuardianFinder,
                      floor: int, target, *, approach: bool, timeout_s: float = 3.0):
    """Return an observed battle or a stable player adjacent to the same guardian."""
    deadline = time.monotonic() + timeout_s
    previous_player = None
    while time.monotonic() < deadline:
        if session.guard and (session.guard._flag() or session.guard._budget()):
            break
        items = session.battle_items()
        text = " ".join(it["text"] for it in items)
        if sum(marker in text for marker in BATTLE_MARKERS) >= 2:
            return "battle", None
        if any(marker in text for marker in VICTORY_MARKERS):
            break
        if approach:
            current, player = locate_guardian(session, finder, floor)
            if current is None:
                break
            gx, gy = current["px"]
            if (abs(gx - target[0]) <= 40 and abs(gy - target[1]) <= 40
                    and player is not None and abs(gx - player[0]) <= 145
                    and -25 <= player[1] - gy <= 225
                    and previous_player is not None
                    and abs(player[0] - previous_player[0]) <= 12
                    and abs(player[1] - previous_player[1]) <= 12):
                return "approached", (gx, gy)
            previous_player = player
        time.sleep(0.15)
    return "unverified", None


def one_battle(session: DaemonSession, finder: GuardianFinder,
               battle: TowerBattleLocal, settle: TowerSettlement, index: int) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": "battle", "index": index, "t_start": now_iso(),
                           "outcome": "BLOCKED"}
    start = time.monotonic()
    try:
        floor = read_floor_stable(session)
        row["floor_before"] = floor
        if floor is None:
            return dict(row, reason="no stable painted floor before engagement")
        found, _ = locate_guardian(session, finder, floor)
        if found is None:
            return dict(row, reason="no calibrated guardian on the current unobstructed frame")
        row["guardian"] = found
        observed_since = time.monotonic()
        if not session.click_px(*found["px"], reason="engage_1"):
            return dict(row, reason="approach click refused", stop_run=True)
        first, target = wait_after_engage(session, finder, floor, found["px"], approach=True)
        row["engage"] = {"after_first": first}
        state = first
        if first == "approached":
            observed_since = time.monotonic()
            if not session.click_px(*target, reason="engage_2"):
                return dict(row, reason="interaction click refused", stop_run=True)
            state, _ = wait_after_engage(session, finder, floor, target,
                                         approach=False, timeout_s=6.0)
            row["engage"]["after_second"] = state
        if state != "battle":
            return dict(row, reason="no verified approach or battle after interaction", stop_run=True)

        outcome = battle.run(session, observed_since=observed_since)
        row["battle"] = outcome.to_dict()
        if not outcome.won:
            return dict(row, outcome="FAILED" if outcome.defeat else "BLOCKED",
                        reason=outcome.reason, stop_run=True)

        # The successor floor can already show behind the reward chain. Only ask for
        # it after the settlement reader has observed the panel area clear.
        evidence = settle.positive_evidence
        settle.positive_evidence = lambda: read_floor_stable(
            session, require_clear=True) == floor + 1
        try:
            result = settle.settle(session)
        finally:
            settle.positive_evidence = evidence
        row["settlement"] = result.to_dict()
        row["xp_values"] = [int(m[1]) for line in result.xp_texts if (m := XP_RE.search(line))]
        after = read_floor_stable(session)
        row["floor_after"] = after
        row["floor_advanced"] = after == floor + 1
        if not result.closed:
            return dict(row, reason=f"settlement unverified: {result.reason}", stop_run=True)
        if after != floor + 1 or outcome.active_success_turns == 0:
            return dict(row, outcome="UNVERIFIED_ACTIVITY", stop_run=True,
                        reason="stable floor +1 and verified active PP change are both required")
        return dict(row, outcome="PROGRESS", reason=f"stable floor {floor} -> {after}")
    except (InputRefused, C.CaptureUnavailable, C.DaemonError) as exc:
        return dict(row, reason=str(exc), stop_run=True)
    finally:
        say(f"battle {index} observation ended after {time.monotonic() - start:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--battles", type=int, default=1, choices=range(1, 4))
    ap.add_argument("--allow-input", action="store_true",
                    help="operator has released input and already made the game frontmost")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-seconds", type=float, default=90.0)
    ap.add_argument("--max-clicks", type=int, default=30)
    args = ap.parse_args()
    if not args.dry_run and (not args.allow_input or control_stop()):
        say("REFUSED: foreground authorization and cleared pause/stop flags are required")
        return 7
    if not (0 < args.max_seconds <= 300 and 0 < args.max_clicks <= 120):
        ap.error("invalid validation budget")
    client = C.DaemonClient(BIN, timeout_s=5.0)
    session = None
    try:
        win = client.win()
        if not win.get("id") or not win.get("pid") or (win.get("w"), win.get("h")) != (1200, 800):
            say(f"REFUSED: calibrated game window identity/geometry unavailable ({win})")
            return 8
        geo = G.WindowGeometry(G.Point(win["x"], win["y"]), G.Size(win["w"], win["h"]),
                               G.RETINA_SCALE)
        permissions = client.send("permissions")
        if permissions.get("screen_capture") is not True:
            say("REFUSED: macOS Screen Recording permission is required for live capture")
            return 8
        guard = None
        if not args.dry_run:
            if permissions.get("post_event") is not True:
                say("REFUSED: macOS Accessibility permission is required for live input")
                return 8
            if win.get("frontmost") is not True or "click.guarded" not in client.caps():
                say("REFUSED: exact game window must already be frontmost and daemon support guarded input")
                return 8
            guard = ActionGuard(window_id=win["id"], pid=win["pid"], geometry=geo,
                                window_probe=client.win, max_clicks=args.max_clicks,
                                max_seconds=args.max_seconds)
        session = DaemonSession(client, geo, guard=guard)
        finder = GuardianFinder()
        if args.dry_run:
            floor = read_floor_stable(session)
            found = locate_guardian(session, finder, floor)[0] if floor is not None else None
            print(json.dumps({"dry_run": True, "floor": floor, "guardian": found},
                             ensure_ascii=False))
            return 0 if found else 3

        battle, settle = TowerBattleLocal(), TowerSettlement()
        failures = 0
        progressed = 0
        for index in range(1, args.battles + 1):
            if control_stop() or guard.clock() - guard.started >= guard.max_seconds:
                break
            row = one_battle(session, finder, battle, settle, index)
            log(row)
            say(f"battle {index}: {row['outcome']} ({row['reason']})")
            if row["outcome"] == "PROGRESS":
                progressed += 1
                failures = 0
            else:
                failures += 1
            if failures >= 2 or row.get("stop_run"):
                break
        log({"kind": "session.end", "progressed": progressed, "requested": args.battles,
             "dispatches": guard.clicks, "guard_history": guard.history})
        return 0 if progressed == args.battles else 3
    except (C.DaemonError, C.CaptureUnavailable) as exc:
        say(f"BLOCKED: {exc}")
        return 8
    finally:
        if session is not None:
            session.close()
        else:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
