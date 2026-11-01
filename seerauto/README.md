# seerauto — pure-vision automation core

A local, deterministic, **no-LLM / no-network** engine for the 《赛尔号 Xin 计划》
macOS client (BetterGI-style). It is the production replacement for the old
`scripts/tower.py` loop whose blind `time.sleep()` waits were the main cost
(`docs/KNOWN_PROBLEMS.md` P3/P4/P7).

## Status (read this before trusting anything)

| Area | Status |
|---|---|
| coordinate math, state machine, executor, budgets, pause/stop | **unit-tested** (synthetic signals) |
| vision / templates / fingerprints / state classification | **replay-tested** against real screenshots in `artifacts/` |
| capture backends | `SubprocessScreencapture` implemented (the honest fallback, ~139 ms/grab); `DaemonCapture` protocol implemented and exercised against a fake JSON-line daemon, **no native daemon exists in this repo yet** |
| OCR | interface + daemon delegation only; **not executed here** (no daemon, and full-frame OCR is 84% of the old loop cost) |
| BATTLE_RESULT / REWARD / ERROR states | rules are **disabled**: `artifacts/` contains no victory/defeat/reward fixture, so nothing about them is verified |
| live game | **never tested** — the client was not running while this package was written |

No claim in this package was verified against a running game. Replay runs
decode stored PNGs; they do not include ScreenCaptureKit handoff, live frame
age, or input.

## Commands

```bash
PYTHONPATH=.pydeps python3 -m seerauto doctor          # env / permission / window report
PYTHONPATH=.pydeps python3 -m seerauto replay artifacts/  # classify every stored screenshot
PYTHONPATH=.pydeps python3 -m seerauto bench           # hot-loop micro-benchmark
PYTHONPATH=.pydeps python3 -m pytest -q                # 167 tests
```

`doctor` prints `game window: NOT FOUND` when the client is not running (the
normal state on this machine).

## Architecture

```
window.py    window discovery (daemon → external JSON lister → static),
             identity/geometry guard, pinning, Retina scale
capture.py   Frame (BGR array + global origin + scale + seq + monotonic ts +
             stage map), CaptureBackend protocol, SubprocessScreencapture,
             DaemonCapture (warm ScreenCaptureKit session), ReplayCapture,
             NullCapture
vision.py    letterbox stage detection, TM_CCOEFF_NORMED ROI template matching
             (cached, half-resolution pyramid), patch fingerprints,
             cheap frame signatures for change detection, thin OCR interface
             and the Analyzer (Frame -> Signals; no OCR on the poll path)
state.py     explicit UNKNOWN/IDLE/BATTLE_TURN/BATTLE_RESULT/REWARD/DIALOG/
             ERROR/PAUSED/STOPPED machine, data-driven rules/transitions/
             timeouts/actions; last-seen-with-TTL signals so "not visible for
             one animation frame" is inconclusive, not IDLE
actions.py   input abstraction that verifies window identity + geometry before
             every click (refuses on movement beyond tolerance)
executor.py  own-clock polling (never blocks for a frame), change-driven
             analysis, per-tick budgets, action confirmation, OCR escalation
             only when UNKNOWN ages out, per-stage perf
perf.py      stage timing recorder (mean/p50/p95/p99/max) + JSON dump
config.py    validated JSON/YAML config (defaults in config/default.json)
tasks/       battle loop skeleton (config-driven coordinates/turn budgets)
PROTOCOL.md  the native daemon contract (warm SCK session, non-blocking latest)
```

## Performance rules encoded here

Measured on this host (replay, 2298x1428 frame, not a live capture):

* `patch_signature` (colour grid) ~0.03 ms; `frame_signature` ~0.11 ms;
  `signature_delta` ~0.02 ms
* ROI template match ~0.4–1.3 ms after two fixes:
  * **`cv2.resize(INTER_AREA)` is pathologically slow in this OpenCV build**
    (~14 ms for 2298x1428 → 160x100; `INTER_LINEAR` is ~0.04 ms).
    `vision.downsample()` stride-subsamples first and is used everywhere.
  * templates are matched on a half-resolution ROI/template pyramid.
* full `Analyzer.analyze` p95 ≈ 3.7 ms → passes the 16 ms per-tick budget
  (asserted by `test_tick_p95_stays_inside_16ms_frame_budget`).
* OCR is never on the poll path. It is reachable only from the executor when the
  state has been UNKNOWN for `capture.unknown_ocr_after_ms`, and only with a
  daemon-provided OCR engine.
* ScreenCaptureKit is change-driven: `capture.latest()` returns the newest frame
  immediately, possibly hundreds of ms old. A stale frame is a normal
  observation; only a state that `expects_motion` (BATTLE_TURN) escalates on a
  stale frame.

## Local dependencies

`cv2` and `numpy` live in `./.pydeps` on this host:

```bash
python3 -m pip install --target .pydeps -r requirements.txt
python3 -m pip install --target .pydeps pytest
PYTHONPATH=.pydeps python3 -m seerauto doctor
```
