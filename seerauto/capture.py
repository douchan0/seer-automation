"""Capture backends: one protocol, several honest implementations.

The engine never grabs more than it needs and never sleeps waiting for a
picture.  A backend returns a :class:`Frame` that carries its own clock
(``timestamp_ns`` monotonic) and sequence number, so the executor can ask
"is this newer / different?" instead of waiting blindly.

Backends
--------
``SubprocessScreencapture``
    One ``/usr/sbin/screencapture -x -R x,y,w,h`` invocation per frame.
    Measured ~139 ms/window on this host: correct but far too slow for a battle
    loop.  Kept as the *honest working fallback*.
``DaemonCapture``
    Talks JSON lines to a native daemon (stdin/stdout) which owns a warm
    ScreenCaptureKit session.  The backend inside the daemon is invisible to
    Python: see ``PROTOCOL.md`` in this package for the exact ops.  The binary
    path is configurable and may be absent - construction then raises
    :class:`CaptureUnavailable`, never a mystery traceback.
``ReplayCapture``
    Replays PNG files from disk (the ``artifacts/`` fixtures).  No window, no
    daemon, no game required - this is what the test-suite and
    ``python3 -m seerauto replay`` use.
``NullCapture``
    Always raises; lets ``doctor`` report "capture: NOT AVAILABLE" cleanly.

Coordinate contract
-------------------
``grab(rect)`` takes ``rect`` in **game_px** space (window-local pixels of the
2x reference capture, i.e. the space of ``docs/state.md``), with ``None``
meaning the whole window.  The returned ``Frame`` exposes the BGR array, the
global logical origin of its top-left pixel, the pixels-per-point ``scale``,
plus a :class:`seerauto.geometry.StageMap` so vision rules can work in
letterbox-free canonical stage coordinates.
"""

from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import numpy as np

from . import geometry as G
from .errors import (
    CaptureError,
    CapturePermissionDenied,
    CaptureUnavailable,
    DaemonError,
    WindowNotFound,
)

FULL_FRAME_PX = G.Rect(0.0, 0.0, *G.CAPTURE_PX)


# --------------------------------------------------------------------------- #
# cv2 helpers (imported lazily so doctor works without .pydeps on PYTHONPATH)
# --------------------------------------------------------------------------- #
def load_cv2():
    """Import cv2 or raise a *typed*, actionable error."""
    try:
        import cv2  # noqa: F401
    except Exception as exc:  # pragma: no cover - host dependent
        raise CaptureUnavailable(
            f"cv2 is not importable ({exc}); run with PYTHONPATH=.pydeps "
            "(see requirements.txt)"
        ) from exc
    return cv2


def decode_png_bytes(data: bytes) -> np.ndarray:
    cv2 = load_cv2()
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise CaptureError("could not decode PNG bytes from capture backend")
    return img


def read_image(path: str | os.PathLike[str]) -> np.ndarray:
    """Read a PNG/BMP into BGR without depending on PIL."""
    cv2 = load_cv2()
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise CaptureError(f"could not read image: {path}")
    return img


def read_raw_bgra(path: str | os.PathLike[str], w: int, h: int, stride: int) -> np.ndarray:
    """Read a raw BGRA frame written by the native daemon into a BGR array.

    Raw is used instead of PNG because PNG encode+decode costs tens of ms per
    frame, which would cancel out the entire point of a warm ScreenCaptureKit
    session.  A raw read is one file read plus one channel slice.
    """
    need = stride * h
    if w <= 0 or h <= 0 or stride < w * 4:
        raise CaptureError(f"bad raw frame geometry w={w} h={h} stride={stride}")
    raw = np.fromfile(str(path), dtype=np.uint8)
    if raw.size < need:
        raise CaptureError(
            f"short raw frame: {path} has {raw.size} bytes, expected >= {need} "
            f"(w={w} h={h} stride={stride})"
        )
    # BGRA rows -> (h, w, 4); dropping alpha leaves B, G, R which is exactly BGR.
    bgra = raw[:need].reshape(h, stride)[:, : w * 4].reshape(h, w, 4)
    return np.ascontiguousarray(bgra[:, :, :3])


# --------------------------------------------------------------------------- #
# frame
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    """One captured image plus everything needed to reason about its pixels."""

    array: np.ndarray
    origin_pt: G.Point = G.Point(0.0, 0.0)
    scale: float = G.RETINA_SCALE
    seq: int = 0
    timestamp_ns: int = 0
    stage: G.StageMap | None = None
    decode_ns: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    # -- shape ------------------------------------------------------------- #
    @property
    def width(self) -> int:
        return int(self.array.shape[1])

    @property
    def height(self) -> int:
        return int(self.array.shape[0])

    @property
    def size_px(self) -> G.Size:
        return G.Size(float(self.width), float(self.height))

    @property
    def full_rect_px(self) -> G.Rect:
        return G.Rect(0.0, 0.0, float(self.width), float(self.height))

    def is_usable(self) -> bool:
        return (
            isinstance(self.array, np.ndarray)
            and self.array.ndim == 3
            and self.array.shape[2] == 3
            and self.width > 0
            and self.height > 0
        )

    # -- time -------------------------------------------------------------- #
    def age_ms(self, now_ns: int | None = None) -> float:
        if not self.timestamp_ns:
            return float("nan")
        now = time.monotonic_ns() if now_ns is None else now_ns
        return max(0.0, (now - self.timestamp_ns) / 1e6)

    def is_newer_than(self, other: "Frame | None") -> bool:
        if other is None:
            return True
        if self.seq and other.seq:
            return self.seq > other.seq
        return self.timestamp_ns > other.timestamp_ns

    # -- coordinates ------------------------------------------------------- #
    def global_pt_from_frame_px(self, px: float, py: float) -> G.Point:
        """Frame pixel -> global logical point (what CGEvent needs)."""
        return G.Point(self.origin_pt.x + px / self.scale, self.origin_pt.y + py / self.scale)

    def frame_px_from_global_pt(self, p: G.Point) -> G.Point:
        return G.Point((p.x - self.origin_pt.x) * self.scale, (p.y - self.origin_pt.y) * self.scale)

    def frame_rect_from_game_px(self, rect: G.Rect) -> G.Rect:
        """Map a ``game_px`` rect into this frame (uses the stage map if set)."""
        if self.stage is not None:
            return self.stage.frame_rect_from_game_px(rect)
        # no stage knowledge: assume the frame covers the window 1:1 at scale
        k = self.scale / G.RETINA_SCALE
        return rect.scale(k)

    def crop(self, rect_px: G.Rect, copy: bool = False) -> "Frame":
        """Crop in frame pixels; keeps origin/scale/stage consistent."""
        r = rect_px.clamp_to(self.full_rect_px).as_int_tuple()
        x, y, w, h = r
        if w <= 0 or h <= 0:
            raise CaptureError(f"empty crop rect {rect_px} for frame {self.size_px}")
        sub = self.array[y : y + h, x : x + w]
        if copy:
            sub = sub.copy()
        stage = self.stage.translate(-x, -y) if self.stage is not None else None
        # the cropped frame's top-left pixel sits inside the original frame, so
        # its global origin moves with the crop (input mapping depends on this).
        new_origin = G.Point(
            self.origin_pt.x + x / self.scale,
            self.origin_pt.y + y / self.scale,
        )
        meta = {**self.meta, "crop": (x, y, w, h)}
        return replace(self, array=sub, origin_pt=new_origin, stage=stage, meta=meta)

    def with_stage(self, stage: G.StageMap) -> "Frame":
        return replace(self, stage=stage)

    def describe(self) -> str:
        return (
            f"{self.width}x{self.height}px seq={self.seq} "
            f"origin=({self.origin_pt.x:.1f},{self.origin_pt.y:.1f})pt "
            f"scale={self.scale:.2f} stage={self.stage.stage_px.as_int_tuple() if self.stage else None}"
        )


def make_frame(
    array: np.ndarray,
    geometry: G.WindowGeometry,
    rect_game_px: G.Rect | None = None,
    seq: int = 0,
    timestamp_ns: int | None = None,
    meta: Mapping[str, Any] | None = None,
    ref: G.ReferenceSpace = G.DEFAULT_REFERENCE,
    decode_ns: int = 0,
) -> Frame:
    """Build a window-scoped frame with a correct stage map.

    ``rect_game_px`` is the *window-local* region the array covers; the frame's
    global origin is derived from the window geometry so a cropped ROI still
    knows exactly where it is on screen.
    """
    if rect_game_px is None:
        rect_game_px = G.Rect(0.0, 0.0, *ref.capture_px.as_tuple())
    scale = geometry.scale
    origin = geometry.global_pt_from_game_px(rect_game_px.x, rect_game_px.y)
    window_px = geometry.pixel_size
    stage_window = ref.stage_px.scale(window_px.w / ref.capture_px.w, window_px.h / ref.capture_px.h)
    # array pixels may have a different density than game_px if the backend
    # captured at another scale; normalise the stage into the *array* space.
    kx = float(array.shape[1]) / rect_game_px.w if rect_game_px.w else 1.0
    ky = float(array.shape[0]) / rect_game_px.h if rect_game_px.h else 1.0
    stage_in_frame = G.Rect(
        (stage_window.x - rect_game_px.x) * kx,
        (stage_window.y - rect_game_px.y) * ky,
        stage_window.w * kx,
        stage_window.h * ky,
    )
    return Frame(
        array=array,
        origin_pt=origin,
        scale=scale,
        seq=seq,
        timestamp_ns=timestamp_ns if timestamp_ns is not None else time.monotonic_ns(),
        decode_ns=int(decode_ns),
        stage=G.StageMap(
            stage_px=stage_in_frame,
            ref_size=ref.stage_px.size,
            ref_offset=G.Point(ref.stage_px.x, ref.stage_px.y),
        ),
        meta=dict(meta or {}),
    )


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #
class CaptureBackend(Protocol):
    """Everything the engine needs from a capture source.

    ``latest()`` is the hot-loop call: it must return the newest frame the
    backend already has, **without waiting for new content**.  ScreenCaptureKit
    is change-driven (a static window can go ~378 ms without a frame), so a
    blocking "wait for the next frame" API would stall the loop.  One-shot
    backends implement it as ``grab()``.
    """

    name: str

    @property
    def available(self) -> bool: ...

    def grab(self, rect: G.Rect | None = None) -> Frame: ...

    def latest(self, rect: G.Rect | None = None) -> Frame: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# one-shot screencapture fallback
# --------------------------------------------------------------------------- #
class SubprocessScreencapture:
    """``/usr/sbin/screencapture -x -R`` fallback (~139 ms per grab)."""

    name = "screencapture"

    def __init__(
        self,
        geometry: G.WindowGeometry,
        screencapture: str = "/usr/sbin/screencapture",
        tmpdir: str | None = None,
        ref: G.ReferenceSpace = G.DEFAULT_REFERENCE,
    ):
        self.geometry = geometry
        self.screencapture = screencapture
        self.tmpdir = tmpdir
        self.ref = ref
        self._seq = 0

    @property
    def available(self) -> bool:
        return Path(self.screencapture).exists()

    def _target_path(self) -> str:
        fd, path = tempfile.mkstemp(prefix="seerauto-", suffix=".png", dir=self.tmpdir)
        os.close(fd)
        return path

    def grab(self, rect: G.Rect | None = None) -> Frame:
        if not self.available:
            raise CaptureUnavailable(f"screencapture not found at {self.screencapture}")
        rect = rect or FULL_FRAME_PX
        global_rect = self.geometry.global_rect_from_game_px(rect)
        x, y, w, h = global_rect.as_int_tuple()
        if w <= 0 or h <= 0:
            raise CaptureError(f"refusing zero-size capture rect {global_rect}")
        path = self._target_path()
        try:
            proc = subprocess.run(
                [self.screencapture, "-x", "-R", f"{x},{y},{w},{h}", path],
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                if "permission" in stderr.lower() or "not authorized" in stderr.lower():
                    raise CapturePermissionDenied(f"screencapture denied: {stderr}")
                raise CaptureError(f"screencapture failed rc={proc.returncode}: {stderr}")
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                raise CaptureError("screencapture produced no output (window off-screen?)")
            decode_started = time.perf_counter_ns()
            array = read_image(path)
            decode_ns = time.perf_counter_ns() - decode_started
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        expected_w = max(1, int(round(rect.w / G.RETINA_SCALE * self.geometry.scale)))
        expected_h = max(1, int(round(rect.h / G.RETINA_SCALE * self.geometry.scale)))
        if abs(array.shape[1] - expected_w) > 2 or abs(array.shape[0] - expected_h) > 2:
            raise CaptureError(
                "capture size mismatch: got "
                f"{array.shape[1]}x{array.shape[0]}px for a {rect.w:.0f}x{rect.h:.0f} game_px rect "
                f"(expected ~{expected_w}x{expected_h}); screen scale likely changed"
            )
        self._seq += 1
        return make_frame(
            array,
            self.geometry,
            rect,
            seq=self._seq,
            meta={"backend": self.name, "rect": rect.as_tuple()},
            ref=self.ref,
            decode_ns=decode_ns,
        )

    def latest(self, rect: G.Rect | None = None) -> Frame:
        """One-shot backend: ``latest`` is a fresh grab (no warm session)."""
        return self.grab(rect)

    def close(self) -> None:  # nothing persistent to release
        return None


# --------------------------------------------------------------------------- #
# daemon client
# --------------------------------------------------------------------------- #
class DaemonClient:
    """Line-oriented JSON client for the native capture/input daemon.

    The protocol is documented in ``seerauto/PROTOCOL.md``.  The daemon owns a
    warm ScreenCaptureKit session; Python never cares which API produced a
    frame.  If the binary is missing, :attr:`available` is False and
    construction of :class:`DaemonCapture` raises ``CaptureUnavailable``.
    """

    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        args: Sequence[str] = (),
        timeout_s: float = 10.0,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        startup_timeout_s: float = 5.0,
    ):
        self.binary = Path(binary) if binary else None
        self.args = list(args)
        self.timeout_s = timeout_s
        self.env = {**os.environ, **env} if env else None
        self.cwd = str(cwd) if cwd else None
        self.startup_timeout_s = startup_timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._caps: set[str] | None = None

    # -- availability ------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self.binary is not None and self.binary.exists() and os.access(self.binary, os.X_OK)

    def describe(self) -> str:
        if self.binary is None:
            return "not configured"
        if not self.binary.exists():
            return f"{self.binary} (missing)"
        return f"{self.binary}{' ' + ' '.join(self.args) if self.args else ''}"

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> "DaemonClient":
        if self._proc is not None:
            return self
        if not self.available:
            raise CaptureUnavailable(f"daemon binary not available: {self.describe()}")
        try:
            self._proc = subprocess.Popen(
                [str(self.binary), *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
                cwd=self.cwd,
                bufsize=0,
            )
        except OSError as exc:
            raise CaptureUnavailable(f"cannot start daemon {self.binary}: {exc}") from exc
        return self

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._write({"op": "quit"})
        except Exception:  # noqa: BLE001 - best effort shutdown
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- wire -------------------------------------------------------------- #
    def _write(self, payload: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise DaemonError("daemon is not running")
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise DaemonError(f"daemon pipe closed while sending {payload.get('op')!r}: {exc}") from exc

    def _read_line(self, timeout_s: float | None = None) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise DaemonError("daemon is not running")
        timeout_s = self.timeout_s if timeout_s is None else timeout_s
        fd = self._proc.stdout.fileno()
        deadline = time.monotonic() + timeout_s
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DaemonError(f"daemon reply timeout after {timeout_s:.1f}s")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise DaemonError(f"daemon reply timeout after {timeout_s:.1f}s")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise DaemonError("daemon exited unexpectedly")
            chunks.extend(chunk)
            newline = chunks.find(b"\n")
            if newline >= 0:
                return bytes(chunks[:newline])

    def send(self, op: str, **kwargs: Any) -> dict[str, Any]:
        """Send one request, return the decoded reply dict."""
        self.start()
        request = {"op": op, **kwargs}
        self._write(request)
        raw = self._read_line()
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DaemonError(f"bad JSON reply for op={op!r}: {raw[:200]!r}") from exc
        if not isinstance(reply, dict):
            raise DaemonError(f"non-object reply for op={op!r}: {reply!r}")
        return reply

    def _require_ok(self, op: str, reply: Mapping[str, Any]) -> dict[str, Any]:
        error = reply.get("error")
        if error:
            text = str(error).lower()
            if "window" in text or "not found" in text:
                raise WindowNotFound(f"daemon op={op!r}: {error}")
            if "permission" in text or "denied" in text or "not authorized" in text:
                raise CapturePermissionDenied(f"daemon op={op!r}: {error}")
            raise DaemonError(f"daemon op={op!r} failed: {error}")
        return dict(reply)

    # -- conveniences used by backends / CLI ------------------------------- #
    def ping(self) -> dict[str, Any]:
        return self._require_ok("ping", self.send("ping"))

    def caps(self, refresh: bool = False) -> set[str]:
        if self._caps is not None and not refresh:
            return set(self._caps)
        reply = self.send("caps")
        ops = reply.get("ops") or reply.get("caps") or []
        self._caps = {str(op) for op in ops} if isinstance(ops, (list, tuple, set)) else set()
        return set(self._caps)

    def win(self) -> dict[str, Any]:
        return self.send("win")

    def pin(self, rect_pt: G.Rect) -> dict[str, Any]:
        x, y, w, h = rect_pt.as_tuple()
        return self._require_ok("pin", self.send("pin", x=x, y=y, w=w, h=h))

    def activate(self) -> dict[str, Any]:
        return self._require_ok("activate", self.send("activate"))

    def capture_start(self, rect_game_px: G.Rect | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if rect_game_px is not None:
            payload["rect"] = list(rect_game_px.as_tuple())
        return self._require_ok("capture.start", self.send("capture.start", **payload))

    def capture_latest(self) -> dict[str, Any]:
        return self._require_ok("capture.latest", self.send("capture.latest"))

    def capture_roi(self, rect_game_px: G.Rect) -> dict[str, Any]:
        return self._require_ok(
            "capture.roi", self.send("capture.roi", rect=list(rect_game_px.as_tuple()))
        )

    def capture_stop(self) -> dict[str, Any]:
        return self._require_ok("capture.stop", self.send("capture.stop"))


# --------------------------------------------------------------------------- #
# daemon-backed capture
# --------------------------------------------------------------------------- #
class DaemonCapture:
    """Capture through the native daemon's warm ScreenCaptureKit session."""

    name = "daemon"

    def __init__(
        self,
        client: DaemonClient,
        geometry: G.WindowGeometry,
        ref: G.ReferenceSpace = G.DEFAULT_REFERENCE,
        warm: bool = True,
        tmpdir: str | None = None,
    ):
        self.client = client
        self.geometry = geometry
        self.ref = ref
        self.warm = warm
        self.tmpdir = tmpdir
        self.session_error: str | None = None
        self._session_ops: set[str] = set()
        self._last_frame: Frame | None = None
        self._seq = 0
        # (key, array) for the most recent decode; see _frame_from_reply
        self._decode_cache: tuple[tuple[Any, ...], np.ndarray] | None = None
        if not client.available:
            raise CaptureUnavailable(f"daemon unavailable: {client.describe()}")
        if warm:
            self._start_session()

    @property
    def available(self) -> bool:
        return self.client.available

    @property
    def supports_roi(self) -> bool:
        return "capture.roi" in self._session_ops

    def _start_session(self) -> None:
        try:
            caps = self.client.caps()
        except DaemonError:
            caps = set()
        try:
            self.client.capture_start(None)
        except (DaemonError, CaptureUnavailable) as exc:
            # daemon without a warm-session op: fall back to one-shot grabs
            self.warm = False
            self.session_error = str(exc)
            return
        self._session_ops = set(caps) or {
            "capture.start",
            "capture.latest",
            "capture.roi",
            "capture.stop",
        }

    # -- frame decoding ---------------------------------------------------- #
    def _frame_from_reply(self, reply: Mapping[str, Any], requested: G.Rect) -> Frame:
        seq = int(reply.get("seq", 0) or 0)
        ts = int(reply.get("ts_ns", 0) or 0) or time.monotonic_ns()
        rect_values = reply.get("rect")
        rect_game = G.Rect(*[float(v) for v in rect_values]) if rect_values else requested
        meta = {"backend": self.name, "daemon": "warm" if self.warm else "shot"}
        fmt = str(reply.get("fmt") or "")
        decode_started = time.perf_counter_ns()

        # A repeated (seq, geometry) means byte-identical pixels: reuse the decoded
        # array instead of re-reading.  On a static screen this makes a poll free,
        # which is the whole reason `latest()` is cheap to call every tick.
        cache_key = (
            seq,
            fmt,
            int(reply.get("w") or 0),
            int(reply.get("h") or 0),
            int(reply.get("stride") or 0),
            tuple(rect_game.as_tuple()) if rect_game is not None else None,
        ) if seq else None
        cached = self._decode_cache
        if cache_key is not None and cached is not None and cached[0] == cache_key:
            array = cached[1]
            decode_ns = 0
            meta["decode"] = "cached"
        else:
            if fmt == "bgra" and reply.get("path"):
                array = read_raw_bgra(
                    str(reply["path"]),
                    int(reply.get("w") or 0),
                    int(reply.get("h") or 0),
                    int(reply.get("stride") or 0),
                )
            elif reply.get("path"):
                array = read_image(str(reply["path"]))
            elif reply.get("png_b64"):
                array = decode_png_bytes(base64.b64decode(reply["png_b64"]))
            elif reply.get("png"):
                array = decode_png_bytes(bytes(reply["png"]))
            else:
                raise DaemonError(f"capture reply has no image payload: keys={sorted(reply)}")
            decode_ns = time.perf_counter_ns() - decode_started
            if cache_key is not None:
                self._decode_cache = (cache_key, array)
        if not self.warm and reply.get("w") and reply.get("h"):
            meta["reported_size"] = (int(reply["w"]), int(reply["h"]))
        # Preserve the daemon's sequence: a repeated seq means "same frame",
        # which is exactly what change detection needs to see.
        if seq:
            self._seq = max(self._seq, seq)
        else:
            self._seq += 1
        return make_frame(
            array,
            self.geometry,
            rect_game,
            seq=self._seq,
            timestamp_ns=ts,
            meta=meta,
            ref=self.ref,
            decode_ns=decode_ns,
        )

    # -- CaptureBackend ---------------------------------------------------- #
    def latest(self, rect: G.Rect | None = None) -> Frame:
        """Newest frame already held by the daemon; never waits for new content.

        A stale frame is a valid observation: it carries its own monotonic
        timestamp + sequence, so the executor can decide from ``(frame, age)``.
        """
        rect = rect or FULL_FRAME_PX
        if not self.warm:
            return self.grab(rect)
        frame = self._frame_from_reply(self.client.capture_latest(), FULL_FRAME_PX)
        if rect != FULL_FRAME_PX:
            frame = frame.crop(frame.frame_rect_from_game_px(rect))
        self._last_frame = frame
        return frame

    def grab(self, rect: G.Rect | None = None) -> Frame:
        rect = rect or FULL_FRAME_PX
        if self.warm and self.supports_roi:
            frame = self._frame_from_reply(self.client.capture_roi(rect), rect)
        elif self.warm:
            # newest full frame + in-memory crop: a crop is microseconds, so
            # this is still a real ROI win versus a second round trip.
            full = self._frame_from_reply(self.client.capture_latest(), FULL_FRAME_PX)
            frame = (
                full
                if rect == FULL_FRAME_PX
                else full.crop(full.frame_rect_from_game_px(rect))
            )
        else:
            frame = self._grab_oneshot(rect)
        self._last_frame = frame
        return frame

    def _grab_oneshot(self, rect: G.Rect) -> Frame:
        fd, path = tempfile.mkstemp(prefix="seerauto-daemon-", suffix=".png", dir=self.tmpdir)
        os.close(fd)
        try:
            reply = self.client.send("shot", path=path, rect=list(rect.as_tuple()))
            if reply.get("error") and not reply.get("path"):
                self.client._require_ok("shot", reply)
            payload = dict(reply)
            payload.setdefault("path", path)
            if not os.path.exists(str(payload.get("path", ""))):
                raise DaemonError("daemon shot produced no file")
            return self._frame_from_reply(payload, rect)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def close(self) -> None:
        if self.warm:
            try:
                self.client.capture_stop()
            except Exception:  # noqa: BLE001
                pass
        self.client.close()


# --------------------------------------------------------------------------- #
# replay backend (fixtures / tests)
# --------------------------------------------------------------------------- #
class ReplayCapture:
    """Replay stored PNGs as frames - the game and daemon are not needed."""

    name = "replay"

    def __init__(
        self,
        paths: Sequence[str | os.PathLike[str]],
        geometry: G.WindowGeometry | None = None,
        ref: G.ReferenceSpace = G.DEFAULT_REFERENCE,
        loop: bool = False,
        stage_detector: Any | None = None,
        clock: Any | None = None,
        dwell_s: float = 0.25,
    ):
        if not paths:
            raise CaptureError("ReplayCapture needs at least one image")
        self.paths = [Path(p) for p in paths]
        self.geometry = geometry or G.WindowGeometry()
        self.ref = ref
        self.loop = loop
        self.stage_detector = stage_detector
        self.index = 0
        self._seq = 0
        # A synthetic clock turns replay into a change-driven source: ``latest``
        # keeps returning the same frame until ``dwell_s`` of synthetic time
        # passes, exactly like a static window under ScreenCaptureKit.
        self.clock = clock
        self.dwell_s = float(dwell_s)
        self._current: Frame | None = None
        self._current_at_ns = 0

    @property
    def available(self) -> bool:
        return any(p.exists() for p in self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    def grab(self, rect: G.Rect | None = None) -> Frame:
        if self.index >= len(self.paths):
            if not self.loop:
                raise CaptureError("replay exhausted")
            self.index = 0
        path = self.paths[self.index]
        self.index += 1
        decode_started = time.perf_counter_ns()
        array = read_image(path)  # cv2.imread decodes eagerly (no lazy handle trap)
        decode_ns = time.perf_counter_ns() - decode_started
        height, width = array.shape[:2]
        self._seq += 1
        # Replayed fixtures are window captures at an arbitrary resolution:
        # declare the frame as covering the full window and let stage detection
        # (if provided) align the canonical stage precisely.
        frame = make_frame(
            array,
            self.geometry,
            G.Rect(0.0, 0.0, *self.ref.capture_px.as_tuple()),
            seq=self._seq,
            meta={"backend": self.name, "path": str(path)},
            ref=self.ref,
            decode_ns=decode_ns,
        )
        if self.stage_detector is not None:
            stage_px = self.stage_detector(frame)
            if stage_px is not None:
                frame = frame.with_stage(
                    G.StageMap(
                        stage_px=stage_px,
                        ref_size=self.ref.stage_px.size,
                        ref_offset=G.Point(self.ref.stage_px.x, self.ref.stage_px.y),
                    )
                )
                frame.meta["stage_detected"] = True
        if rect is not None and rect != FULL_FRAME_PX:
            frame = frame.crop(frame.frame_rect_from_game_px(rect))
        return frame

    def latest(self, rect: G.Rect | None = None) -> Frame:
        """Newest replayed frame; with a clock, repeats while ``dwell_s`` holds."""
        if self.clock is None:
            frame = self.grab()
        else:
            now = int(self.clock())
            if self._current is None or (now - self._current_at_ns) >= int(self.dwell_s * 1e9):
                self._current = self.grab()
                self._current_at_ns = now
            frame = self._current
        if rect is not None and rect != FULL_FRAME_PX:
            return frame.crop(frame.frame_rect_from_game_px(rect))
        return frame

    def close(self) -> None:
        return None


class NullCapture:
    """Always unavailable; used by ``doctor`` and as a safe default."""

    name = "none"

    def __init__(self, reason: str = "no capture backend configured"):
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def grab(self, rect: G.Rect | None = None) -> Frame:
        raise CaptureUnavailable(self.reason)

    def latest(self, rect: G.Rect | None = None) -> Frame:
        raise CaptureUnavailable(self.reason)

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# factories
# --------------------------------------------------------------------------- #
def build_backend(
    kind: str,
    geometry: G.WindowGeometry,
    *,
    daemon_binary: str | None = None,
    daemon_args: Sequence[str] = (),
    screencapture_path: str = "/usr/sbin/screencapture",
    ref: G.ReferenceSpace = G.DEFAULT_REFERENCE,
) -> CaptureBackend:
    """Create a backend by name, raising typed errors when unavailable.

    ``kind`` is one of ``daemon``, ``screencapture``, ``replay`` (requires the
    extra args), ``none``.  The daemon path is fully optional: if the binary is
    absent the caller falls back to ``screencapture``.
    """
    kind = (kind or "none").lower()
    if kind == "daemon":
        client = DaemonClient(daemon_binary, args=daemon_args)
        return DaemonCapture(client, geometry, ref=ref)
    if kind == "screencapture":
        return SubprocessScreencapture(geometry, screencapture=screencapture_path, ref=ref)
    if kind == "none":
        return NullCapture()
    raise CaptureUnavailable(f"unknown capture backend {kind!r}")


def replay_files(directory: str | os.PathLike[str], recursive: bool = False) -> list[Path]:
    """Collect PNG/JPEG fixtures from a directory (sorted, deterministic)."""
    root = Path(directory)
    if not root.exists():
        raise CaptureError(f"fixture directory does not exist: {root}")
    pattern = "**/*" if recursive else "*"
    files = [
        p
        for p in sorted(root.glob(pattern))
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp")
    ]
    return files


__all__ = [
    "CaptureBackend",
    "DaemonCapture",
    "DaemonClient",
    "Frame",
    "NullCapture",
    "ReplayCapture",
    "SubprocessScreencapture",
    "build_backend",
    "decode_png_bytes",
    "load_cv2",
    "make_frame",
    "read_image",
    "replay_files",
]
