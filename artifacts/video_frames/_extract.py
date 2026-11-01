#!/usr/bin/env python3
"""Reproducible re-extraction of the video-derived CANDIDATE result assets.

Provenance: every PNG in this directory is a frame of
    artifacts/video/BV1Fu8h6YEAz.mp4   (1280x720, 30 fps, 2447.9 s, H.264)
    recorded by a third party (bilibili BV1Fu8h6YEAz) from the 赛尔号 "Xin 计划" client.
NONE of these images is a screenshot of the live client on this machine.

Usage:
    PYTHONPATH=.pydeps python3 artifacts/video_frames/_extract.py            # re-extract all
    PYTHONPATH=.pydeps python3 artifacts/video_frames/_extract.py --preview  # + crop preview sheet

The crop boxes below are hand-measured on the 1280x720 source frames (see
docs/TEMPLATE_CANDIDATES.md). They are *video* pixel boxes, NOT live-client boxes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "artifacts" / "video" / "BV1Fu8h6YEAz.mp4"
OUT = ROOT / "artifacts" / "video_frames"
TPL = OUT / "templates_candidate"

# (filename stem, seconds, note) -- see manifest.json for the authoritative record.
FRAMES = [
    ("victory__t04m42s", 282.45, "normal-battle victory dialog (chapter 02)"),
    ("victory__t12m39s", 759.30, "victory dialog for the 1st seal (虚无封印)"),
    ("victory__t14m56s", 896.00, "victory dialog for 2nd seal (元素/能量封印) - cleanest instance"),
    ("victory__t16m14s", 974.50, "victory dialog for 3rd seal (生命封印, text 生命封印)"),
    ("victory__t25m05s", 1505.20, "victory dialog, 7th seal run"),
    ("victory__t25m58s", 1558.80, "victory dialog, 7th seal run (last of the 1-7 sweep)"),
    ("victory__t39m50s", 2390.40, "victory dialog ending the S-level true-body fight"),
    ("seal_cleared__t12m40s", 760.00, "seal-cleared result dialog (虚无 already released)"),
    ("seal_cleared__t14m58s", 898.50, "seal-cleared result dialog (元素 already released)"),
    ("reward_item__t12m41s", 761.00, "item reward panel ('1个天赋改造药剂已经放入你的储存箱！')"),
    ("exp_settle__t14m57s", 897.00, "exp settlement panel ('XIN精灵获得经验：0')"),
    ("levelup_settle__t26m51s", 1611.00, "level-up settlement panel (升级到了52级)"),
    ("flee_confirm__t33m51s", 2031.75, "flee confirmation dialog ('你确定要逃离这次战斗吗？')"),
    ("flee_success__t33m53s", 2033.25, "flee result dialog ('恭喜你，逃跑成功！')"),
    ("event_note_failed__t33m55s", 2035.00, "FALSE-POSITIVE TRAP: static event note containing '挑战失败'"),
    ("battle_ref__t16m14s", 974.50, "plain battle frame carrying the 当前回合 pill (scale calibration)"),
]

# name -> (source stem, x, y, w, h, purpose)
CROPS = [
    ("victory_glyph", "victory__t14m56s", 598, 279, 89, 43,
     "the gold 胜利 glyph alone, tight - primary win discriminator"),
    ("victory_glyph_ctx", "victory__t14m56s", 590, 271, 107, 59,
     "same glyph with 8px of panel context (use if the tight crop overfits video noise)"),
    ("victory_head_band", "victory__t14m56s", 500, 266, 270, 150,
     "glyph + both congratulation text lines, no mascot/button"),
    ("victory_panel_full", "victory__t14m56s", 434, 236, 438, 266,
     "whole victory dialog frame (contains an animated mascot - prefer head_band)"),
    ("seal_cleared_panel", "seal_cleared__t12m40s", 430, 256, 432, 250,
     "whole 'already released' result dialog"),
    ("reward_item_panel", "reward_item__t12m41s", 430, 256, 432, 250,
     "item-reward panel body"),
    ("exp_settle_panel", "exp_settle__t14m57s", 430, 256, 432, 250,
     "post-victory exp settlement panel body"),
    ("flee_confirm_panel", "flee_confirm__t33m51s", 430, 256, 432, 250,
     "flee confirmation dialog body"),
    ("flee_success_panel", "flee_success__t33m53s", 430, 256, 432, 250,
     "flee result dialog body"),
    ("levelup_panel", "levelup_settle__t26m51s", 413, 206, 465, 360,
     "level-up settlement panel (taller component)"),
    ("event_note_failed", "event_note_failed__t33m55s", 555, 62, 248, 126,
     "static event note - NOT a result screen; documents the 失败 OCR trap"),
    ("battle_round_pill", "battle_ref__t16m14s", 538, 90, 218, 66,
     "当前回合 pill - scale calibration reference (live twin: seerauto/templates/battle_turn_badge.png)"),
]


def ffmpeg_frame(seconds: float, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{seconds:.2f}", "-i", str(VIDEO),
         "-frames:v", "1", "-threads", "2", str(dest), "-y"],
        check=True,
    )


def main() -> int:
    if not VIDEO.exists():
        print(f"missing source video: {VIDEO}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    TPL.mkdir(parents=True, exist_ok=True)

    for stem, sec, _note in FRAMES:
        dest = OUT / f"{stem}.png"
        ffmpeg_frame(sec, dest)
        print(f"frame  {dest.relative_to(ROOT)}")

    for name, src, x, y, w, h, _purpose in CROPS:
        with Image.open(OUT / f"{src}.png") as im:
            crop = im.crop((x, y, x + w, y + h))
            crop.save(TPL / f"{name}.png", optimize=True)
        print(f"crop   {(TPL / (name + '.png')).relative_to(ROOT)}  {w}x{h}")

    if "--preview" in sys.argv:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 15)
        except Exception:  # noqa: BLE001
            font = ImageFont.load_default()
        cell_w, cols = 320, 4
        tiles = []
        for name, _src, _x, _y, w, h, _p in CROPS:
            with Image.open(TPL / f"{name}.png") as im:
                im = im.convert("RGB")
                th = max(1, int(im.height * cell_w / im.width))
                tiles.append((name, f"{w}x{h}", im.resize((cell_w, th), Image.LANCZOS)))
        cell_h = max(t.height for _, _, t in tiles)
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cell_w * cols, (cell_h + 20) * rows), (22, 22, 22))
        dr = ImageDraw.Draw(sheet)
        for i, (name, size, im) in enumerate(tiles):
            ox, oy = (i % cols) * cell_w, (i // cols) * (cell_h + 20)
            dr.rectangle([ox, oy, ox + cell_w, oy + 20], fill=(0, 0, 0))
            dr.text((ox + 3, oy + 2), f"{name} {size}", fill=(255, 255, 0), font=font)
            sheet.paste(im, (ox, oy + 20))
        sheet.save(OUT / "crops_preview.png")
        print(f"preview {(OUT / 'crops_preview.png').relative_to(ROOT)}")

    print(json.dumps({"frames": len(FRAMES), "crops": len(CROPS)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
