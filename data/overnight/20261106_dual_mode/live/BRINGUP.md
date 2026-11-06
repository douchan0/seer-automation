# BRINGUP — 2026-11-06 night live client (gameops)

Machine: macOS 15.7.7, 2 displays (main 1470×956 pt @ 0,0; second display above with negative y).
Client: `/Applications/SeerGame.app`, `xin计划 (1.0.8.9)`.
Authorization: user authorized 2026-11-06 22:00 → 2026-11-07 07:00, launch + **foreground** GUI input.
Scope actually used: launch, art-pack dialog dismissal, login, one battery-enable UI interaction. No battle / tower / PvP / SPT-opened.

## 1. Window geometry (measured this session, DO NOT hardcode)

| field | value |
|---|---|
| window id | **3284** |
| title | `xin计划 (1.0.8.9)` |
| bounds | **x=268, y=44, w=1200, h=800 pt** |
| screenshot | **2400×1600 px** (scale 2.0) |
| aux window (art-pack) | id=3285, 260×258 pt — **gone after dismissal** |
| frontmost at handover | `SeerGame` |

**Position changes during a single bring-up**: first read after launch = `(270,68)`; after the art-pack
dialog was dismissed the main window was `(268,44)`. Always re-read from `seerctl wl` right before any click.

### Coordinate conversion (verified twice this session)

```
global_pt = (268 + game_px_x / 2, 44 + game_px_y / 2)      # game_px = window-local device px, 2400×1600
```
The shot includes the client's own custom title bar ( Electron `xin计划 (1.0.8.9)` bar is part of the
1200×800 content ), so `win.y` is the content origin — no extra title-bar correction.

**Stage offset ≈ 0 this session.** SPT icon observed at game_px `(2149,347)`; docs say `(2151,347)` → dx=2, dy=0
(OCR jitter). Still measure it per session per AGENTS §5.5.6.

## 2. Commands that worked (exact)

```bash
cd /Users/ceontou/Documents/code/seer-automation-night
open -a /Applications/SeerGame.app                       # THE way to raise + focus the client
./scripts/seerctl wl                                     # window inventory, INCLUDES the aux window
printf '{"op":"win"}\n{"op":"quit"}\n' | ./native/bin/seerdx   # largest window only (never sees the aux dialog)
PYTHONPATH=.pydeps python3 tools/ctl.py shot  <path.png> # window capture, works occluded, 2400×1600
PYTHONPATH=.pydeps python3 tools/ctl.py look             # capture+OCR, prints game_px per text box
PYTHONPATH=.pydeps python3 tools/ctl.py clickpx X Y      # foreground click at game_px (has move guard + frontmost guard)
PYTHONPATH=.pydeps python3 tools/login.py --timeout 240  # announcement → login → server → confirmed in the world
```

Art-pack dialog (aux window) — the path that worked (because `tools/fix_startup.py` is broken, see §5):

```bash
screencapture -x -R<aux x>,<aux y>,<aux w>,<aux h> /tmp/aux.png
printf '{"op":"ocr","path":"/tmp/aux.png","min":0.25}\n{"op":"quit"}\n' | ./native/bin/seerdx
# 暂不导入 box is Vision-normalised bottom-left origin:
#   cx = win.x + (x + w/2) * win.w ;  cy = win.y + (1 - (y + h/2)) * win.h
printf '{"op":"clickpid","x":<cx>,"y":<cy>,"n":1}\n{"op":"quit"}\n' | ./native/bin/seerdx
```

## 3. Battery HUD (右下角电池) — coordinates + rules (verbatim)

| item | game_px | global_pt |
|---|---|---|
| battery widget (clicked here, opens the dialog) | **(2163, 1350)** | **(1349.5, 719.0)** |
| its countdown readout (OCR) | (2159–2161, 1351) | — |
| dialog 确认 button | (1047, 981) | (791.5, 534.5) |
| dialog 取消 button | (1364, 981) | (950.0, 534.5) |

**Verbatim text**

- Arrival modal: `检测到本日还未开启8倍经验与4倍学习力，可随时点击右下角电池主动开启` → button `确认` (this one dismisses fine; it is NOT the enable).
- Battery click → confirm dialog:
  ```
  本日还未开启8倍经验与4倍学习力，
  开启之后中途不可取消。
  确认要开启8倍经验与4倍学习力时间吗?
  ```
  buttons `确认` / `取消`.
- HUD: countdown **05:36 @22:53 → 05:24 @23:06**, i.e. real-time HH:MM countdown (≈5.5 h left at start of
  session); gauge = 2 lit segments (red, orange) + black; two round buttons above the gauge (speaker icon,
  orange "slash" icon).
- **NOT FOUND**: any 每日次数 / 剩余次数 / 消耗道具 text; any N倍 badge on the HUD; any "开启成功" toast.

**Current enabled state: NOT ENABLED (unconfirmed).** Four separate attempts clicked the exact `确认` pixel
(game_px 1047,981 / 1045,985 / 1047,981 / 1047,981). Each time the dialog closed within 0.35 s, but:
1) an 8-frame × 0.35 s OCR loop after the click found no success text,
2) pixel-diff of the battery ROI before/after changed only the countdown digits,
3) re-clicking the battery still returns `本日还未开启8倍经验与4倍学习力`.
So the game itself still reports the 8×/4× as **not started**. I did **not** claim it as on.
Hand-over state: dialog closed with `取消` (23:06), world idle, no modal open.

Hypotheses for the next operator (not tested): the enable may need a different hit path (double press /
press-and-hold), or it is gated on something else — research (`docs/plan-from-video.md:44`,
`docs/research/PUNI_CURRENT_VERSION_GUIDE.md:225`) says the official route is `初级班辅导员`/`高级班辅导员`
in the 精灵太空站 training room, and the "time exhausted" message is
`当日 8 倍经验与 4 倍学习力时间已耗尽`. Server-side refusal was not distinguishable from a missed click.

## 4. Bring-up timeline (all times +08:00, 2026-11-06)

| time | event | evidence |
|---|---|---|
| 22:47:19 | `open -a` → main 1200×800 @ (270,68), aux 260×258 @ (433,-693) (2nd display) | `wl_before_launch.json` |
| 22:48 | aux dialog OCR: `未检测本地有美术包，是否需要导入？` / `立即导入` / `前往下载` / `暂不导入` | `aux_dialog_2248.png` |
| 22:49 | aux dismissed; aux window **moved** (433,-693)→(574,159) between read and click | `probe_2256.jsonl` |
| 22:51:20 | login page: `航行公告` + `游戏公告`/`我已知晓`; announcement says `1.11月份版本更新已发布` | `world_t0.png` |
| 22:52:49 | `login.py` → `confirmed in the world` (啦公星 not on page 1 → number field `1` + `快速进入`) | `world_t0.png` |
| 22:53 | arrival modal (8×/4× hint) → dismissed with `确认` | `world_t0.png`, `world_clean.png` |
| 22:53 | geometry stable 1200×800 @ (268,44) → 2400×1600 px | `world_clean.png` |
| 22:56–23:06 | battery experiments (see §3) | `battery_*.png` |
| 23:06:49 | handover: in world, idle, **frontmost = SeerGame**, no modal, no loop | `world_clean_final_2306.png` |

## 5. Failures (verbatim) — fix these before the next live session

1. **`tools/fix_startup.py` is broken** — unusable as shipped:
   `NameError: name 'time' is not defined` at `tools/fix_startup.py:68` (`time.sleep(0.8)`, file has no
   `import time`). The entire one-command art-pack unstick path is therefore dead; I had to reproduce the
   capture→OCR→clickpid sequence by hand.
2. **`seerdx activate` does not raise the window on macOS 15.** It returns `{"ok":true}` while the frontmost
   app stays whatever it was (`DeepSeek Harness`, `Microsoft Edge`). `.activateAllWindows` without
   `.activateIgnoringOtherApps` is not enough. Consequence: `tools/ctl.py` `ensure_frontmost()` (12 × activate)
   will burn its retries and refuse, and `tools/login.py`'s `clear_popup()` relies on the same lane.
   **Workaround that works: `open -a /Applications/SeerGame.app`** (then `lsappinfo front` = SeerGame).
3. **`osascript -e 'tell application "SeerGame" to activate'` is worse than useless here** — the shell/osascript
   call itself brought the *harness* app back to the front (`lsappinfo front` = `DeepSeek Harness` right after).
4. **`screencapture -R` with a negative y (second display) captures the wrong content when the target window is
   occluded.** The art-pack dialog sat at (433,-693) behind the Edge/bilibili window; `-R433,-693,260,258` returned
   pixels of the *video*, not the dialog. Use `-l <windowid>` or raise the app first.
5. **Aux window moved between geometry read and click.** The click was computed from the stale rect and only
   landed on `暂不导入` by luck. Re-read `seerctl wl` after raising and immediately before the click.
6. Login ran while the user was actively using the machine (Edge/bilibili, Safari, WeChat all open). Foreground
   clicks necessarily steal focus — done only for the login/battery steps, nothing else.

## 6. Still open (read-only recon c/d/e/f + b were NOT done)

`b` character panel (pet/level/HP/PP/xp_to_next): NOT DONE (needs opening the 精灵/属性 panel).
`c` 谱尼 entry (SPT → 太空站左翼 → 谱尼, seven-seal panel): NOT DONE.
`d` 交易行 gating text: NOT DONE. Lead's note (unverified): first tab of 商城 (`交易行 / 官方商城 / 运行商人`).
`e` 经验分配器 entry: NOT DONE. Lead's note (unverified): `基地 → 基地手册 → 家具`.
`f` 试炼之塔 floor + 精灵恢复 reachability: NOT DONE.
Everything above needs a new authorized live slot; none of it is claimed as found.
