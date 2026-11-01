"""Window discovery / identity tests - no game required.

The external finder is exercised with *real subprocesses* printing the same
JSON shape ``scripts/seerctl wl`` produces, so the parsing path is genuinely
tested rather than mocked.
"""

from __future__ import annotations

import json
import sys

import pytest

from seerauto import geometry as G
from seerauto import window as W
from seerauto.errors import WindowMoved, WindowNotFound

WIN_JSON = [
    {"id": 101, "owner": "Window Server", "title": "StatusIndicator", "layer": 2147483630,
     "bounds": {"x": 0, "y": 0, "w": 10, "h": 18}},
    {"id": 555, "owner": "SeerGame", "title": "xin计划 (1.0.8.9)", "layer": 0,
     "bounds": {"x": 300, "y": 120, "w": 1149, "h": 714}},
    {"id": 556, "owner": "SeerGame", "title": "helper", "layer": 0,
     "bounds": {"x": 10, "y": 10, "w": 40, "h": 30}},
    {"id": 557, "owner": "SeerGame", "title": "menu", "layer": 25,
     "bounds": {"x": 0, "y": 0, "w": 300, "h": 200}},
]


def _finder(command, **kwargs):
    return W.ExternalJSONWindowFinder(command=command, **kwargs)


def _cmd(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_parse_windows_filters_layer_and_size():
    finder = _finder(_cmd(f"import json;print(json.dumps({WIN_JSON!r}))"))
    windows = finder.list_windows()
    ids = {w.window_id for w in windows}
    assert 555 in ids  # the real game window
    assert 556 in ids  # small window is listed (filtering is by owner/area later)
    assert 557 not in ids  # layer != 0
    assert 101 not in ids  # non-zero window layers are always filtered


def test_find_picks_largest_matching_window():
    finder = _finder(_cmd(f"import json;print(json.dumps({WIN_JSON!r}))"))
    info = finder.find()
    assert info.window_id == 555
    assert info.geometry.origin_pt == G.Point(300, 120)
    assert info.size_pt == G.Size(1149, 714)
    assert info.pixel_size == G.Size(2298, 1428)


def test_find_owner_alias_case_insensitive():
    payload = [{"id": 9, "owner": "SEERGAME", "title": "", "layer": 0,
                "bounds": {"x": 1, "y": 2, "w": 1149, "h": 714}}]
    finder = _finder(_cmd(f"import json;print(json.dumps({payload!r}))"))
    assert finder.find().window_id == 9


def test_find_no_match_reports_reason():
    finder = _finder(_cmd("print('[]')"))
    with pytest.raises(WindowNotFound) as exc:
        finder.find()
    assert "no window matching" in str(exc.value)
    assert finder.last_error is None


def test_unparsable_output_is_reported_not_raised():
    finder = _finder(_cmd("print('not json at all')"))
    assert finder.list_windows() == []
    assert finder.last_error and "unparsable" in finder.last_error
    with pytest.raises(WindowNotFound):
        finder.find()


def test_missing_command_is_unavailable():
    finder = _finder(["/nonexistent/seerctl-does-not-exist", "wl"])
    assert not finder.available
    assert finder.list_windows() == []
    with pytest.raises(WindowNotFound):
        finder.find()


def test_json_lines_output_parsed():
    code = (
        "print('log noise')\n"
        "print(json.dumps([{'id': 1, 'owner': 'SeerGame', 'title': 't', 'layer': 0,"
        " 'bounds': {'x': 0, 'y': 0, 'w': 1149, 'h': 714}}]))\n"
    ).replace("print(json.dumps", "import json;print(json.dumps")
    finder = _finder(_cmd(code))
    assert finder.find().window_id == 1


# --------------------------------------------------------------------------- #
# static / daemon finders
# --------------------------------------------------------------------------- #
def test_static_finder():
    geo = G.WindowGeometry(origin_pt=G.Point(300, 120))
    finder = W.StaticWindowFinder(geo)
    info = finder.find()
    assert info.geometry.is_close(geo)


def test_daemon_finder_with_fake_client():
    class Client:
        available = True

        def win(self):
            return {"id": 77, "x": 300, "y": 120, "w": 1149, "h": 714}

    finder = W.DaemonWindowFinder(Client())
    info = finder.find()
    assert info.window_id == 77
    assert info.size_pt == G.Size(1149, 714)


def test_daemon_finder_no_window():
    class Client:
        available = True

        def win(self):
            return {"error": "no window"}

    finder = W.DaemonWindowFinder(Client())
    with pytest.raises(WindowNotFound):
        finder.find()
    assert finder.last_error == "no window"


# --------------------------------------------------------------------------- #
# composite discovery
# --------------------------------------------------------------------------- #
def test_find_window_aggregates_report():
    geo = G.WindowGeometry(origin_pt=G.Point(300, 120))
    info, report = W.find_window([W.StaticWindowFinder(geo)])
    assert info.geometry.is_close(geo)
    assert report.found is info
    assert report.attempts[0][0] == "static"


def test_find_window_not_found_lists_all_attempts():
    class Empty:
        name = "empty"

        available = True
        last_error = "demo failure"

        def list_windows(self):
            return []

        def find(self):
            raise WindowNotFound("nope")

    with pytest.raises(WindowNotFound) as exc:
        W.find_window([Empty(), Empty()])
    assert "demo failure" in str(exc.value)


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def _info(origin=(300, 120), size=(1149, 714), wid=5):
    return W.WindowInfo(
        window_id=wid,
        owner="SeerGame",
        title="t",
        geometry=G.WindowGeometry(origin_pt=G.Point(*origin), size_pt=G.Size(*size)),
    )


def test_window_matches_tolerance():
    assert _info().matches(_info(origin=(301, 121)), tol_pt=2)
    assert not _info().matches(_info(origin=(310, 120)), tol_pt=2)


def test_window_matches_rejects_size_change():
    assert not _info().matches(_info(size=(1000, 700)), tol_pt=2)


def test_assert_matches_raises_window_moved_with_detail():
    with pytest.raises(WindowMoved) as exc:
        _info().assert_matches(_info(origin=(900, 500)))
    message = str(exc.value)
    assert "expected" in message and "found" in message


def test_window_describe_mentions_identity():
    text = _info().describe()
    for part in ("id=5", "SeerGame", "1149x714", "scale=2.00"):
        assert part in text


# --------------------------------------------------------------------------- #
# pinning + scale probe
# --------------------------------------------------------------------------- #
def test_pin_window_roundtrip():
    class Pinner:
        def __init__(self):
            self.rect = None

        def pin(self, rect_pt):
            self.rect = rect_pt
            return {"id": 12, "x": rect_pt.x, "y": rect_pt.y, "w": rect_pt.w, "h": rect_pt.h}

    pinner = Pinner()
    info = W.pin_window(pinner, G.Rect(300, 120, 1149, 714))
    assert pinner.rect.as_tuple() == (300.0, 120.0, 1149.0, 714.0)
    assert info.window_id == 12
    assert info.geometry.origin_pt == G.Point(300, 120)


def test_pin_window_error_is_typed():
    class Pinner:
        def pin(self, rect_pt):
            return {"error": "no ax window"}

    with pytest.raises(WindowNotFound, match="pin failed"):
        W.pin_window(Pinner(), G.Rect(0, 0, 1149, 714))


def test_probe_capture_scale():
    assert W.probe_capture_scale(lambda: (2298, 1428), G.Rect(0, 0, 1149, 714)) == 2.0
    assert W.probe_capture_scale(lambda: (1149, 714), G.Rect(0, 0, 1149, 714)) == 1.0
    # failure keeps the documented fallback
    def boom():
        raise RuntimeError("no permission")

    assert W.probe_capture_scale(boom, G.Rect(0, 0, 1149, 714), fallback=1.5) == 1.5
    assert W.probe_capture_scale(lambda: (0, 0), G.Rect(0, 0, 1149, 714), fallback=2.5) == 2.5


def test_default_owner_aliases_cover_client_names():
    assert W._owner_matches("SeerGame", "", W.DEFAULT_OWNER_ALIASES)
    assert W._owner_matches("Electron", "xin计划 (1.0.8.9)", W.DEFAULT_OWNER_ALIASES)
    assert not W._owner_matches("Safari", "seer puni", W.DEFAULT_OWNER_ALIASES)
