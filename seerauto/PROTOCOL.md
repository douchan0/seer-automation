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
| `capture.latest` | — | `{"seq": int, "ts_ns": int, "path": str \| "png_b64": str, "w": int, "h": int, "rect": [x,y,w,h]?}` |
| `capture.roi` | `rect` `[x, y, w, h]` in game_px | like `capture.latest`, cropped from the newest frame |
| `capture.stop` | — | `{"ok": true}` |
| `shot` | `path`, `rect` optional | `{"path": str, "w": int, "h": int}` (one-shot, no warm session) |
| `ocr` | `path`, `min` | `{"items": [{"text", "conf", "x", "y", "w", "h"}]}` Vision-normalised boxes |
| `click` | `x, y` (global points), `n` | `{"ok": true}` |
| `move` | `x, y` (global points) | `{"ok": true}` |
| `key` | `code` (macOS virtual key) | `{"ok": true}` |
| `type` | `text` | `{"ok": true}` |
| `quit` | — | `{"bye": true}` then exit |

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

The daemon must not decide states, sleep, or click on its own. All policy lives
in `seerauto/state.py`, `seerauto/executor.py` and `seerauto/tasks/`.
