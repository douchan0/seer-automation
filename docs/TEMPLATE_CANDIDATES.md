# TEMPLATE_CANDIDATES — video-derived result-screen templates (NOT VALIDATED)

> 中文摘要：本文档登记从 B 站 BV1Fu8h6YEAz（40:48）挖出的**胜利/结算类画面**候选素材。
> **它们仍然只是 CANDIDATE，不能直接启用 `seerauto/config/default.json` 里的
> `battle_result_*` / `reward_panel` 规则。**
> 唯一的真机交叉验证是 §2.4 那一次**样本外测量**：把视频里挖出的 `胜利` 字形按 ×1.64 放大，
> 与并行任务在 23:53 抓到的**真机胜利弹窗**（`artifacts/live/victory_moguguai_2400x1600.png`）比对，
> NCC = **0.93**，位置误差 8 px。这说明**同一套 UI、可以做，但不是"已验证"** —— 只有一个状态、一张截图，
> 没有 replay、没有失败/奖励画面、规则一条都没启用。分辨率/布局风险见 §1。
> **失败（战败）画面在视频里 NOT FOUND** —— 作者全程没输过（33:51 他是主动 `逃跑`，不是战败），见 §3。

Owner of this document: the frame-mining task (`task-5`), Round 4 prep.
Additive-only: this file and `artifacts/video_frames/` are the only paths that were written.
`seerauto/`, `tests/`, `native/`, `scripts/`, `seerauto/config/default.json` and every pre-existing doc were **not** touched.

---

## 1. Read this first: the resolution / scale / layout risk

**These crops are 1280×720 video frames. The live client is captured at 2298×1428. A template mined from the
video is NOT a live-client template and may not match the live game at all.**

| | video | live client |
|---|---|---|
| pixel size | 1280 × 720 | capture 2298 × 1428 (`reference.capture_px`) |
| window | — | 1149 × 714 pt, `scale = 2.0` (`reference.window_pt`) |
| game stage inside the capture | n/a | `stage_px = [102, 128, 2092, 1220]` → aspect 1.715 |
| frame / canvas aspect | 1.778 (16:9), game fills 100 % of the frame | 16:9 canvas letterboxed inside a 1.715 stage |

So the two sources differ by:

1. **A scale factor of ≈ ×1.64** (see §2 — measured, not guessed).
2. **A letterbox offset** (the live 16:9 canvas sits inside a 1220 px-tall stage, ~19 px bars top and bottom).
3. **Video compression.** The video is H.264 with chroma subsampling at 1× ; the live client renders crisp at 2×.
   Resampled video crops are softer than live UI, which costs NCC even at the perfect scale.
4. **Burned-in video overlays** that are *not* game UI: bilibili watermark (top-left), Chinese subtitles
   (bottom band, y ≈ 655–715), the `2倍速播放中…` caption, full-frame text cards, and the author's own drawn
   labels (e.g. `谱尼真身 第二命（元素封印）` at 30:43 — white sans-serif, clearly post-production).
   **A crop that reaches the bottom ~10 % or the top-left corner harvests bilibili, not the game.**

Everything below is therefore labelled **CANDIDATE**:

* **there is exactly one out-of-sample check** (§2.4): a single live capture of the **victory** dialog, taken by a
  parallel task at 23:53, matches these video crops at **NCC ≈ 0.93** when the crops are scaled ×1.64. That is
  strong evidence the approach works — it is **not** a validation, because it is one state, one screenshot, no
  replay run, and the engine's own capture path/ROIs were not exercised;
* nothing here has been compared against the live client for **defeat**, **reward**, **seal-cleared**, **exp** or
  **flee** — those states were never captured live;
* the crops must not be copied into `seerauto/templates/` and the disabled rules
  (`battle_result_victory`, `battle_result_defeat`, `battle_result_text`, `reward_panel`) must stay disabled.
  Enabling a rule on an unvalidated template converts an unknown into a **silent misclassification** in production.
  That decision belongs to the owner of `seerauto/config/default.json`, **after** a real replay test;
* the same game is in both sources (§2.3) and the live victory dialog is now confirmed to be layout-identical, but the
  video's **client build is not identifiable** — no version string is readable anywhere in the 40 minutes.

---

## 2. How far these actually are from the live client (measured)

### 2.1 Method

`artifacts/flee.png` and `artifacts/battle1.png` (1100×630) look like live battle screenshots, but they are **not at a
known scale** — matching against them peaks at s ≈ 0.71 with NCC ≈ 0.86, which is inconsistent with every other
measurement. Do not calibrate against them.

The retina templates in `seerauto/templates/` *are* live-client pixels, so the useful test is the reverse one:
take the live retina crop and find it inside the video.

```
seerauto/templates/battle_action_grid.png  (607×323 retina)
  vs frames/07_truebody_s/f_00059.png  → s = 0.612  NCC = 0.959
  vs frames/04_seal_1_4/f_00020.png    → s = 0.609  NCC = 0.775
  vs artifacts/video_frames/victory__t16m14s.png → s = 0.609  NCC = 0.951
```

### 2.2 What that means

* **Uniform ×1.64 from video pixels to live-capture pixels** (`1 / 0.61`), i.e. the live 16:9 canvas is
  ≈ 2102 × 1182 px, which is exactly the 2092-wide stage with ~19 px letterbox bars. The numbers close.
* **Uniform, not stretched.** Forcing the 3.7 % stage/frame aspect difference into the resize *drops* NCC from
  0.954 to 0.845. Conclusion: the live client scales the UI uniformly and letterboxes it — do **not** do an
  anisotropic (x and y different) resize of these crops.
* Working hypothesis for the validation round:
  `live_x = 102 + video_x × 1.64`, `live_y = 149.5 + video_y × 1.64`.

### 2.3 Is it the same client at all?

**Same game, same UI generation, probably the same build family — but not proven.**

* The bottom-right battle action grid (战斗/道具/精灵/逃跑) matches the live retina template at **NCC 0.959**, at a
  clean uniform scale. If this were a re-drawn UI this would be far lower.
* The 当前回合 pill, the 当前战报 log panel, the pet/HP panel and the dialog frame all have the same construction in
  both sources.
* Both show the same Halloween event art (万圣活动) and the same 交易行/官方商城 buttons, which dates the video to a
  similar era as the 1.0.8.9 client.
* Evidence **against** perfect identity: the video's battle screen shows a `投票` button in the extreme top-right
  corner that appears in **neither** live capture (`artifacts/flee.png` nor `artifacts/live/victory_moguguai_2400x1600.png`);
  and the 当前回合 pill is content-width, so it measures 0.160 vs 0.167 of canvas width between the live
  `battle_turn_badge.png` crop and the video. Neither is structural — the *result dialog itself* matches at NCC 0.93
  in §2.4 — but together they say: expect small UI drift, verify every screen instead of assuming.
* **No live-client capture of any result dialog exists** *in this task's own output*; the only live evidence is the
  parallel capture discussed in §2.4, which covers **victory only**.

### 2.4 Out-of-sample check against a real live capture (the one number that matters)

While this task was running, a **parallel task captured the live client**: `artifacts/live/victory_moguguai_2400x1600.png`
(23:53) — a genuine 2400×1600 screenshot of the 1.0.8.9 client showing the **victory dialog of a routine wild battle**
(蘑菇怪, 当前回合 3). That file is **not mine** and was not modified; it is read-only evidence here.

Without fitting anything, the crops from the video were scaled by **×1.64** (the value measured in §2.2 *before* this
capture existed) and matched against it:

| video crop | vs LIVE victory capture | vs LIVE battle capture (negative control) |
|---|---|---|
| `victory_glyph.png` | **NCC 0.931 @ ×1.64** | 0.488 |
| `victory_head_band.png` | **NCC 0.934 @ ×1.64** | 0.543 |
| `victory_panel_full.png` | **NCC 0.937 @ ×1.64** | 0.511 |
| `battle_round_pill.png` | 0.782 @ ×1.64 | 0.773 (expected — the pill is in both) |

Independent cross-check: the same video glyph at ×1.64 scores **0.910** against the parallel task's own live crop
`seerauto/templates/live/live_victory_word.png` (400×100), and that crop also peaks at ×1.64.

Geometry check. The capture's game stage measures x 154…2245 (**2092 px wide — identical to
`reference.stage_px`**) and y ≈ 210…1430 (1220 px), with the 16:9 canvas letterboxed inside it. Predicted glyph
position from the video (`598,279` × 1.64 + canvas origin) = `(1131, 688)`; **measured `(1123, 690)`** → 8 px error
(0.4 % of canvas width). So the confirmed mapping is:

```
live_stage_px = stage_origin + video_px × 1.64    (uniform; canvas letterboxed ~19 px top/bottom inside the 1220 stage)
```

**What this does prove:** the video was made from the same client build family, the battle result dialog is
layout-identical, and a video-derived crop rescaled ×1.64 is *usable* (well above the engine's `min_score: 0.8`)
for the **victory** state.

**What it does not prove:** one screenshot, one state, one battle type; no defeat/reward/seal/exp/flee evidence;
no replay test; the engine's capture path and ROI (`vision.rois.*`, 2298×1428 reference) was not exercised; and the
live capture is 2400×1600 with the stage at a different offset than `reference.stage_px = [102,128,…]`, i.e. the
window geometry **does move between sessions** and offsets must be recomputed, never hard-coded.
The templates remain **CANDIDATES**; the rules remain **disabled**.

---

## 3. Inventory (what was found)

Source: `artifacts/video/BV1Fu8h6YEAz.mp4`
sha256 `f39866154fb1771e2fc4397090f69c2115e61cfcd378ee76321abe5be80336f3` · 1280×720 · 30 fps · 2447.9 s.
Authoritative machine-readable record: [`artifacts/video_frames/manifest.json`](../artifacts/video_frames/manifest.json).

| state | count | timestamps (mm:ss) | what it looks like | frame file |
|---|---|---|---|---|
| **victory 胜利** | **7** | 04:42 · 12:39 · **14:56** · 16:14 · 25:05 · 25:58 · 39:50 | in-battle centred dialog: gold `胜利` glyph, `恭喜你！在这场战斗中 / 获得胜利，继续加油！`, blue `确认` | `victory__t14m56s.png` (+6) |
| seal cleared | 3 | 12:40 · 14:58 · 25:06 | `谱尼的第N封印：X 已经解除…` + `确认` | `seal_cleared__t12m40s.png` … |
| item reward | 1 | 12:41 | item icon `>>` container icon, `1个天赋改造药剂已经放入你的储存箱！` | `reward_item__t12m41s.png` |
| exp settlement | 2 | 14:57 · 26:51 | pet + `XIN精灵获得经验：N` / `离升级还需经验：N`; the 26:51 variant is the taller level-up panel | `exp_settle__t14m57s.png`, `levelup_settle__t26m51s.png` |
| flee prompt | 1 | 33:51 | `你确定要逃离这次战斗吗？` + 确认/取消 | `flee_confirm__t33m51s.png` |
| flee success | 1 | 33:53 | `恭喜你，逃跑成功！` | `flee_success__t33m53s.png` |
| **defeat 失败** | **0** | — | **NOT FOUND** — see below | — |

How these were found (so the next round can redo it): OCR index (`frames/_ocr.json`) first → 1 fps whole-video scan
scored for "big centred light panel" (341 candidates) → Vision OCR (`scripts/seerctl ocr`) over all of them →
whole-video 4 fps sweep with the gold glyph as a template (7 hits, NCC 0.987–0.998) → every battle→map transition
inspected frame by frame. OCR text alone was never trusted: the panel regions were **viewed as PNGs** before being
accepted.

### 3.1 Defeat (战斗失败) — NOT FOUND, and why that matters

* 40 minutes, 7 victories on camera, **zero defeats**. At 33:51 the author is losing a true-body fight and instead of
  dying he clicks `逃跑` (`恭喜你，逃跑成功！`), then heals (`已花费50个XIN豆…`) and re-enters.
* The word 失败 **is** all over the video, but it is the *static event note*
  `圣能爆发期间，七大封印全面削弱，挑战失败后可继续进行挑战！` which sits on the seal maps for the entire sequence.
* **Consequence for the engine:** a win/lose rule cannot be validated end-to-end from this video. At best the video
  supports "victory is directly observable"; "lose" would have to be inferred (battle ended, no 胜利 dialog, back on
  the map) or — correctly — captured from the live client by actually losing a fight.
* Equally important: the disabled `battle_result_text` rule uses the needles `[胜利, 失败, 挑战结束]`. **`失败` alone
  would fire almost permanently** on the seal maps. That rule must not be re-enabled with a bare 失败 needle no matter
  what templates exist.

---

## 4. The candidate crops

All in [`artifacts/video_frames/templates_candidate/`](../artifacts/video_frames/templates_candidate/).
Boxes are **video pixels in the 1280×720 source frame** — they are *not* live-client boxes. The live box would be
obtained with the ≈ ×1.64 mapping of §2 (and then *verified*, not assumed).

| template | size | source | role | risk |
|---|---|---|---|---|
| `victory_glyph.png` | 89×43 | 14:56 | **primary win discriminator** — the gold `胜利` glyph | a defeat glyph would not match it; the dialog animates in |
| `victory_head_band.png` | 270×150 | 14:56 | glyph + both text lines, no mascot/button | — |
| `victory_glyph_ctx.png` | 107×59 | 14:56 | glyph with 8 px of panel context | worse separation than the tight glyph (panel starfield dilutes it) |
| `victory_panel_full.png` | 438×266 | 14:56 | whole dialog frame | includes an animated mascot — prefer the head band |
| `seal_cleared_panel.png` | 432×250 | 12:40 | "seal already released" confirmation | seal-specific text |
| `reward_item_panel.png` | 432×250 | 12:41 | **REWARD rule candidate** | on screen < 0.5 s of video time |
| `exp_settle_panel.png` | 432×250 | 14:57 | post-win exp settlement | value can be **negative** (`获得经验：-211` at 26:51) |
| `flee_confirm_panel.png` | 432×250 | 33:51 | flee prompt (relevant to `CAP-ESCAPE`) | — |
| `flee_success_panel.png` | 432×250 | 33:53 | flee succeeded — a usable **"battle ended and it was NOT a win"** negative | — |
| `levelup_panel.png` | 465×360 | 26:51 | taller level-up variant | different component height |
| `event_note_failed.png` | 248×126 | 33:55 | **false-positive specimen, NOT a template** | documents the `失败` trap |
| `battle_round_pill.png` | 218×66 | 16:14 | scale calibration vs `seerauto/templates/battle_turn_badge.png` | — |

**Measured separation inside the video** (this is the only "validation" that exists, and it is *within the video*):

```
victory_glyph      victory frames 0.989 … 0.998  (all at the same pixel offset, 598,279)
                   non-victory frames ≤ 0.426
victory_head_band  victory 0.865 … 0.998 ; non-victory ≤ 0.551
```

That margin is wide enough that the *state* identification is solid. It says **nothing** about the live client.

---

## 5. What this does NOT deliver

* **No defeat template.** There is no evidence in the video of what a loss looks like.
* **No error/连接断开 template** (also NOT FOUND).
* **No rule validation.** One out-of-sample NCC measurement exists for *victory only* (§2.4); there is no replay
  test, no ROI-scoped run, and no check against the engine's 2298×1428 capture path. `seerauto/config/default.json`
  was **not** edited; the `battle_result_*` and `reward_panel` rules are still `"enabled": false` and must stay that
  way until someone runs a replay test with a live capture.
* **No live evidence for reward / seal-cleared / exp / flee** panels — those crops are still pure video material,
  even though their panel frame is demonstrably the same component as the validated victory panel.
* **No timing data.** The video is played at 2× in the battle segments and the author clicks `确认` within a fraction
  of a second, so the 0.3–1.3 s on-screen duration above is a *video* duration, not a live one.

---

## 6. What the validation round should do (shortest path)

Steps 1–3 already happened during this task (the parallel live-capture task produced
`artifacts/live/victory_moguguai_2400x1600.png` and the ×1.64 prediction was confirmed, §2.4). What is still missing:

1. ~~Win a battle on the live client and capture the dialog~~ — **done (victory only)**.
2. ~~Scale sweep~~ — **done: peak at ×1.64, NCC 0.93**. Keep the actual number in the rule's comment.
3. **Replay test against the engine's own capture path** at its reference geometry (2298×1428 /
   `stage_px = [102,128,2092,1220]`) — the existing live capture is 2400×1600 with the stage at
   `(154, 210)`, so offsets differ. Compute the ROI from the *measured* stage origin at run time; never hard-code
   `(154, 210)` or `(102, 128)`.
4. **ROI-scoped run**: put a `template_present` with `min_score ≈ 0.85` on `victory_glyph` rescaled ×1.64 inside a ROI
   of roughly `x 0.44–0.62, y 0.35–0.48` (stage-relative, derived from `598,279`+`89×43` at ×1.64), confirm it fires on
   a fresh win and does **not** fire on `artifacts/live/battle_turn1_2400x1600.png` (measured NCC there: 0.488).
5. **Then and only then** create `seerauto/templates/battle_victory_banner.png` and let the owner of
   `seerauto/config/default.json` flip `battle_result_victory` on. This task deliberately did not do it.
6. **Test the animating case**: at 39:50.20 the glyph is mid-animation and only scores 0.74 against its own template.
   A rule that fires on one frame will flicker; require a settled panel or N-of-M hysteresis.
7. **Defeat / reward / seal / exp have no shortcut:** each needs one real live capture of that screen.
   Defeat in particular can only be obtained by actually losing a fight.
8. Note for whoever enables rules: the live victory capture also shows an **achievement toast** (`获得成就 击败蘑菇怪`)
   overlapping the bottom action grid — an element that never appears in the video. It will pollute
   `battle_action_grid`/`idle_action_bar` matches near a kill.

---

## 7. Provenance

* Frames and crops: [`artifacts/video_frames/_extract.py`](../artifacts/video_frames/_extract.py) — re-runnable and
  idempotent; every file in that directory is a frame of the third-party video, never a live-client screenshot.
* Manifest: [`artifacts/video_frames/manifest.json`](../artifacts/video_frames/manifest.json).
* OCR evidence quoted above comes from macOS Vision (`scripts/seerctl ocr <png> 0.3`) run on the actual frames; the
  Chinese strings were transcribed from the **viewed images**, not from OCR output alone.
* Live-client geometry numbers are quoted from `seerauto/config/default.json` (`reference`, `window`), not measured
  in this task.
