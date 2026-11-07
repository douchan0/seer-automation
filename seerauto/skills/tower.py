"""Local, model-free tower engine: guardian finding, turn decisions, settlement.

Why this module exists (2026-11-07, measured on the live client):

* A hand-driven loop lost **56.3 s** between "battle UI visible" (08:28:36.637) and
  the first skill-click command (08:29:32.962).  The game's move countdown is 10 s,
  so the game picked the move for us - a "win" that proves nothing about autonomous
  play.  Ordinary turns must be decided and clicked **locally**, before the
  countdown expires.
* The settlement chain lost **51.7 s** between "victory/确认 visible"
  (08:29:48.118) and the first confirm command (08:30:39.856), and a burst of four
  confirms skipped the verbatim XP panel entirely.
* Floor 2 stalled on ``no engageable monster`` because a click on the player's own
  sprite opened the 个人信息 overlay on top of the tower floor; the guardian must be
  located from the **current** frame, never from a coordinate remembered from the
  previous floor.

Everything here is local: no LLM calls, no per-turn model involvement.  The model
only looks at phases and exceptions.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import cv2
import numpy as np

from .act import RunBattle  # read_skills/pick_skill are RunBattle classmethods (no MovePicker class exists)

# --------------------------------------------------------------------------- #
# markers (verbatim from the live client)
# --------------------------------------------------------------------------- #

#: the battle panel is up: the round counter and/or the move use-counters
BATTLE_MARKERS = ("当前回合", "使用次数", "战报")
VICTORY_MARKERS = ("胜利", "获得胜利")
DEFEAT_MARKERS = ("很遗憾", "挑战未能获胜", "这场较量你输了")
OVERLAY_MARKERS = ("米米号", "登船日期", "SPT战绩", "精灵信息", "精灵王之战")
XP_RE = re.compile(r"XIN精灵获得经验\s*[:：]?\s*(\d+)")
ROUND_RE = re.compile(r"当前回合\s*[:：]?\s*(\d+)")

#: the client's move countdown (measured): after this the game picks for us
DEFAULT_COUNTDOWN_S = 10.0


def _now() -> float:
    return time.monotonic()


def _texts(items: Iterable[Mapping[str, Any]]) -> str:
    return " ".join(str(it.get("text") or "") for it in items)


def _count_markers(text: str, markers: Sequence[str]) -> int:
    return sum(1 for m in markers if m in text)


# --------------------------------------------------------------------------- #
# guardian finding (current frame only)
# --------------------------------------------------------------------------- #

#: the tower scene area, game_px, where sprites can stand
SCENE_ROI = (200, 340, 2260, 1330)


@dataclass
class GuardianFinder:
    """Find the floor guardian sprite in the CURRENT frame.

    Corrected 2026-11-07 09:12 from real frames (the first version was wrong twice):

    * the platform's own teal is hue ~80-90, so the exclusion band must be narrow
      ``(76, 96)`` - the first version used (80,105) and **excluded the blue
      guardian** (measured hue 102 at the guardian's body);
    * there is **no colour-based faction rule**: the floor-3 guardian measured hue 20
      (BGR 109,182,218 - an orange 烈焰猩猩), so excluding "orange = our pet" deleted
      the guardian there too (supervisor 2026-11-07: colour must not decide faction);
    * the player avatar is excluded by its ``hello`` name label instead;
    * a sprite must be compact (40..220 px each side, fill >= 0.35); platform
      decorations are wide and flat.

    The caller must still verify by the battle UI: a candidate is a hypothesis, and
    clicking it is a bounded probe, never an assumption.
    """

    min_area_px: int = 2000
    player_name: str = "hello"
    #: the platform's own teal only (measured 80-90); keep it narrow
    platform_hue: tuple[int, int] = (76, 96)
    min_saturation: int = 110
    min_value: int = 90
    min_side_px: int = 40
    max_side_px: int = 230
    min_fill: float = 0.35
    max_candidates: int = 3

    def candidates(self, frame: np.ndarray,
                   items: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
        """Up to ``max_candidates`` sprite-like blobs, best first."""

        if frame is None or frame.size == 0:
            return []
        x0, y0, x1, y1 = SCENE_ROI
        h, w = frame.shape[:2]
        x1, y1 = min(x1, w), min(y1, h)
        roi = frame[y0:y1, x0:x1]
        if roi.size == 0:
            return []
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hue, sat, val = cv2.split(hsv)
        mask = ((sat >= self.min_saturation) & (val >= self.min_value)).astype(np.uint8)
        lo, hi = self.platform_hue
        mask[(hue >= lo) & (hue <= hi)] = 0
        player_box = self._player_box(items)
        if player_box is not None:
            px0, py0, px1, py1 = player_box
            mask[max(0, py0 - y0):max(0, py1 - y0),
                 max(0, px0 - x0):max(0, px1 - x0)] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        found: list[dict[str, Any]] = []
        for idx in range(1, count):
            area = int(stats[idx, cv2.CC_STAT_AREA])
            if area < self.min_area_px:
                continue
            bw = int(stats[idx, cv2.CC_STAT_WIDTH])
            bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
            if not (self.min_side_px <= bw <= self.max_side_px
                    and self.min_side_px <= bh <= self.max_side_px):
                continue
            ratio = bw / float(bh)
            if not 0.5 <= ratio <= 2.0:
                continue
            fill = area / float(bw * bh)
            if fill < self.min_fill:
                continue
            cx, cy = cents[idx]
            found.append({"px": (float(cx + x0), float(cy + y0)), "area": area,
                          "bbox": (int(stats[idx, 0] + x0), int(stats[idx, 1] + y0),
                                   bw, bh),
                          "fill": round(fill, 3),
                          "mean_rgb": self._mean_rgb(roi, labels, idx)})
        found.sort(key=lambda c: -c["area"])
        return found[: self.max_candidates]

    def find(self, frame: np.ndarray,
             items: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
        best = self.candidates(frame, items)
        return best[0] if best else None

    def _player_box(self, items: Sequence[Mapping[str, Any]]) -> tuple[int, int, int, int] | None:
        for it in items:
            if self.player_name and self.player_name in str(it.get("text") or ""):
                px = it.get("px") or (None, None)
                if px[0] is None:
                    continue
                cx, cy = float(px[0]), float(px[1])
                return (int(cx - 90), int(cy - 260), int(cx + 90), int(cy + 40))
        return None

    @staticmethod
    def _mean_rgb(roi: np.ndarray, labels: np.ndarray, idx: int) -> list[int]:
        sel = labels == idx
        if not sel.any():
            return [0, 0, 0]
        b, g, r = (float(roi[:, :, i][sel].mean()) for i in range(3))
        return [int(r), int(g), int(b)]


# --------------------------------------------------------------------------- #
# local battle loop (decide + click before the countdown)
# --------------------------------------------------------------------------- #

@dataclass
class BattleOutcome:
    won: bool
    defeat: bool
    turns: int
    xp_texts: list[str] = field(default_factory=list)
    timings: list[dict[str, Any]] = field(default_factory=list)
    active_success_turns: int = 0
    late_turns: int = 0
    reason: str = ""
    unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"won": self.won, "defeat": self.defeat, "turns": self.turns,
                "xp_texts": list(self.xp_texts), "timings": list(self.timings),
                "active_success_turns": self.active_success_turns,
                "late_turns": self.late_turns, "reason": self.reason,
                "unknown": self.unknown}


class TowerBattleLocal:
    """Fight one tower battle with local decisions only.

    ``run`` waits for the battle UI, then for each round: read the move slots,
    pick one, and **click it immediately** - all inside the 10 s countdown.  Every
    round records ``t_round_ui / t_decision / t_click`` so a "win" can be audited
    against the countdown instead of being trusted.
    """

    def __init__(self, *, countdown_s: float = DEFAULT_COUNTDOWN_S,
                 max_turns: int = 30, turn_timeout_s: float = 14.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = _now) -> None:
        self.countdown_s = countdown_s
        self.max_turns = max_turns
        self.turn_timeout_s = turn_timeout_s
        self.sleep = sleep
        self.clock = clock

    # -- helpers ----------------------------------------------------------- #
    def _ocr(self, session: Any) -> list[dict[str, Any]]:
        for conf in (0.25, 0.15):
            try:
                items = session.ocr(min_conf=conf)
            except Exception:  # noqa: BLE001 - OCR is best effort here
                items = []
            if items:
                return list(items)
        return []

    def _state(self, session: Any) -> tuple[str, int | None, list[dict[str, Any]]]:
        items = self._ocr(session)
        text = _texts(items)
        if _count_markers(text, VICTORY_MARKERS) >= 1:
            return "victory", None, items
        if _count_markers(text, DEFEAT_MARKERS) >= 1:
            return "defeat", None, items
        if _count_markers(text, OVERLAY_MARKERS) >= 2:
            return "overlay", None, items
        rounds = ROUND_RE.search(text)
        return ("battle" if _count_markers(text, BATTLE_MARKERS) >= 2 else "unknown",
                int(rounds.group(1)) if rounds else None, items)

    def _local_power_table(self, session: Any) -> dict[str, int] | None:
        """A power table that is LOCAL only.

        RunBattle's own BATTLE path calls ``learn_power_table`` which shells out to
        an external vision model with a 90 s timeout **before the first click** - that
        must never happen on the real-time turn path (supervisor 2026-11-07 08:44).
        So this module simply does not call it: either a verified table is already in
        the session/world state, or ``pick_skill`` falls back to its local
        damage-first slot order.
        """

        for attr in ("skill_power_table", "power_table"):
            table = getattr(session, attr, None)
            if isinstance(table, Mapping) and table:
                return {str(k): int(v) for k, v in table.items()}
        return None

    def _wait_battle_ui(self, session: Any, timeout_s: float) -> float | None:
        """Wait until the battle panel is up; return the monotonic timestamp."""

        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            state, _round, _items = self._state(session)
            if state == "battle":
                return self.clock()
            if state in ("victory", "defeat"):
                return None
            self.sleep(0.25)
        return None

    # -- main -------------------------------------------------------------- #
    def run(self, session: Any) -> BattleOutcome:
        outcome = BattleOutcome(won=False, defeat=False, turns=0)
        t_battle = self._wait_battle_ui(session, self.turn_timeout_s)
        if t_battle is None:
            state, _r, _i = self._state(session)
            outcome.reason = f"battle UI never appeared (state={state})"
            outcome.unknown = True
            return outcome
        for turn in range(1, self.max_turns + 1):
            state, round_no, items = self._state(session)
            t_round_ui = self.clock()
            if state == "victory":
                outcome.won = True
                outcome.reason = "victory marker"
                return outcome
            if state == "defeat":
                outcome.defeat = True
                outcome.reason = "defeat marker"
                return outcome
            if state == "overlay":
                outcome.reason = "a non-battle overlay is up mid-fight"
                outcome.unknown = True
                return outcome
            if state == "unknown":
                # give the next animation a chance, but bounded
                self.sleep(0.4)
                state, round_no, items = self._state(session)
                if state == "unknown":
                    outcome.reason = "lost the battle screen"
                    outcome.unknown = True
                    return outcome
            try:
                cells = RunBattle.read_skills(session)
            except Exception as exc:  # noqa: BLE001
                outcome.reason = f"cannot read the move slots: {exc}"
                outcome.unknown = True
                return outcome
            pick = RunBattle.pick_skill(cells, self._local_power_table(session))
            t_decision = self.clock()
            if pick is None:
                outcome.reason = "no usable move (PP exhausted?)"
                outcome.unknown = True
                return outcome
            px = pick.get("px")
            clicked = False
            try:
                clicked = bool(session.click_px(px[0], px[1], reason="tower_move"))
            except Exception as exc:  # noqa: BLE001
                outcome.reason = f"move click failed: {exc}"
                outcome.unknown = True
                return outcome
            t_click = self.clock()
            # did the click beat the countdown?  The countdown starts when this
            # round's UI appears, and the worst case for us is that the UI had been
            # up for a while before we sampled it, so we measure from t_round_ui and
            # treat anything at/over the countdown as a LATE (not active) turn.
            late = (t_click - t_round_ui) >= self.countdown_s
            outcome.turns += 1
            if late:
                outcome.late_turns += 1
            else:
                outcome.active_success_turns += 1
            got_round_change = self._wait_round_change(session, round_no)
            outcome.timings.append({
                "turn": turn, "round": round_no, "slot": pick.get("slot"),
                "pp": pick.get("pp"), "px": [float(px[0]), float(px[1])],
                "t_round_ui": round(t_round_ui, 3),
                "t_decision": round(t_decision, 3), "t_click": round(t_click, 3),
                "decision_s": round(t_decision - t_round_ui, 3),
                "click_s": round(t_click - t_round_ui, 3),
                "clicked": clicked, "late_vs_countdown": late,
                "round_changed": got_round_change,
            })
            if not got_round_change:
                # either the battle ended (victory/defeat) or the screen is stuck
                state, _r, _i = self._state(session)
                if state == "victory":
                    outcome.won = True
                    outcome.reason = "victory after the last turn"
                    return outcome
                if state == "defeat":
                    outcome.defeat = True
                    outcome.reason = "defeat after the last turn"
                    return outcome
                if state == "unknown":
                    outcome.reason = "no round change and no recognisable screen"
                    outcome.unknown = True
                    return outcome
        outcome.reason = f"turn budget exhausted ({self.max_turns})"
        outcome.unknown = True
        return outcome

    def _wait_round_change(self, session: Any, round_no: int | None,
                           timeout_s: float = 8.0) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            state, current, _items = self._state(session)
            if state in ("victory", "defeat"):
                return False
            if state == "battle":
                if round_no is None:
                    return True
                if current is not None and current != round_no:
                    return True
            self.sleep(0.2)
        return False


# --------------------------------------------------------------------------- #
# settlement: read the popup, capture the XP line, click ONE confirm, verify
# --------------------------------------------------------------------------- #

@dataclass
class SettlementResult:
    xp_texts: list[str] = field(default_factory=list)
    confirms: int = 0
    latencies: list[float] = field(default_factory=list)
    closed: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"xp_texts": list(self.xp_texts), "confirms": self.confirms,
                "latencies_s": [round(v, 3) for v in self.latencies],
                "closed": self.closed, "reason": self.reason}


class TowerSettlement:
    """Close the post-battle panel chain in a read-then-click loop.

    Each iteration: OCR the current popup, capture any verbatim
    ``XIN精灵获得经验：N`` line, find the CURRENT confirm button and click it once,
    then wait for the popup to change.  The latency from "popup visible" to "click
    sent" is recorded for every step - the live failure we must not repeat was a
    51.7 s gap plus four rapid confirms that skipped the XP panel.
    """

    def __init__(self, *, confirm_words: Sequence[str] = ("确认", "确定"),
                 max_steps: int = 8, step_timeout_s: float = 6.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = _now) -> None:
        self.confirm_words = tuple(confirm_words)
        self.max_steps = max_steps
        self.step_timeout_s = step_timeout_s
        self.sleep = sleep
        self.clock = clock

    def _ocr(self, session: Any) -> list[dict[str, Any]]:
        try:
            return list(session.ocr(min_conf=0.2))
        except Exception:  # noqa: BLE001
            return []

    def _confirm_button(self, items: Sequence[Mapping[str, Any]]):
        for word in self.confirm_words:
            for it in items:
                text = str(it.get("text") or "")
                if word in text and not XP_RE.search(text):
                    px = it.get("px") or (None, None)
                    if px[0] is not None:
                        return (float(px[0]), float(px[1])), word
        return None, None

    def settle(self, session: Any) -> SettlementResult:
        result = SettlementResult()
        seen: set[str] = set()
        for _step in range(self.max_steps):
            items = self._ocr(session)
            text = _texts(items)
            for line in (str(it.get("text") or "") for it in items):
                if XP_RE.search(line) and line not in seen:
                    seen.add(line)
                    result.xp_texts.append(line)
            if not items:
                result.closed = True
                result.reason = "no popup left on screen"
                return result
            (px, word) = self._confirm_button(items)
            if px is None:
                # nothing clickable: either we are done or the panel is unknown
                if any(m in text for m in OVERLAY_MARKERS):
                    result.reason = "a non-settlement overlay is up"
                    return result
                result.closed = True
                result.reason = "no confirm button on screen"
                return result
            t_appear = self.clock()
            try:
                session.click_px(px[0], px[1], reason=f"settle_{word}")
            except Exception as exc:  # noqa: BLE001
                result.reason = f"confirm click failed: {exc}"
                return result
            result.latencies.append(self.clock() - t_appear)
            result.confirms += 1
            # wait for the screen to change before the next read
            before = text
            deadline = self.clock() + self.step_timeout_s
            while self.clock() < deadline:
                self.sleep(0.25)
                after = _texts(self._ocr(session))
                if after != before:
                    break
        result.reason = result.reason or "step budget exhausted"
        return result
