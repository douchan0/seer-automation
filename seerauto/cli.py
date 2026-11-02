"""Command line entry points.

    python3 -m seerauto doctor                 environment / permission / window check
    python3 -m seerauto replay artifacts/      run the vision+state pipeline on stored PNGs
    python3 -m seerauto bench                  micro-benchmark the hot-loop primitives

All three work with no game and no network.  ``replay`` and ``bench`` are
explicitly *replay-tested* tools: they prove the pipeline on real screenshots,
they do not claim anything about live-game behaviour.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from . import capture as C
from . import geometry as G
from . import vision as V
from .actions import Actions, ClickAction, ClickPointAction, RecordingInput, StopAction
from .config import AppConfig, load_config
from .errors import CaptureUnavailable, ConfigError, SeerAutoError, WindowNotFound
from .executor import TickContext
from .perf import PerfRecorder
from .state import Detection, State, StateMachine, model_from_config
from .tasks.battle import BattleTask
from .window import (
    DaemonWindowFinder,
    ExternalJSONWindowFinder,
    StaticWindowFinder,
    WindowInfo,
    find_window,
)

DEFAULT_REPLAY_DIR = "artifacts"


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _load(args: argparse.Namespace) -> AppConfig:
    return load_config(getattr(args, "config", None))


def _expected_fingerprints(config: AppConfig) -> dict[str, V.Fingerprint]:
    return {
        name: V.Fingerprint(
            grid=spec.grid,
            cells=spec.cells,
            mean=spec.mean,
            std=spec.std,
        )
        for name, spec in config.vision.fingerprints.items()
    }


def _static_window(config: AppConfig) -> WindowInfo:
    origin = config.window.expected_origin_pt or (0.0, 0.0)
    size = config.window.expected_size_pt
    return WindowInfo(
        window_id=-1,
        owner="SeerAuto",
        title="replay-static",
        geometry=G.WindowGeometry(
            origin_pt=G.Point(*origin), size_pt=G.Size(*size), scale=config.window.scale
        ),
        source="static",
    )


def _describe_action(action: Any, config: AppConfig) -> str:
    if action is None:
        return "-"
    if isinstance(action, StopAction):
        return f"STOP({action.reason})"
    if isinstance(action, ClickAction):
        parts = []
        for name in action.targets:
            rect = config.game_px(name)
            parts.append(f"{name}@({rect.x:.0f},{rect.y:.0f})")
        return "click " + " -> ".join(parts)
    if isinstance(action, ClickPointAction):
        return f"click ({action.x:.0f},{action.y:.0f})"
    return f"{type(action).__name__}({action.label})"


def _scores(signals: V.Signals) -> str:
    if not signals.templates:
        return "-"
    return ",".join(f"{name}={match.score:.3f}" for name, match in sorted(signals.templates.items()))


def _version(module_name: str) -> str:
    try:
        module = __import__(module_name)
        return str(getattr(module, "__version__", "unknown"))
    except Exception as exc:  # noqa: BLE001
        return f"NOT AVAILABLE ({exc})"


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def _probe_screen_recording(config: AppConfig) -> tuple[str, str]:
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="seerauto-doctor-", suffix=".png")
        os.close(fd)
        proc = subprocess.run(
            [config.capture.screencapture_path, "-x", "-R", "0,0,8,8", path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return "FAILED", (proc.stderr or "").strip() or f"rc={proc.returncode}"
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return "FAILED", "screencapture produced no file"
        try:
            image = C.read_image(path)
        except CaptureUnavailable:
            return "UNKNOWN", "captured a file but cv2 is missing (cannot verify contents)"
        return "OK", f"{image.shape[1]}x{image.shape[0]} px probe"
    except (OSError, subprocess.SubprocessError) as exc:
        return "FAILED", str(exc)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _build_finders(config: AppConfig) -> list[Any]:
    finders: list[Any] = []
    command = config.window.finder_command or ExternalJSONWindowFinder.default_command()
    finders.append(
        ExternalJSONWindowFinder(
            command=command,
            owner_aliases=config.window.owner_aliases,
            scale=config.window.scale,
        )
    )
    if config.capture.daemon_binary:
        client = C.DaemonClient(config.capture.daemon_binary, args=config.capture.daemon_args)
        finders.append(DaemonWindowFinder(client, scale=config.window.scale))
    return finders


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"seerauto {__version__} - environment report")
    print(f"python:      {sys.version.split()[0]} ({platform.system()} {platform.release()} {platform.machine()})")
    print(f"cv2:         {_version('cv2')}")
    print(f"numpy:       {_version('numpy')}")

    # -- config ----------------------------------------------------------- #
    try:
        config = _load(args)
        print(f"config:      OK {config.path}")
        print(
            f"             {len(config.states.rules)} rules "
            f"({sum(1 for r in config.states.rules if r.enabled)} enabled), "
            f"{len(config.states.definitions)} states, "
            f"{len(config.vision.fingerprints)} fingerprints"
        )
        missing_templates = [
            name
            for name, spec in config.vision.templates.items()
            if not (config.vision.template_dir / spec.file).exists()
        ]
        print(
            f"templates:   {len(config.vision.templates)} declared, "
            f"{'all present' if not missing_templates else 'MISSING ' + ', '.join(missing_templates)}"
        )
    except ConfigError as exc:
        print(f"config:      FAILED - {exc}")
        return 2

    # -- window ----------------------------------------------------------- #
    finders = _build_finders(config)
    try:
        info, report = find_window(finders, config.window.owner_aliases)
        print(f"game window: FOUND - {info.describe()}")
        if not info.geometry.is_close(
            G.WindowGeometry(
                origin_pt=G.Point(*(config.window.expected_origin_pt or (0, 0))),
                size_pt=G.Size(*config.window.expected_size_pt),
                scale=config.window.scale,
            ),
            tol_pt=config.window.tolerance_pt,
        ):
            print("             WARNING: geometry differs from config.window.expected_*")
        for line in report.as_lines():
            print(line)
    except WindowNotFound as exc:
        print("game window: NOT FOUND")
        print(f"             {exc}")
        for line in _finder_details(finders):
            print(line)

    # -- permissions / capture ------------------------------------------- #
    state, detail = _probe_screen_recording(config)
    print(f"screen rec.: {state} - {detail}")

    one_shot = C.SubprocessScreencapture(
        G.WindowGeometry(
            origin_pt=G.Point(*(config.window.expected_origin_pt or (0, 0))),
            scale=config.window.scale,
        ),
        screencapture=config.capture.screencapture_path,
    )
    print(f"capture:     screencapture={'available' if one_shot.available else 'MISSING'} ({config.capture.screencapture_path})")
    if config.capture.daemon_binary:
        client = C.DaemonClient(config.capture.daemon_binary, args=config.capture.daemon_args)
        print(f"             daemon={'available' if client.available else 'NOT AVAILABLE'} ({client.describe()})")
        print("             warm-session ops: capture.start / capture.latest / capture.roi / capture.stop")
    else:
        client = None
        print("             daemon=not configured (fallback: screencapture, ~139 ms per grab)")
    print(f"ocr:         {'daemon' if client else 'disabled'} (pure-vision default: templates first, OCR only as escalation)")

    # -- budgets ---------------------------------------------------------- #
    budgets = config.budgets
    print(
        "budgets:     "
        f"iterations<={budgets.max_iterations}, duration<={budgets.max_duration_s:g}s, "
        f"errors<={budgets.max_consecutive_errors}, action_retries<={budgets.action_retries}"
    )
    print(
        "battle:      "
        f"turns<={config.battle.max_turns_per_battle}, battles<={config.battle.max_battles}, "
        f"on_loss={config.battle.on_loss}"
    )
    print(
        "honesty:     replay-tested on artifacts/ only; no live-game verification was performed "
        "(game client not running)."
    )
    return 0


def _finder_details(finders: Sequence[Any]) -> list[str]:
    lines: list[str] = []
    for finder in finders:
        name = getattr(finder, "name", type(finder).__name__)
        if not getattr(finder, "available", True):
            lines.append(f"             {name}: NOT AVAILABLE")
            continue
        try:
            windows = finder.list_windows()
        except Exception as exc:  # noqa: BLE001
            lines.append(f"             {name}: error - {exc}")
            continue
        detail = getattr(finder, "last_error", None)
        owners = sorted({w.owner for w in windows})[:6]
        lines.append(
            f"             {name}: {len(windows)} window(s)"
            + (f", owners={owners}" if owners else "")
            + (f", note={detail}" if detail else "")
        )
    return lines


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def cmd_replay(args: argparse.Namespace) -> int:
    config = _load(args)
    directory = args.directory or DEFAULT_REPLAY_DIR
    try:
        files = C.replay_files(directory)
    except SeerAutoError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"replay: no PNG/JPEG fixtures in {directory}", file=sys.stderr)
        return 2

    perf = PerfRecorder()
    detector = V.TemplateDetector(V.TemplateCache(config.vision.template_dir))
    analyzer = V.Analyzer.from_config(config, detector=detector, ocr=V.NullOcr(), perf=perf)
    expected = _expected_fingerprints(config)
    model = model_from_config(config.states, expected)
    actions = Actions(
        backend=RecordingInput(),
        expected_window=_static_window(config),
        finder=None,
        tolerance_pt=config.window.tolerance_pt,
        dry_run=True,
    )

    print(f"replay: {len(files)} fixture(s) from {directory} (no game, no network)")
    header = f"{'file':26s} {'size':>10s} {'stage':>6s} {'state':<13s} {'would-do':<28s} {'rule':<24s} {'total_ms':>8s}"
    print(header)
    print("-" * len(header))

    records: list[dict[str, Any]] = []
    state_counts: dict[str, int] = {}
    for path in files:
        started = time.perf_counter_ns()
        record: dict[str, Any] = {"file": path.name, "path": str(path)}
        try:
            backend = C.ReplayCapture([path], stage_detector=V.detect_stage_rect)
            frame = backend.grab()
            signals = analyzer.analyze(frame, time.monotonic_ns())
            machine = StateMachine(model)
            detection: Detection = machine.classify(signals, time.monotonic_ns())
            task = BattleTask(config.battle)
            ctx = TickContext(
                now_ns=time.monotonic_ns(),
                state=detection.state,
                signals=signals,
                detection=detection,
                transition=None,
                frame=frame,
                actions=actions,
                config=config,
                elapsed_s=0.0,
                since_action_s=None,
                changed_since_action=True,
                perf=perf,
            )
            action = task.decide(ctx)
            elapsed_ms = (time.perf_counter_ns() - started) / 1e6
            stage_kind = "detect" if frame.meta.get("stage_detected") else "ratio"
            state_counts[detection.state.value] = state_counts.get(detection.state.value, 0) + 1
            print(
                f"{path.name:26s} {frame.width:5d}x{frame.height:<4d} {stage_kind:>6s} "
                f"{detection.state.value:<13s} {_describe_action(action, config):<28s} "
                f"{detection.rule_id:<24s} {elapsed_ms:8.2f}"
            )
            record.update(
                {
                    "width": frame.width,
                    "height": frame.height,
                    "stage": "detected" if stage_kind == "detect" else "proportional",
                    "stage_rect": list(signals.stage_rect_px or []),
                    "state": detection.state.value,
                    "rule": detection.rule_id,
                    "evidence": list(detection.evidence),
                    "templates": {n: round(m.score, 4) for n, m in signals.templates.items()},
                    "proposed_action": _describe_action(action, config),
                    "ms": round(elapsed_ms, 3),
                }
            )
        except SeerAutoError as exc:
            record.update({"error": f"{type(exc).__name__}: {exc}"})
            print(f"{path.name:26s} ERROR {type(exc).__name__}: {exc}")
        records.append(record)

    print("-" * len(header))
    print("states: " + ", ".join(f"{k}={v}" for k, v in sorted(state_counts.items())))
    print()
    print(perf.format_table(("detect_template", "detect_fingerprint", "decide")))
    print()
    print("note: replay is replay-tested only - decoded PNG fixtures, no live capture, no OCR.")
    if config.notes:
        for note in config.notes:
            print(f"note: {note}")

    if args.json:
        payload = {
            "version": __version__,
            "mode": "replay",
            "directory": str(directory),
            "records": records,
            "state_counts": state_counts,
            "perf": perf.summary(),
        }
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {out}")
    return 0


# --------------------------------------------------------------------------- #
# bench
# --------------------------------------------------------------------------- #
def cmd_bench(args: argparse.Namespace) -> int:
    config = _load(args)
    frame_path = args.frame or str(Path(DEFAULT_REPLAY_DIR) / "game.png")
    if not Path(frame_path).exists():
        print(f"bench: fixture not found: {frame_path}", file=sys.stderr)
        return 2
    iterations = max(1, int(args.iterations))
    perf = PerfRecorder()
    detector = V.TemplateDetector(V.TemplateCache(config.vision.template_dir))
    analyzer = V.Analyzer.from_config(config, detector=detector, ocr=V.NullOcr(), perf=perf)

    decode_started = time.perf_counter_ns()
    image = C.read_image(frame_path)
    decode_ns = time.perf_counter_ns() - decode_started
    geometry = G.WindowGeometry(
        origin_pt=G.Point(*(config.window.expected_origin_pt or (0, 0))),
        size_pt=G.Size(*config.window.expected_size_pt),
        scale=config.window.scale,
    )
    frame = C.make_frame(image, geometry, G.Rect(0.0, 0.0, *config.reference.capture_px.as_tuple()))
    stage = V.detect_stage_rect(frame)
    if stage is not None:
        frame = frame.with_stage(
            G.StageMap(
                stage_px=stage,
                ref_size=config.reference.stage_px.size,
                ref_offset=G.Point(config.reference.stage_px.x, config.reference.stage_px.y),
            )
        )
    fingerprint_rois = {name: spec.roi for name, spec in config.vision.fingerprints.items()}
    template_specs = dict(analyzer.templates)

    print(f"bench: {frame_path} ({frame.width}x{frame.height} px, decode {decode_ns / 1e6:.2f} ms)")
    print(f"       {iterations} iterations per primitive - static PNG replay, NOT a live capture measurement")
    print()
    for _ in range(iterations):
        for name, roi in fingerprint_rois.items():
            started = time.perf_counter_ns()
            V.patch_signature(frame, roi)
            perf.record("detect_fingerprint", time.perf_counter_ns() - started, fingerprint=name)
        for name, spec in template_specs.items():
            started = time.perf_counter_ns()
            detector.match(frame, spec)
            perf.record("detect_template", time.perf_counter_ns() - started, template=name)
        started = time.perf_counter_ns()
        sig = V.frame_signature(frame)
        perf.record("detect_fingerprint", time.perf_counter_ns() - started, what="frame_signature")
        started = time.perf_counter_ns()
        V.signature_delta(sig, sig)
        perf.record("detect_fingerprint", time.perf_counter_ns() - started, what="signature_delta")
        started = time.perf_counter_ns()
        analyzer.analyze(frame, time.monotonic_ns())
        perf.record("tick_total", time.perf_counter_ns() - started, what="analyze_total")
        perf.next_iteration()

    print(perf.format_table((
        "detect_fingerprint",
        "detect_template",
        "tick_total",
    )))
    print()
    summary = perf.stages.get("tick_total")
    if summary is not None and summary.count:
        p95_ms = summary.as_dict()["p95_ms"]
        budget_ms = 16.0
        verdict = "PASS" if p95_ms <= budget_ms else "FAIL"
        print(f"frame budget: analyze p95={p95_ms:.2f} ms vs {budget_ms:.0f} ms -> {verdict}")
    print("note: capture_read/capture_copy/ocr are excluded here; OCR is an escalation path only.")
    print("note: static replay numbers do not include ScreenCaptureKit handoff or decode of live frames.")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"mode": "bench", "frame": frame_path, "perf": perf.summary()}, indent=2) + "\n")
        print(f"wrote {out}")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m seerauto",
        description="Pure-vision automation core for the XIN计划 client (no LLM, no network).",
    )
    parser.add_argument("--version", action="version", version=f"seerauto {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="environment, permission and window check")
    doctor.add_argument("--config", default=None, help="config file (JSON/YAML)")
    doctor.set_defaults(func=cmd_doctor)

    replay = sub.add_parser("replay", help="run the pipeline over stored screenshots")
    replay.add_argument("directory", nargs="?", default=DEFAULT_REPLAY_DIR)
    replay.add_argument("--limit", type=int, default=0)
    replay.add_argument("--json", default=None, help="write the full report as JSON")
    replay.add_argument("--config", default=None)
    replay.set_defaults(func=cmd_replay)

    from .goalcli import add_goal_subcommand

    add_goal_subcommand(sub)

    bench = sub.add_parser("bench", help="micro-benchmark the hot-loop primitives")
    bench.add_argument("--frame", default=None, help="fixture PNG (default artifacts/game.png)")
    bench.add_argument("--iterations", type=int, default=30)
    bench.add_argument("--json", default=None)
    bench.add_argument("--config", default=None)
    bench.set_defaults(func=cmd_bench)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except CaptureUnavailable as exc:
        print(f"capture unavailable: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI crash must be readable, not a traceback
        from .runner.control import LockHeld

        if isinstance(exc, LockHeld):
            print(f"refused: {exc}", file=sys.stderr)
            return 4
        raise
