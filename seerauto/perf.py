"""Stage timing recorder.

The user's performance requirement is simple: *the loop's latency must always
be observable*.  Every stage the engine performs - capture, decode, detect,
decide, input, wait, confirm - is wrapped in ``rec.stage(...)`` and lands in a
recorder that can dump a JSON summary at any moment.

Pure stdlib on purpose: this module must keep working even when ``cv2`` is
missing (``python -m seerauto doctor`` and ``bench`` are where you notice).
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from statistics import mean
from typing import Dict, Iterable, Iterator, List, Optional

#: Canonical stage names, aligned with the project-wide benchmark vocabulary so
#: numbers are comparable across agents/tools.  Not enforced (callers may add
#: custom stages), but using these keeps ``bench`` output comparable.
#:
#: ``capture_read``  - read latest frame from the backend (no waiting for new)
#: ``capture_copy``  - memcpy/decode/crop of the frame
#: ``detect_fingerprint`` - patch signatures / downsampled diffs
#: ``detect_template``    - ROI template matching
#: ``ocr``           - rare escalation only, never on the poll path
#: ``decide``        - state machine classification + transition
#: ``model``         - task policy step (what would we click next)
#: ``input``         - pointer/keyboard action (+ window guard)
#: ``wait``          - tick sleep / change-wait
#: ``confirm``       - post-action observation (did the screen change?)
STAGES = (
    "capture_read",
    "capture_copy",
    "decode",
    "detect_fingerprint",
    "detect_template",
    "ocr",
    "decide",
    "model",
    "input",
    "wait",
    "confirm",
)


def percentile(sorted_values: List[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..100).  Returns ``nan`` if empty."""
    if not sorted_values:
        return float("nan")
    if q <= 0:
        return sorted_values[0]
    if q >= 100:
        return sorted_values[-1]
    # nearest rank
    rank = int(round((q / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(rank, len(sorted_values) - 1))]


@dataclass
class StageStats:
    count: int = 0
    total_ns: int = 0
    min_ns: int = 0
    max_ns: int = 0
    samples_ns: List[int] = field(default_factory=list)

    def add(self, duration_ns: int) -> None:
        self.count += 1
        self.total_ns += duration_ns
        if self.count == 1 or duration_ns < self.min_ns:
            self.min_ns = duration_ns
        if duration_ns > self.max_ns:
            self.max_ns = duration_ns
        self.samples_ns.append(duration_ns)

    def as_dict(self) -> Dict[str, float]:
        ordered = sorted(self.samples_ns)
        return {
            "count": self.count,
            "total_ms": self.total_ns / 1e6,
            "mean_ms": (self.total_ns / self.count) / 1e6 if self.count else 0.0,
            "min_ms": self.min_ns / 1e6,
            "p50_ms": percentile(ordered, 50) / 1e6,
            "p95_ms": percentile(ordered, 95) / 1e6,
            "p99_ms": percentile(ordered, 99) / 1e6,
            "max_ms": self.max_ns / 1e6,
        }


class Timer:
    """Context manager returned by :meth:`PerfRecorder.stage`.

    ``timer.extra`` is merged into the sample tags after the block, so a stage
    can be annotated with what it saw (e.g. ``{"hits": 2}``).
    """

    __slots__ = ("recorder", "name", "tags", "start_ns", "duration_ns", "extra")

    def __init__(self, recorder: "PerfRecorder", name: str, tags: Optional[Dict[str, object]] = None):
        self.recorder = recorder
        self.name = name
        self.tags = dict(tags or {})
        self.start_ns = 0
        self.duration_ns = 0
        self.extra: Dict[str, object] = {}

    def __enter__(self) -> "Timer":
        self.start_ns = self.recorder.clock()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.duration_ns = self.recorder.clock() - self.start_ns
        merged = {**self.tags, **self.extra, "ok": exc_type is None}
        self.recorder.record(self.name, self.duration_ns, **merged)
        return False


class PerfRecorder:
    """Collects per-stage timings and dumps a JSON summary."""

    def __init__(self, clock=time.perf_counter_ns, enabled: bool = True, max_samples: int = 20000):
        self.clock = clock
        self.enabled = enabled
        self.max_samples = max_samples
        self.start_ns = clock()
        self.iteration = 0
        self.stages: Dict[str, StageStats] = {}
        self.samples: List[Dict[str, object]] = []
        self.counters: Dict[str, int] = {}
        self.notes: List[str] = []

    # -- recording --------------------------------------------------------- #
    def stage(self, name: str, **tags: object) -> Timer:
        return Timer(self, name, tags)

    def record(self, name: str, duration_ns: int, **tags: object) -> None:
        if not self.enabled:
            return
        stats = self.stages.setdefault(name, StageStats())
        stats.add(int(duration_ns))
        if len(self.samples) < self.max_samples:
            self.samples.append(
                {
                    "stage": name,
                    "iteration": self.iteration,
                    "ns": int(duration_ns),
                    **tags,
                }
            )

    @contextmanager
    def timed(self, name: str, **tags: object) -> Iterator[Timer]:
        timer = self.stage(name, **tags)
        with timer as t:
            yield t

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def next_iteration(self) -> int:
        self.iteration += 1
        return self.iteration

    def note(self, message: str) -> None:
        self.notes.append(message)

    # -- reporting --------------------------------------------------------- #
    def stage_names(self) -> List[str]:
        return sorted(self.stages)

    def summary(self) -> Dict[str, object]:
        stages = {name: stats.as_dict() for name, stats in sorted(self.stages.items())}
        wall_ns = self.clock() - self.start_ns
        return {
            "iterations": self.iteration,
            "wall_ms": wall_ns / 1e6,
            "stages": stages,
            "counters": dict(sorted(self.counters.items())),
            "notes": list(self.notes),
        }

    def format_table(self, stages: Optional[Iterable[str]] = None) -> str:
        """Human readable fixed-width table for ``seerauto bench``."""
        names = list(stages) if stages is not None else self.stage_names()
        if not names:
            return "(no timings recorded)"
        header = f"{'stage':<12}{'n':>7}{'mean_ms':>10}{'p50_ms':>10}{'p95_ms':>10}{'max_ms':>10}{'total_ms':>11}"
        lines = [header, "-" * len(header)]
        for name in names:
            stats = self.stages.get(name)
            if not stats or not stats.count:
                continue
            d = stats.as_dict()
            lines.append(
                f"{name:<12}{d['count']:>7d}{d['mean_ms']:>10.3f}{d['p50_ms']:>10.3f}"
                f"{d['p95_ms']:>10.3f}{d['max_ms']:>10.3f}{d['total_ms']:>11.3f}"
            )
        if len(lines) == 2:
            return "(no timings recorded)"
        return "\n".join(lines)

    def dump_json(self, path: str, extra: Optional[Dict[str, object]] = None) -> str:
        payload = self.summary()
        if extra:
            payload["extra"] = extra
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return path

    def reset(self) -> None:
        self.start_ns = self.clock()
        self.iteration = 0
        self.stages.clear()
        self.samples.clear()
        self.counters.clear()
        self.notes.clear()


def summarize_samples(samples_ns: Iterable[int]) -> Dict[str, float]:
    """Convenience helper used by tests and ad-hoc benches."""
    ordered = sorted(int(s) for s in samples_ns)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "mean_ms": mean(ordered) / 1e6,
        "p50_ms": percentile(ordered, 50) / 1e6,
        "p95_ms": percentile(ordered, 95) / 1e6,
        "max_ms": ordered[-1] / 1e6,
    }
