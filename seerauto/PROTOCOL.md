# seerauto daemon protocol (JSON lines over stdin/stdout)

This is the contract between `seerauto/capture.py` and a native helper daemon.
The daemon owns the platform capture path; Python never cares how a frame was
produced. On macOS 15 the only viable capture API is **ScreenCaptureKit**:
`CGDisplayCreateImage` and `CGWindowListCreateImage` are obsoleted in macOS 15
(verified by compile probe, see `docs/PROJECT_AUDIT.md` §2), so a
CGWindowListCreateImage-shaped daemon cannot exist here.

The reference implementation is being benchmarked separately; this document is
the interface it must satisfy. If the daemon is absent, `DaemonCapture` raises
`CaptureUnavailable` and the engine falls back to
`SubprocessScreencapture` (works today, ~139 ms/grab).

## Transport

One JSON object per line in each direction. Requests:

```json
{"op": "capture.latest"}
```

Replies are one JSON object per line. Errors are not exceptions:

```json
{"error": "no window"}
{"error": "permission denied"}
```

Python maps `error` strings containing `window` / `not found` to
`WindowNotFound`, `permission` / `denied` to `CapturePermissionDenied`,
anything else to `DaemonError`. The daemon may write freely to stderr.

## Timing rules (important)

ScreenCaptureKit is **change-driven, not clock-driven**: a static window may
emit a frame only every ~378 ms (measured), while an animating window emits at
~17 ms. Therefore:

* `capture.latest` must return **immediately** with the newest frame the daemon
  already holds. It must **never** block until new content arrives.
* a repeated `seq` (stale frame) is a **valid** reply, not an error;
* each frame carries `ts_ns` (monotonic clock) and `seq` (monotonic counter) so
  the executor can decide from `(frame, age)`;
* frames should be delivered without a per-frame allocation (allocate the
  buffer once, memcpy into it). Per-frame allocation roughly doubles the
  handoff cost (measured 0.89 ms → 0.52 ms when preallocated).

## Operations

| op | request fields | reply |
|---|---|---|
| `ping` | — | `{"ok": true}` |
| `caps` | — | `{"ops": ["capture.start", "capture.latest", ...]}` |
| `win` | — | `{"id": int, "x": pt, "y": pt, "w": pt, "h": pt}` or `{"error": "no window"}` |
| `pin` | `x, y, w, h` (logical points) | new window bounds like `win` |
| `activate` | — | `{"ok": true}` |
| `capture.start` | `rect` optional `[x, y, w, h]` in game_px | `{"ok": true, "seq": 0}` |
| `capture.latest` | — | `{"seq": int, "ts_ns": int, "path": str \| "png_b64": str, "w": int, "h": int, "rect": [x,y,w,h]?, "fmt"?: str}` |
| `capture.roi` | `rect` `[x, y, w, h]` in game_px | like `capture.latest`, cropped from the newest frame |
| `capture.stop` | — | `{"ok": true}` |
| `shot` | `path`, `rect` optional | `{"path": str, "w": int, "h": int}` (one-shot, no warm session) |
| `ocr` | `path`, `min` | `{"items": [{"text", "conf", "x", "y", "w", "h"}]}` Vision-normalised boxes |
| `click` | `x, y` (global points), `n` | `{"ok": true}` |
| `click.guarded` | Window-local `x, y` (points), captured `id, pid`, expected `bounds: [x, y, w, h]` (points) | `{"ok": true}` after one HID click, or `{"error": "guarded click refused"}` without input |
| `move` | `x, y` (global points) | `{"ok": true}` |
| `key` | `code` (macOS virtual key) | `{"ok": true}` |
| `type` | `text` | `{"ok": true}` |
| `quit` | — | `{"bye": true}` then exit |

### Extension: `fmt: "bgra"` (implemented by `native/seerdx.swift`)

`capture.latest` / `capture.roi` may return a **raw frame** instead of an encoded
one:

```json
{"fmt": "bgra", "path": "/tmp/seerdx_frame.bgra", "w": 2400, "h": 1600,
 "stride": 9600, "seq": 42, "ts_ns": 123456789}
```

Why: PNG encode+decode costs tens of milliseconds per frame, which cancels out
the benefit of a warm capture session. A raw frame is one file read plus one
channel slice. Measured on the live game window: a real decode costs ~30 ms
while a **cache hit costs 0 ms**, so this only matters on frames that actually
changed.

Client rules:

* `stride` may exceed `w * 4` (row padding) — read row by row, do not assume
  packed rows;
* alpha is dropped by the client (`bgra[:, :, :3]` is already BGR order);
* a client that does not understand `fmt` simply ignores it and falls back to
  the `shot`/PNG path, so the extension never breaks an old client;
* the client caches by `(seq, w, h, stride, rect)`: a repeated key is
  byte-identical, so it must **not** re-read the file. This is what makes
  polling a static screen nearly free.

### Window selection

The daemon targets the game window by owner-name substring. `--target <name>`
overrides it, which is how the daemon is tested against an ordinary window when
the game is not running.

Coordinate note: `game_px` is window-local device pixels of the 2x reference
capture (`docs/state.md` space, 2298x1428). Global input points are
`window_origin_pt + game_px / 2.0`; the daemon receives already-converted global
points from `actions.py`, so it never has to know about `game_px`.

## Feature discovery / fallback

1. `DaemonCapture` calls `caps` (ignored when it errors) and then
   `capture.start`.
2. If `capture.start` fails (unknown op / no permission), the backend degrades
   to one-shot `shot` and marks itself `warm = False`. The engine keeps working,
   just slower.
3. If `capture.roi` is not advertised, Python crops the newest full frame in
   memory instead (microseconds) - still one round trip per tick.

## Not in scope for the daemon

`win` also returns the owning PID and whether the exact game window is
frontmost. The game's 52×20 auxiliary surface can rank ahead of the canvas
without covering the click point. `click.guarded` verifies that the target
point hits the canvas in the layer-0 window list, then applies the identity,
geometry, foreground, and global pause/stop guards.

The read-only `permissions` operation returns `screen_capture` and
`post_event`. The live runner reports missing macOS permissions before opening
a stream or dispatching input. Operators can explicitly request the matching
macOS prompt with `seerdx --request-capture-access` or
`seerdx --request-input-access`. The daemon never opens permission prompts
during routine capture or clicking.

The daemon must not decide states, sleep, or click on its own. All policy lives
in `seerauto/state.py`, `seerauto/executor.py` and `seerauto/tasks/`.
