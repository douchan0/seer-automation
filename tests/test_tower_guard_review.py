"""Offline adversarial review of the tower runner's input boundary and settlement chain.

Targets (external audit ``CX-SDK-AUDIT-20261107-01/02``):

* A - ``tools/tower_live.py::ActionGuard`` must re-check, before EVERY dispatch:
  the user's pause/stop flags, the task budget (clicks + elapsed), the window
  identity/geometry and that the game window is frontmost; a refusal is recorded
  and never dispatched; there is no background->foreground fallback.
* B - ``seerauto/skills/tower.py::TowerSettlement`` must never confirm a frozen
  popup twice, must never report a rejected click / OCR failure as ``closed``,
  must only click a UI that really looks like a settlement popup, must dispatch
  at most once per popup and only advance on an OBSERVED scene change, and must
  measure latency from the first frame the popup was visible.

Everything here uses fakes: a fake daemon client, a fake clock, and flag files
created inside ``tmp_path`` and injected through ``pause_paths`` / ``stop_paths``.
The real control files (``/tmp/seerauto.master.pause`` / ``/tmp/seerauto.stop``)
are never read, created or removed by this module, and nothing here touches the
game, the daemon or the network.

"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import geometry as G  # noqa: E402
from seerauto.skills.tower import TowerSettlement  # noqa: E402
from tools.tower_live import (  # noqa: E402
    ActionGuard,
    DaemonSession,
    InputRefused,
)

#: the calibrated live window: 1200x800 pt at (225, 53), 2x capture
GEO = G.WindowGeometry(
    origin_pt=G.Point(225.0, 53.0),
    size_pt=G.Size(1200.0, 800.0),
    scale=G.RETINA_SCALE,
)
WIN_ID = 7
WIN = {"id": WIN_ID, "pid": 111, "frontmost": True, "x": 225.0, "y": 53.0, "w": 1200.0, "h": 800.0}

_UNSET = object()


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class FakeClock:
    """Deterministic monotonic clock; ``sleep`` and ``advance`` move it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class FakeDaemon:
    """Stands in for ``seerauto.capture.DaemonClient`` (send/win/close only)."""

    def __init__(self, win: dict | None = None) -> None:
        self.ops: list[tuple[str, dict]] = []
        self.win_reply = dict(win if win is not None else WIN)

    def send(self, op: str, **kwargs) -> dict:
        self.ops.append((op, kwargs))
        if op == "win":
            return dict(self.win_reply)
        if op == "click.guarded":
            x, y, w, h = kwargs["bounds"]
            expected = self.win_reply
            if (kwargs["id"], kwargs["pid"]) != (expected["id"], expected["pid"]):
                return {"error": "guarded click refused"}
            if max(abs(expected[key] - value)
                   for key, value in zip(("x", "y", "w", "h"), (x, y, w, h))) > 2:
                return {"error": "guarded click refused"}
        return {"ok": True}

    def win(self) -> dict:
        return self.send("win")

    def close(self) -> None:
        pass

    def clicks(self) -> list[tuple[str, dict]]:
        return [op for op in self.ops if op[0] == "click.guarded"]


def guard_for(tmp_path, client=None, *, clock=None, window_probe=_UNSET,
              **kwargs) -> ActionGuard:
    """A guard whose flags live in ``tmp_path`` - never the real control files."""

    clock = clock or FakeClock()
    if window_probe is _UNSET:
        window_probe = (lambda: client.win()) if client is not None else (lambda: dict(WIN))
    return ActionGuard(
        window_id=WIN_ID,
        pid=111,
        geometry=GEO,
        pause_paths=(str(tmp_path / "seerauto.master.pause"),),
        stop_paths=(str(tmp_path / "seerauto.stop"),),
        clock=clock.now,
        started=clock.t,
        window_probe=window_probe,
        **kwargs,
    )


def item(text: str, x: float, y: float, conf: float = 0.9) -> dict:
    return {"text": text, "px": (x, y), "conf": conf}


class PopupSession:
    """Scripted settlement chain: OCR shows ``popups[step]``; a click advances."""

    def __init__(self, popups, *, click_result=True, ocr_error=None,
                 on_click=None) -> None:
        self.popups = [list(p) for p in popups]
        self.step = 0
        self.clicks: list[tuple[int, float, float, str]] = []
        self.click_result = click_result
        self.ocr_error = ocr_error
        self.on_click = on_click
        self.ocr_calls = 0

    def ocr(self, min_conf: float = 0.2):  # noqa: ARG002 - API shape
        self.ocr_calls += 1
        if self.ocr_error is not None:
            raise self.ocr_error
        return list(self.popups[min(self.step, len(self.popups) - 1)])

    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        self.clicks.append((self.step, x, y, reason))
        if self.on_click is not None:
            self.on_click()
        if self.click_result is not True:
            return self.click_result          # rejected: the scene does NOT change
        self.step = min(self.step + 1, len(self.popups) - 1)
        return True


def settlement(clock: FakeClock, *, positive: bool = True) -> TowerSettlement:
    # 04: returning "closed" requires positive evidence that the tower scene is back;
    # blank OCR alone is not proof.  The tests that end on an empty popup supply it.
    return TowerSettlement(clock=clock.now, sleep=clock.sleep,
                           positive_evidence=(lambda: positive))


# --------------------------------------------------------------------------- #
# A - pause flag
# --------------------------------------------------------------------------- #

def test_pause_flag_refuses_and_never_dispatches(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    (tmp_path / "seerauto.master.pause").touch()

    with pytest.raises(InputRefused) as excinfo:
        guard.allow("engage_1")
    assert "pause" in str(excinfo.value)
    assert guard.clicks == 0
    assert guard.history[-1]["kind"] == "input_refused"
    assert guard.history[-1]["check"] == "flag"

    # the runner's own session must not reach the wire either
    session = DaemonSession(client, GEO, guard=guard)
    with pytest.raises(InputRefused):
        session.click_px(1000, 600, reason="engage_1")
    assert client.clicks() == []


# --------------------------------------------------------------------------- #
# A - stop flag
# --------------------------------------------------------------------------- #

def test_stop_flag_refuses_and_never_dispatches(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    (tmp_path / "seerauto.stop").touch()

    with pytest.raises(InputRefused) as excinfo:
        guard.allow("tower_move")
    assert "stop" in str(excinfo.value)
    assert guard.clicks == 0
    assert guard.history[-1]["check"] == "flag"

    session = DaemonSession(client, GEO, guard=guard)
    with pytest.raises(InputRefused):
        session.click_px(1200, 1246, reason="tower_move")
    assert client.clicks() == []


# --------------------------------------------------------------------------- #
# A - task budget
# --------------------------------------------------------------------------- #

def test_click_budget_exhausted_is_refused(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client, max_clicks=2)
    session = DaemonSession(client, GEO, guard=guard)

    assert session.click_px(1000, 600, reason="a") is True
    assert session.click_px(1000, 600, reason="b") is True
    with pytest.raises(InputRefused) as excinfo:
        session.click_px(1000, 600, reason="c")

    assert "click budget" in str(excinfo.value)
    assert guard.clicks == 2                  # the refused dispatch was not counted
    assert len(client.clicks()) == 2          # ... and was not sent


def test_time_budget_exhausted_is_refused(tmp_path):
    clock = FakeClock()
    client = FakeDaemon()
    guard = guard_for(tmp_path, client, clock=clock, max_seconds=30.0)
    session = DaemonSession(client, GEO, guard=guard)

    assert session.click_px(1000, 600, reason="a") is True
    clock.advance(31.0)
    with pytest.raises(InputRefused) as excinfo:
        session.click_px(1000, 600, reason="b")

    assert "time budget" in str(excinfo.value)
    assert guard.clicks == 1
    assert len(client.clicks()) == 1


# --------------------------------------------------------------------------- #
# A - window identity and geometry, per dispatch
# --------------------------------------------------------------------------- #

def test_window_id_change_is_refused(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    session = DaemonSession(client, GEO, guard=guard)
    assert session.click_px(1000, 600, reason="a") is True

    client.win_reply["id"] = WIN_ID + 1        # same geometry, different window
    with pytest.raises(InputRefused) as excinfo:
        session.click_px(1000, 600, reason="b")
    assert "window" in str(excinfo.value)
    assert len(client.clicks()) == 1


def test_window_geometry_shift_beyond_tolerance_is_refused(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    session = DaemonSession(client, GEO, guard=guard)
    assert session.click_px(1000, 600, reason="a") is True

    client.win_reply["x"] = 225.0 + 4.0        # tolerance is 2 pt
    with pytest.raises(InputRefused) as excinfo:
        session.click_px(1000, 600, reason="b")
    assert "geometry" in str(excinfo.value)
    assert len(client.clicks()) == 1


def test_geometry_within_tolerance_is_allowed(tmp_path):
    """A 2 pt jitter is inside the calibrated tolerance and must not stall the run."""

    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    client.win_reply["x"] = 227.0
    client.win_reply["y"] = 51.0
    assert DaemonSession(client, GEO, guard=guard).click_px(1000, 600, reason="a") is True
    assert len(client.clicks()) == 1


def test_window_probe_returning_none_is_refused(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client, window_probe=lambda: None)
    session = DaemonSession(client, GEO, guard=guard)

    with pytest.raises(InputRefused) as excinfo:
        session.click_px(1000, 600, reason="a")
    assert "no game window" in str(excinfo.value)
    assert client.clicks() == []


def test_window_probe_error_is_refused(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client, window_probe=lambda: {"error": "no window"})
    with pytest.raises(InputRefused) as excinfo:
        guard.allow("a")
    assert "window" in str(excinfo.value)
    assert guard.clicks == 0


def test_a_guard_without_a_working_window_probe_fails_closed(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client, window_probe=None)
    with pytest.raises(InputRefused) as excinfo:
        guard.allow("a")
    assert "no window probe" in str(excinfo.value)
    assert guard.clicks == 0


# --------------------------------------------------------------------------- #
# A - frontmost, per dispatch
# --------------------------------------------------------------------------- #

def test_frontmost_other_application_is_refused(tmp_path):
    client = FakeDaemon(win=dict(WIN, frontmost=False))
    guard = guard_for(tmp_path, client)
    with pytest.raises(InputRefused, match="not frontmost"):
        DaemonSession(client, GEO, guard=guard).click_px(1000, 600)
    assert client.clicks() == []
    assert [op for op, _ in client.ops] == ["win"]


def test_frontmost_unavailable_is_refused(tmp_path):
    client = FakeDaemon(win={key: val for key, val in WIN.items() if key != "frontmost"})
    guard = guard_for(tmp_path, client)
    with pytest.raises(InputRefused, match="not frontmost"):
        DaemonSession(client, GEO, guard=guard).click_px(1000, 600)
    assert client.clicks() == []


# --------------------------------------------------------------------------- #
# A - happy path: one click per dispatch, probe consulted every time
# --------------------------------------------------------------------------- #

def test_happy_path_clicks_once_per_dispatch_and_reprobes_every_time(tmp_path):
    client = FakeDaemon()
    probe_calls: list[int] = []

    def window_probe():
        probe_calls.append(1)
        return client.win()

    guard = guard_for(tmp_path, client, window_probe=window_probe)
    session = DaemonSession(client, GEO, guard=guard)

    assert session.click_px(1000, 600, reason="engage_1") is True
    assert session.click_px(1000, 600, reason="engage_2") is True

    clicks = client.clicks()
    assert len(clicks) == 2
    assert guard.clicks == 2
    assert len(probe_calls) == 2               # not a one-off check at startup
    assert guard.history[-1]["kind"] == "input_allowed"
    assert clicks[0][1] == {"x": 500.0, "y": 300.0, "id": WIN_ID, "pid": 111,
                            "bounds": [225.0, 53.0, 1200.0, 800.0]}
    assert len(guard.history) == 2


# --------------------------------------------------------------------------- #
# A - the session refuses before the wire, and re-checks after the guard
# --------------------------------------------------------------------------- #

def test_daemon_session_refuses_before_sending_any_click(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    (tmp_path / "seerauto.master.pause").touch()
    session = DaemonSession(client, GEO, guard=guard)

    with pytest.raises(InputRefused):
        session.click_px(1000, 600, reason="tower_move")
    assert client.ops == []                    # no click, no shot, no activate


def test_daemon_session_rechecks_geometry_between_read_and_click(tmp_path):
    """A refusal after the Python check must propagate the native result."""
    client = FakeDaemon(win=dict(WIN, y=83.0))
    guard = guard_for(tmp_path, client, window_probe=lambda: dict(WIN))
    session = DaemonSession(client, GEO, guard=guard)

    assert session.click_px(1000, 600, reason="a") is False
    assert [op for op, _ in client.ops] == ["click.guarded"]
    assert guard.history[-1]["check"] == "native"


# --------------------------------------------------------------------------- #
# B - frozen popup: exactly one confirm, never "closed"
# --------------------------------------------------------------------------- #

def test_frozen_popup_is_confirmed_once_and_reported_unverified():
    popup = [item("胜利", 900, 700), item("确认", 1191, 996)]
    session = PopupSession([popup])           # the popup never changes
    clock = FakeClock()

    result = settlement(clock).settle(session)

    assert len(session.clicks) == 1, session.clicks        # never confirmed twice
    assert result.confirms == 1
    assert result.frozen_popups >= 1
    assert result.status == "unverified"
    assert result.closed is False
    assert "frozen" in result.reason


# --------------------------------------------------------------------------- #
# B - rejected click / raised click are "failed", never "closed"
# --------------------------------------------------------------------------- #

def test_rejected_confirm_click_is_failed_not_closed():
    popup = [item("XIN精灵获得经验：1710", 1100, 700), item("确认", 1188, 999)]
    session = PopupSession([popup], click_result=False)
    clock = FakeClock()

    result = settlement(clock).settle(session)

    assert result.status == "failed"
    assert result.closed is False
    assert result.rejected_clicks >= 1
    assert result.confirms <= 1
    assert len(session.clicks) <= 1


def test_a_confirm_click_that_raises_is_failed_not_closed():
    popup = [item("恭喜你得到了5个积累经验", 1100, 700), item("确认", 1198, 983)]

    class RaisingSession(PopupSession):
        def click_px(self, x, y, reason=""):
            self.clicks.append((self.step, x, y, reason))
            raise RuntimeError("daemon pipe closed")

    session = RaisingSession([popup])
    result = settlement(FakeClock()).settle(session)

    assert result.status == "failed"
    assert result.closed is False
    assert result.rejected_clicks == 1
    assert len(session.clicks) == 1


# --------------------------------------------------------------------------- #
# B - OCR failure is "unverified", never "closed"
# --------------------------------------------------------------------------- #

def test_ocr_failure_is_unverified_not_closed():
    popup = [item("胜利", 900, 700), item("确认", 1191, 996)]
    session = PopupSession([popup], ocr_error=RuntimeError("no frame yet"))
    result = settlement(FakeClock()).settle(session)

    assert result.status == "unverified"
    assert result.closed is False
    assert result.ocr_failures >= 1
    assert session.clicks == []
    assert result.confirms == 0


# --------------------------------------------------------------------------- #
# B - a stray 「确认」 alone is not a settlement popup
# --------------------------------------------------------------------------- #

def test_a_stray_confirm_without_settlement_wording_is_not_clicked():
    session = PopupSession([
        [item("使用次数：10/25", 1200, 1246), item("确认", 1191, 996)],
    ])
    result = settlement(FakeClock()).settle(session)

    assert session.clicks == []
    assert result.confirms == 0
    assert result.closed is False
    assert result.status == "unverified"
    assert "not a settlement UI" in result.reason or "not a settlement popup" in result.reason


# --------------------------------------------------------------------------- #
# B - the normal three-popup chain
# --------------------------------------------------------------------------- #

def test_three_popup_chain_confirms_three_times_and_captures_xp_first():
    clock = FakeClock()
    victory = [item("胜利", 900, 700), item("确认", 1191, 996)]
    xp_popup = [item("XIN精灵获得经验：1710", 1100, 700),
                item("离升级还需经验：4650", 1100, 740),
                item("确认", 1188, 999)]
    reward = [item("恭喜你得到了5个积累经验", 1100, 700), item("确认", 1198, 983)]

    session = PopupSession([victory, xp_popup, reward, []],
                           on_click=lambda: clock.advance(0.25))
    result = settlement(clock).settle(session)

    assert result.status == "closed"
    assert result.closed is True
    assert result.confirms == 3
    assert [c[0] for c in session.clicks] == [0, 1, 2]     # one dispatch per popup
    assert [c[3] for c in session.clicks] == ["settle_确认"] * 3
    # every confirm landed on the CURRENT popup's button
    assert session.clicks[1][1:3] == (1188.0, 999.0)
    assert session.clicks[2][1:3] == (1198.0, 983.0)

    # the XP line was read before the click that hid it (step 3 no longer shows it)
    assert session.step == 3
    assert "1710" not in " ".join(str(i["text"]) for i in session.ocr())
    assert result.xp_texts == ["XIN精灵获得经验：1710"]

    # latency is measured from the first frame of each popup, not left at zero
    assert len(result.latencies) == 3
    assert all(value >= 0.0 for value in result.latencies)
    assert result.latencies[0] > 0.1
    assert max(result.latencies) < 1.0
    assert [round(v, 3) for v in result.latencies] == [0.25, 0.25, 0.25]


# --------------------------------------------------------------------------- #
# defects found by this review (xfail = kept evidence, not a weakened test)
# --------------------------------------------------------------------------- #

def test_window_pid_change_is_refused_when_the_probe_reports_one(tmp_path):
    client = FakeDaemon()
    guard = guard_for(tmp_path, client)
    guard.allow("a")

    client.win_reply["pid"] = 222              # same id and geometry, new process
    with pytest.raises(InputRefused) as excinfo:
        guard.allow("b")
    assert "window" in str(excinfo.value)


def test_a_profile_overlay_with_a_stray_confirm_is_not_clicked():
    session = PopupSession([[
        item("米米号：188365991", 700, 440),
        item("登船日期：2016-01-01", 700, 480),
        item("精灵信息", 700, 520),
        item("确认", 1191, 996),
    ]])
    result = settlement(FakeClock()).settle(session)

    assert session.clicks == []
    assert result.confirms == 0


def test_the_same_popup_with_ocr_jitter_is_not_confirmed_twice():
    class JitterSession:
        """Same pixels; a re-render after the first click drops one low-conf line."""

        def __init__(self) -> None:
            self.clicks: list[tuple[float, float, str]] = []
            self.clicked = False

        def ocr(self, min_conf: float = 0.2):  # noqa: ARG002
            popup = [item("胜利", 900, 700), item("确认", 1191, 996)]
            if not self.clicked:
                popup.append(item("奖励", 700, 800, conf=0.21))
            return popup

        def click_px(self, x: float, y: float, reason: str = "") -> bool:
            self.clicks.append((x, y, reason))
            self.clicked = True
            return True

    session = JitterSession()
    result = settlement(FakeClock()).settle(session)

    assert len(session.clicks) == 1, session.clicks
    assert result.confirms <= 1


def test_a_daemon_ocr_error_is_not_reported_as_closed(tmp_path):
    class ErrorClient(FakeDaemon):
        def send(self, op: str, **kwargs) -> dict:
            self.ops.append((op, kwargs))
            if op == "win":
                return dict(self.win_reply)
            if op == "ocr":
                return {"error": "no frame yet"}     # daemon says: no frame
            return {"ok": True}

    client = ErrorClient()
    session = DaemonSession(client, GEO)
    session.latest = lambda: np.zeros((1600, 2400, 3), dtype=np.uint8)
    result = settlement(FakeClock()).settle(session)

    assert result.status != "closed"
    assert result.closed is False
