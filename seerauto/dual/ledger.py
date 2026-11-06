"""Append-only JSONL ledgers and a crash-safe checkpoint.

The night runs unattended for nine hours, which means the process *will* be killed
at an arbitrary instruction — by a deadline, a SIGINT, a crash in a child, or a
laptop lid.  Two properties follow, and both are tested:

**An append is a commit.**  One JSON line, one ``write(2)`` on an ``O_APPEND`` fd,
then ``fsync``.  A record is either fully in the file or not in it at all; no
rewriting, no read-modify-write, no truncation.  A crash *can* still leave a
half-written tail line (the kernel may write a prefix of a multi-byte line before
the process dies), so readers tolerate exactly that: an unparsable trailing line is
skipped and counted, not raised.  Anything unparsable in the *middle* is reported
too — that is corruption or a second writer, and silently ignoring it would turn
the ledger into a story nobody can audit.

**The checkpoint is replaced, never edited.**  Write to ``checkpoint.json.tmp`` in
the same directory, ``fsync`` it, ``os.replace`` it, then ``fsync`` the directory.
A reader therefore sees either the old complete file or the new complete file, and a
crash can at worst leave a stale ``.tmp`` that is ignored (and swept by
``Ledger.sweep_tmp``).

Schemas, so a reader never has to guess: every record gets ``ts`` (local ISO-8601,
with offset) and ``ts_epoch`` (float) from the ledger at append time — the caller
cannot backdate a record.  Which fields a record carries depends on the ledger; the
conventions live in :mod:`seerauto.dual.state` (it is the reader) and
:mod:`seerauto.dual.switch` / :mod:`seerauto.dual.watchdog` (the writers).

``model_usage.jsonl`` is the one ledger this module does not define: it is written by
``tools/usage_meter.py`` (schema ``usage_snapshot.v1``, ``kind="usage_snapshot"``).
The dual layer only reads that file's size / last ``ts`` — the single-writer rule
matters more than a shared schema, and the two writers would otherwise disagree
about what "tokens" means.

Writes are synchronous and fsynced on purpose.  At the watchdog's sub-second tick
this would be too expensive, which is why the watchdog throttles its own
``runtime_health`` record (see its docstring) instead of this writer growing a
buffering mode: a ledger that might lose the last N seconds is not evidence.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

__all__ = [
    "DEFAULT_DATA_DIR",
    "LEDGERS",
    "CHECKPOINT_NAME",
    "Ledger",
    "LedgerError",
    "stamp",
    "parse_ts",
]

#: repo root, one level above the ``seerauto`` package
ROOT = Path(__file__).resolve().parents[2]

#: The night's evidence directory (matches the experiment manifest already on disk).
DEFAULT_DATA_DIR = ROOT / "data" / "overnight" / "20261106_dual_mode"

#: Closed set, so a typo cannot create a ninth ledger nobody reads.
LEDGERS: tuple[str, ...] = (
    "events",
    "model_usage",
    "mode_switches",
    "world_snapshots",
    "runtime_health",
    "failures",
    "milestones",
)

CHECKPOINT_NAME = "checkpoint.json"

#: 64 KiB is far above the largest record we write; a single write(2) of that size is
#: atomic in practice on the local filesystem, which is what the append contract needs.
_MAX_RECORD_BYTES = 64 * 1024


class LedgerError(RuntimeError):
    """A ledger operation was asked to do something outside the contract."""


def stamp(ts: float | datetime | None = None) -> tuple[str, float]:
    """(local ISO-8601 with offset, epoch float) for a record's timestamp."""

    if ts is None:
        moment = datetime.now().astimezone()
    elif isinstance(ts, datetime):
        moment = ts.astimezone() if ts.tzinfo else ts.astimezone()
    else:
        moment = datetime.fromtimestamp(float(ts)).astimezone()
    return moment.isoformat(timespec="seconds"), moment.timestamp()


def parse_ts(value: Any) -> float | None:
    """Parse the timestamp shapes this repo writes, or None.

    Accepts ``ts_epoch`` (float), ``ts``/``at``/``started_at``/``ended_at`` ISO-8601
    strings (with or without offset; naive is assumed local) and bare epoch numbers.
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


class Ledger:
    """All JSONL ledgers + the checkpoint under one data directory."""

    def __init__(self, data_dir: str | Path | None = None, *, fsync: bool = True,
                 lock_timeout_s: float = 5.0) -> None:
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.fsync = bool(fsync)
        self.checkpoint_path = self.data_dir / CHECKPOINT_NAME
        self.lock_timeout_s = float(lock_timeout_s)

    # -- paths ------------------------------------------------------------- #
    def path(self, name: str) -> Path:
        if name not in LEDGERS:
            raise LedgerError(f"unknown ledger {name!r}; known: {', '.join(LEDGERS)}")
        return self.data_dir / f"{name}.jsonl"

    def ensure(self, *, create_ledgers: bool = True) -> "Ledger":
        """Create the directory (and empty ledger files) if absent."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if create_ledgers:
            for name in LEDGERS:
                self.path(name).touch(exist_ok=True)
        return self

    # -- writing ----------------------------------------------------------- #
    def append(self, name: str, payload: Mapping[str, Any],
               *, ts: float | datetime | None = None) -> dict[str, Any]:
        """Append one record and fsync it.  Returns the record as written.

        The ledger owns ``ts``/``ts_epoch``: they are set from the write moment (or
        from an explicit ``ts`` argument for backfills/tests), never from ``payload``,
        so a record cannot claim to have been written earlier than it was.
        """

        if not isinstance(payload, Mapping):
            raise LedgerError(f"{name} record must be a mapping, got "
                              f"{type(payload).__name__}")
        path = self.path(name)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        record = dict(payload)
        iso, epoch = stamp(ts)
        record["ts"] = iso
        record["ts_epoch"] = epoch
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        if len(data) > _MAX_RECORD_BYTES:
            raise LedgerError(f"{name} record is {len(data)} bytes > "
                              f"{_MAX_RECORD_BYTES}; split it instead of writing a "
                              f"multi-megabyte line")

        # O_RDWR (not O_WRONLY) so the last byte can be inspected: if the previous
        # append was interrupted mid-line, this record must start on a fresh line
        # instead of being concatenated onto the unparsable tail.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:  # pragma: no cover - a regular file cannot do this
                    raise LedgerError(f"short write to {path}")
                view = view[written:]
            if self.fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        return record

    # -- reading ----------------------------------------------------------- #
    def iter_records(self, name: str) -> Iterator[tuple[int, dict[str, Any] | None]]:
        """Yield ``(line_number, record_or_None)``; None marks an unparsable line.

        A missing file yields nothing.  The caller decides whether a ``None`` is the
        tolerated crash-truncated tail or something worse; :meth:`problems` is the
        convenience view for the latter.
        """

        path = self.path(name)
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        with fh:
            for lineno, line in enumerate(fh, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    yield lineno, None
                    continue
                yield lineno, record if isinstance(record, dict) else None

    def read(self, name: str) -> list[dict[str, Any]]:
        """Every parseable record, in write order.  Unparsable lines are skipped."""

        return [record for _, record in self.iter_records(name) if record is not None]

    def problems(self, name: str) -> list[dict[str, Any]]:
        """Unparsable line numbers, with whether each is the tolerated tail.

        ``truncated_tail=True`` means it is the last line of the file and the ledger
        therefore holds a crash-truncated append — normal, and all earlier records
        are intact.  ``False`` means the file has a hole in it and should be
        investigated before anyone quotes a count from it.

        Reads the file once: ``status`` asks this for every ledger, and a night's
        ``events.jsonl`` is the biggest thing on disk by 07:00.
        """

        try:
            with self.path(name).open("r", encoding="utf-8", errors="replace") as fh:
                lines = [line.strip() for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for lineno, text in enumerate(lines, 1):
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                out.append({"line": lineno, "truncated_tail": lineno == len(lines)})
                continue
            if not isinstance(record, dict):
                out.append({"line": lineno, "truncated_tail": lineno == len(lines)})
        return out

    def tail(self, name: str, n: int = 1) -> list[dict[str, Any]]:
        if n <= 0:
            return []
        return self.read(name)[-n:]

    def last(self, name: str) -> dict[str, Any] | None:
        tail = self.tail(name, 1)
        return tail[0] if tail else None

    def count(self, name: str) -> int:
        return len(self.read(name))

    def size(self, name: str) -> int:
        try:
            return self.path(name).stat().st_size
        except FileNotFoundError:
            return 0

    def sizes(self) -> dict[str, int]:
        return {name: self.size(name) for name in LEDGERS}

    def signature(self) -> tuple[tuple[str, int, float], ...]:
        """(name, size, mtime) per ledger — cheap "did anything change" probe."""

        out = []
        for name in LEDGERS:
            try:
                st = self.path(name).stat()
                out.append((name, st.st_size, st.st_mtime))
            except FileNotFoundError:
                out.append((name, 0, 0.0))
        return tuple(out)

    # -- checkpoint -------------------------------------------------------- #
    # Readers do NOT take the lock: ``os.replace`` is atomic, so a read either sees
    # the old complete file or the new complete file.  Writers do take it, because
    # the interesting failure is two *read-modify-write* cycles interleaving (the
    # watchdog saving its state while `switch` saves a new mode) — that silently
    # loses one of the two updates.  The lock is an advisory ``flock`` on
    # ``checkpoint.json.lock``, released by the OS if the process dies.
    def _lock_path(self, target: Path | None = None) -> Path:
        target = target or self.checkpoint_path
        return target.with_name(target.name + ".lock")

    def lock_path(self, *, path: str | Path | None = None) -> Path:
        return self._lock_path(Path(path) if path else None)

    @contextlib.contextmanager
    def checkpoint_lock(self, *, path: str | Path | None = None,
                        timeout_s: float | None = None) -> Iterator[None]:
        """Exclusive advisory lock for a checkpoint read-modify-write cycle.

        Blocks up to ``timeout_s`` (default 5 s) and raises rather than silently
        losing an update: a caller that cannot get the lock must find out.
        """

        target = Path(path) if path else self.checkpoint_path
        lock = self._lock_path(target)
        lock.parent.mkdir(parents=True, exist_ok=True)
        timeout = self.lock_timeout_s if timeout_s is None else float(timeout_s)
        fh = lock.open("a+")
        started = time.monotonic()
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() - started >= timeout:
                        raise LedgerError(
                            f"checkpoint lock {lock} was not acquired within "
                            f"{timeout}s; another process is holding it") from None
                    time.sleep(0.01)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover
                pass
            fh.close()

    def save_checkpoint(self, state: Mapping[str, Any],
                        *, path: str | Path | None = None) -> Path:
        """Atomically replace the checkpoint (tmp + fsync + rename + dir fsync).

        Takes the checkpoint lock: a blind overwrite racing a read-modify-write
        would otherwise drop whichever update landed first.
        """

        target = Path(path) if path else self.checkpoint_path
        with self.checkpoint_lock(path=target):
            return self._save_checkpoint_unlocked(state, target)

    def _save_checkpoint_unlocked(self, state: Mapping[str, Any],
                                  target: Path) -> Path:
        """The actual atomic replace; assumes the checkpoint lock is held."""

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(dict(state), fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            # Never leave a half-written body where a reader could find it.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        self._fsync_dir(target.parent)
        return target

    def load_checkpoint(self, *, path: str | Path | None = None,
                        default: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Load the checkpoint, or ``default`` when absent/unreadable.

        An unreadable checkpoint is moved aside to ``checkpoint.json.corrupt-<stamp>``
        and an ``events`` record is appended, because the only way it can be corrupt
        is a bug or an edited file — either one must leave a trace.
        """

        target = Path(path) if path else self.checkpoint_path
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return dict(default) if default is not None else None
        try:
            state = json.loads(text)
        except json.JSONDecodeError as exc:
            aside = target.with_name(f"{target.name}.corrupt-{int(time.time())}")
            try:
                target.replace(aside)
            except OSError:
                aside = None
            self._append_safely("events", {
                "kind": "checkpoint.corrupt",
                "error": f"{type(exc).__name__}: {exc}",
                "moved_aside": str(aside) if aside else None,
            })
            return dict(default) if default is not None else None
        if not isinstance(state, dict):
            return dict(default) if default is not None else None
        return state

    def update_checkpoint(self, changes: Mapping[str, Any] | None = None,
                          *, path: str | Path | None = None,
                          **kwargs: Any) -> dict[str, Any]:
        """Merge ``changes``/``kwargs`` into the checkpoint and save it atomically.

        One lock covers load+merge+save, so concurrent writers cannot lose each
        other's keys (the 2026-11-06 acceptance run demonstrated a lost mode switch
        when the watchdog and the CLI interleaved their read-modify-write cycles).
        """

        def mutate(state: dict[str, Any]) -> None:
            state.update(changes or {})
            state.update(kwargs)
            state["checkpoint_updated_epoch"] = round(time.time(), 3)

        return self.mutate_checkpoint(mutate, path=path)

    def mutate_checkpoint(self, mutate: Callable[[dict[str, Any]], None],
                          *, path: str | Path | None = None,
                          default: Mapping[str, Any] | None = None
                          ) -> dict[str, Any]:
        """Locked read-modify-write: ``mutate(state)`` edits in place, then it saves.

        The returned dict is what was written.  Use this for any update that depends
        on the current contents; a plain :meth:`save_checkpoint` is only safe when
        the caller owns every key it writes.
        """

        target = Path(path) if path else self.checkpoint_path
        with self.checkpoint_lock(path=target):
            state = self.load_checkpoint(path=target, default=default or {}) or {}
            mutate(state)
            self._save_checkpoint_unlocked(state, target)
            return state

    def _append_safely(self, name: str, payload: Mapping[str, Any]) -> None:
        """Best-effort append used from error paths; never raises."""

        try:
            self.append(name, payload)
        except Exception:  # noqa: BLE001 - an error path must not raise a second error
            pass

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:  # pragma: no cover - macOS always allows this
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(fd)

    def sweep_tmp(self) -> list[str]:
        """Remove stale ``*.tmp`` siblings (a crash between tmp-write and rename)."""

        removed = []
        for tmp in self.data_dir.glob("*.tmp"):
            try:
                tmp.unlink()
                removed.append(str(tmp))
            except OSError:  # pragma: no cover
                pass
        return removed
