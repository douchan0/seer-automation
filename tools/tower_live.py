#!/usr/bin/env python3
"""Local, model-free tower acceptance runner (offline-repair revision).

Fixes requested by `CX-SDK-AUDIT-20261107-01/02` (audit:
`.codex-sdk-audit/docs/research/20261107-sdk/AUDIT.md`):

**P1 input safety** - ``ActionGuard`` is a single input boundary.  EVERY dispatch
re-checks, in order: the user's stop/pause flags, the task budget (clicks + elapsed),
the window identity (id, and pid when the probe provides one) and geometry, and that
the game window is actually frontmost.  The old code checked geometry only, inside
``click_px``, and treated "a window exists" as frontmost once at startup.  Flag paths,
clock and probes are injectable, so the offline tests never touch the real control
files.

**P2 cross-floor** - the painted floor regex now accepts three digits (100+), and a
floor transition only counts when the SAME ``floor+1`` is read twice, independently of
any XP text.

**P1 selection stats** - ``TowerBattleLocal`` (seerauto/skills/tower.py) counts a turn
as an active success only when the input was accepted, it beat the countdown, and an
observable round effect followed; it never clicks twice in one round.

**No background fallback** - this runner only has the foreground lane.  A refusal is
reported, never retried through a different channel.

    PYTHONPATH=.pydeps python3 tools/tower_live.py --dry-run
    PYTHONPATH=.pydeps python3 tools/tower_live.py --battles 3 --allow-input

Pause: touch /tmp/seerauto.master.pause    Stop: touch /tmp/seerauto.stop
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

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
SHOT = "/tmp/tower_live_frame.png"
PAUSE = "/tmp/seerauto.master.pause"
STOP = "/tmp/seerauto.stop"
FLOOR_ROI = (1127, 1191, 1325, 1314)          # calibrated 2026-11-07
#: three digits: the audit found 100+ rejected by the old two-digit pattern
FLOOR_RE = re.compile(r"^0*(?P<floor>\d{1,3})$")
OVERLAY_MARKERS = ("米米号", "登船日期", "SPT战绩", "精灵信息", "精灵王之战")


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
    #: when the window probe reports a pid it must stay the same (the audit asks for
    #: identity to cover id *and* pid, not id alone)
    pid: int | None = None
    pause_paths: tuple[str, ...] = (PAUSE,)
    stop_paths: tuple[str, ...] = (STOP,)
    max_clicks: int = 400
    max_seconds: float = 900.0
    geometry_tolerance_pt: float = 2.0
    clock: Callable[[], float] = time.monotonic
    window_probe: Callable[[], dict[str, Any]] | None = None
    frontmost_probe: Callable[[], str | None] | None = None
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
        if elapsed > self.max_seconds:
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
        if wid is None:
            # fail closed: an identity check that cannot run is not a passed check
            return "window probe did not report an id; cannot verify identity"
        if self.window_id is not None and int(wid) != int(self.window_id):
            return f"window identity changed ({self.window_id} -> {wid})"
        wpid = win.get("pid")
        if wpid is not None:
            # identity covers id AND pid: remember the first observation, then verify
            # every later one (live `win()` reports no pid, so this branch only runs
            # for callers whose probe actually provides one)
            if self.pid is None:
                self.pid = int(wpid)
                self.history.append({"ts": now_iso(), "kind": "pid_recorded",
                                     "pid": int(wpid)})
            elif int(wpid) != int(self.pid):
                return f"window pid changed ({self.pid} -> {wpid})"
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

    def _frontmost(self) -> str | None:
        if self.frontmost_probe is None:
            # fail closed: no probe means the frontmost check cannot pass
            return "no frontmost probe configured; refusing to dispatch blind"
        try:
            owner = self.frontmost_probe()
        except Exception as exc:  # noqa: BLE001
            return f"frontmost probe failed: {exc}"
        if owner is None:
            return "cannot determine the frontmost application"
        if "SeerGame" not in str(owner):
            return f"the game window is not frontmost (frontmost={owner})"
        return None

    # -- public ------------------------------------------------------------ #
    def allow(self, reason: str) -> None:
        """Raise ``InputRefused`` unless every check passes."""

        for name, check in (("flag", self._flag), ("budget", self._budget),
                            ("identity", self._identity),
                            ("frontmost", self._frontmost)):
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
    """Minimal LiveSession-shaped adapter over the native capture daemon."""

    def __init__(self, client: Any, geo: G.WindowGeometry, *,
                 cache_s: float = 0.15, guard: ActionGuard | None = None) -> None:
        self.client = client
        self.geo = geo
        self.cache_s = cache_s
        self.guard = guard
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
        reply = self.client.send("ocr", path=path, min=min_conf)
        if not isinstance(reply, dict) or reply.get("error") or reply.get("ok") is False:
            # a daemon error is NOT an empty screen: the settlement chain must never
            # read this as "closed" (audit: blank-OCR hazard with a resident stream)
            raise RuntimeError(f"capture/OCR daemon error: {reply!r}"[:300])
        items = reply.get("items") or []
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
        # the single input boundary: flags, budget, identity, geometry, frontmost
        if self.guard is not None:
            self.guard.allow(reason)        # raises InputRefused
        gp = self.geo.global_pt_from_game_px(float(x), float(y))
        current = geometry_of(self.client)
        moved = (abs(current.origin_pt.x - self.geo.origin_pt.x) > 2
                 or abs(current.origin_pt.y - self.geo.origin_pt.y) > 2
                 or abs(current.size_pt.w - self.geo.size_pt.w) > 2
                 or abs(current.size_pt.h - self.geo.size_pt.h) > 2)
        if moved:
            raise InputRefused("window moved between capture and click; refusing")
        self.client.send("click", x=gp.x, y=gp.y, n=1)
        self._cache = None
        return True


def geometry_of(client: Any) -> G.WindowGeometry:
    w = client.win()
    return G.WindowGeometry(
        origin_pt=G.Point(float(w["x"]), float(w["y"])),
        size_pt=G.Size(float(w["w"]), float(w["h"])),
        scale=G.RETINA_SCALE,
    )


def ensure_frontmost(client: Any, *, expected_id: int | None = None,
                     tries: int = 12) -> bool:
    """Raise the client and verify the SAME window, not merely that one exists."""

    client.send("activate")
    time.sleep(0.4)
    for _ in range(tries):
        try:
            w = client.win()
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
            continue
        if not w.get("w"):
            time.sleep(0.2)
            continue
        wid = w.get("id")
        if (expected_id is not None and wid is not None
                and int(wid) != int(expected_id)):
            return False
        return True
    return False


def default_frontmost_probe(root: str = ROOT) -> Callable[[], str | None]:
    def probe() -> str | None:
        import subprocess as _sp

        ctl = os.path.join(root, "scripts", "seerctl")
        try:
            out = _sp.run([ctl, "wl"], capture_output=True, text=True, timeout=5.0)
            windows = json.loads(out.stdout or "[]")
        except Exception:  # noqa: BLE001
            return None
        top = next((w for w in windows if w.get("layer") == 0), None)
        return str(top.get("owner")) if top else None

    return probe


#: the painted number may arrive alone, with OCR noise, or as 「第28层」
FLOOR_CN_RE = re.compile(r"第\s*(?P<floor>\d{1,3})\s*层")


def parse_floor_text(text: str) -> int | None:
    """Best-effort floor number from one OCR line."""

    stripped = str(text).strip()
    m = FLOOR_RE.match(stripped)
    if m:
        return int(m.group("floor"))
    m = FLOOR_CN_RE.search(stripped)
    if m:
        return int(m.group("floor"))
    # a standalone 1-3 digit run with no other digits around it
    m = re.search(r"(?<!\d)(\d{1,3})(?!\d)", stripped)
    if m:
        return int(m.group(1))
    return None


BATTLE_TEXT_MARKERS = ("当前回合", "战报")


def screen_text(session: DaemonSession) -> str:
    items = session.ocr(min_conf=0.2)
    return " ".join(sorted(str(i["text"]) for i in items))


def wait_change_or_battle(session: DaemonSession, before: str, *,
                          timeout_s: float = 2.0) -> str:
    """``"battle"`` | ``"changed"`` | ``"unchanged"`` - never a blind guess."""

    deadline = time.monotonic() + timeout_s
    previous: str | None = None
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            text = screen_text(session)
        except Exception:  # noqa: BLE001
            continue
        if sum(1 for m in BATTLE_TEXT_MARKERS if m in text) >= 2:
            return "battle"
        if previous is not None and text == previous and text != before:
            return "changed"
        previous = text
    return "unchanged"


def read_floor(session: DaemonSession) -> int | None:
    x0, y0, x1, y1 = FLOOR_ROI
    path = session.shot(SHOT)
    reply = session.client.send("ocr", path=path, min=0.15)
    if not isinstance(reply, dict) or reply.get("error") or reply.get("ok") is False:
        raise RuntimeError(f"capture/OCR daemon error while reading the floor: {reply!r}"[:200])
    items = reply.get("items") or []
    w, h = session.pixel_size
    cx_roi, cy_roi = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    candidates: list[tuple[float, float, int]] = []
    for it in items:
        cx = (it["x"] + it["w"] / 2) * w
        cy = (1 - (it["y"] + it["h"] / 2)) * h
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        value = parse_floor_text(str(it["text"]))
        if value is None:
            continue
        try:
            conf = float(it.get("conf") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        dist = abs(cx - cx_roi) + abs(cy - cy_roi)
        candidates.append((conf, -dist, value))
    if not candidates:
        return None
    # highest confidence wins; closest to the ROI centre breaks ties.  The previous
    # version kept the LAST match, which made two numeric labels order-dependent.
    candidates.sort(reverse=True)
    return candidates[0][2]


def read_floor_stable(session: DaemonSession, *, tries: int = 3) -> int | None:
    """A floor number only counts when two consecutive reads agree."""

    last: int | None = None
    for _ in range(max(2, tries)):
        value = read_floor(session)
        if value is not None and last is not None and value == last:
            return value
        last = value
        time.sleep(0.3)
    return None


def stopped() -> str | None:
    if os.path.exists(STOP):
        return "stop flag"
    if os.path.exists(PAUSE):
        return "pause flag"
    return None


# --------------------------------------------------------------------------- #
# one battle
# --------------------------------------------------------------------------- #

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

    floor_before = read_floor_stable(session)
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

    gx, gy = found["px"]
    try:
        before_text = screen_text(session)
        session.click_px(gx, gy, reason="engage_1")
        after_first = wait_change_or_battle(session, before_text, timeout_s=1.8)
        after_second = None
        if after_first != "battle":
            # 04: the second click is no longer a blind "sleep 1.2 s then click".
            # It happens only after a bounded OBSERVATION of the first click's effect,
            # and what was observed is recorded either way.
            session.click_px(gx, gy, reason="engage_2")
            after_second = wait_change_or_battle(session, before_text, timeout_s=2.0)
        row["engage"] = {"after_first": after_first, "after_second": after_second}
        if after_first == "battle" or after_second == "battle":
            row["engage_ok"] = True
    except InputRefused as exc:
        row.update(outcome="BLOCKED", reason=f"input refused during engage: {exc}")
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
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

    # the chain may only be "closed" if, after the last confirm, the painted ground
    # shows EXACTLY floor_before+1.  A missed OCR box (None) proves nothing and keeps
    # the chain unverified - 收到别的数字或读不到都不是证明。
    expected_next = (floor_before + 1) if floor_before is not None else None
    settle.positive_evidence = (
        lambda expected=expected_next, s=session:
        (expected is not None and read_floor_stable(s) == expected))
    floor_after = read_floor_stable(session)
    row["floor_after"] = floor_after
    xp_values = []
    for line in settle_result.xp_texts:
        m = XP_RE.search(line)
        if m:
            xp_values.append(int(m.group(1)))
    row["xp_values"] = xp_values

    # A floor transition must be a STABLE before+1, independent of any XP text.
    advanced = (floor_before is not None and floor_after is not None
                and floor_after == floor_before + 1)
    row["floor_advanced"] = advanced
    if not settle_result.closed:
        # 04: a floor reading of before+1 behind an XP popup is NOT permission to go
        # on.  If the settlement chain is unconfirmed the whole run stops here.
        row["outcome"] = "BLOCKED"
        row["stop_run"] = True
        row["reason"] = (f"settlement not confirmed (status={settle_result.status}: "
                         f"{settle_result.reason[:120]}); floor_advanced={advanced} "
                         "is recorded but the run stops before any next battle")
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
    if advanced:
        row["outcome"] = "PROGRESS"
        row["reason"] = f"floor {floor_before} -> {floor_after} (stable reads)"
    else:
        row["outcome"] = "UNVERIFIED_ACTIVITY"
        row["reason"] = ("the floor transition is not a stable before+1 "
                         f"(floor {floor_before}->{floor_after})"
                         + ("; XP text was captured but is recorded separately"
                            if xp_values else "")
                         + (f"; settlement status={settle_result.status}"
                            if settle_result.status != "closed" else ""))
    row["elapsed_s"] = round(time.monotonic() - t0, 2)
    return row


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--battles", type=int, default=3)
    ap.add_argument("--allow-input", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--target", default=None)
    ap.add_argument("--max-seconds", type=float, default=240.0,
                    help="hard cap for the whole run (also enforced per dispatch)")
    ap.add_argument("--max-clicks", type=int, default=80,
                    help="hard cap on dispatches for the whole run")
    ap.add_argument("--pause-file", default=PAUSE)
    ap.add_argument("--stop-file", default=STOP)
    args = ap.parse_args()

    client = C.DaemonClient(BIN, args=["--target", args.target] if args.target else [],
                            timeout_s=30.0)
    session = None
    try:
        geo = geometry_of(client)
        window_id = None
        try:
            window_id = client.win().get("id")
        except Exception:  # noqa: BLE001
            window_id = None
        say(f"window id={window_id} {geo.origin_pt} {geo.size_pt} px={geo.pixel_size}")
        if args.dry_run:
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
                "guard": {"window_id": window_id,
                          "pause_file": args.pause_file,
                          "stop_file": args.stop_file,
                          "max_clicks": args.max_clicks,
                          "max_seconds": args.max_seconds,
                          "per_dispatch_checks": ["flag", "budget", "identity",
                                                  "frontmost"]},
                "ocr_lines": [i["text"] for i in items][:12],
                "plan": ["locate the guardian in THIS frame",
                         "click body twice (walk + interact), each dispatch guarded",
                         "TowerBattleLocal: local move choice inside the countdown; a "
                         "turn counts only with an observable round effect",
                         "TowerSettlement: read -> capture XP -> ONE confirm -> wait "
                         "for an observed change; frozen popups are not re-confirmed",
                         "read the floor twice and require a stable floor+1"],
            }, ensure_ascii=False, indent=2))
            return 0
        if not args.allow_input:
            say("REFUSED: pass --allow-input to actually click")
            return 6
        if stopped():
            say(f"REFUSED: {stopped()}")
            return 7
        if not ensure_frontmost(client, expected_id=window_id):
            say("REFUSED: the game window is not frontmost or its identity changed")
            return 8
        geo = geometry_of(client)
        guard = ActionGuard(
            window_id=window_id, geometry=geo,
            pause_paths=(args.pause_file,), stop_paths=(args.stop_file,),
            max_clicks=args.max_clicks, max_seconds=args.max_seconds,
            window_probe=lambda: client.win(),
            frontmost_probe=default_frontmost_probe(),
        )
        session = DaemonSession(client, geo, guard=guard)
        finder = GuardianFinder()
        battle = TowerBattleLocal()
        # "closed" needs positive evidence that the un-occluded tower is back: a
        # readable painted floor number is that evidence (blank OCR is not).
        # "closed" needs positive evidence that the un-occluded tower is back AND the
        # floor is exactly the expected next one.  That can only be known per battle
        # (floor_before+1), so one_battle sets the callback; the honest default is None
        # (blank OCR alone can NEVER close the chain).
        settle = TowerSettlement(positive_evidence=None)
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
            if row.get("stop_run"):
                say("stopping the whole run: the settlement chain was not confirmed")
                failed += 1
                break
            if row["outcome"] != "PROGRESS":
                failed += 1
            say(f"battle {index}: {row['outcome']} "
                f"({row.get('reason', '')[:90]})")
            if failed >= 2:
                say("stopping: two non-PROGRESS battles")
                break
        log({"kind": "session.end", "battles": done, "non_progress": failed,
             "dispatches": guard.clicks, "guard_refusals": [
                 h for h in guard.history if h.get("kind") == "input_refused"],
             "elapsed_s": round(time.monotonic() - started, 1)})
        return 0 if failed == 0 else 3
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
