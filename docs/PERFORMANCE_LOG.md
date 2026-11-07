# PERFORMANCE_LOG.md — every benchmark run, with its achieved load

One entry per executed run. **A latency number without its load context is not
reproducible**, so every entry records the achieved load average and core count
from the run's own JSON (`load_log` / `load_summary`), not from memory.

Machine: Apple M2, **8 cores**, 24 GB, macOS 15.7.7 (24G720), arm64.
Contention rule used throughout: `load_per_core_1m > 1.0` ⇒ marked `contended`,
and any absolute millisecond figure from that run is **provisional** — prefer
ratios taken within the same run.

Legend: **LOADED** = machine busy with other agents/builds. **QUIESCED-ATTEMPT**
= other agents asked to stop, but load never reached < 1/core; the achieved
figure is stated, and the label is *not* a claim that the machine was quiet.

---

## 2026-11-01

### Run 0 — SDK probe (no benchmark)

Command:
```bash
swiftc -O -module-cache-path /tmp/bench/mc -o native/bin/bench_capture native/bench_capture.swift \
  -framework ScreenCaptureKit -framework CoreGraphics -framework Vision -framework AppKit \
  -framework ImageIO -framework CoreVideo -framework QuartzCore
```
**Result: the classic capture APIs do not exist on this SDK.** First compile
attempt failed with
`error: 'CGWindowListCreateImage' is unavailable in macOS: Please use
ScreenCaptureKit instead` / `obsoleted in macOS 15.0`. `CGDisplayCreateImage`
fails the same way. ScreenCaptureKit is the only in-process option. Also
confirmed: the sandbox rejects the default module cache
(`unable to open output file '.../ModuleCache/...pcm': Operation not permitted`),
so `-module-cache-path /tmp/bench/mc` is mandatory.

Load: not recorded (compile only).

---

### Run 1 — `20261101T084359Z_LOADED.json` (smoke, `--quick`)

```bash
python3 bench/bench_all.py --quick
```
* Label: LOADED · n: 5 capture / 3 loop / 3 OCR / 4 CV / 5 vision
* **Load: 10.5 peak, 8 cores (1.31/core) — contended**
* Purpose: first end-to-end validation of the harness.
* Notable: `PROD shot()` 133.6 ms, SCK per-call 61.4 ms, stream read-latest 0.1 ms,
  fps 57.6, inter-frame 17.37 ms, static-screen interval 396.5 ms.
* Superseded by Run 4 (larger n). Kept for the static-window observation.

---

### Run 2 — `20261101T085045Z_LOADED.json`

```bash
python3 bench/bench_all.py --quick
```
* Label: LOADED · **Load: 86.3 peak, 8 cores (10.8/core) — severely contended**
* Purpose: adding the real-game-content synthetic window leg.
* **This run is not usable as a baseline.** Load 86 on 8 cores inflates
  everything unpredictably. It is retained only as evidence of the
  load-sensitivity finding (see Run 4 / §6.1 of the baseline).
* Notable: capture-path speed-up measured 2.95× here — **superseded and
  retracted**, see baseline §9.2 (OCR item counts did not match between paths).

---

### Run 3 — `20261101T085322Z_LOADED.json`

```bash
python3 bench/bench_all.py --quick
```
* Label: LOADED · **Load: 46.0 peak, 8 cores (5.75/core) — contended**
* Purpose: verifying the leg-scoped bottleneck ranking after the first
  decomposition bug. Again not a baseline.

---

### Run 4 — `20261101T085754Z_LOADED.json` (full n)

```bash
python3 bench/bench_all.py --label LOADED --capture-iters 20 --loop-iters 10 \
  --ocr-iters 10 --cli-iters 8 --cv-iters 12 --vision-iters 15
```
* Label: LOADED · **Load: 10.7 peak, 8 cores (1.34/core) — contended**
* Purpose: first full-sample run. n=20 for capture micro, n=10 for loops.
* Notable: `screencapture` window 134.9 ms · SCK per-call 57.5 ms · read-latest 0.1 ms
  (n=100) · fps 57.2 · inter-frame 17.47/17.30/19.01 ms · startCapture 42.7 ms ·
  static-screen 383.0 ms (16 frames) · RSS delta 2.5 MB ·
  loops on game content: PROD 410.7 → SCK 278.8 ms (1.47×, item counts 12 vs 12 — comparable).
* **This is the first defensible run.** It contributes the load 10.7 point to the
  load-response comparison in baseline §2.3.

---

### Run 5 — `20261101T091231Z_QUIESCED-ATTEMPT.json` — **FAILED (deadlock)**

```bash
python3 bench/bench_all.py --label QUIESCED-ATTEMPT --capture-iters 20 \
  --loop-iters 10 --ocr-iters 10 --cli-iters 8 --cv-iters 12 --vision-iters 15
```
* Label: QUIESCED-ATTEMPT · Load: 10.4 peak · **capture leg `rc = -15` (SIGTERM)**
* **No usable data.** The Swift capture leg deadlocked for 13 minutes with the
  main thread parked in `_dispatch_semaphore_wait_slow` under
  `sckScreenshotSync` — a `DispatchSemaphore.wait()` on the main thread waiting
  for an SCK async completion that needs the main run loop serviced. The Lead
  had to SIGTERM, then SIGKILL, the child.
* Root cause and fix are recorded in baseline §8.4 and §9.2. The orchestrator
  also crashed at the last step here (`bench/stats.py:130 IndexError`,
  six headers vs seven cells) — that crash destroyed an otherwise-complete
  run's console output. Both defects were fixed before Run 7:
  * `waitPumping()` — poll the semaphore with a zero timeout and pump the run
    loop between polls, with a hard timeout, replacing all four blocking waits.
  * `print_table()` now pads/truncates rows defensively, and the results JSON is
    written to disk **before** any report rendering.

---

### Run 6 — `20261101T091811Z_QUIESCED-ATTEMPT.json` (full n, completed)

```bash
python3 bench/bench_all.py --label QUIESCED-ATTEMPT --capture-iters 20 \
  --loop-iters 10 --ocr-iters 10 --cli-iters 8 --cv-iters 12 --vision-iters 15
```
* Label: QUIESCED-ATTEMPT · **Load: 10.4 start → 8.6 end, 8 cores (1.07/core) —
  contended** (other agents were asked to stop; the tail of the earlier peak
  remained, so this is *not* an idle-machine run)
* **Status: completed, exit 0.** Best complete run before the final one.
* Notable: `screencapture` window 134.6 ms · full screen 210.0 ms · `PROD shot()`
  133.4 ms · SCK per-call 45.4 ms · SCK ROI 44.0 ms · read-latest 0.06 ms (n=100) ·
  game-res memcpy 0.52 ms · fps 58.1 · inter-frame 17.23/17.12/18.71 ms ·
  startCapture 37.0 ms · read-latest staleness 10.06/9.59/21.51/28.56 ms ·
  RSS delta 2.2 MB.
* **Two contradictions were found in this run's data** (a derived ranking that
  disagreed with its own inputs by 2.3×, and an SCK loop measuring slower than a
  subprocess loop). Both are analysed and resolved in baseline §9; the second was
  caused by the two paths capturing *different pixels* (45 vs 53 OCR items) on an
  occluded window.
* OCR leg (exact, n=10): `.accurate` full 171.56 ms / `.fast` full 68.09 ms /
  `.accurate` ROI 900×400 75.45 ms / `.fast` ROI 49.93 ms; cold first call
  397.9 ms vs 171.6 ms warm (2.3×). One-shot CLI `seerctl ocr` 807.05 ms;
  spawn-only control `seerctl pos` 34.40 ms.

---

### Run 7 — `20261101T092549Z_QUIESCED-FINAL.json` (final, full n)

```bash
python3 bench/bench_all.py --label QUIESCED-FINAL --capture-iters 20 \
  --loop-iters 10 --ocr-iters 10 --cli-iters 8 --cv-iters 12 --vision-iters 15
```
* Label: QUIESCED-FINAL · **Load: 5.74 start → 7.48 peak, 8 cores
  (0.72–0.93 per core) — the quietest window achieved all session, still above
  idle.**
* Status: **completed, exit 0.** This is the source run for every published table
  in `docs/PERFORMANCE_BASELINE.md`.
* Purpose: final run after both deadlock/formatting fixes and after adding
  `loop_prod_instrumented` / `loop_inproc_instrumented`, which time capture /
  decode / OCR **inside each iteration** so the stage split needs no
  cross-backend subtraction.
* Instrumented production loop (n=10, 12 OCR items, **total 313.82 ms**):
  `vision_ocr_accurate` 144.68 ms (46.1 %) · `capture_subprocess` 132.28 ms
  (42.2 %) · `png_decode_forced` 36.86 ms (11.7 %).
* Instrumented in-process loop (n=10, 12 OCR items, **total 190.52 ms**):
  `vision_ocr_accurate` 146.54 ms (76.9 %) · `sck_capture` 43.98 ms (23.1 %).
* **Capture-path speed-up 1.65×** (313.82 → 190.52 ms) with matched OCR output
  (12 vs 12 items, relative drift 0.0).
* Comparability guard worked: game-content pair `comparable: true` (12 vs 12);
  generic-window pair `comparable: false` (53 vs 34 items — occluded window, the
  two capture paths saw different pixels). This is the §9.2 defect being caught
  automatically instead of silently published.
* Capture micro (n=20): subprocess window 133.60 ms · full screen 199.84 ms ·
  `PROD shot()` 134.15 ms · SCK per-call 44.09 ms · SCK ROI 43.22 ms ·
  read-latest 0.04 ms (n=100) · game-res memcpy 0.42 ms.
* Stream: 57.7 fps · inter-frame 17.33/17.23/18.63 ms · `startCapture` 46.55 ms ·
  staleness 8.83/8.66/16.70/17.26 ms · RSS delta 2.78 MB · static-screen
  351.2 ms (4 frames).
* OCR (n=10): `.accurate` full 146.42 ms · `.fast` full 62.34 ms ·
  `.accurate` ROI 64.76 ms · `.fast` ROI 41.77 ms · cold first call 360.3 ms.
  CLI `seerctl ocr` 430.99 ms · spawn-only control `seerctl pos` 27.12 ms.
* Vision (n=15/5): fingerprint 3 patches 0.0385 ms · ROI 300×200 template 0.9565 ms ·
  ROI 900×400 7.8410 ms · full-frame template 63.8811 ms (3.8× over budget) ·
  13 MB frame copy 14.0345 ms.

---

## Method notes carried across all runs

1. **Nothing was measured on the live game.** `/Applications/SeerGame.app` was
   not running in any run (`seer_game_window_count: 0`). Capture-cost results use
   a generic on-screen window; loop/OCR results use a synthetic borderless
   non-activating window displaying the real `artifacts/game.png` at the real
   game geometry (1149×714 pt @ (300,120) → 2298×1428 px). Both are labelled in
   every JSON record.
2. **No input was ever synthesised.** The suite never moves the mouse and never
   posts clicks or keys. Window level was raised to `.floating` for the synthetic
   window so `screencapture -R` could not be occluded — that is a window-level
   change, not an input event, and it takes no focus.
3. **Load is recorded three times per run** (`start` / mid-legs / `end`) and
   summarised as `load_summary` in each JSON.
4. **Ratios over absolutes.** On a contended machine, absolute milliseconds move
   by 4× (see baseline §6.1); ratios measured within one run are stable.
5. **Two silent measurement bugs were found and are documented** (baseline §6):
   OCR latency scales ~4× with load, and `CGImageSourceCreateImageAtIndex` is
   lazy so timing it alone understates decode by ~150×.

---

### Run 8 — `20261101T092806Z_LOADED.json` (acceptance run, no arguments)

```bash
python3 bench/bench_all.py          # exactly the documented entry point, default flags
```
* Label: LOADED · **Load: 5.05 start / 5.05 peak, 8 cores (0.63 per core) — the
  lowest load of any run.** Label is `LOADED` because the default tag was used;
  in practice this was the quietest window of the session.
* **Status: completed, exit 0.** This is the acceptance check for R1-A: the
  documented command runs end to end with no arguments and exits 0.
* **Independent replication of Run 7's headline result**, with matched OCR
  output (12 vs 12 items):
  * production loop **336.07 ms** = `capture_subprocess` 152.6 +
    `vision_ocr_accurate` 146.0 + `png_decode_forced` 37.5
  * in-process loop **191.95 ms** = `sck_capture` 43.8 + `vision_ocr_accurate` 148.1
  * **speed-up 1.75×**
* Run 7 measured **1.65×** at load 0.72–0.93/core. Two independent runs, both
  with matched content, agree on **1.65–1.75×**. The OCR stage agrees to within
  1.4 % across both runs and both capture paths (144.7 / 146.5 / 146.0 / 148.1 ms),
  which is the strongest evidence in this log that the stage split is real and
  not an artefact of one run.

---

## 2026-11-02 — Round 4: REAL GAME loop (live client, first time)

Everything above this line was measured **without the game running**: generic
windows, synthetic windows showing `artifacts/game.png`, or PNG fixtures. This
entry is the first one measured against the **actual game client**.

* Environment: `xin计划 1.0.8.9`, window **1200×800 pt @ (225,53) → 2400×1600 px**,
  scale 2.0, server 1号啦公星, character `hello`.
* Machine: Apple M2, 8 cores, macOS 15.7.7. Load during these runs ~2.9–4.1
  (`vm.loadavg` sampled around each run; recorded in the JSON where relevant).
* Command: `PYTHONPATH=.pydeps python3 tools/battle_run.py --battles 3`
* Raw data: `bench/results/round4_3battles_*.json`, `round4_turnfix_*.json`,
  `daemon_*.json`.

### Capture, on the real game window (read-only probe)

| path | mean | p50 | p95 | n |
|---|---|---|---|---|
| `SubprocessScreencapture.grab()` (old) | **164.44 ms** | 160.04 | 246.70 | 20 |
| engine `latest()` (warm stream, all polls) | **4.33 ms** | 2.32 | 30.34 | 40 |
| └ static screen, decode cache hit | **2.27 ms** | 2.31 | 2.42 | 37 |
| └ a frame that actually changed (real decode) | 29.75 ms | 30.34 | 31.06 | 3 |
| `grab(ROI 400×300)` (daemon crops natively) | 2.32 ms | 2.35 | 2.40 | 40 |
| warm session start (one-off) | 122.9 ms | — | — | 1 |

**Engine-level "what is on screen": 164.44 ms → 4.33 ms = 38×.**

### The battle loop, 3 consecutive battles (all won)

Per-stage, n = number of occurrences across 3 battles:

| stage | n | mean | p50 | p95 |
|---|---|---|---|---|
| `capture` (warm `latest()`) | 184 | 18.95 ms | 26.62 | 40.91 |
| `detect` (4 tight-ROI template matches) | 184 | 8.21 ms | 7.78 | 9.88 |
| `action` (CGEvent click submit) | 172 | 34.65 ms | 34.52 | 36.19 |
| `wait` (game's own turn animation) | 157 | **362.73 ms** | 364.91 | 382.48 |
| `ocr` (escalation path only) | 4 | 1177.97 ms | 1147.41 | 1335.89 |

**Per-poll "look and decide" = 27.2 ms** (capture + detect).

### After the turn-pacing fix (one click per turn)

The first run clicked ~57× per battle because it re-evaluated 0.35 s after each
click while the game was still animating. Replaced with a two-phase wait
(combat-log pixel change → screen settle):

| metric | before | after |
|---|---|---|
| clicks per battle | ~57 | **~7–17** |
| `wait` mean | 362.73 ms | **2961 ms** |
| per-poll look | 27.2 ms | **35.2 ms** (capture 27.1 + detect 8.2) |
| battle duration | ~46 s | ~45 s |

**The `wait` growing is the fix working.** A turn genuinely takes ~3 s (the
game's animation) and a battle is ~13 turns. The engine's own cost is 35 ms out
of ~3000 ms per turn — **about 1.2 %**. The vision loop is no longer the
bottleneck; the game's animation is, and we now follow it instead of racing it.

### Pause / resume / stop (real client)

* Pause via control file `/tmp/seerauto.pause`: **21.3 s with zero clicks**.
* Resume: re-synchronised state and finished the battle correctly.
* SIGINT: `stopped: interrupted`, timing summary printed from the `finally`
  block, process exited cleanly, game kept running.

### Two measurement traps found on the real client

1. **`shot` returns a near-blank ~6 KB PNG while a warm stream is active.**
   OCR of it silently returned zero items. The escalation path must export the
   warm frame to PNG instead of calling `shot`.
2. **Two capture daemons against the same window do not coexist.** A second
   `seerdx` trying to take a screenshot while another holds the stream failed
   outright. Reuse one session when measuring.

### What this entry does NOT cover

* Nothing was measured on a **second** display, a moved/resized window, or after
  a client restart (the stage offset moves between sessions — see
  `docs/TEMPLATE_CANDIDATES.md`).
* `wait` is a 3 s settle timeout, not a detected "it is my turn" event; roughly
  1 s/turn may still be recoverable there.
* No loss was ever observed (8 wins, 0 losses), so nothing here describes a
  losing battle.

## 2026-11-07 分支整合（实验继续暂停）

以1106 dual-mode为基准合并1105控制分支及独立选服修复，随后合入main。冲突决策与离线验证见 `docs/BRANCH_MERGE_20261107.md`。未进行性能测量或真机验收，A/B/C仍未验收。等待用户明确开始。
