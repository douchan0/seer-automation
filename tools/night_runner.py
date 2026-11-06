#!/usr/bin/env python3
"""night_runner — a thin, auditable supervisor around the verified wild grinder.

`tools/wild_cycle.py` already knows how to farm: it opens the live session, fights
passes, heals, recovers from a frozen UI, and writes one JSON record per pass to
`data/overnight/wild_cycle.jsonl`.  What it does **not** do is tell the control plane
that anything happened.  That gap was the most important honest caveat in the
2026-11-06 control-plane review:

* the dual plane could only see `EXECUTOR_ALIVE` from the wild-cycle ledger's mtime —
  a heartbeat is not progress;
* nothing recorded a *verified milestone* (wins/xp actually moved), so
  `GAME_PROGRESSING` stayed UNKNOWN all night and the watchdog had no real baseline.

This runner closes that gap without touching the verified executor:

    subprocess(wild_cycle.py)  ->  wild_cycle.jsonl (read-only tail)
        |                                   |
        |  executor_heartbeat.json (5 s)    +--> milestone ONLY on a numeric delta
        |                                        (wins/xp changed), never on liveness
        +--> /tmp/seerauto.{pause,master.pause,stop} at cooperation points (SIGINT)

Design rules, all enforced here rather than in the executor:

* **One child at a time.**  Two drivers on one window is how the 2026-11-06 bring-up
  ended up with two daemons fighting over ScreenCaptureKit.
* **Never SIGKILL.**  Pause/stop/deadline send SIGINT and wait; the child closes its
  live session in a ``finally``.  If it ignores SIGINT we say so instead of killing it.
* **Progress is numeric.**  A milestone is written only when a new ledger record shows
  ``wins > 0`` or ``xp > 0``.  "The child is still running" writes a heartbeat, never a
  milestone — that distinction is the entire point of the dual-mode experiment.
* **Bounded recovery.**  At most 3 restarts, 30/60/120 s backoff, each restart written
  to ``failures.jsonl`` with the child's last 20 log lines.  After that the run ends
  FAILED with a reason rather than spinning to the deadline.
* **The end is machine-readable.**  One JSON object with the canonical eight-value
  outcome, wins/xp/restarts/elapsed/reason.

Nothing here imports the game client or the vision stack: it is glue over a
subprocess, so it can be tested end-to-end with fakes and it cannot corrupt the
executor's already-verified behaviour.

    # dry run: print the exact child command and exit
    PYTHONPATH=.pydeps python3 tools/night_runner.py --dry-run --minutes 40

    # the real 40-minute farming window (a human/agent starts this, not the runner)
    PYTHONPATH=.pydeps python3 tools/night_runner.py --minutes 40 \
        --deadline-at 2026-11-07T07:00:00+08:00
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seerauto.dual.ledger import Ledger, parse_ts  # noqa: E402
from seerauto.dual.outcome import Outcome  # noqa: E402

DATA_DIR = ROOT / "data" / "overnight" / "20261106_dual_mode"
WILD_CYCLE = ROOT / "tools" / "wild_cycle.py"
WILD_LEDGER = ROOT / "data" / "overnight" / "wild_cycle.jsonl"
WILD_SUMMARY = ROOT / "data" / "overnight" / "wild_summary.json"
HEARTBEAT = DATA_DIR / "executor_heartbeat.json"
OVERNIGHT_CONTROL = ROOT / "tools" / "overnight_control.py"

PAUSE_FILES = ("/tmp/seerauto.master.pause", "/tmp/seerauto.pause")
STOP_FILE = "/tmp/seerauto.stop"

#: wild_cycle's own exit codes (documented in its module docstring).
EXIT_PROGRESS = 0
EXIT_NO_PROGRESS = 10
EXIT_BLOCKED = 11
EXIT_PAUSED = 20


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    """One farming window.  Every path is overridable so tests never touch the repo."""

    minutes: float = 40.0
    deadline_at: str | None = None
    battles: int = 3
    heal_every: int = 3
    data_dir: Path = DATA_DIR
    wild_cycle: Path = WILD_CYCLE
    wild_ledger: Path = WILD_LEDGER
    wild_summary: Path = WILD_SUMMARY
    heartbeat_file: Path = HEARTBEAT
    control: Path = OVERNIGHT_CONTROL
    python: str = sys.executable
    poll_s: float = 2.0
    heartbeat_s: float = 5.0
    max_restarts: int = 3
    backoff_s: tuple[float, ...] = (30.0, 60.0, 120.0)
    max_no_progress_runs: int = 3
    pause_files: tuple[str, ...] = PAUSE_FILES
    stop_file: str = STOP_FILE

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes": self.minutes,
            "deadline_at": self.deadline_at,
            "battles": self.battles,
            "heal_every": self.heal_every,
            "data_dir": str(self.data_dir),
            "wild_cycle": str(self.wild_cycle),
            "wild_ledger": str(self.wild_ledger),
            "heartbeat_file": str(self.heartbeat_file),
            "max_restarts": self.max_restarts,
            "backoff_s": list(self.backoff_s),
        }


def window_end_epoch(config: Config, now: float) -> float:
    """Absolute end of the window: now+minutes, capped by ``--deadline-at``."""

    end = now + max(0.0, float(config.minutes)) * 60.0
    if config.deadline_at:
        hard = datetime.fromisoformat(config.deadline_at)
        if hard.tzinfo is None:
            raise ValueError("--deadline-at must carry a timezone offset, "
                             "e.g. 2026-11-07T07:00:00+08:00")
        end = min(end, hard.timestamp())
    return end


def build_child_argv(config: Config, remaining_s: float,
                     end_epoch: float | None = None) -> list[str]:
    """The exact wild_cycle invocation for one child run.

    Only flags wild_cycle actually defines; the window is expressed both as minutes
    and as an absolute ``--deadline-at`` because the child re-checks the clock and a
    relative budget alone drifts when a battle runs long.  ``end_epoch`` lets a caller
    with an injected clock pass its own window end instead of the wall clock.
    """

    remaining_min = max(1.0 / 60.0, remaining_s / 60.0)
    end = end_epoch if end_epoch is not None else window_end_epoch(config, time.time())
    return [
        config.python, str(config.wild_cycle),
        "--battles", str(int(config.battles)),
        "--heal-every", str(int(config.heal_every)),
        "--minutes", f"{remaining_min:.1f}",
        "--deadline-at",
        datetime.fromtimestamp(end).astimezone().isoformat(timespec="seconds"),
    ]


def progress_argv(config: Config, proof: str) -> list[str]:
    return [config.python, str(config.control),
            "--data-dir", str(config.data_dir),
            "progress", "--node", "wild.farm", "--proof", proof,
            "--evidence", str(config.wild_ledger)]


def status_argv(config: Config) -> list[str]:
    return [config.python, str(config.control), "--data-dir", str(config.data_dir),
            "status"]


# --------------------------------------------------------------------------- #
# read-only ledger tail (tolerates a half-written last line)
# --------------------------------------------------------------------------- #
class LedgerTail:
    """Incremental reader of the executor's JSONL ledger.

    Only *complete* lines are consumed: a crash can leave a truncated last line, and
    consuming it would either lose a record or double-count it on the next read.  The
    byte offset is kept so a 9-hour ledger is never re-parsed.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self._buffer = b""
        self.malformed = 0

    def skip_to_end(self) -> None:
        """Start after the existing records (a previous night's passes are not ours)."""

        try:
            self.offset = self.path.stat().st_size
        except OSError:
            self.offset = 0
        self._buffer = b""

    def drain(self) -> Iterator[dict[str, Any]]:
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return
        if not chunk:
            return
        data = self._buffer + chunk
        parts = data.split(b"\n")
        if data.endswith(b"\n"):
            complete, self._buffer = parts[:-1], b""
        else:
            # Half-written tail: keep the bytes for the next read.  Only complete
            # lines are consumed, so a crash cannot lose or double-count a record.
            complete, self._buffer = parts[:-1], parts[-1]
        self.offset += len(chunk) - len(self._buffer)
        for raw in complete:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                self.malformed += 1
                continue
            if isinstance(record, dict):
                yield record


# --------------------------------------------------------------------------- #
# outcome mapping
# --------------------------------------------------------------------------- #
def backoff_for(config: Config, restart_index: int) -> float:
    """30/60/120 s, then the last value (the budget is capped before this matters)."""

    if not config.backoff_s:
        return 0.0
    return float(config.backoff_s[min(restart_index, len(config.backoff_s) - 1)])


# --------------------------------------------------------------------------- #
# the runner
# --------------------------------------------------------------------------- #
class NightRunner:
    """Runs one bounded farming window around the verified executor."""

    def __init__(self, config: Config, *,
                 spawn: Callable[[list[str], Path], Any] | None = None,
                 run_control: Callable[[list[str], float], tuple[int, str]] | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 child_log: Path | None = None) -> None:
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.spawn = spawn or self._spawn_default
        self.run_control = run_control or self._run_control_default
        self.ledger = Ledger(config.data_dir)
        self.tail = LedgerTail(config.wild_ledger)
        self.child_log = child_log or (config.data_dir / "night_runner_child.log")

        self.started_epoch = 0.0
        self.end_epoch = 0.0
        self.child_pid: int | None = None
        self.child: Any = None
        self.run_index = 0
        self.restarts = 0
        self.wins = 0
        self.xp = 0.0
        self.milestones = 0
        self.consecutive_no_progress = 0
        self.last_output_ts: float | None = None
        self.last_milestone_ts: float | None = None
        self.blocked_runs = 0
        self._next_heartbeat = 0.0
        self._final_written = False
        self._last_final: dict[str, Any] = {}

    # -- defaults ---------------------------------------------------------- #
    def _spawn_default(self, argv: list[str], log_path: Path) -> Any:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = log_path.open("ab")
        try:
            return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                    cwd=str(ROOT), start_new_session=False)
        finally:
            fh.close()  # the child keeps its own duplicated descriptor

    def _run_control_default(self, argv: list[str], timeout: float) -> tuple[int, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, cwd=str(ROOT))
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 124, f"{type(exc).__name__}: {exc}"

    # -- small helpers ----------------------------------------------------- #
    def _heartbeat(self, state: str, stopped: bool = False,
                   paused: bool = False) -> dict[str, Any]:
        now = self.clock()
        payload = {
            "ts": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "ts_epoch": round(now, 3),
            "pid": os.getpid(),
            "child_pid": self.child_pid,
            "started_at": datetime.fromtimestamp(self.started_epoch).astimezone()
            .isoformat(timespec="seconds"),
            "window_end": datetime.fromtimestamp(self.end_epoch).astimezone()
            .isoformat(timespec="seconds"),
            "last_output_ts": (datetime.fromtimestamp(self.last_output_ts)
                               .astimezone().isoformat(timespec="seconds")
                               if self.last_output_ts else None),
            "last_milestone_ts": (datetime.fromtimestamp(self.last_milestone_ts)
                                  .astimezone().isoformat(timespec="seconds")
                                  if self.last_milestone_ts else None),
            "wins_total": self.wins,
            "xp_total": round(self.xp, 1),
            "consecutive_no_progress": self.consecutive_no_progress,
            "current_mode": self._current_mode(),
            "restarts": self.restarts,
            "milestones": self.milestones,
            "state": state,
            "stopped": stopped,
            "paused": paused,
        }
        self._write_json_atomic(self.config.heartbeat_file, payload)
        return payload

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _current_mode(self) -> str | None:
        code, out = self.run_control(status_argv(self.config), 20.0)
        if code != 0:
            return None
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            return payload.get("mode")
        return None

    def _paused(self) -> str | None:
        for path in self.config.pause_files:
            if os.path.exists(path):
                return path
        return None

    def _stop_requested(self) -> bool:
        return os.path.exists(self.config.stop_file)

    def _deadline_reached(self, now: float) -> bool:
        return now >= self.end_epoch

    def _child_log_tail(self, lines: int = 20) -> list[str]:
        try:
            content = self.child_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return content.splitlines()[-lines:]

    # -- the ledger -> milestone translation ------------------------------- #
    def drain_ledger(self) -> list[dict[str, Any]]:
        """Read new executor records; emit a milestone for each numeric delta.

        A record with ``wins == 0 and xp == 0`` is activity without progress: it
        updates ``consecutive_no_progress`` and never produces a milestone.
        """

        emitted = []
        for record in self.tail.drain():
            self.last_output_ts = parse_ts(record.get("at")) or parse_ts(
                record.get("ts")) or self.clock()
            wins = int(record.get("wins") or 0)
            xp = float(record.get("xp") or 0.0)
            if wins <= 0 and xp <= 0:
                self.consecutive_no_progress += 1
                continue
            self.consecutive_no_progress = 0
            self.wins += wins
            self.xp += xp
            proof = json.dumps({
                "pass": record.get("pass"),
                "wins": wins,
                "xp": round(xp, 1),
                "level": record.get("level"),
                "at": record.get("at"),
            }, ensure_ascii=False, separators=(",", ":"))
            code, out = self.run_control(progress_argv(self.config, proof), 30.0)
            if code == 0:
                self.milestones += 1
                self.last_milestone_ts = self.clock()
                emitted.append({"proof": proof, "wins": wins, "xp": xp})
            else:
                # Do not crash the farming window for a bookkeeping failure, but make
                # it visible in failures.jsonl so the gap is not silent.
                self.ledger.append("failures", {
                    "kind": "milestone_record_failed",
                    "reason": f"overnight_control progress exited {code}",
                    "proof": proof,
                    "output_tail": out.strip().splitlines()[-3:],
                    "evidence": str(self.config.control),
                })
        return emitted

    # -- child lifecycle --------------------------------------------------- #
    def _start_child(self, remaining_s: float) -> Any:
        argv = build_child_argv(self.config, remaining_s, self.end_epoch)
        self.run_index += 1
        self.child_log = self.config.data_dir / f"night_runner_child_{self.run_index}.log"
        log(f"child {self.run_index}: {' '.join(argv)}")
        self.ledger.append("events", {"kind": "runner.child.start", "run": self.run_index,
                                      "argv": argv})
        child = self.spawn(argv, self.child_log)
        self.child = child
        self.child_pid = int(getattr(child, "pid", 0) or 0) or None
        self._next_heartbeat = 0.0
        self._heartbeat("running")
        return child

    def _sigint_child(self, child: Any, grace_s: float = 30.0) -> bool:
        """SIGINT and wait; a second SIGINT, then give up without SIGKILL.

        Returns True when the child exited.  We never escalate to SIGKILL: the child
        closes its live session in ``finally``, and a hard kill would leave a
        half-clicked UI for the next run.
        """

        if child is None:
            return True
        try:
            child.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        deadline = self.clock() + grace_s
        while child.poll() is None and self.clock() < deadline:
            self.sleep(0.5)
        if child.poll() is None:
            try:
                child.send_signal(signal.SIGINT)
            except (OSError, ProcessLookupError):
                pass
            deadline = self.clock() + 10.0
            while child.poll() is None and self.clock() < deadline:
                self.sleep(0.5)
        return child.poll() is not None

    def _monitor_child(self, child: Any) -> str:
        """Watch one child.  Returns exit/pause/stop/deadline/interrupt."""

        while child.poll() is None:
            now = self.clock()
            self.drain_ledger()
            if now >= self._next_heartbeat:
                self._heartbeat("running")
                self._next_heartbeat = now + self.config.heartbeat_s
            if self._deadline_reached(now):
                self._sigint_child(child)
                return "deadline"
            if self._stop_requested():
                self._sigint_child(child)
                return "stop"
            if self._paused():
                self._sigint_child(child)
                return "pause"
            self.sleep(self.config.poll_s)
        self.drain_ledger()
        self._heartbeat("running")
        return "exit"

    def _wait_while_paused(self, pause_path: str) -> str:
        """Idle at the pause flag, still heart-beating, until resumed/stopped/deadline."""

        log(f"paused by {pause_path}; child stopped with SIGINT, waiting")
        self.ledger.append("events", {"kind": "runner.paused", "flag": pause_path})
        last = 0.0
        while True:
            now = self.clock()
            if now >= self._next_heartbeat:
                self._heartbeat("paused", paused=True)
                self._next_heartbeat = now + self.config.heartbeat_s
            if self._stop_requested():
                return "stop"
            if self._deadline_reached(now):
                return "deadline"
            if not self._paused():
                return "resumed"
            self.sleep(1.0)

    def _record_restart(self, *, code: int | None, reason: str, backoff: float,
                        outcome: Outcome) -> None:
        self.ledger.append("failures", {
            "kind": "restart",
            "outcome": outcome.value,
            "restart": self.restarts,
            "max_restarts": self.config.max_restarts,
            "backoff_s": backoff,
            "exit_code": code,
            "reason": reason,
            "log_tail": self._child_log_tail(20),
            "evidence": str(self.child_log),
        })

    # -- the window -------------------------------------------------------- #
    def run(self) -> dict[str, Any]:
        cfg = self.config
        self.started_epoch = self.clock()
        self.end_epoch = window_end_epoch(cfg, self.started_epoch)
        self.tail.skip_to_end()
        self.ledger.append("events", {
            "kind": "runner.start",
            "window_end": datetime.fromtimestamp(self.end_epoch).astimezone()
            .isoformat(timespec="seconds"),
            "minutes": cfg.minutes,
            "argv": build_child_argv(cfg, self.end_epoch - self.started_epoch,
                                     self.end_epoch),
        })
        log(f"window {cfg.minutes:.0f} min, ends "
            f"{datetime.fromtimestamp(self.end_epoch):%H:%M:%S}")

        outcome: Outcome = Outcome.NO_WORK
        reason = "window ended without a recorded pass"
        stopped = paused = deadline = False

        while True:
            now = self.clock()
            if self._stop_requested():
                stopped, outcome, reason = True, Outcome.PAUSED, "stop flag"
                break
            pause_path = self._paused()
            if pause_path:
                paused = True
                state = self._wait_while_paused(pause_path)
                paused = False
                if state == "stop":
                    stopped, outcome, reason = True, Outcome.PAUSED, "stop flag"
                    break
                if state == "deadline":
                    deadline, outcome = True, Outcome.DEADLINE_REACHED
                    reason = "deadline reached while paused"
                    break
                now = self.clock()
            if self._deadline_reached(now):
                deadline, outcome = True, Outcome.DEADLINE_REACHED
                reason = "window deadline reached"
                break

            child = self._start_child(self.end_epoch - now)
            verdict = self._monitor_child(child)
            code = child.poll()

            if verdict == "stop":
                stopped, outcome, reason = True, Outcome.PAUSED, "stop flag (SIGINT)"
                break
            if verdict == "deadline":
                deadline, outcome = True, Outcome.DEADLINE_REACHED
                reason = "window deadline reached (SIGINT)"
                break
            if verdict == "pause":
                continue  # handled at the top of the loop, no restart counted

            self.ledger.append("events", {"kind": "runner.child.end",
                                          "run": self.run_index, "exit_code": code,
                                          "wins": self.wins, "xp": round(self.xp, 1)})
            if self._deadline_reached(self.clock()):
                deadline, outcome = True, Outcome.DEADLINE_REACHED
                reason = "window deadline reached"
                break

            # A clean exit before the window ends is a continuation, not a failure —
            # unless it produced nothing, in which case it is the 2026-11-04 pattern.
            if code in (EXIT_PROGRESS, EXIT_NO_PROGRESS) and not self._deadline_reached(
                    self.clock()):
                if self.consecutive_no_progress >= cfg.max_no_progress_runs:
                    outcome = Outcome.NO_WORK
                    reason = (f"{self.consecutive_no_progress} passes with no numeric "
                              f"progress; stopping instead of spinning")
                    break
                self.sleep(2.0)
                continue

            if code == EXIT_BLOCKED:
                self.blocked_runs += 1
            bad_outcome = (Outcome.BLOCKED if code == EXIT_BLOCKED else Outcome.FAILED)
            if self.restarts >= cfg.max_restarts:
                outcome = bad_outcome
                reason = (f"child exited {code} and the restart budget "
                          f"({cfg.max_restarts}) is exhausted")
                # The budget end is a terminal failure, not a restart: keep it a
                # separate kind so the restart count in failures.jsonl is exact.
                self.ledger.append("failures", {
                    "kind": "runner.failed",
                    "outcome": bad_outcome.value,
                    "restarts": self.restarts,
                    "max_restarts": cfg.max_restarts,
                    "exit_code": code,
                    "reason": reason,
                    "log_tail": self._child_log_tail(20),
                    "evidence": str(self.child_log),
                })
                break
            backoff = backoff_for(cfg, self.restarts)
            self.restarts += 1
            self._record_restart(code=code, reason=f"child exited {code}", backoff=backoff,
                                 outcome=bad_outcome)
            log(f"restart {self.restarts}/{cfg.max_restarts} in {backoff:.0f}s")
            self._heartbeat("restarting")
            slept = 0.0
            while slept < backoff:
                if self._stop_requested():
                    stopped = True
                    break
                if self._deadline_reached(self.clock()):
                    deadline = True
                    break
                self.sleep(min(1.0, backoff - slept))
                slept += 1.0
            if stopped:
                outcome, reason = Outcome.PAUSED, "stop flag during backoff"
                break
            if deadline:
                outcome, reason = Outcome.DEADLINE_REACHED, "deadline during backoff"
                break

        final = self._finalize(outcome, reason, stopped=stopped, paused=paused,
                               deadline=deadline)
        print(json.dumps(final, ensure_ascii=False, separators=(",", ":")), flush=True)
        return final

    def _finalize(self, outcome: Outcome, reason: str, *, stopped: bool,
                  paused: bool, deadline: bool) -> dict[str, Any]:
        if self._final_written:
            return self._last_final
        now = self.clock()
        self._heartbeat("stopped", stopped=stopped or paused, paused=paused)
        final = {
            "outcome": outcome.value,
            "reason": reason,
            "elapsed_s": round(now - self.started_epoch, 1),
            "wins": self.wins,
            "xp": round(self.xp, 1),
            "restarts": self.restarts,
            "milestones": self.milestones,
            "runs": self.run_index,
            "started_at": datetime.fromtimestamp(self.started_epoch).astimezone()
            .isoformat(timespec="seconds"),
            "ended_at": datetime.fromtimestamp(now).astimezone()
            .isoformat(timespec="seconds"),
            "data_dir": str(self.config.data_dir),
        }
        self.ledger.append("events", {"kind": "runner.final", **{
            key: value for key, value in final.items() if key not in ("data_dir",)}})
        self._final_written = True
        self._last_final = final
        return final


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Auditable supervisor around tools/wild_cycle.py: heartbeat, "
                    "numeric milestones, bounded restarts, canonical outcome.")
    ap.add_argument("--minutes", type=float, default=40.0,
                    help="length of this farming window (default 40)")
    ap.add_argument("--deadline-at", default=None,
                    help="absolute ISO-8601 deadline with offset (window is capped by it)")
    ap.add_argument("--battles", type=int, default=3, help="battles per pass")
    ap.add_argument("--heal-every", type=int, default=3)
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--wild-ledger", default=str(WILD_LEDGER))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact child command and exit without running")
    return ap


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(minutes=args.minutes, deadline_at=args.deadline_at,
                  battles=args.battles, heal_every=args.heal_every,
                  data_dir=Path(args.data_dir), wild_ledger=Path(args.wild_ledger),
                  heartbeat_file=Path(args.data_dir) / "executor_heartbeat.json")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_args(args)
        end = window_end_epoch(config, time.time())
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.dry_run:
        now = time.time()
        remaining = max(0.0, end - now)
        payload = {
            "dry_run": True,
            "child_argv": build_child_argv(config, remaining, end),
            "window_end": datetime.fromtimestamp(end).astimezone()
            .isoformat(timespec="seconds"),
            "window_minutes": round(remaining / 60.0, 1),
            "heartbeat_file": str(config.heartbeat_file),
            "wild_ledger": str(config.wild_ledger),
            "milestone_argv_example": progress_argv(
                config, '{"pass":1,"wins":2,"xp":104.0}'),
            "starts_nothing": True,
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
              flush=True)
        return 0

    runner = NightRunner(config)
    try:
        final = runner.run()
    except KeyboardInterrupt:
        runner._sigint_child(runner.child)
        final = runner._finalize(Outcome.INTERRUPTED, "KeyboardInterrupt",
                                 stopped=False, paused=False, deadline=False)
        print(json.dumps(final, ensure_ascii=False, separators=(",", ":")), flush=True)
    # Canonical exit codes (same table as seerauto.runner.outcome, so a shell or
    # supervisor can branch without parsing the JSON).
    return {
        Outcome.PROGRESS.value: 0,
        Outcome.NO_WORK.value: 3,
        Outcome.FAILED.value: 1,
        Outcome.UNVERIFIED_ACTIVITY.value: 4,
        Outcome.BLOCKED.value: 7,
        Outcome.DEADLINE_REACHED.value: 124,
        Outcome.PAUSED.value: 130,
        Outcome.INTERRUPTED.value: 130,
    }.get(final["outcome"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
