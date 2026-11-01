"""Input-guard tests (``KNOWN_PROBLEMS.md`` P6: never click after a move)."""

from __future__ import annotations

import pytest

from seerauto import geometry as G
from seerauto.actions import (
    Actions,
    ClickAction,
    ClickPointAction,
    DaemonInput,
    RecordingInput,
    StopAction,
)
from seerauto.config import load_config
from seerauto.errors import SafetyViolation, WindowMoved, WindowNotFound
from seerauto.window import WindowInfo


def _window(origin=(300, 120), size=(1149, 714), wid=7) -> WindowInfo:
    return WindowInfo(
        window_id=wid,
        owner="SeerGame",
        title="t",
        geometry=G.WindowGeometry(origin_pt=G.Point(*origin), size_pt=G.Size(*size)),
    )


class FakeFinder:
    name = "fake"

    def __init__(self, window: WindowInfo, exc: Exception | None = None):
        self.window = window
        self.exc = exc
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def list_windows(self):
        return [self.window]

    def find(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.window


def test_click_converts_game_px_to_global_and_verifies():
    backend = RecordingInput()
    finder = FakeFinder(_window())
    actions = Actions(backend, _window(), finder=finder, tolerance_pt=2.0)
    record = actions.click_game_px(1151, 1281, label="skill")
    assert record.verified == "live"
    assert finder.calls == 1
    assert len(backend.calls) == 1
    kind, payload = backend.calls[0]
    assert kind == "click"
    (x, y), clicks = payload
    assert x == pytest.approx(300 + 1151 / 2)
    assert y == pytest.approx(120 + 1281 / 2)
    assert clicks == 1
    assert actions.records == [record]


def test_click_refused_when_window_moved():
    backend = RecordingInput()
    moved = _window(origin=(900, 500))
    actions = Actions(backend, _window(), finder=FakeFinder(moved), tolerance_pt=2.0)
    with pytest.raises(WindowMoved):
        actions.click_game_px(500, 500)
    assert backend.calls == []
    assert actions.records == []


def test_click_refused_when_window_gone():
    backend = RecordingInput()
    finder = FakeFinder(_window(), exc=WindowNotFound("no window"))
    actions = Actions(backend, _window(), finder=finder)
    with pytest.raises(WindowNotFound):
        actions.click_game_px(500, 500)
    assert backend.calls == []


def test_click_outside_window_is_refused():
    backend = RecordingInput()
    actions = Actions(backend, _window(), finder=FakeFinder(_window()))
    with pytest.raises(SafetyViolation, match="outside the game window"):
        actions.click_game_px(5000, 100)
    assert backend.calls == []


def test_sub_pixel_movement_inside_tolerance_is_followed():
    backend = RecordingInput()
    slightly_moved = _window(origin=(301, 121))
    actions = Actions(backend, _window(), finder=FakeFinder(slightly_moved), tolerance_pt=2.0)
    record = actions.click_game_px(1000, 1000)
    (x, y), _ = backend.calls[0][1]
    # click follows the *current* geometry, not the stale expected one
    assert record.global_pt == (x, y)
    assert x == pytest.approx(301 + 500)
    assert y == pytest.approx(121 + 500)


def test_dry_run_records_without_input():
    backend = RecordingInput()
    actions = Actions(backend, _window(), finder=FakeFinder(_window()), dry_run=True)
    record = actions.click_game_px(1151, 1281, label="skill")
    assert backend.calls == []
    assert record.dry_run is True
    assert record.verified == "dry-run"
    assert record.game_px == (1151.0, 1281.0)


def test_static_mode_is_labelled_not_pretended():
    backend = RecordingInput()
    actions = Actions(backend, _window(), finder=None)
    record = actions.click_game_px(10, 10)
    assert record.verified == "static"
    assert len(backend.calls) == 1


def test_execute_click_action_uses_config_coordinates():
    config = load_config()
    backend = RecordingInput()
    actions = Actions(
        backend,
        _window(origin=tuple(config.window.expected_origin_pt or (0, 0))),
        finder=None,
        dry_run=True,
    )
    records = actions.execute(ClickAction(targets=("fight", "skill"), label="round"), config)
    assert len(records) == 2
    # 战斗按钮 (1789,1103) then 技能 (1151,1281), config -> game_px -> global pt
    assert records[0].game_px == (1789.0, 1103.0)
    assert records[1].game_px == (1151.0, 1281.0)
    assert records[0].global_pt == (300 + 1789 / 2, 120 + 1103 / 2)


def test_execute_click_point_and_stop_guard():
    config = load_config()
    actions = Actions(RecordingInput(), _window(), dry_run=True)
    records = actions.execute(ClickPointAction(x=10, y=20, label="p"), config)
    assert records[0].game_px == (10.0, 20.0)
    with pytest.raises(SafetyViolation):
        actions.execute(StopAction(reason="x"), config)


def test_daemon_input_sends_global_points():
    class FakeClient:
        available = True

        def __init__(self):
            self.sent = []

        def send(self, op, **kwargs):
            self.sent.append((op, kwargs))
            return {"ok": True}

    client = FakeClient()
    backend = DaemonInput(client)
    backend.click_global(G.Point(500.5, 600.25), clicks=2)
    backend.move_global(G.Point(1, 2))
    backend.key(53)
    backend.type_text("hi")
    assert client.sent[0] == ("click", {"x": 500.5, "y": 600.25, "n": 2})
    assert client.sent[1] == ("move", {"x": 1, "y": 2})
    assert client.sent[2] == ("key", {"code": 53})
    assert client.sent[3] == ("type", {"text": "hi"})


def test_recording_input_forwards():
    class Sink:
        name = "sink"
        available = True

        def __init__(self):
            self.calls = []

        def click_global(self, point, clicks=1):
            self.calls.append(("click", point.as_tuple(), clicks))

        def move_global(self, point):
            self.calls.append(("move", point.as_tuple()))

        def key(self, code):
            self.calls.append(("key", code))

        def type_text(self, text):
            self.calls.append(("type", text))

    sink = Sink()
    recording = RecordingInput(forward=sink)
    recording.click_global(G.Point(1, 2), 1)
    assert sink.calls == [("click", (1, 2), 1)]
    assert recording.calls == [("click", ((1, 2), 1))]
