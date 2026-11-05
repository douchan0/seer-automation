"""Run control: pause/stop signalling and the single-executor lock.

A long-horizon run must be stoppable from *outside* the process (a human or
another agent in a different shell), and two runs must never drive the same
game window at once.  Both are filesystem primitives on purpose: no sockets,
no signals that silently kill, no shared memory.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path

DEFAULT_PAUSE_FILE = "/tmp/seerauto.pause"
DEFAULT_STOP_FILE = "/tmp/seerauto.stop"
DEFAULT_LOCK_FILE = "/tmp/seerauto.executor.lock"


class LockHeld(RuntimeError):
    """Another process already owns the executor."""


class RunControl:
    """Cooperative pause/stop via control files.

    * ``touch /tmp/seerauto.pause``  -> the run stops *acting* within one
      observation tick and waits; remove the file to resume (the runner
      re-observes before acting again, because the game kept moving).
    * ``touch /tmp/seerauto.stop``   -> the run finishes the current skill,
      writes a checkpoint and exits cleanly.
    """

    def __init__(self, pause_file: str = DEFAULT_PAUSE_FILE,
                 stop_file: str = DEFAULT_STOP_FILE):
        self.pause_file = Path(pause_file)
        self.stop_file = Path(stop_file)
        self._paused_since: float | None = None
        self._slept: float = 0.0
        # The runner installs these callbacks so every pause entry point,
        # including long-running Skills, writes the same durable checkpoint.
        self.on_pause = None
        self.on_resume = None

    # -- queries ----------------------------------------------------------- #
    def paused(self) -> bool:
        return self.pause_file.exists()

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    # -- actions ----------------------------------------------------------- #
    def request_stop(self) -> None:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(f"{time.time():.0f}\n", encoding="utf-8")

    def request_pause(self) -> None:
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.write_text(f"{time.time():.0f}\n", encoding="utf-8")

    def clear_stop(self) -> None:
        """Clear only a stale stop request; preserve an active user pause."""
        try:
            self.stop_file.unlink()
        except FileNotFoundError:
            pass

    def clear(self) -> None:
        """Clear pause and stop requests (used only by an explicit resume)."""
        for p in (self.pause_file, self.stop_file):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self._paused_since = None
        self._slept = 0.0

    # -- blocking ---------------------------------------------------------- #
    def wait_while_paused(self, poll_s: float = 0.2, *, on_pause=None,
                          on_resume=None) -> float:
        """Block while the pause file exists.  Returns seconds spent paused."""

        if not self.paused():
            return 0.0
        started = time.monotonic()
        pause_cb = on_pause if on_pause is not None else self.on_pause
        if pause_cb is not None:
            pause_cb()
        while self.paused():
            if self.stop_requested():
                break
            time.sleep(poll_s)
        elapsed = time.monotonic() - started
        self._slept += elapsed
        resume_cb = on_resume if on_resume is not None else self.on_resume
        if resume_cb is not None:
            resume_cb(elapsed)
        return elapsed

    @property
    def paused_total_s(self) -> float:
        return self._slept


class InstanceLock:
    """Exclusive advisory lock so only one process drives the window.

    ScreenCaptureKit tolerates a second warm stream badly (a duplicate daemon's
    ``shot`` returns a near-blank image), and two click streams would interleave
    into nonsense, so this is a hard requirement rather than a nicety.
    """

    def __init__(self, path: str = DEFAULT_LOCK_FILE):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.seek(0)
            owner = fh.read().strip() or "unknown"
            fh.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockHeld(
                    f"another run already owns the executor ({self.path}, owner={owner})")
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} since={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    def holder(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None


__all__ = ["RunControl", "InstanceLock", "LockHeld", "DEFAULT_PAUSE_FILE",
           "DEFAULT_STOP_FILE", "DEFAULT_LOCK_FILE"]
