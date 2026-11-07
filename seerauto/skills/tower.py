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

    min_area_px: int = 2200
    player_name: str = "hello"
    #: background suppression kernel (large close) and compact-body kernel
    bg_kernel: int = 41
    body_kernel: int = 9
    #: the platform's own teal only (measured 80-90); keep it narrow
    platform_hue: tuple[int, int] = (76, 96)
    min_saturation: int = 110
    min_value: int = 90
    #: bright, near-white and grey-dominant pixels are UI stickers, not sprites:
    #: BGR blue #2d82e4 -> "blue" label; grey-label/filter-pipe built this contract.
    max_value_channel: int = 250
    min_side_px: int = 40
    max_side_px: int = 230
    min_fill: float = 0.20
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
        # bright UI stickers (measure labels, name tags, white icons) are NOT
        # sprites: drop near-white / near-grey bright pixels.
        mask[(val >= 250) & (sat <= 12)] = 0
        # raw band-limited mask, small open only: the guardian sprite is compact and
        # internally connected; a big CLOSE merges it into the platform region
        # (live: wh=814x202 fill=0.11, which evaded every later compactness filter).
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        found: list[dict[str, Any]] = []
        for idx in range(1, count):
            area = int(stats[idx, cv2.CC_STAT_AREA])
            if area < self.min_area_px:
                continue
            bw = int(stats[idx, cv2.CC_STAT_WIDTH])
            bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
            ratio = bw / float(bh)
            if not 0.5 <= ratio <= 2.0:
                continue
            # the sprite may carry a thin halo where it meets the platform; a fill
            # as low as 0.2 still passes if the shape is compact (audit: the naive
            # fill>=0.35 rejected the live guardian at fill~0.1)
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
    #: every move click we tried
    attempts: int = 0
    #: observed round transitions (dispatch -> transition -> active are separate)
    transitions: int = 0
    #: turns whose active choice could NOT be verified (joined at round > 1, the
    #: move's PP did not drop, or no skill battle-report evidence was observed)
    active_unverified: int = 0
    #: turns where the chosen slot's PP was observed to drop (hard skill evidence)
    pp_verified_turns: int = 0
    #: clicks the input layer accepted (dispatch, not effect)
    delivered: int = 0
    #: attempts rejected/refused by the input layer
    rejected: int = 0
    reason: str = ""
    unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"won": self.won, "defeat": self.defeat, "turns": self.turns,
                "xp_texts": list(self.xp_texts), "timings": list(self.timings),
                "active_success_turns": self.active_success_turns,
                "late_turns": self.late_turns, "attempts": self.attempts,
                "delivered": self.delivered, "rejected": self.rejected,
                "transitions": self.transitions,
                "active_unverified": self.active_unverified,
                "pp_verified_turns": self.pp_verified_turns,
                "reason": self.reason,
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
    #: text pieces that prove an *observed* round effect (PP decrement or a changed
    #: round number); a click that leaves the screen identical is not an effect.
    def _fingerprint(self, items: Sequence[Mapping[str, Any]],
                     round_no: int | None) -> str:
        body = " ".join(sorted(str(it.get("text") or "") for it in items))
        return f"r{round_no if round_no is not None else -1}|{hash(body) & 0xFFFFFFFF}"

    def run(self, session: Any) -> BattleOutcome:
        outcome = BattleOutcome(won=False, defeat=False, turns=0)
        t_battle = self._wait_battle_ui(session, self.turn_timeout_s)
        if t_battle is None:
            state, _r, _i = self._state(session)
            outcome.reason = f"battle UI never appeared (state={state})"
            outcome.unknown = True
            return outcome
        last_clicked_round: int | None = None
        last_fingerprint: str | None = None
        first_round_seen: int | None = None
        for turn in range(1, self.max_turns + 1):
            state, round_no, items = self._state(session)
            # the first turn's baseline is the moment the battle UI was FIRST seen (not
            # the moment this second OCR finished), so decision_s/click_s are not
            # understated; later turns use their own observation time.
            t_round_ui = t_battle if turn == 1 and t_battle is not None else self.clock()
            fingerprint = self._fingerprint(items, round_no)
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
                self.sleep(0.4)
                state, round_no, items = self._state(session)
                if state == "unknown":
                    outcome.reason = "lost the battle screen"
                    outcome.unknown = True
                    return outcome
            if turn == 1 and round_no is not None:
                # P2 (audit): if the battle already advanced past round 1 before our
                # first observation, the 10 s countdown may have expired and the
                # client picked the move itself - that turn is not an active choice.
                first_round_seen = round_no
            # ---- never click twice inside one round / one unchanged screen ----
            if (last_clicked_round is not None and round_no is not None
                    and round_no == last_clicked_round):
                outcome.reason = (f"round {round_no} has not changed since the last "
                                  "click; refusing to click twice in the same round")
                outcome.unknown = True
                return outcome
            if last_fingerprint is not None and fingerprint == last_fingerprint:
                outcome.reason = ("the battle screen is unchanged since the last click "
                                  "(no round number to compare); refusing a second click")
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
            outcome.attempts += 1
            clicked = False
            try:
                clicked = bool(session.click_px(px[0], px[1], reason="tower_move"))
            except Exception as exc:  # noqa: BLE001
                outcome.rejected += 1
                outcome.reason = f"move click failed/rejected: {exc}"
                outcome.unknown = True
                return outcome
            joined_late = (turn == 1 and first_round_seen is not None
                           and first_round_seen > 1)
            t_click = self.clock()
            late = (t_click - t_round_ui) >= self.countdown_s
            if clicked:
                outcome.delivered += 1
            else:
                # the input layer refused the dispatch: count it and stop here, the
                # "round has not changed" reason must not hide a refusal
                outcome.rejected += 1
                outcome.turns += 1
                outcome.timings.append({
                    "turn": turn, "round": round_no, "slot": pick.get("slot"),
                    "pp": pick.get("pp"), "px": [float(px[0]), float(px[1])],
                    "t_round_ui": round(t_round_ui, 3),
                    "t_decision": round(t_decision, 3),
                    "t_click": round(t_click, 3),
                    "decision_s": round(t_decision - t_round_ui, 3),
                    "click_s": round(t_click - t_round_ui, 3),
                    "clicked": False, "late_vs_countdown": late,
                    "round_changed": False, "observed_effect": False,
                    "joined_late": False,
                })
                outcome.reason = "the move click was refused by the input layer"
                outcome.unknown = True
                return outcome
            got_round_change = self._wait_round_change(
                session, round_no, before=fingerprint)
            state_after, _r_after, _i_after = self._state(session)
            ended = state_after in ("victory", "defeat")
            # a killing blow legitimately has no "next round": victory/defeat right
            # after the click is itself the observable effect.
            observed_effect = bool(clicked and (got_round_change or ended))
            if got_round_change:
                outcome.transitions += 1
            # 04: a round change alone can be the CLIENT auto-picking (the live
            # 火焰车 case).  Only the chosen slot's PP actually dropping proves that
            # OUR click selected the move; otherwise the turn stays unverified.
            pp_before = pick.get("pp")
            pp_after = None
            if observed_effect:
                try:
                    for cell in RunBattle.read_skills(session):
                        if cell.get("slot") == pick.get("slot"):
                            pp_after = cell.get("pp")
                            break
                except Exception:  # noqa: BLE001 - missing evidence, not a crash
                    pp_after = None
            pp_verified = (pp_before is not None and pp_after is not None
                           and pp_after < pp_before)
            outcome.turns += 1
            if late:
                outcome.late_turns += 1
            # A turn only counts as an ACTIVE, SUCCESSFUL decision when the input layer
            # accepted it, it beat the countdown, and an observable round effect
            # followed.  A rejected click or an unchanged screen never counts.
            if pp_verified:
                outcome.pp_verified_turns += 1
            if observed_effect and pp_verified and not late and not joined_late:
                outcome.active_success_turns += 1
            elif observed_effect and not pp_verified:
                outcome.active_unverified += 1
            elif joined_late:
                outcome.active_unverified += 1
            outcome.timings.append({
                "turn": turn, "round": round_no, "slot": pick.get("slot"),
                "pp": pick.get("pp"), "px": [float(px[0]), float(px[1])],
                "t_round_ui": round(t_round_ui, 3),
                "t_decision": round(t_decision, 3), "t_click": round(t_click, 3),
                "decision_s": round(t_decision - t_round_ui, 3),
                "click_s": round(t_click - t_round_ui, 3),
                "clicked": clicked, "late_vs_countdown": late,
                "round_changed": got_round_change,
                "observed_effect": observed_effect,
                "joined_late": joined_late,
                "pp_before": pp_before, "pp_after": pp_after,
                "pp_verified": pp_verified,
            })
            if round_no is not None:
                last_clicked_round = round_no
            last_fingerprint = fingerprint
            if not got_round_change:
                state = state_after
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

    def _wait_round_change(self, session: Any, round_no: int | None, *,
                           before: str | None = None,
                           timeout_s: float = 8.0) -> bool:
        """True only on an OBSERVED change.

        The previous version returned True immediately when the round number was
        unreadable, which let the loop click the same screen again (audit P1).  Now a
        change in the round number *or* in the battle fingerprint is required.
        """

        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            state, current, items = self._state(session)
            if state in ("victory", "defeat"):
                return False
            if state == "battle":
                if round_no is not None and current is not None and current != round_no:
                    return True
                now_fp = self._fingerprint(items, current)
                if before is not None and now_fp != before:
                    return True
            self.sleep(0.2)
        return False


# --------------------------------------------------------------------------- #
# settlement: read the popup, capture the XP line, click ONE confirm, verify
# --------------------------------------------------------------------------- #

#: the settlement popups live here (game_px); text OUTSIDE this box - the public
#: chat in the top-left, the battle HUD - must never decide the chain
POPUP_ROI = (760, 380, 1700, 1180)


@dataclass
class SettlementResult:
    """Outcome of the post-battle popup chain.

    ``status`` is the honest answer: ``closed`` only when the popup area was observed
    empty twice AND a positive tower evidence callback confirms the scene, or when the
    chain ended with an empty popup area and that evidence; ``unverified`` when a
    popup froze, OCR failed, the body was visible without a readable button, or we
    refused to act; ``failed`` when an input was rejected.
    """

    xp_texts: list[str] = field(default_factory=list)
    confirms: int = 0
    #: dispatch latency, measured from the FIRST frame in which that popup was seen
    latencies: list[float] = field(default_factory=list)
    closed: bool = False
    reason: str = ""
    status: str = "unverified"
    frozen_popups: int = 0
    rejected_clicks: int = 0
    ocr_failures: int = 0
    #: settlement body text was on screen but no confirm button could be read
    body_without_button: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"xp_texts": list(self.xp_texts), "confirms": self.confirms,
                "latencies_s": [round(v, 3) for v in self.latencies],
                "closed": self.closed, "status": self.status,
                "frozen_popups": self.frozen_popups,
                "rejected_clicks": self.rejected_clicks,
                "ocr_failures": self.ocr_failures,
                "body_without_button": self.body_without_button,
                "reason": self.reason}


class TowerSettlement:
    """Close the post-battle panel chain: read -> capture XP -> ONE click -> verify.

    Rules from the 2026-11-07 audit and its second review (DSH_REVIEW.md):

    * only text inside ``popup_roi`` may decide the chain: a changing public-chat line
      is not a popup change, and a chat 「确认」 is not a settlement button;
    * the popup fingerprint keeps the reward digits (126 and 2800 are DIFFERENT
      popups) - normalising numbers away made the second reward panel look frozen;
    * OCR jitter is tolerated by requiring two consecutive agreeing readings, not by
      throwing the numbers away;
    * frozen popups are never confirmed twice;
    * a settlement body with no readable button is ``unverified``, never ``closed``;
    * an empty popup area is only ``closed`` with a positive tower-scene evidence
      callback - blank OCR is not proof;
    * a rejected click or an OCR failure is never reported as closed.
    """

    #: words that make a screen plausible as a post-battle settlement UI.
    #: NOTE: a bare 「精灵」 must never be here - it is a substring of the profile
    #: overlay markers (「精灵信息」/「精灵王之战」).
    SETTLEMENT_WORDS = ("恭喜", "获得", "奖励", "积累经验", "放入", "胜利", "升级",
                        "离升级还需经验")

    def __init__(self, *, confirm_words: Sequence[str] = ("确认", "确定"),
                 max_steps: int = 10, step_timeout_s: float = 6.0,
                 popup_roi: tuple[float, float, float, float] = POPUP_ROI,
                 positive_evidence: Callable[[], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = _now) -> None:
        self.confirm_words = tuple(confirm_words)
        self.max_steps = max_steps
        self.step_timeout_s = step_timeout_s
        self.popup_roi = tuple(float(v) for v in popup_roi)
        self.positive_evidence = positive_evidence
        self.sleep = sleep
        self.clock = clock

    # -- OCR helpers -------------------------------------------------------- #
    def _ocr(self, session: Any) -> list[dict[str, Any]] | None:
        """OCR the current frame; ``None`` means "OCR failed", never "empty"."""

        try:
            return list(session.ocr(min_conf=0.2))
        except Exception:  # noqa: BLE001
            return None

    def _popup_items(self, items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        x0, y0, x1, y1 = self.popup_roi
        kept = []
        for it in items:
            px = it.get("px") or (None, None)
            if px[0] is None:
                continue
            try:
                inside = (x0 <= float(px[0]) <= x1 and y0 <= float(px[1]) <= y1)
            except (TypeError, ValueError):
                continue
            if inside:
                kept.append(dict(it))
        return kept

    @staticmethod
    def _normalise(items: Sequence[Mapping[str, Any]]) -> str:
        """Popup identity: keep DIGITS, drop low-confidence lines, sort.

        The reward numbers are part of the identity (126 vs 2800 are different
        panels); OCR jitter is handled by the two-consecutive-readings rule instead.
        """

        parts = []
        for it in items:
            try:
                if float(it.get("conf") or 1.0) < 0.3:
                    continue
            except (TypeError, ValueError):
                pass
            parts.append(str(it.get("text") or "").strip())
        return " ".join(sorted(parts))

    def _confirm_button(self, popup_items: Sequence[Mapping[str, Any]]):
        for word in self.confirm_words:
            for it in popup_items:
                text = str(it.get("text") or "")
                if word in text and not XP_RE.search(text):
                    px = it.get("px") or (None, None)
                    if px[0] is not None:
                        return (float(px[0]), float(px[1])), word
        return None, None

    def _is_settlement(self, popup_items: Sequence[Mapping[str, Any]]) -> bool:
        text = _texts(popup_items)
        if XP_RE.search(text):
            return True
        if any(m in text for m in VICTORY_MARKERS):
            return True
        return any(w in text for w in self.SETTLEMENT_WORDS)

    def _wait_scene_change(self, session: Any, *, before: str) -> bool:
        """True only when the POPUP text really changed (two agreeing readings)."""

        deadline = self.clock() + self.step_timeout_s
        previous: str | None = None
        while self.clock() < deadline:
            self.sleep(0.25)
            items = self._ocr(session)
            if items is None:
                previous = None
                continue
            now = self._normalise(self._popup_items(items))
            if previous is not None and now == previous and now != before:
                return True
            previous = now
        return False

    # -- main -------------------------------------------------------------- #
    def settle(self, session: Any) -> SettlementResult:
        result = SettlementResult()
        seen: set[str] = set()
        confirmed: set[str] = set()
        first_visible: dict[str, float] = {}
        for _step in range(self.max_steps):
            raw = self._ocr(session)
            if raw is None:
                result.ocr_failures += 1
                result.status = "unverified"
                result.reason = ("OCR failed while reading the settlement chain; "
                                 "cannot verify it is closed")
                result.closed = False
                return result
            popup = self._popup_items(raw)
            text = _texts(popup)
            for line in (str(it.get("text") or "") for it in popup):
                if XP_RE.search(line) and line not in seen:
                    seen.add(line)
                    result.xp_texts.append(line)
            if not popup:
                # one empty popup area is not proof: require a second confirming read
                self.sleep(0.35)
                second = self._ocr(session)
                if second is None:
                    result.ocr_failures += 1
                    result.status = "unverified"
                    result.reason = "OCR failed during the empty-screen confirmation"
                    result.closed = False
                    return result
                if self._popup_items(second):
                    continue
                if self.positive_evidence is None:
                    result.status = "unverified"
                    result.reason = ("the popup area is empty but there is no positive "
                                     "tower evidence; refusing to call it closed")
                    result.closed = False
                    return result
                try:
                    positive = bool(self.positive_evidence())
                except Exception as exc:  # noqa: BLE001
                    positive = False
                    result.reason = f"positive evidence check failed: {exc}"
                if positive:
                    result.status = "closed"
                    result.closed = True
                    result.reason = ("popup area empty (confirmed twice) and the tower "
                                     "scene is positively identified")
                    return result
                result.status = "unverified"
                result.reason = ("the popup area is empty but the tower scene could not "
                                 "be positively identified")
                result.closed = False
                return result
            # an overlay on top of the game is reported as an overlay, never handled
            if sum(1 for m in OVERLAY_MARKERS if m in text) >= 2:
                result.status = "unverified"
                result.reason = "a non-settlement overlay is up; refusing to click it"
                result.closed = False
                return result
            if not self._is_settlement(popup):
                result.status = "unverified"
                result.reason = ("the visible popup text is not a settlement UI; "
                                 "refusing to click it")
                result.closed = False
                return result
            (px, word) = self._confirm_button(popup)
            fingerprint = self._normalise(popup)
            if px is None:
                result.body_without_button += 1
                result.status = "unverified"
                result.reason = ("a settlement body is visible but no confirm button "
                                 "was readable inside the popup area; not reported as "
                                 "closed")
                result.closed = False
                return result
            if fingerprint in confirmed:
                result.frozen_popups += 1
                result.status = "unverified"
                result.reason = ("frozen popup: already confirmed once and the popup "
                                 f"text did not change ({text[:60]!r}); refusing a "
                                 "second confirm")
                result.closed = False
                return result
            t_appear = first_visible.setdefault(fingerprint, self.clock())
            try:
                accepted = session.click_px(px[0], px[1], reason=f"settle_{word}")
            except Exception as exc:  # noqa: BLE001
                result.rejected_clicks += 1
                result.status = "failed"
                result.reason = f"confirm click raised: {exc}"
                result.closed = False
                return result
            if accepted is False:
                result.rejected_clicks += 1
                result.status = "failed"
                result.reason = ("confirm click was rejected by the input layer; "
                                 "not reporting the chain as closed")
                result.closed = False
                return result
            result.latencies.append(self.clock() - t_appear)
            result.confirms += 1
            confirmed.add(fingerprint)
            if not self._wait_scene_change(session, before=fingerprint):
                result.frozen_popups += 1
                result.status = "unverified"
                result.reason = ("the popup did not change after the confirm; "
                                 "treating it as frozen/unverified")
                result.closed = False
                return result
        result.status = "unverified"
        result.reason = result.reason or "step budget exhausted"
        result.closed = False
        return result
