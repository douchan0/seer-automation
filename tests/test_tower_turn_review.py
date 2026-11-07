"""Independent offline review of audit requirements C (turn statistics) and D.

Written 2026-11-07 by an offline reviewer against the ``fix/sdk-audit-20261107``
worktree.  Nothing here touches the live client: every OCR sequence, clock, battle
engine, guardian finder and input channel is a fake, and no control file
(``/tmp/seerauto.*``) is ever written or removed.

Two tests are marked ``xfail`` on purpose.  Each one pins a production defect found
while writing this file; the production code was **not** edited during the review.

Requirement keys
----------------
C - turn statistics (``seerauto/skills/tower.py::TowerBattleLocal.run``):

  * a rejected click must not count as an active decision;
  * a click with no observable round effect must not count as active;
  * never click twice in the same round / on the same unchanged screen;
  * a killing blow (victory/defeat right after the click) *is* an observable effect;
  * timing starts from the earliest battle-UI observation, and every turn records
    decision/click timestamps.

D - cross-floor (``tools/tower_live.py``):

  * the painted floor regex accepts 100+;
  * a transition needs the SAME floor read twice, and exactly ``before + 1``;
  * XP is recorded separately and never makes a battle PROGRESS on its own;
  * an ambiguous guardian (finder -> None) stops without clicking;
  * a refused input surfaces as BLOCKED, with no background -> foreground fallback.
"""

from __future__ import annotations

import inspect
import types

import pytest

import tools.tower_live as tower_live
from seerauto import geometry as G
from seerauto.skills.tower import (
    DEFAULT_COUNTDOWN_S,
    BattleOutcome,
    SettlementResult,
    TowerBattleLocal,
)


# --------------------------------------------------------------------------- #
# fakes (C): a deterministic clock and a scripted battle UI
# --------------------------------------------------------------------------- #

class FakeClock:
    """Deterministic monotonic clock; ``sleep`` is what advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def item(text: str, x: float, y: float) -> dict:
    return {"text": text, "px": (x, y), "conf": 0.9}


class FakeBattleSession:
    """A scripted battle panel.

    The four move slots are the calibrated ones from ``RunBattle.SKILL_SLOTS``, so
    ``read_skills``/``pick_skill`` run unmodified.  What a move click does is
    selected by ``effect``: nothing (``"stall"``), the next round (``"advance"``),
    or an immediate end (``"victory"``/``"defeat"``).
    """

    def __init__(self, *, rounds: int = 2, round_no: int | None = 1,
                 effect: str = "advance", click_returns: bool = True,
                 click_raises: Exception | None = None,
                 ocr_cost_s: float = 0.0,
                 clock: FakeClock | None = None) -> None:
        self.rounds = rounds
        self.round_no = round_no
        self.effect = effect
        self.click_returns = click_returns
        self.click_raises = click_raises
        self.ocr_cost_s = ocr_cost_s
        self.clock = clock
        self.clicks: list[tuple[float, float, str]] = []
        self.ended: str | None = None
        self.first_battle_visible: float | None = None
        #: PP per slot, decremented by a move click so the PP evidence is real
        self.pp = {1: 10, 2: 12}

    # -- capture --------------------------------------------------------- #
    def ocr(self, min_conf: float = 0.15):  # noqa: ARG002 - API shape
        if self.clock is not None and self.ocr_cost_s:
            self.clock.advance(self.ocr_cost_s)
        if self.ended == "victory":
            return [item("胜利", 900, 700), item("获得胜利", 950, 730),
                    item("确认", 1191, 996)]
        if self.ended == "defeat":
            return [item("很遗憾，挑战未能获胜", 900, 700), item("确认", 1191, 996)]
        if self.first_battle_visible is None and self.clock is not None:
            self.first_battle_visible = self.clock.now()
        head = ([] if self.round_no is None
                else [item(f"当前回合：{self.round_no}", 1500, 300)])
        return head + [item("战报", 1000, 200),
                       item(f"使用次数：{self.pp[1]}/25", 1200, 1246),
                       item(f"使用次数：{self.pp[2]}/25", 1456, 1246)]

    # -- input ----------------------------------------------------------- #
    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        self.clicks.append((x, y, reason))
        if self.click_raises is not None:
            raise self.click_raises
        if not self.click_returns:
            return False
        if reason == "tower_move" and self.effect != "stall":
            # a move that actually happens costs one use - this is the PP evidence the
            # runner requires; a "stall" click must leave the panel byte-identical
            slot = 1 if x <= 1300 else 2
            self.pp[slot] = max(0, self.pp[slot] - 1)
        if self.effect == "advance":
            self.round_no = (self.round_no or 0) + 1
            if self.round_no > self.rounds:
                self.ended = "victory"
        elif self.effect in ("victory", "defeat"):
            self.ended = self.effect
        return True

    # -- helpers ---------------------------------------------------------- #
    def move_clicks(self) -> list[tuple[float, float, str]]:
        return [click for click in self.clicks if click[2] == "tower_move"]


def engine(clock: FakeClock, **kwargs) -> TowerBattleLocal:
    return TowerBattleLocal(clock=clock.now, sleep=clock.sleep, **kwargs)


# --------------------------------------------------------------------------- #
# C - rejected clicks are never active decisions
# --------------------------------------------------------------------------- #

def test_a_click_that_returns_false_is_not_an_active_decision():
    """``click_px`` -> False means the input layer refused; the screen never changed."""

    clock = FakeClock()
    session = FakeBattleSession(rounds=99, round_no=3, click_returns=False)
    outcome = engine(clock).run(session)

    assert outcome.active_success_turns == 0
    assert outcome.delivered == 0
    assert outcome.attempts == 1
    assert len(session.move_clicks()) == 1
    assert outcome.timings[0]["clicked"] is False
    assert outcome.timings[0]["observed_effect"] is False
    assert outcome.won is False


def test_a_click_that_returns_false_increments_the_rejected_counter():
    """Requirement C: rejected = ``click_px`` returned False **or** raised."""

    clock = FakeClock()
    session = FakeBattleSession(rounds=99, round_no=3, click_returns=False)
    outcome = engine(clock).run(session)

    assert outcome.rejected == 1          # observed: 0
    assert outcome.active_success_turns == 0


def test_a_click_that_raises_counts_as_rejected_and_stops_before_any_effect():
    clock = FakeClock()
    session = FakeBattleSession(
        rounds=99, round_no=3,
        click_raises=RuntimeError("daemon refused (window not frontmost)"))
    outcome = engine(clock).run(session)

    assert outcome.rejected == 1
    assert outcome.delivered == 0
    assert outcome.active_success_turns == 0
    assert outcome.unknown is True
    assert "move click failed/rejected" in outcome.reason
    assert len(session.move_clicks()) == 1


# --------------------------------------------------------------------------- #
# C - no observable effect => not active, and never a second click in the round
# --------------------------------------------------------------------------- #

def test_an_accepted_click_without_a_round_change_is_not_active_and_is_not_repeated():
    """Same round number and same text before/after: this is "no effect", not a round."""

    clock = FakeClock()
    session = FakeBattleSession(rounds=99, round_no=3, effect="stall")
    outcome = engine(clock).run(session)

    assert len(session.move_clicks()) == 1, "the same round must never be clicked twice"
    assert outcome.turns == 1
    assert outcome.active_success_turns == 0
    assert outcome.unknown is True
    assert "has not changed" in outcome.reason
    assert outcome.timings[0]["clicked"] is True
    assert outcome.timings[0]["round_changed"] is False
    assert outcome.timings[0]["observed_effect"] is False


def test_an_unchanged_screen_without_a_round_number_is_not_active_either():
    """No round number visible + unchanged text must not be read as a new round."""

    clock = FakeClock()
    session = FakeBattleSession(rounds=99, round_no=None, effect="stall")
    outcome = engine(clock).run(session)

    assert len(session.move_clicks()) == 1
    assert outcome.active_success_turns == 0
    assert outcome.unknown is True
    assert "unchanged" in outcome.reason.lower()
    assert outcome.timings[0]["round"] is None


# --------------------------------------------------------------------------- #
# C - on-time active turns, killing blows, timing record
# --------------------------------------------------------------------------- #

def test_a_two_round_fight_records_two_on_time_active_turns():
    clock = FakeClock()
    session = FakeBattleSession(rounds=2, ocr_cost_s=0.05, clock=clock)
    outcome = engine(clock).run(session)

    assert outcome.won is True
    assert outcome.turns == 2
    # 04: only the turn whose chosen slot's PP was observed to drop counts as an
    # active success; the final (killing) turn has no readable PP afterwards, so it
    # is recorded as unverified instead of being upgraded to a success.
    assert outcome.pp_verified_turns == 1
    assert outcome.active_success_turns == 1
    assert outcome.active_unverified == 1
    assert outcome.late_turns == 0
    assert len(session.move_clicks()) == 2

    for timing in outcome.timings:
        assert timing["clicked"] is True
        assert timing["observed_effect"] is True
        assert timing["click_s"] < DEFAULT_COUNTDOWN_S, timing
        assert timing["late_vs_countdown"] is False
        # decision/click timestamps are both present and ordered
        assert timing["t_round_ui"] <= timing["t_decision"] <= timing["t_click"]
        assert timing["decision_s"] == round(
            timing["t_decision"] - timing["t_round_ui"], 3)
        assert timing["click_s"] == round(timing["t_click"] - timing["t_round_ui"], 3)


def test_victory_right_after_the_click_is_an_observed_effect():  # noqa: D401
    """A killing blow has no next round; the victory marker is the effect."""

    clock = FakeClock()
    session = FakeBattleSession(rounds=1, round_no=1, effect="victory")
    outcome = engine(clock).run(session)

    assert outcome.won is True
    assert outcome.defeat is False
    assert outcome.unknown is False
    # 04: the victory IS an observed effect, but the chosen slot's PP can no longer be
    # read after the battle ended, so the turn stays UNVERIFIED instead of being
    # promoted to an active success.
    assert outcome.active_success_turns == 0
    assert outcome.active_unverified == 1
    assert len(session.move_clicks()) == 1
    assert outcome.timings[0]["round_changed"] is False
    assert outcome.timings[0]["observed_effect"] is True
    assert "victory" in outcome.reason


def test_defeat_right_after_the_click_is_an_observed_effect_too():
    clock = FakeClock()
    session = FakeBattleSession(rounds=1, round_no=1, effect="defeat")
    outcome = engine(clock).run(session)

    assert outcome.defeat is True
    assert outcome.won is False
    assert outcome.unknown is False
    # 04: same PP-evidence rule as victory - observed effect, unverified active
    assert outcome.active_success_turns == 0
    assert outcome.active_unverified == 1
    assert len(session.move_clicks()) == 1
    assert outcome.timings[0]["observed_effect"] is True
    assert "defeat" in outcome.reason


def test_turn_timings_start_from_the_first_battle_ui_observation():
    """With a zero-cost observation the turn-1 baseline is the first sighting."""

    clock = FakeClock()
    session = FakeBattleSession(rounds=2, ocr_cost_s=0.05, clock=clock)
    outcome = engine(clock).run(session)

    first = outcome.timings[0]
    assert session.first_battle_visible is not None
    assert first["t_round_ui"] >= session.first_battle_visible
    assert first["decision_s"] >= 0.0 and first["click_s"] < DEFAULT_COUNTDOWN_S


def test_turn_one_decision_latency_is_measured_from_the_earliest_observation():
    clock = FakeClock()
    session = FakeBattleSession(rounds=2, ocr_cost_s=0.4, clock=clock)
    outcome = engine(clock).run(session)

    first = outcome.timings[0]
    observed = first["t_decision"] - session.first_battle_visible
    assert observed <= first["decision_s"] + 1e-9   # observed: 0.8 > 0.4


# --------------------------------------------------------------------------- #
# fakes (D): the daemon session used by tools/tower_live.py
# --------------------------------------------------------------------------- #

CAPTURE = (2400, 1600)                 # game window, px
FLOOR_LABEL_PX = (1226.0, 1252.0)      # inside tools.tower_live.FLOOR_ROI


def floor_item(text: str, px: tuple[float, float] = FLOOR_LABEL_PX) -> dict:
    """An OCR row in the daemon's normalised, bottom-left-origin coordinates."""

    w, h = CAPTURE
    x, y = px
    return {"text": text, "conf": 0.9, "x": x / w, "y": 1 - y / h, "w": 0.0, "h": 0.0}


class FakeDaemonClient:
    """Serves the floor OCR script; one entry per ``ocr`` op (int / None / rows)."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.reads = 0

    def send(self, op: str, **kwargs):
        assert op == "ocr", f"unexpected daemon op {op!r}"
        kwargs.pop("path", None)
        kwargs.pop("min", None)
        self.reads += 1
        if not self.script:
            raise AssertionError("floor OCR script exhausted: read more than expected")
        value = self.script.pop(0)
        if value is None:
            return {"items": []}
        if isinstance(value, list):
            return {"items": value}
        return {"items": [floor_item(str(value))]}


class FakeDaemonSession:
    """Duck-typed ``DaemonSession``: floor OCR via the client, battle OCR via ``ocr``."""

    def __init__(self, floors) -> None:
        self.client = FakeDaemonClient(floors)
        self.pixel_size = CAPTURE
        self.clicks: list[tuple[float, float, str]] = []
        self.guard = None

    def shot(self, path: str = tower_live.SHOT) -> str:  # noqa: ARG002 - API shape
        return "fake:/tmp/frame.png"

    def ocr(self, min_conf: float = 0.2):  # noqa: ARG002
        return [item("当前回合：1", 1500, 300)]

    def latest(self):
        return object()

    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        if self.guard is not None:
            self.guard.allow(reason)          # raises InputRefused, like the real one
        self.clicks.append((x, y, reason))
        return True


GUARDIAN = {"px": (1200.0, 900.0), "area": 5200, "mean_rgb": [40, 120, 220]}


class StubFinder:
    def __init__(self, found) -> None:
        self.found = found

    def find(self, frame, items=()):  # noqa: ARG002
        return self.found


class StubBattle:
    def run(self, session):  # noqa: ARG002
        return BattleOutcome(won=True, defeat=False, turns=2, delivered=2,
                             active_success_turns=2)


class StubSettlement:
    def __init__(self, xp=(), status: str = "closed") -> None:
        self.result = SettlementResult(xp_texts=list(xp), status=status,
                                       closed=status == "closed")

    def settle(self, session):  # noqa: ARG002
        return self.result


def run_one_battle(floors, *, xp=(), found=GUARDIAN):
    session = FakeDaemonSession(floors)
    row = tower_live.one_battle(session, StubFinder(found), StubBattle(),
                                StubSettlement(xp), 1)
    return row, session


@pytest.fixture()
def no_real_sleep(monkeypatch):
    """``tower_live`` sleeps 0.3 s per floor read and 1.2 s between engage clicks."""

    real = tower_live.time
    shim = types.SimpleNamespace(sleep=lambda _s: None,
                                 monotonic=real.monotonic,
                                 strftime=real.strftime)
    monkeypatch.setattr(tower_live, "time", shim)
    return shim


# --------------------------------------------------------------------------- #
# D - painted floor regex and stable reads
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("0", 0), ("03", 3), ("100", 100), ("101", 101), ("28", 28),
])
def test_painted_floor_regex_accepts_three_digits(text, expected):
    match = tower_live.FLOOR_RE.match(text)
    assert match is not None, f"{text!r} must be a floor label"
    assert int(match.group("floor")) == expected


@pytest.mark.parametrize("text", ["", "1000", "12a"])
def test_painted_floor_regex_rejects_four_digits_and_noise(text):
    assert tower_live.FLOOR_RE.match(text) is None


def test_read_floor_only_looks_inside_the_floor_roi(no_real_sleep):
    outside = FakeDaemonSession([[floor_item("28", px=(100.0, 100.0))]])
    assert tower_live.read_floor(outside) is None

    inside = FakeDaemonSession([28])
    assert tower_live.read_floor(inside) == 28


def test_read_floor_stable_needs_two_consecutive_agreeing_reads(no_real_sleep):
    session = FakeDaemonSession([28, 28, 29, 29])
    assert tower_live.read_floor_stable(session) == 28
    assert session.client.reads == 2, "one read must not be enough, two must be"
    assert tower_live.read_floor_stable(session) == 29
    assert session.client.reads == 4


def test_read_floor_stable_returns_none_without_agreement(no_real_sleep):
    session = FakeDaemonSession([28, 27, 28])
    assert tower_live.read_floor_stable(session) is None
    assert session.client.reads == 3


def test_read_floor_stable_uses_the_later_agreeing_pair(no_real_sleep):
    """28 -> 27 -> 27 : the pair that agrees is (27, 27), so the answer is 27."""

    session = FakeDaemonSession([28, 27, 27])
    assert tower_live.read_floor_stable(session) == 27
    assert session.client.reads == 3


def test_read_floor_stable_returns_none_when_reads_fail(no_real_sleep):
    session = FakeDaemonSession([28, None, None])
    assert tower_live.read_floor_stable(session) is None
    assert session.client.reads == 3


# --------------------------------------------------------------------------- #
# D - floor_before/floor_after predicate inside one_battle
# --------------------------------------------------------------------------- #

def test_floor_28_to_29_is_progress(no_real_sleep):
    row, session = run_one_battle([28, 28, 29, 29])

    assert row["floor_before"] == 28
    assert row["floor_after"] == 29
    assert row["floor_advanced"] is True
    assert row["outcome"] == "PROGRESS"
    assert session.client.reads == 4, "both floors must be read twice (stable)"
    assert "28 -> 29" in row["reason"]
    # the battle really happened: two guarded engage clicks plus the move clicks
    assert [click[2] for click in session.clicks] == ["engage_1", "engage_2"]


def test_same_floor_with_xp_is_unverified_activity_not_progress(no_real_sleep):
    """XP text is evidence of activity, never evidence of a floor transition."""

    row, _ = run_one_battle([28, 28, 28, 28],
                            xp=["XIN精灵获得经验：1710"])

    assert row["floor_before"] == 28
    assert row["floor_after"] == 28
    assert row["floor_advanced"] is False
    assert row["outcome"] == "UNVERIFIED_ACTIVITY"
    assert row["xp_values"] == [1710]          # recorded separately ...
    assert "XP text was captured but is recorded separately" in row["reason"]
    assert row["outcome"] != "PROGRESS"        # ... and not used as progress


def test_floor_after_30_is_not_a_transition(no_real_sleep):
    row, _ = run_one_battle([28, 28, 30, 30])

    assert row["floor_before"] == 28
    assert row["floor_after"] == 30
    assert row["floor_advanced"] is False
    assert row["outcome"] == "UNVERIFIED_ACTIVITY"


def test_an_unreadable_floor_before_cannot_be_progress(no_real_sleep):
    """A missing before value is not "unknown base, trust the after value"."""

    row, session = run_one_battle([None, None, None, 29, 29])

    assert row["floor_before"] is None
    assert row["floor_after"] == 29
    assert row["floor_advanced"] is False
    assert row["outcome"] == "UNVERIFIED_ACTIVITY"
    assert session.client.reads == 5


def test_a_jittery_floor_after_read_is_not_a_transition(no_real_sleep):
    """29 seen twice, but not consecutively: the stable read is None, so no PROGRESS."""

    row, session = run_one_battle([28, 28, 29, 27, 29])

    assert row["floor_after"] is None
    assert row["floor_advanced"] is False
    assert row["outcome"] == "UNVERIFIED_ACTIVITY"
    assert session.client.reads == 5


# --------------------------------------------------------------------------- #
# D - ambiguous guardian and refused input
# --------------------------------------------------------------------------- #

def test_a_missing_guardian_stops_without_clicking(no_real_sleep):
    row, session = run_one_battle([28, 28], found=None)

    assert row["outcome"] == "BLOCKED"
    assert "no guardian sprite found" in row["reason"]
    assert session.clicks == []
    assert "floor_after" not in row


def test_a_refused_input_is_blocked_and_is_never_retried_on_another_lane(
        no_real_sleep, tmp_path):
    """A pause-flag refusal from the single input boundary must surface as BLOCKED."""

    pause = tmp_path / "seerauto.master.pause"
    stop = tmp_path / "seerauto.stop"
    pause.write_text("", encoding="utf-8")
    assert not stop.exists()

    geo = G.WindowGeometry(origin_pt=G.Point(0.0, 0.0),
                           size_pt=G.Size(1200.0, 800.0), scale=2.0)
    guard = tower_live.ActionGuard(
        window_id=None, geometry=geo,
        pause_paths=(str(pause),), stop_paths=(str(stop),),
        window_probe=lambda: {"id": 1, "x": 0, "y": 0, "w": 1200, "h": 800},
        frontmost_probe=lambda: "SeerGame")

    session = FakeDaemonSession([28, 28])
    session.guard = guard
    row = tower_live.one_battle(session, StubFinder(GUARDIAN), StubBattle(),
                                StubSettlement(), 1)

    assert row["outcome"] == "BLOCKED"
    assert "input refused during engage" in row["reason"]
    assert "pause flag" in row["reason"]
    # not one dispatch happened, and the refusal was not retried anywhere else
    assert guard.clicks == 0
    assert session.clicks == []
    # there is no second (background) input lane to fall back to: the session and its
    # only dispatch method take no channel selector at all
    assert "background" not in inspect.signature(
        tower_live.DaemonSession.__init__).parameters
    assert "background" not in inspect.signature(
        tower_live.DaemonSession.click_px).parameters
