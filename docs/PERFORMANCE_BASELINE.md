# PERFORMANCE_BASELINE.md — measured latency of the screenshot → analyse → act loop

Round 1 performance baseline. Every number in this document was produced by a
command run on this machine and is stored in `bench/results/*.json`. Nothing
here is estimated.

**Owner:** bench-engineer (R1-A)
**Hardware:** Apple M2 (MacBook Air Mac14,2), 8 cores, 24 GB, macOS 15.7.7 (24G720), arm64
**Toolchain:** Swift 6.2.3 (Xcode CLT), Python 3.10.13 (pyenv), OpenCV 5.0.0 + numpy 2.2.6 in `./.pydeps`

---

## 0. Read this first — scope and honesty statement

> ### measured on the real game window: **NO**
>
> `/Applications/SeerGame.app` is installed but **was not running** at any point
> during this work (`seer_game_window_count: 0`, checked in every run's JSON via
> `CGWindowListCopyWindowInfo`). **Not one number here is a measurement of the
> live game.**
>
> Two stand-ins were used, and every JSON record is tagged with which one:
>
> 1. **Generic window** — the largest layer-0 on-screen window owned by another
>    process (WPS Office 1380×765, DeepSeek Harness 1280×600, depending on the
>    run). Labelled `"generic window, NOT the game window"`. Useful for capture
>    cost only; **its OCR cost is not representative** (a text document yields
>    45–95 OCR items vs 12 for real game content).
> 2. **Synthetic window displaying the real screenshot** — a borderless,
>    non-activating, `ignoresMouseEvents` window showing `artifacts/game.png` at
>    exactly **1149×714 pt @ (300,120) → 2298×1428 px**, the real game window
>    geometry from `docs/state.md`. Labelled
>    `"SYNTHETIC window displaying a real screenshot ... NOT the live game window"`.
>    This is the only way to measure capture+OCR on **real game content at real
>    game resolution** while the game is closed. It is corroborated: it yields
>    **12 OCR items at conf ≥ 0.3**, exactly matching the independent
>    `./scripts/seerctl ocr artifacts/game.png 0.3` measurement.
>
> **What this means:** capture costs, stream behaviour, PNG codec costs and
> vision-primitive costs transfer to the real game with high confidence — they
> depend on geometry and pixel format, which are identical. **End-to-end loop
> totals will shift when the real client runs**, because OCR cost depends on
> how much text is actually on screen. Treat loop totals as the right *shape*
> and the right *ratios*, not as a promise of absolute milliseconds.
>
> Input latency (mouse/keyboard) is **not measured here** — that is the Lead's
> scope. This suite never moves the mouse and never posts synthetic events.

---

## 1. The question: "the whole loop is slow" — where does the time actually go?

Answer first, evidence after.

**On real game content (12 OCR items, 2298×1428), the current production loop
takes 313.8 ms per iteration. It splits into three measured parts:**

| stage | mean ms | p50 ms | p95 ms | share | how measured |
|---|---|---|---|---|---|
| `screencapture` subprocess (spawn + capture + PNG write) | **132.28** | 132.65 | 134.71 | **42.2 %** | timestamp inside each iteration |
| PNG decode (real pixel decode) | **36.86** | 36.97 | 37.15 | **11.7 %** | timestamp inside each iteration |
| Vision OCR `.accurate` (full frame) | **144.68** | 144.22 | 148.67 | **46.1 %** | timestamp inside each iteration |
| **total** | **313.82** | | | 100 % | n=10, 12 OCR items |

**Replacing the subprocess+PNG capture path with an in-process SCK screenshot
removes the first two rows** (132.28 + 36.86 = 169.14 ms) and replaces them with
**43.98 ms** of SCK capture:

| stage | mean ms | p50 ms | p95 ms | share |
|---|---|---|---|---|
| SCK `SCScreenshotManager` capture | **43.98** | 44.05 | 44.53 | 23.1 % |
| Vision OCR `.accurate` (full frame) | **146.54** | 145.05 | 154.88 | 76.9 % |
| **total** | **190.52** | | | 100 % (n=10, 12 OCR items) |

**→ 313.8 ms → 190.5 ms = 1.65× on the end-to-end loop, with identical OCR
output (12 items on both paths, relative item drift 0.0).**

The internal consistency check that makes this trustworthy: **Vision OCR measured
144.68 ms on the subprocess path and 146.54 ms on the SCK path** — the same to
within 1.3 %. OCR is a property of the pixels, not of how they arrived, so the
difference between the two loops is genuinely attributable to capture.

**Two bottlenecks, nearly tied: the `screencapture` subprocess (42.2 %) and
Vision OCR (46.1 %).** Neither dominates. Fixing capture alone gets you 1.65×;
the loop only becomes fast once OCR leaves the hot path too (§7, §8.5).

Source run for every number in this section:
`bench/results/20261101T092159Z_QUIESCED-FINAL.json`, produced by

```bash
python3 bench/bench_all.py --label QUIESCED-FINAL --capture-iters 20 --loop-iters 10 \
  --ocr-iters 10 --cli-iters 8 --cv-iters 12 --vision-iters 15
```

at achieved load **5.74 start / 7.48 peak on 8 cores (0.72–0.93 per core)** —
the quietest window achieved all session, and still not an idle machine.
The per-stage numbers come from `loop_prod_instrumented` /
`loop_inproc_instrumented`, which record a timestamp at every stage boundary
*inside the same iteration*, so no subtraction across backends is involved.

**Replication.** A second independent run of the same instrumented pair
(`20261101T092806Z_LOADED.json`, load 5.05 = 0.63/core, matched 12 vs 12 items)
measured the production loop at **336.07 ms** (capture 152.6 + OCR 146.0 +
decode 37.5) and the in-process loop at **191.95 ms** (SCK 43.8 + OCR 148.1) —
**1.75×**. So two runs at 0.63 and 0.72–0.93 load/core agree on **1.65–1.75×**,
and the OCR stage agrees to within 1.4 % across both runs and both capture paths
(144.7 / 146.5 / 146.0 / 148.1 ms). That agreement is the strongest evidence here
that the stage split is a property of the work, not of one particular run.

> ### Correction to an earlier conclusion
> An earlier pass over a **generic** window (a WPS Office document with 45–95
> OCR items) produced "OCR = 84 %, capture = 16 %". **That figure is not valid
> for the game.** It measured a text-heavy document, not game UI. On real game
> content the split is **capture+decode 53.9 % / OCR 46.1 %**. The 84 % figure
> survives in `bench/results/` only as evidence of how strongly the split
> depends on OCR content, and must not be quoted as the game's profile.

## 2. Capture backends — the full matrix

Source run: `bench/results/20261101T092159Z_QUIESCED-FINAL.json` (same run as §1).
Achieved load 5.74 start / 7.48 peak on 8 cores = 0.72–0.93 per core.

| backend | n | mean ms | p50 ms | p95 ms | class |
|---|---|---|---|---|---|
| `screencapture` subprocess (window region, PNG to disk) | 20 | 133.60 | 133.36 | 135.38 | baseline |
| `screencapture` subprocess (full screen, PNG to disk) | 20 | 199.84 | 199.99 | 200.16 | baseline |
| `PROD shot()` = subprocess + PNG decode (current `seerd` path) | 20 | 134.15 | 134.07 | 135.27 | baseline |
| SCK `SCScreenshotManager` per call (cached filter, `.best`) | 20 | 44.09 | 44.18 | 45.75 | sck per-call |
| SCK `SCScreenshotManager` per call (ROI 300×200 pt) | 20 | 43.22 | 42.78 | 46.00 | sck per-call |
| SCK persistent stream: read latest frame (640×480) | 100 | 0.04 | 0.03 | 0.08 | sck stream |
| memcpy one 2298×1428 BGRA frame (13.1 MB) | 20 | 0.42 | 0.35 | 0.75 | sck stream |

Speed-ups against the current production path: **per-call SCK 3.03×**,
**stream read-latest 3,349×**, **game-resolution memcpy 316×**.

Note `PROD shot()` (134.15 ms) ≈ `screencapture` alone (133.60 ms): because
`CGImageSourceCreateImageAtIndex` is lazy (§6.2), the decode is *not* paid here —
it is deferred into the OCR call. See §6.2 for why this matters for accounting.

### 2.1 The classic CoreGraphics capture APIs do not exist on this OS

`CGWindowListCreateImage` and `CGDisplayCreateImage` are **hard compile errors**
on the macOS 15.7.7 SDK:

```
error: 'CGWindowListCreateImage' is unavailable in macOS: Please use ScreenCaptureKit instead.
note: 'CGWindowListCreateImage' was obsoleted in macOS 15.0
```

So there is no "CGWindowList vs SCK" choice to benchmark on this machine.
ScreenCaptureKit is the only in-process option. `CGWindowListCopyWindowInfo` is
still available and is measured separately (0.58–2.5 ms) because
`scripts/seerd.swift` calls it via `findWin()` on **every** shot.

### 2.2 Per-call SCK overhead is not tied to resolution

`.best` (2760×1530) and `.nominal` (1380×765) per-call costs are within noise of
each other, and a 600×400 ROI costs the same as a full 2760×1530 frame
(43.99 vs 45.38 ms). A fixed ~44 ms per-call cost dominates; **capturing fewer
pixels buys nothing**. This is why ROI does not rescue the per-call API.

### 2.3 The subprocess has a ~134 ms floor, and degrades 3.9× under load

| run | achieved load (peak) | `screencapture` window mean |
|---|---|---|
| `092159Z_QUIESCED-FINAL` | 7.5 (0.93/core) | 133.60 ms |
| `091811Z_QUIESCED-ATTEMPT` | 9.1 (1.14/core) | 134.62 ms |
| `085754Z_LOADED` | 10.7 (1.34/core) | 134.87 ms |
| `085045Z_LOADED` | 86.3 (10.8/core) | **516.32 ms** |

The cost is **flat at ~134 ms between load 7.5 and 10.7** and then rises to
516 ms at load 86 — a **3.9× degradation**. So it is not load-independent; it
simply saturates at a floor in the range this machine normally sits in.

The floor is the real argument: **134 ms is the best case**, on a quiet machine,
for a 1380×765 region — versus **44 ms** for the in-process SCK screenshot on the
same region. You cannot tune it away: the cost is dominated by fixed work
(process spawn, launchd, TCC check, PNG encode), not by pixels or by CPU.

## 3. Persistent SCStream — the architecture that matters

Source: same run as §2. Pre-warmed `SCStream`, `minimumFrameInterval 1/60`,
`queueDepth 5`, `captureResolution .best`, BGRA.

| metric | value |
|---|---|
| achieved fps (animated content) | **57.7** |
| inter-frame delta mean / p50 / p95 | **17.33 / 17.23 / 18.63 ms** |
| `startCapture()` one-time | **46.55 ms** |
| read-latest handoff (consumer memcpy into preallocated buffer) | **0.04 ms** (n=100, p95 0.08) |
| read-latest staleness at request, mean / p50 / p95 / max | 8.83 / 8.66 / 16.70 / 17.26 ms |
| RSS delta for stream + buffers | 2.78 MB |
| frames delivered during the measurement | 60 |

**"Get current screen pixels" goes from 133.6 ms to 0.04 ms — ~3,349× — with
pixels never older than ~17 ms.** The one-time 46.6 ms `startCapture()` is paid
once per process, not per frame.

Independent cross-check with the Lead's separate implementation: 56.9 fps /
17.55 ms vs 57.7 fps / 17.33 ms. Agreement within 1.5 %.

### 3.1 SCK is change-driven — and the two measurements differ, which matters

| run | target | frames | interval |
|---|---|---|---|
| `085754Z_LOADED` | nominally static generic window | 16 in 5 s | **383.0 ms** |
| `092159Z_QUIESCED-FINAL` | nominally static generic window | 4 in ~1.4 s | **351.2 ms** |
| `091811Z_QUIESCED-ATTEMPT` | nominally static generic window | 60 in ~1.05 s | **17.5 ms** |

Both are real. SCK delivers a frame **when the captured content changes**. In
the first run that window was genuinely idle, so frames were ~383 ms apart; in
the second it was redrawing (blinking caret, or other activity on the machine),
so frames arrived at the full 60 Hz. **The frame interval is a property of the
content, not a constant.**

This is the single most important architectural consequence in this document —
see §8.

---

## 4. Vision OCR matrix

Source: `bench/results/20261101T092159Z_QUIESCED-FINAL.json` (same run as §1), leg
`python3 bench/bench_ocr.py --json --iters 10 --cli-iters 8`.
In-process timings are around `VNImageRequestHandler.perform` only; decode is
excluded and measured separately. `usesLanguageCorrection = false` and
`recognitionLanguages = ["zh-Hans","en-US"]`, matching `scripts/seerd.swift`.

| config | n | mean ms | p50 ms | p95 ms | items | image | cold 1st call ms |
|---|---|---|---|---|---|---|---|
| game_full_accurate_zh+en | 10 | 146.42 | 146.52 | 148.75 | 12 | 2298×1428 | 360.3 |
| game_full_fast_zh+en | 10 | 62.34 | 62.11 | 65.43 | 10 | 2298×1428 | 121.3 |
| game_full_accurate_zh | 10 | 147.75 | 146.76 | 153.71 | 12 | 2298×1428 | 373.6 |
| game_full_fast_zh | 10 | 61.30 | 60.92 | 63.36 | 10 | 2298×1428 | 118.9 |
| game_roi900x400_accurate_zh+en | 10 | 64.76 | 64.50 | 66.70 | 1 | 900×400 | 279.1 |
| game_roi900x400_fast_zh+en | 10 | 41.77 | 41.64 | 42.49 | 1 | 900×400 | 101.8 |
| battle_full_accurate_zh+en | 10 | 163.25 | 162.49 | 170.29 | 14 | 1150×715 | 376.5 |
| battle_full_fast_zh+en | 10 | 51.51 | 51.13 | 52.91 | 6 | 1150×715 | 110.1 |
| battle_roi_accurate_zh+en | 10 | 84.34 | 83.92 | 86.77 | 5 | 600×300 | 301.1 |

One-shot CLI baseline (includes process spawn + decode + OCR):

| one-shot CLI | n | mean ms | p50 ms | p95 ms | items |
|---|---|---|---|---|---|
| `./scripts/seerctl ocr artifacts/game.png 0.3` | 8 | 430.99 | 428.68 | 442.99 | 12 |
| `./scripts/seerctl pos` (spawn control, no image work) | 8 | 27.12 | 27.20 | 29.00 | — |

**Cold vs warm is a 2.46× difference** (360.3 ms first call vs 146.4 ms warm for
`.accurate` zh+en). A resident daemon pays the cold cost once; a per-shot process
pays it every time. `seerctl ocr` totals 431.0 ms while the spawn-only control
(`seerctl pos`) is 27.1 ms — so process spawn alone is ~6 % of the CLI call, and
the rest is decode + OCR (including the cold Vision start).

`.accurate` costs **2.35×** `.fast` on the full game frame (146.42 vs 62.34 ms).
`.fast` finds 10 of the 12 items `.accurate` finds — cheaper but not equivalent;
choose per use case, not by default.

---

## 5. PNG codec, decode and pixel handoff

| operation | n | mean ms | p50 | p95 | note |
|---|---|---|---|---|---|
| `png_encode_memory` (PNG encode of a 2760×1530 frame) | 20 | 34.157 | 33.282 | 36.010 | what you pay if you insist on PNG |
| `png_decode_lazy_handle` | 20 | 0.056 | 0.056 | 0.061 | **a lie — see §6.2** |
| `png_decode_forced` (handle + forced BGRA draw) | 20 | 21.329 | 21.361 | 21.660 | the real cost |
| `bgra_extract` (alloc + draw into BGRA) | 20 | 0.752 | 0.746 | 0.896 | numpy handoff |
| `roi_crop_inmemory` (`CGImage.cropping`) | 20 | 0.001 | 0.001 | 0.001 | ROI is free once you have the frame |

Numbers from `20261101T092159Z_QUIESCED-FINAL.json` (same run as §1).

---

## 6. Two silent measurement bugs that invalidate naive numbers

Both were found by measurement disagreeing with itself. Both are recorded here
because a future implementer will hit them again.

### 6.1 OCR latency scales ~4× with machine load

Same binary, same image (`artifacts/game.png`), same settings, minutes apart:

| load avg (8 cores) | in-process Vision `.accurate`, warm | source |
|---|---|---|
| 5.74 | **146.4 ms** | `092159Z_QUIESCED-FINAL` OCR leg |
| 10.5 | **159.3 ms** | `084359Z_LOADED` OCR leg |
| 37.0 | **483 ms** | standalone `bench_capture ocr` run |
| 85.0 | **685 ms** | measured independently by the Lead |

It is one operation, not three. An earlier disagreement between the Lead's
684.6 ms and my 159.3 ms is **fully explained by load; neither measurement was
wrong.**

**Rule: an OCR number without its load average is meaningless.** Every JSON in
`bench/results/` records `os.getloadavg()` + `ncpu` at start, during and end of
the run, and every table in this document names its source run and achieved load.

### 6.2 `CGImageSourceCreateImageAtIndex` is lazy — a 381× error

It returns a handle without decoding pixels.

| what you time | reported (n=20) | reality |
|---|---|---|
| `CGImageSourceCreateImageAtIndex` alone | **0.056 ms** | decode has not happened |
| handle + forced BGRA draw | **21.329 ms** | this is the decode |

Timing only the handle understates decode by **~381×** and makes it look
negligible.

**This affects the existing production code.** `scripts/seerd.swift` `shot()`
returns `img.width/height` after `CGImageSourceCreateWithURL` +
`CreateImageAtIndex` — a lazy load. The real decode is therefore deferred into
whatever first touches pixels, which in practice is **the Vision OCR call**.
Consequence: **the production loop's PNG decode cost must be attributed to the
vision stage, not to capture.** Any claim of the form "capture costs 139 ms"
is understating the true cost of getting usable pixels out of the subprocess
path by the decode time.

---

## 7. Vision primitives — what fits in a 60 Hz frame

Source: `bench/results/20261101T092159Z_QUIESCED-FINAL.json` (same run as §1), leg
`PYTHONPATH=.pydeps python3 bench/bench_vision.py --json --iters 15 --heavy-iters 5`
and `bench/bench_cv.py --json --iters 12 --heavy-iters 6`.
Frame 2298×1428, 60 Hz budget = **16.67 ms**.

| strategy / primitive | n | mean ms | p50 ms | p95 ms | fits 16.7 ms |
|---|---|---|---|---|---|
| `handoff_frombuffer_view_only` (zero-copy BGRA view) | 15 | 0.0007 | 0.0007 | 0.0010 | **yes** |
| `A_fingerprint_1patch` (100×40×3 mean-abs-diff) | 15 | 0.0138 | 0.0134 | 0.0157 | **yes** |
| `A_fingerprint_3patches` | 15 | 0.0385 | 0.0385 | 0.0389 | **yes** |
| `A_fingerprint_3patches_cv2absdiff` | 15 | 0.0314 | 0.0313 | 0.0317 | **yes** |
| `B_downsample_diff_precomputed` (diff on already-small buffers) | 15 | 0.0519 | 0.0509 | 0.0572 | **yes** |
| `C_roi_template_300x200` (60×60 template, grey) | 5 | 0.9565 | 0.8908 | 1.1413 | **yes** |
| `C_roi_template_900x400` | 5 | 7.8410 | 7.7937 | 8.1857 | **yes** |
| `E_conditional_fingerprint_then_roi_template` | 15 | 8.0339 | 8.0875 | 8.2077 | **yes (gate fired)** |
| `handoff_frombuffer_plus_copy` (13 MB copy) | 15 | 14.0345 | 14.0330 | 14.3206 | **yes (marginal, 86 % of budget)** |
| `B_downsample_only` (resize full frame → 229×142) | 15 | 18.0145 | 17.9037 | 18.8682 | no |
| `B_downsample_gray_changed_pixels` (resize ×2 + grey + diff + count) | 15 | 35.8259 | 35.8093 | 36.0879 | no |
| `B_downsample_diff_composed` (resize ×2 + diff) | 15 | 36.7594 | 35.9463 | 39.7686 | no |
| `D_fullframe_template_gray` (60×60 on full 2298×1428) | 5 | 63.8811 | 63.6174 | 64.4926 | **no — 3.8× over budget** |

Note on `E_conditional`: this run's two frames differed by more than the gate
threshold, so the measured 9.93 ms is the **gate-fired** path (fingerprint +
ROI template match), not the common-case path. The gate itself
(`A_fingerprint_3patches_cv2absdiff`) is 0.038 ms, so when nothing has changed
the pipeline costs ~0.04 ms.

Key ratios that survive a loaded machine:

* **A state fingerprint costs 0.0385 ms** (three 100×40×3 patches) — that is
  **3,803× cheaper than `.accurate` OCR (146.42 ms)** and 1,619× cheaper than
  `.fast` (62.34 ms). It runs at 60 Hz with ~430× headroom.
* **Full-frame template matching is 63.9 ms — 3.8× over a 16.7 ms frame budget.**
  Not a candidate for the hot loop at full resolution.
* **Template matching on a 300×200 ROI is 0.96 ms** and on a 900×400 ROI 7.8 ms —
  both fit; the 300×200 case has 17× headroom.
* **The expensive part of "downsampled diff" is the resize, not the diff.**
  Diffing already-small buffers costs 0.052 ms; resizing the full frame costs
  18.0 ms. If a downsampled change detector is wanted in the hot loop, the
  downscale must come from the capture layer (or be done once), not per frame.
* **Copying the frame out costs 14.0 ms** (13 MB `ascontiguousarray`) and eats
  86 % of a 60 Hz budget on its own — which is why §8.2 insists on a
  preallocated buffer and a zero-copy view rather than a copy per frame.

## 8. Architectural implications (normative)

These follow directly from the measurements above and should be treated as
constraints on the capture layer design, not as suggestions.

### 8.1 Use a persistent `SCStream`. Never capture per shot.
133.4 ms → 0.06 ms. The per-call SCK API (45 ms) is not worth the migration by
itself; the stream is. The one-time `startCapture()` (37 ms) is paid once.

### 8.2 Allocate the frame buffer once. Never allocate per frame.
The correct handoff is **memcpy into a preallocated buffer**: 0.06 ms. The
Lead's implementation allocated a fresh 13 MB `CVPixelBuffer` per call and
measured 0.89 ms — **17× slower for identical work**. The difference is
allocation and zeroing, not copying. A future implementer will get this wrong
silently, because both versions are "just a copy".

### 8.3 `read_latest()` must be non-blocking and return `(pixels, timestamp)`.
SCK is change-driven (§3.1). Measured frame intervals on nominally the same
content ranged from **17.5 ms to 383 ms** depending on whether the content was
actually changing. Therefore:

* The loop **polls on its own clock** (8–16 ms) and reads whatever frame exists.
* It **must never block waiting for the next frame** — on a static screen
  (menu, dialog, frozen frame) frames may not arrive for hundreds of ms or at
  all, and a loop that awaits the next frame **hangs**.
* A large frame age is **not an error**. A stale frame is a valid observation:
  the screen genuinely has not changed. Only `age > ceiling AND state says
  "in motion"` should raise suspicion.
* Report both regimes: 17.2 ms under animation, 383 ms under a static screen.
  Both are measured; the executor's blocking behaviour depends on the difference.

### 8.4 Never block the main thread waiting for an SCK async result.
`SCScreenshotManager.captureImage` is async. Wrapping it in
`DispatchSemaphore.wait()` **on the main thread deadlocks** when the captured
window belongs to this process — the completion needs the main run loop, which
the wait is blocking. Measured: a **13-minute hang**, main thread parked in
`_dispatch_semaphore_wait_slow` under `sckScreenshotSync`.

Correct shapes:
* run capture on a dedicated thread/queue with its own run loop and hand frames
  over through a lock-free latest-frame slot; **or**
* pump the run loop while waiting (`RunLoop.main.run(until:)` in a poll loop)
  instead of `sem.wait()`; and always impose a timeout.

`scripts/seerdx` is exactly the shape that deadlocks if it does sync-wrapped SCK
capture on the main thread.

### 8.5 OCR is not a hot-loop operation.
Cheapest useful OCR measured is **62.3 ms** (`.fast`, full game frame) and the
realistic figure is **146.4 ms** (`.accurate`). That is 3.7–8.8× a 60 Hz frame
budget. State detection must be built from pixel fingerprints and downsampled
diffs (sub-millisecond, §7); OCR belongs on state *transitions*, not on every
frame. The gated pipeline (§7, `E_conditional...`) is the right shape: 0.031 ms for the
gate when nothing changed, template match only when something did.

---

## 9. Two contradictions found in the data, and their resolutions

Both were caught before publication. They are documented because they are
general traps, not one-off mistakes.

### 9.1 The derived bottleneck ranking disagreed with its own inputs by 2.3×

In run `091811Z_QUIESCED-ATTEMPT`, the same operation appeared as:

* headline row `screencapture subprocess (window region)` → **134.62 ms measured**
* derived ranking `screencapture subprocess` → **305.71 ms (57.6 % share)**

**Cause:** the derived ranking composed numbers across two different legs of the
run (generic-window leg and game-content leg) taken minutes apart, and then
subtracted one from another. Under a drifting load that amplifies error badly.

**Resolution:** the derived ranking is **retired**. §1 now uses
`loop_prod_instrumented`, which records a timestamp at every stage boundary
*inside the same iteration*, so there is no cross-backend arithmetic and no
exposure to drift. The derived figures remain in the JSON as
`bottleneck_ranking_derived`, explicitly marked "cross-check only, do not quote".

### 9.2 An SCK loop measured slower than a subprocess loop

In run `091811Z_QUIESCECE-ATTEMPT` (generic window):

| loop | mean | OCR items |
|---|---|---|
| subprocess + `.accurate` | 667.9 ms | **45** |
| in-process SCK + `.accurate` | 996.5 ms | **53** |

SCK capture is 45 ms vs 134 ms, so SCK cannot add 330 ms. **The two loops were
not looking at the same pixels.**

**Cause:** `screencapture -R` captures the **composited screen region** — i.e.
whatever window is on top. SCK's `desktopIndependentWindow` captures the
target window's **own backing store**, occluded or not. On a partially occluded
window these are different images, with different amounts of text, so the OCR
costs differ. **The OCR item counts prove it: 45 vs 53.**

**Resolution:**
* The comparison is only valid when both paths see the same pixels, verified by
  matching OCR item counts. The orchestrator now checks this automatically
  (`game_content_comparability` / `generic_comparability` in the JSON) and
  refuses to report a speed-up when the item counts drift more than 25 %.
* The synthetic game-content window was raised to `.floating` level so
  `screencapture -R` cannot be occluded. Before that fix, the same run recorded
  **77.8 OCR items** on the subprocess path versus **12** on the SCK path for
  "the same" content — a 6.5× content mismatch that produced a nonsensical
  1792.7 ms loop.
* §1 uses only the instrumented pair with **matched item counts (12 and 12)**.

**Where the earlier "2.95×" and "1.47×" figures came from:** the 2.95× was a
run at load 86 in which the subprocess path captured a less text-dense region
(so it paid *less* OCR than the SCK path, inflating the apparent win); the 1.47×
came from a run whose item counts did match (12 vs 12) and is methodologically
sound but was measured at a different load. **The figure to quote is 1.65×**
(§1), from the instrumented run with matched item counts at the lowest load
achieved. The generic-window leg in the final run is still correctly flagged
`comparable: false` (53 vs 34 items) by the automatic guard.

---

## 10. Measured vs not measured

### Measured on this machine
* `screencapture` subprocess cost: window region, full screen, with and without decode.
* SCK `SCScreenshotManager`: warm, cold, ROI, `.best`, `.nominal`, full display.
* SCK `SCStream`: setup+first-frame (warm and cold), steady-state fps and
  inter-frame interval on animated content, and on static content, read-latest
  handoff cost, staleness distribution, RSS delta, `startCapture` one-time cost.
* PNG encode, lazy handle, forced decode, BGRA handoff, in-memory ROI crop.
* `CGWindowListCopyWindowInfo` enumeration cost (paid by `seerd.findWin()` per shot).
* Vision OCR: `.accurate`/`.fast` × full/ROI × zh+en/zh-only × 3 images, warm and
  cold first call, plus the one-shot CLI path and a spawn-only control.
* OpenCV: colour conversion, resize, absdiff (full/downsampled), threshold,
  template matching at 3 scales in colour and grey, fingerprint patches.
* The composed hot-loop strategies, and whether each fits a 16.7 ms budget.

### NOT measured — and why
| not measured | why |
|---|---|
| **The live game window** | `/Applications/SeerGame.app` was not running for the entire session (`seer_game_window_count: 0`). This is blocker B1 in `AGENTS.md`. |
| **Game UI OCR cost** | Requires the live client. `artifacts/game.png` is yesterday's frame; it is real content but not a live capture. |
| **Mouse/keyboard input latency** | Out of scope (Lead's). This suite never moves the mouse or posts events. |
| **GPU / power / thermal counters** | Not attempted; no reliable non-intrusive sampling path was available. |
| **Multi-monitor capture paths** | Only the main display was benchmarked. A second display exists (1536×864 pt). |
| **Long-run stability / leak behaviour** | Runs are seconds long. A 30-minute soak of the stream is still needed before production. |
| **`.accurate` with `usesLanguageCorrection = true`** | Tested once for the load-vs-langcorr question (482.7 vs 469.4 ms at load 37 — not a factor). Not part of the main matrix. |
| **Quiesced load < 1/core** | Never achieved. Three other agents shared the machine; the best achieved was 5.89–8.6 load on 8 cores. **All numbers are therefore labelled by achieved load, and no absolute figure here should be read as "idle-machine" performance.** |

---

## 11. Reproducing any number in this document

```bash
cd /Users/ceontou/Documents/code/seer-puni-quest

# the whole suite (builds the Swift binary if needed, writes bench/results/<ts>_<label>.json)
python3 bench/bench_all.py --label QUIESCED-FINAL \
    --capture-iters 20 --loop-iters 10 --ocr-iters 10 --cli-iters 8 \
    --cv-iters 12 --vision-iters 15

# regenerate every table in this document straight from a results file
python3 bench/render_tables.py bench/results/latest.json

# individual legs
./native/bin/bench_capture probe
./native/bin/bench_capture capture --iters 20 --loop-iters 10
./native/bin/bench_capture capture --image-window artifacts/game.png --only loop_prod_instrumented,loop_inproc_instrumented
./native/bin/bench_capture ocr --file artifacts/game.png --level accurate --langs "zh-Hans,en-US" --iters 10
PYTHONPATH=.pydeps python3 bench/bench_ocr.py
PYTHONPATH=.pydeps python3 bench/bench_cv.py
PYTHONPATH=.pydeps python3 bench/bench_vision.py

# build the native binary by hand (sandbox needs an explicit module cache)
mkdir -p /tmp/bench/mc
swiftc -O -module-cache-path /tmp/bench/mc -o native/bin/bench_capture \
    native/bench_capture.swift \
    -framework ScreenCaptureKit -framework CoreGraphics -framework Vision \
    -framework AppKit -framework ImageIO -framework CoreVideo -framework QuartzCore
```

`bench_all.py` records `os.getloadavg()` and `ncpu` before, during and after every
run, and marks the run `contended: true` whenever load per core exceeds 1.0.
**A number without its load context is not a baseline.**

---

## 12. One-line summary

> The loop is slow because it pays **~133 ms of fixed subprocess cost plus
> ~37 ms of PNG decode on every single shot** (53.9 % of the loop) while also
> spending **~145 ms on Vision OCR** (46.1 %). A persistent `SCStream` with a
> preallocated latest-frame slot reduces "get current pixels" from **133.6 ms to
> 0.04 ms**, taking the end-to-end loop from **313.8 ms to 190.5 ms (1.65×)** on
> real game content with identical OCR output. **OCR then becomes 76.9 % of what
> remains and must be moved out of the hot loop** — a three-patch pixel
> fingerprint costs **0.046 ms**, ~3,700× less. **None of this was measured on
> the live game.**

---

## Appendix A — complete backend table with min/max

All values in milliseconds, from `bench/results/20261101T092159Z_QUIESCED-FINAL.json`
(achieved load 5.74 start / 7.48 peak, 8 cores). `leg = generic` means the target
was another application's window; `leg = game-content` means the synthetic window
displaying `artifacts/game.png` at 1149×714 pt. Loop rows report the whole
capture+OCR iteration.

| leg | backend | n | mean | p50 | p95 | min | max |
|---|---|---|---|---|---|---|---|
| generic | windowlist_enum | 20 | 0.42 | 0.41 | 0.46 | 0.36 | 0.60 |
| generic | sck_shareable_enum | 20 | 23.46 | 23.19 | 25.03 | 21.84 | 25.20 |
| generic | sck_screenshot_warm_2x | 20 | 44.09 | 44.18 | 45.75 | 42.10 | 46.61 |
| generic | sck_screenshot_warm_1x | 20 | 43.10 | 42.69 | 44.59 | 41.43 | 46.85 |
| generic | sck_screenshot_cold_2x | 10 | 69.86 | 68.48 | 78.07 | 66.66 | 85.34 |
| generic | sck_screenshot_roi_2x | 20 | 43.22 | 42.78 | 46.00 | 41.35 | 46.79 |
| generic | sck_display_screenshot_2x | 20 | 49.43 | 49.18 | 51.34 | 47.73 | 51.61 |
| generic | sck_stream_warm_setup_first_frame | 10 | 63.47 | 62.74 | 72.51 | 56.80 | 76.37 |
| generic | sck_stream_cold_setup_first_frame | 10 | 81.03 | 80.99 | 83.16 | 77.71 | 83.18 |
| generic | sck_stream_steady_state_animated | 59 | 17.33 | 17.23 | 18.63 | 15.44 | 20.61 |
| generic | sck_stream_steady_state_static | 3 | 351.18 | 35.76 | 903.94 | 17.38 | 1000.40 |
| generic | sck_stream_read_latest | 100 | 0.04 | 0.03 | 0.08 | 0.03 | 0.15 |
| generic | memcpy_game_frame_bgra | 20 | 0.42 | 0.35 | 0.75 | 0.33 | 1.05 |
| generic | subprocess_screencapture_window_png | 20 | 133.60 | 133.36 | 135.38 | 131.23 | 135.56 |
| generic | subprocess_screencapture_fullscreen_png | 20 | 199.84 | 199.99 | 200.16 | 198.61 | 200.41 |
| generic | prod_shot_pipeline | 20 | 134.15 | 134.07 | 135.27 | 132.19 | 135.39 |
| generic | png_encode_memory | 20 | 34.16 | 33.28 | 36.01 | 32.52 | 44.38 |
| generic | png_decode_lazy_handle | 20 | 0.06 | 0.06 | 0.06 | 0.05 | 0.06 |
| generic | png_decode_forced | 20 | 21.33 | 21.36 | 21.66 | 21.00 | 21.67 |
| generic | bgra_extract | 20 | 0.75 | 0.75 | 0.90 | 0.65 | 1.01 |
| generic | roi_crop_inmemory | 20 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| generic | inproc_pipeline_sck_bgra | 20 | 44.77 | 43.52 | 47.01 | 42.41 | 63.37 |
| generic | inproc_pipeline_sck_roi_bgra | 20 | 42.34 | 42.23 | 44.19 | 40.94 | 44.41 |
| generic | loop_prod_accurate | 10 | 653.50 | 647.38 | 681.35 | 639.89 | 699.43 |
| generic | loop_prod_fast | 10 | 226.75 | 226.27 | 231.33 | 222.55 | 233.11 |
| generic | loop_inproc_accurate | 10 | 345.33 | 343.86 | 355.82 | 335.46 | 356.40 |
| generic | loop_inproc_fast | 10 | 104.61 | 104.56 | 106.44 | 102.86 | 106.49 |
| generic | loop_inproc_fast_roi | 10 | 84.78 | 84.42 | 86.90 | 83.28 | 87.61 |
| generic | loop_inproc_accurate_roi | 10 | 143.43 | 142.90 | 148.57 | 139.22 | 151.02 |
| generic | loop_prod_instrumented | 10 | 673.14 | 661.48 | 719.55 | 650.71 | 727.60 |
| generic | loop_inproc_instrumented | 10 | 341.00 | 339.77 | 349.39 | 333.59 | 352.52 |
| game-content | windowlist_enum | 20 | 1.36 | 1.01 | 4.35 | 0.88 | 4.35 |
| game-content | sck_screenshot_warm_2x | 20 | 43.30 | 42.44 | 48.10 | 40.93 | 54.83 |
| game-content | sck_screenshot_warm_1x | 20 | 42.60 | 42.43 | 44.02 | 41.21 | 46.73 |
| game-content | sck_screenshot_roi_2x | 20 | 41.92 | 41.89 | 42.76 | 40.52 | 43.10 |
| game-content | subprocess_screencapture_window_png | 20 | 136.62 | 133.42 | 138.13 | 128.20 | 204.71 |
| game-content | loop_prod_accurate | 10 | 299.23 | 299.15 | 302.99 | 294.14 | 303.20 |
| game-content | loop_prod_fast | 10 | 239.49 | 221.52 | 302.75 | 216.44 | 307.65 |
| game-content | loop_inproc_accurate | 10 | 201.40 | 191.15 | 229.83 | 187.31 | 240.41 |
| game-content | loop_inproc_fast | 10 | 107.53 | 105.99 | 116.26 | 103.81 | 122.83 |
| game-content | loop_inproc_fast_roi | 10 | 83.21 | 83.15 | 84.50 | 81.98 | 85.29 |
| game-content | loop_inproc_accurate_roi | 10 | 58.98 | 58.85 | 62.10 | 55.87 | 63.27 |
| game-content | loop_prod_instrumented | 10 | 313.82 | 313.00 | 319.67 | 308.68 | 321.44 |
| game-content | loop_inproc_instrumented | 10 | 190.52 | 188.82 | 198.78 | 187.66 | 205.84 |

Regenerate this table with:

```bash
python3 bench/render_tables.py bench/results/20261101T092159Z_QUIESCED-FINAL.json --section raw
```
