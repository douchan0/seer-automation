"""Regression tests for the 2026-11-07 background-capture bug.

`tools/tower_grind.py` under `nohup` kept seeing a constant dark frame
`mean_rgb=(61,107,150)` while a foreground session saw the real tower picture.
That RGB is exactly a *solid* BGRA(150,107,61) frame: a placeholder/stalled warm
stream.  The capture layer used to accept any decodable array as "the game", so
the placeholder was silently used as content and the night's automation acted on
a dead picture.

These tests pin the two fixes:

* a uniform frame from a **warm** stream raises ``CaptureUnavailable`` (loud, with
  the mean colour in the message) instead of being returned as the game screen;
* a normal frame is still accepted, and every frame now carries
  ``meta["mean_rgb"]/["std"]/["uniform"]/["source_path"]`` so a probe can tell
  which process/path produced it.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from seerauto import capture as C
from seerauto import geometry as G
from seerauto.errors import CaptureUnavailable, DaemonError

GEOM = G.WindowGeometry(origin_pt=G.Point(0.0, 0.0),
                        size_pt=G.Size(1200.0, 800.0), scale=2.0)
W, H = 320, 200
STRIDE = W * 4


def _write_bgra(path: pathlib.Path, bgr, *, w: int = W, h: int = H) -> pathlib.Path:
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[:, :, 0] = bgr[0]
    frame[:, :, 1] = bgr[1]
    frame[:, :, 2] = bgr[2]
    frame[:, :, 3] = 255
    frame.tofile(path)
    return path


class FakeDaemon:
    """A stand-in for DaemonClient that answers the warm-session ops."""

    available = True

    def __init__(self, reply: dict):
        self.reply = dict(reply)

    def describe(self) -> str:
        return "fake-seerdx"

    def caps(self) -> set[str]:
        return {"capture.start", "capture.latest", "capture.roi", "capture.stop"}

    def capture_start(self, rect=None) -> dict:
        return {"ok": True, "seq": 1}

    def capture_latest(self) -> dict:
        return dict(self.reply)

    def capture_roi(self, rect) -> dict:
        reply = dict(self.reply)
        reply["rect"] = list(rect.as_tuple())
        return reply

    def close(self) -> None:
        pass


def _capture(tmp_path, *, uniform: bool, reject: bool = True) -> C.DaemonCapture:
    bgr = (150, 107, 61) if uniform else None
    if uniform:
        path = _write_bgra(tmp_path / "frame.bgra", bgr)
    else:
        frame = np.random.randint(0, 255, (H, W, 4), dtype=np.uint8)
        frame[:, :, 3] = 255
        path = tmp_path / "frame.bgra"
        frame.tofile(path)
    reply = {"fmt": "bgra", "path": str(path), "w": W, "h": H, "stride": STRIDE,
             "seq": 1, "ts_ns": 12345}
    return C.DaemonCapture(FakeDaemon(reply), GEOM, warm=True,
                           reject_uniform=reject)


def test_uniform_placeholder_frame_raises_instead_of_being_used(tmp_path):
    capture = _capture(tmp_path, uniform=True)
    with pytest.raises(CaptureUnavailable) as excinfo:
        capture.latest()
    message = str(excinfo.value)
    assert "placeholder" in message
    assert "(61.0, 107.0, 150.0)" in message      # the exact RGB reported live
    assert capture.uniform_frames_seen == 1
    assert capture.last_stats["uniform"] is True


def test_normal_frame_is_accepted_and_carries_diagnostics(tmp_path):
    capture = _capture(tmp_path, uniform=False)
    frame = capture.latest()
    assert frame.array.shape == (H, W, 3)
    assert frame.meta["uniform"] is False
    assert frame.meta["backend"] == "daemon"
    assert frame.meta["warm_session"] is True
    assert frame.meta["source_path"].endswith("frame.bgra")
    assert isinstance(frame.meta["mean_rgb"], list)
    assert frame.meta["std"] > 1.0
    assert frame.seq == 1


def test_reject_uniform_can_be_disabled_for_diagnostics(tmp_path):
    capture = _capture(tmp_path, uniform=True, reject=False)
    frame = capture.latest()
    assert frame.meta["uniform"] is True
    assert capture.uniform_frames_seen == 1


def test_reply_without_payload_is_a_daemon_error(tmp_path):
    reply = {"seq": 1, "ts_ns": 1}
    capture = C.DaemonCapture(FakeDaemon(reply), GEOM, warm=True)
    with pytest.raises(DaemonError) as excinfo:
        capture.latest()
    assert "no image payload" in str(excinfo.value)


def test_frame_stats_recognises_the_live_placeholder_colour():
    solid = np.zeros((H, W, 3), dtype=np.uint8)
    solid[:, :, 0] = 150   # B
    solid[:, :, 1] = 107   # G
    solid[:, :, 2] = 61    # R
    stats = C.frame_stats(solid)
    assert stats["uniform"] is True
    assert stats["mean_rgb"] == [61.0, 107.0, 150.0]
    assert stats["std"] == 0.0

    noise = np.random.randint(0, 255, (H, W, 3), dtype=np.uint8)
    assert C.frame_stats(noise)["uniform"] is False
