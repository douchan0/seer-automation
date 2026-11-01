"""Capture-layer tests.

Two of them run *real subprocesses*:

* ``FakeScreencapture`` proves the one-shot ``screencapture -R`` fallback path
  (argument building, size validation, permission error mapping) using a stub
  binary that writes a PNG;
* ``fake daemon`` speaks the real JSON-line protocol over stdin/stdout, so
  ``DaemonClient``/``DaemonCapture`` (warm session, ROI crop, one-shot
  downgrade, error mapping) are exercised end to end without the game.

Neither test needs the game client, ScreenCaptureKit, or network access.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from seerauto import capture as C
from seerauto import geometry as G
from seerauto.errors import (
    CaptureError,
    CapturePermissionDenied,
    CaptureUnavailable,
    DaemonError,
    WindowNotFound,
)

cv2 = pytest.importorskip("cv2")


# --------------------------------------------------------------------------- #
# helpers: dependency-free PNG writer (the stub processes must not need cv2)
# --------------------------------------------------------------------------- #
def write_png(path: str | os.PathLike[str], width: int, height: int, rgb=(12, 34, 56)) -> None:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    Path(path).write_bytes(png)


def make_image(width: int, height: int, value: int = 40) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


FAKE_SCREENCAPTURE = r'''
import sys, os
args = sys.argv[1:]
path = args[-1]
rect = args[args.index("-R") + 1] if "-R" in args else "0,0,1149,714"
x, y, w, h = (int(float(v)) for v in rect.split(","))
scale = float(os.environ.get("FAKE_SCALE", "2"))
bump = int(os.environ.get("FAKE_SIZE_BUMP", "0"))
import struct, zlib
def chunk(tag, data):
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
W, H = max(1, int(w * scale) + bump), max(1, int(h * scale) + bump)
raw = b"".join(b"\x00" + b"\x08\x10\x18" * W for _ in range(H))
png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
open(path, "wb").write(png)
if os.environ.get("FAKE_PERMISSION"):
    sys.stderr.write("Screen recording permission denied\n")
    sys.exit(1)
sys.exit(0)
'''


FAKE_DAEMON = r'''
import sys, os, json, struct, zlib

def write_png(path, w, h):
    w, h = max(1, int(w)), max(1, int(h))
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x11\x22\x33" * w for _ in range(h))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    open(path, "wb").write(png)

def done(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

outdir = os.environ.get("FAKE_OUT", "/tmp")
seq = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        done({"error": "bad json"})
        continue
    op = req.get("op", "")
    no_win = os.environ.get("FAKE_NOWIN")
    no_warm = os.environ.get("FAKE_NO_WARM")
    static = os.environ.get("FAKE_STATIC")
    if no_win and op in ("win", "capture.latest", "capture.roi", "shot"):
        done({"error": "no window"}); continue
    if op == "ping":
        done({"ok": True})
    elif op == "caps":
        ops = ["shot"] if no_warm else ["capture.start", "capture.latest", "capture.roi", "capture.stop", "shot"]
        done({"ops": ops})
    elif op == "win":
        done({"id": 7, "x": 300, "y": 120, "w": 1149, "h": 714})
    elif op == "capture.start":
        done({"error": "unknown op capture.start"} if no_warm else {"ok": True, "seq": 0})
    elif op == "capture.stop":
        done({"ok": True})
    elif op in ("capture.latest", "capture.roi"):
        rect = req.get("rect") if op == "capture.roi" else None
        x, y, w, h = rect if rect else (0, 0, 2298, 1428)
        if static:
            # change-driven source: the same frame keeps being the newest one
            seq, ts = 1, 1000
            path = os.path.join(outdir, "static.png")
        else:
            seq += 1
            ts = 1000 * seq
            path = os.path.join(outdir, "frame_%d.png" % seq)
        write_png(path, w, h)
        done({"seq": seq, "ts_ns": ts, "rect": [x, y, w, h], "path": path})
    elif op == "shot":
        rect = req.get("rect") or [0, 0, 2298, 1428]
        x, y, w, h = rect
        path = req.get("path") or os.path.join(outdir, "shot.png")
        write_png(path, w, h)
        done({"path": path, "w": int(w), "h": int(h)})
    elif op == "ocr":
        done({"items": [{"text": "当前回合：3", "conf": 0.9, "x": 0.4, "y": 0.8, "w": 0.2, "h": 0.05}]})
    elif op == "quit":
        done({"bye": True}); break
    else:
        done({"error": "unknown op " + op})
'''


def write_script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    if not body.startswith("#!"):
        body = f"#!{sys.executable}\n" + body
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def geometry() -> G.WindowGeometry:
    return G.WindowGeometry(origin_pt=G.Point(300, 120))


# --------------------------------------------------------------------------- #
# Frame + make_frame
# --------------------------------------------------------------------------- #
def test_make_frame_full_window(geometry):
    frame = C.make_frame(make_image(2298, 1428), geometry, seq=3, timestamp_ns=999)
    assert frame.size_px == G.Size(2298, 1428)
    assert frame.stage is not None
    assert frame.stage.stage_px.as_int_tuple() == (102, 128, 2092, 1220)
    assert frame.seq == 3 and frame.timestamp_ns == 999
    assert frame.origin_pt == G.Point(300, 120)
    assert frame.meta == {}


def test_make_frame_roi_offsets_stage_and_origin(geometry):
    rect = G.Rect(1000, 1100, 200, 100)
    frame = C.make_frame(make_image(200, 100), geometry, rect_game_px=rect)
    assert frame.stage is not None
    # stage origin relative to the ROI: (102-1000, 128-1100)
    assert frame.stage.stage_px.as_int_tuple() == (-898, -972, 2092, 1220)
    assert frame.stage.ref_offset == G.Point(102, 128)
    assert frame.stage.ref_size == G.Size(2092, 1220)
    assert frame.origin_pt == G.Point(300 + 1000 / 2, 120 + 1100 / 2)
    # the ROI's own top-left pixel maps back to the pointer we captured
    p = frame.global_pt_from_frame_px(0, 0)
    assert p == frame.origin_pt


def test_frame_crop_updates_origin_and_stage(geometry):
    frame = C.make_frame(make_image(2298, 1428), geometry)
    cropped = frame.crop(G.Rect(100, 200, 300, 150))
    assert cropped.size_px == G.Size(300, 150)
    assert cropped.origin_pt == G.Point(300 + 50, 120 + 100)
    assert cropped.stage is not None
    assert cropped.stage.stage_px.as_int_tuple() == (2, -72, 2092, 1220)
    assert cropped.meta["crop"] == (100, 200, 300, 150)


def test_frame_crop_rejects_outside(geometry):
    frame = C.make_frame(make_image(100, 100), geometry)
    with pytest.raises(CaptureError):
        frame.crop(G.Rect(200, 200, 10, 10))


def test_frame_freshness(geometry):
    a = C.make_frame(make_image(10, 10), geometry, seq=1, timestamp_ns=1_000_000)
    b = C.make_frame(make_image(10, 10), geometry, seq=2, timestamp_ns=2_000_000)
    assert b.is_newer_than(a)
    assert not a.is_newer_than(b)
    assert a.age_ms(now_ns=3_000_000) == pytest.approx(2.0)


def test_frame_unusable_array(geometry):
    frame = C.make_frame(np.zeros((0, 0, 3), dtype=np.uint8), geometry)
    assert not frame.is_usable()


# --------------------------------------------------------------------------- #
# SubprocessScreencapture
# --------------------------------------------------------------------------- #
def test_subprocess_screencapture_grabs_with_expected_size(tmp_path, geometry):
    stub = write_script(tmp_path, "screencapture", FAKE_SCREENCAPTURE)
    backend = C.SubprocessScreencapture(geometry, screencapture=stub)
    assert backend.available
    frame = backend.grab()
    assert frame.size_px == G.Size(2298, 1428)
    assert frame.stage is not None and frame.stage.stage_px.w == pytest.approx(2092)


def test_subprocess_screencapture_roi(tmp_path, geometry):
    stub = write_script(tmp_path, "screencapture", FAKE_SCREENCAPTURE)
    backend = C.SubprocessScreencapture(geometry, screencapture=stub)
    frame = backend.grab(G.Rect(500, 600, 100, 50))
    # game_px is already 2x pixel space: 100 game_px = 50pt = 100 device px
    assert frame.size_px == G.Size(100, 50)
    assert frame.origin_pt == G.Point(300 + 250, 120 + 300)


def test_subprocess_screencapture_size_mismatch_is_loud(tmp_path, geometry):
    stub = write_script(tmp_path, "screencapture", FAKE_SCREENCAPTURE)
    backend = C.SubprocessScreencapture(geometry, screencapture=stub)
    old = os.environ.get("FAKE_SCALE")
    os.environ["FAKE_SCALE"] = "1"
    try:
        with pytest.raises(CaptureError, match="size mismatch"):
            backend.grab()
    finally:
        if old is None:
            os.environ.pop("FAKE_SCALE", None)
        else:
            os.environ["FAKE_SCALE"] = old


def test_subprocess_screencapture_permission_maps_to_typed_error(tmp_path, geometry):
    stub = write_script(tmp_path, "screencapture", FAKE_SCREENCAPTURE)
    backend = C.SubprocessScreencapture(geometry, screencapture=stub)
    os.environ["FAKE_PERMISSION"] = "1"
    try:
        with pytest.raises(CapturePermissionDenied):
            backend.grab()
    finally:
        os.environ.pop("FAKE_PERMISSION", None)


def test_subprocess_screencapture_missing_binary(geometry):
    backend = C.SubprocessScreencapture(geometry, screencapture="/nonexistent/screencapture")
    assert not backend.available
    with pytest.raises(CaptureUnavailable):
        backend.grab()


# --------------------------------------------------------------------------- #
# ReplayCapture
# --------------------------------------------------------------------------- #
def test_replay_capture_sequence_and_loop(tmp_path, geometry):
    paths = []
    for i, (w, h) in enumerate(((2298, 1428), (1150, 715))):
        p = tmp_path / f"fixture_{i}.png"
        write_png(p, w, h, rgb=(i * 40, 0, 0))
        paths.append(p)
    backend = C.ReplayCapture(paths, geometry=geometry, loop=True)
    f1 = backend.grab()
    f2 = backend.grab()
    f3 = backend.grab()  # loops back
    assert (f1.size_px, f2.size_px, f3.size_px) == (
        G.Size(2298, 1428),
        G.Size(1150, 715),
        G.Size(2298, 1428),
    )
    assert [f1.seq, f2.seq, f3.seq] == [1, 2, 3]
    assert f1.meta["backend"] == "replay"


def test_replay_capture_exhausted(tmp_path):
    p = tmp_path / "only.png"
    write_png(p, 100, 50)
    backend = C.ReplayCapture([p])
    backend.grab()
    with pytest.raises(CaptureError, match="exhausted"):
        backend.grab()


def test_replay_capture_with_stage_detector_and_crop(tmp_path):
    p = tmp_path / "f.png"
    write_png(p, 2298, 1428)
    detector = lambda frame: G.Rect(10, 20, 1000, 800)
    backend = C.ReplayCapture([p], stage_detector=detector)
    frame = backend.grab()
    assert frame.stage is not None
    assert frame.stage.stage_px.as_int_tuple() == (10, 20, 1000, 800)
    # a game_px rect is mapped through the *detected* stage, not a fixed ratio
    mapped = frame.frame_rect_from_game_px(G.Rect(200, 300, 100, 50))
    assert mapped.as_int_tuple() == (57, 133, 48, 33)
    cropped = frame.crop(mapped)
    assert cropped.size_px == G.Size(48, 33)


def test_replay_capture_requires_paths():
    with pytest.raises(CaptureError):
        C.ReplayCapture([])


def test_null_capture_is_honest():
    backend = C.NullCapture("daemon missing")
    assert not backend.available
    with pytest.raises(CaptureUnavailable, match="daemon missing"):
        backend.grab()


def test_replay_files_discovery(tmp_path):
    write_png(tmp_path / "a.png", 4, 4)
    write_png(tmp_path / "b.PNG", 4, 4)
    (tmp_path / "note.txt").write_text("x")
    files = C.replay_files(tmp_path)
    assert [p.name for p in files] == ["a.png", "b.PNG"]
    with pytest.raises(CaptureError):
        C.replay_files(tmp_path / "missing")


# --------------------------------------------------------------------------- #
# daemon protocol (real subprocess, no game)
# --------------------------------------------------------------------------- #
@pytest.fixture
def daemon():
    created = []

    def _make(tmp_path, env=None, args=("-u",)):
        script = write_script(tmp_path, "fake_daemon.py", FAKE_DAEMON)
        client = C.DaemonClient(
            binary=sys.executable,
            args=[*args, script],
            timeout_s=5.0,
            env={"FAKE_OUT": str(tmp_path), **(env or {})},
        )
        created.append(client)
        return client

    yield _make
    for client in created:
        client.close()


def test_daemon_client_ping_and_caps(daemon, tmp_path):
    client = daemon(tmp_path)
    assert client.ping() == {"ok": True}
    assert "capture.roi" in client.caps()
    assert client.win()["id"] == 7


def test_daemon_capture_warm_roi(daemon, tmp_path, geometry):
    client = daemon(tmp_path)
    backend = C.DaemonCapture(client, geometry)
    assert backend.warm and backend.supports_roi
    frame = backend.grab(G.Rect(400, 500, 300, 200))
    assert frame.size_px == G.Size(300, 200)
    assert frame.seq == 1
    assert frame.timestamp_ns == 1000
    assert frame.origin_pt == G.Point(300 + 200, 120 + 250)
    assert frame.stage is not None
    assert frame.stage.stage_px.as_int_tuple() == (-298, -372, 2092, 1220)
    backend.close()
    assert not client.running


def test_daemon_capture_warm_without_roi_crops_locally(daemon, tmp_path, geometry):
    client = daemon(tmp_path)
    backend = C.DaemonCapture(client, geometry)
    # pretend the daemon only offers capture.latest
    backend._session_ops = {"capture.start", "capture.latest", "capture.stop"}
    assert not backend.supports_roi
    frame = backend.grab(G.Rect(400, 500, 300, 200))
    assert frame.size_px == G.Size(300, 200)
    assert frame.stage.stage_px.as_int_tuple() == (-298, -372, 2092, 1220)


def test_daemon_capture_downgrades_to_one_shot(daemon, tmp_path, geometry):
    client = daemon(tmp_path, env={"FAKE_NO_WARM": "1"})
    backend = C.DaemonCapture(client, geometry)
    assert not backend.warm
    assert backend.session_error
    frame = backend.grab(G.Rect(0, 0, 1149, 714))
    assert frame.size_px == G.Size(1149, 714)
    assert frame.stage is not None


def test_daemon_no_window_is_typed(daemon, tmp_path, geometry):
    client = daemon(tmp_path, env={"FAKE_NOWIN": "1"})
    backend = C.DaemonCapture(client, geometry)
    with pytest.raises(WindowNotFound):
        backend.grab()
    assert client.win()["error"] == "no window"


def test_daemon_missing_binary_is_unavailable(geometry):
    client = C.DaemonClient(binary="/nonexistent/seerd")
    assert not client.available
    with pytest.raises(CaptureUnavailable):
        C.DaemonCapture(client, geometry)


def test_daemon_start_without_binary_raises():
    client = C.DaemonClient(binary=None)
    with pytest.raises(CaptureUnavailable):
        client.start()
    assert client.describe() == "not configured"


def test_daemon_error_reply_raises():
    client = C.DaemonClient(binary=None)
    with pytest.raises(DaemonError):
        client._write({"op": "ping"})  # not running -> typed error


def test_build_backend_unknown_and_none(geometry):
    with pytest.raises(CaptureUnavailable):
        C.build_backend("mystery", geometry)
    backend = C.build_backend("none", geometry)
    assert isinstance(backend, C.NullCapture)
    one_shot = C.build_backend("screencapture", geometry)
    assert isinstance(one_shot, C.SubprocessScreencapture)


def test_build_backend_daemon_missing_binary(geometry):
    with pytest.raises(CaptureUnavailable):
        C.build_backend("daemon", geometry, daemon_binary="/nonexistent/seerd")


def test_daemon_client_timeout(daemon, tmp_path):
    script = tmp_path / "silent.py"
    script.write_text("import time\nwhile True: time.sleep(1)\n")
    client = C.DaemonClient(
        binary=sys.executable, args=["-u", str(script)], timeout_s=0.2
    )
    try:
        with pytest.raises(DaemonError, match="timeout"):
            client.send("ping")
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# non-blocking latest() / synthetic replay clock (Lead-mandated behaviours)
# --------------------------------------------------------------------------- #
def test_daemon_latest_returns_stale_frame_without_blocking(daemon, tmp_path, geometry):
    """A change-driven source returns the same frame; ``latest`` must not wait."""
    client = daemon(tmp_path, env={"FAKE_STATIC": "1"})
    backend = C.DaemonCapture(client, geometry)
    first = backend.latest()
    second = backend.latest()
    assert first.seq == second.seq == 1
    assert first.timestamp_ns == second.timestamp_ns == 1000
    # the frame is stale but valid: age is computed from its own timestamp
    assert second.age_ms(now_ns=1000 + 5_000_000) == pytest.approx(5.0)
    # no blocking on new content: two calls take microseconds, not 378 ms
    started = time.perf_counter()
    for _ in range(5):
        backend.latest()
    assert (time.perf_counter() - started) < 0.5


def test_replay_latest_serves_frames_on_a_synthetic_clock(tmp_path, geometry):
    paths = []
    for i, shade in enumerate((10, 200)):
        p = tmp_path / f"clock_{i}.png"
        write_png(p, 60, 40, rgb=(shade, shade, shade))
        paths.append(p)
    now = [0]
    backend = C.ReplayCapture(paths, geometry=geometry, clock=lambda: now[0], dwell_s=0.25)
    first = backend.latest()
    assert first.seq == 1
    now[0] = 100_000_000  # 100 ms < dwell: same frame, same seq
    same = backend.latest()
    assert same.seq == 1
    now[0] = 300_000_000  # past dwell: next frame
    advanced = backend.latest()
    assert advanced.seq == 2


def test_replay_measures_real_decode_time(tmp_path, geometry):
    p = tmp_path / "decode.png"
    write_png(p, 400, 300)
    backend = C.ReplayCapture([p], geometry=geometry)
    frame = backend.grab()
    # cv2.imread forces the decode (unlike CGImageSourceCreateImageAtIndex), and
    # the backend must report it so the executor can book it under capture_copy.
    assert frame.decode_ns > 0
