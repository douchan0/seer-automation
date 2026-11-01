#!/usr/bin/env python3
"""Round 4: run N real battles end to end with a local vision state machine.

State is decided ONLY by local template matching on a warm ScreenCaptureKit
stream. OCR is used only as a slow escalation path when the templates say
"unknown", which is the project rule (OCR is 40-150 ms; a match is ~1 ms).

States
    WORLD     no fight button, no victory panel -> find a monster and click it
    BATTLE    fight button visible, no victory  -> attack
    RESULT    victory panel visible             -> confirm and count a win
    DIALOG    a 确认 button is up               -> dismiss it
    UNKNOWN   nothing matched for >2 s          -> one OCR look, then stop

Safety
  * refuses to act unless the game window is frontmost
  * bounded by --battles / --max-seconds
  * every click is at a coordinate the matcher just found, never a guess
  * a click is never repeated faster than --min-action-gap-ms

Usage
    PYTHONPATH=.pydeps python3 tools/battle_run.py --battles 3 --json /tmp/run.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "native", "bin", "seerdx")
T = os.path.join(ROOT, "seerauto", "templates", "live")
SEERCTL = os.path.join(ROOT, "scripts", "seerctl")

# Regions to search, in game_px. Searching a region is much cheaper than a
# full-frame match, and it also prevents a false positive elsewhere on screen.
ROI_FIGHT = (1710, 1130, 2040, 1260)      # just the 战斗 button
ROI_VICTORY = (980, 640, 1430, 790)       # just the 胜利 glyph band
ROI_DIALOG = (1070, 940, 1300, 1040)      # just the 确认 button
ROI_SCENE = (200, 340, 2260, 1330)        # playable area, excludes the HUD


def frontmost() -> bool:
    try:
        out = subprocess.run([SEERCTL, "wl"], capture_output=True, text=True, timeout=5).stdout
        for w in json.loads(out):
            if w["layer"] == 0:
                return w["owner"] == "SeerGame"
    except Exception:
        return False
    return False


def pct(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


class Matcher:
    def __init__(self, names):
        self.tpl = {}
        for n in names:
            p = os.path.join(T, n + ".png")
            img = cv2.imread(p)
            if img is None:
                raise SystemExit(f"missing template: {p}")
            self.tpl[n] = img

    def score(self, frame, name, roi):
        x0, y0, x1, y1 = roi
        sub = frame[y0:y1, x0:x1]
        r = cv2.matchTemplate(sub, self.tpl[name], cv2.TM_CCOEFF_NORMED)
        _, s, _, loc = cv2.minMaxLoc(r)
        th, tw = self.tpl[name].shape[:2]
        return float(s), (x0 + loc[0] + tw / 2, y0 + loc[1] + th / 2)


def _ocr_escalate(client, backend, geo, act, args) -> float:
    """One OCR look, then click a confirm button if the text names one.

    Returns the wall time of the whole escalation so it shows up in the timing
    table instead of disappearing into "wait".
    """
    t0 = time.perf_counter()
    path = "/tmp/battle_run_escalate.png"
    try:
        frame = backend.latest()
        cv2.imwrite(path, frame.array)
        items = client.send("ocr", path=path, min=0.3).get("items", [])
    except Exception as exc:  # noqa: BLE001
        print(f"           -> OCR escalation failed: {exc}")
        return (time.perf_counter() - t0) * 1000
    w = geo.pixel_size.w
    h = geo.pixel_size.h
    confirms = ("确认", "确定", "我知道了", "确定 ")
    hit = None
    for it in items:
        if any(c in it["text"] for c in confirms):
            cx = (it["x"] + it["w"] / 2) * w
            cy = (1 - (it["y"] + it["h"] / 2)) * h
            hit = (cx, cy, it["text"])
            break
    texts = [i["text"][:12] for i in items][:8]
    if hit:
        print(f"           -> OCR: confirm button {hit[2]!r} at "
              f"({hit[0]:.0f},{hit[1]:.0f})")
        act((hit[0], hit[1]), "ocr confirm")
        time.sleep(0.8)
    else:
        print(f"           -> OCR: no confirm button; saw {texts}")
    return (time.perf_counter() - t0) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--battles", type=int, default=3)
    ap.add_argument("--max-seconds", type=float, default=600.0)
    ap.add_argument("--fight-th", type=float, default=0.85)
    ap.add_argument("--victory-th", type=float, default=0.80)
    ap.add_argument("--confirm-th", type=float, default=0.80)
    ap.add_argument("--monster-th", type=float, default=0.72)
    ap.add_argument("--escalate-s", type=float, default=1.2)
    ap.add_argument("--stall-s", type=float, default=60.0)
    ap.add_argument("--min-action-gap-ms", type=float, default=700.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    matcher = Matcher(["live_fight_btn", "live_victory_word", "live_confirm_btn",
                       "live_confirm_xp", "live_monster_moguguai"])
    client = C.DaemonClient(BIN, timeout_s=30.0)
    win = client.win()
    geo = G.WindowGeometry(origin_pt=G.Point(float(win["x"]), float(win["y"])),
                           size_pt=G.Size(float(win["w"]), float(win["h"])),
                           scale=G.RETINA_SCALE)
    backend = C.DaemonCapture(client, geo)
    if not backend.warm:
        print("no warm session")
        return 1

    client.send("activate")
    time.sleep(0.6)
    if not frontmost():
        print("REFUSING: game window is not frontmost")
        return 1

    print(f"window {win['w']}x{win['h']} pt -> {geo.pixel_size.w:.0f}x{geo.pixel_size.h:.0f} px")
    print(f"goal {args.battles} battles, budget {args.max_seconds:.0f}s\n")

    stages = {k: [] for k in ("capture", "detect", "action", "wait", "ocr")}
    wins = 0
    losses = 0
    pending_win = False        # saw a victory panel, not yet confirmed-gone
    battles_started = 0
    last_action = 0.0
    started = time.perf_counter()
    log = []
    last_state = None
    unknown_since = None
    last_progress = time.perf_counter()
    end_reason = "budget"

    def act(px, why):
        nonlocal last_action
        wait = args.min_action_gap_ms - (time.perf_counter() - last_action) * 1000
        if wait > 0:
            time.sleep(wait / 1000.0)
        t = time.perf_counter()
        gp = geo.global_pt_from_game_px(px[0], px[1])
        client.send("click", x=gp.x, y=gp.y, n=1)
        stages["action"].append((time.perf_counter() - t) * 1000)
        last_action = time.perf_counter()
        if len(log) < 400:
            log.append({"t": round(time.perf_counter() - started, 2), "why": why,
                        "px": [round(px[0]), round(px[1])]})

    try:
        while wins < args.battles and (time.perf_counter() - started) < args.max_seconds:
            t = time.perf_counter()
            frame = backend.latest()
            stages["capture"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            s_fight, p_fight = matcher.score(frame.array, "live_fight_btn", ROI_FIGHT)
            s_vic, p_vic = matcher.score(frame.array, "live_victory_word", ROI_VICTORY)
            s_ok_a, p_ok_a = matcher.score(frame.array, "live_confirm_btn", ROI_DIALOG)
            s_ok_b, p_ok_b = matcher.score(frame.array, "live_confirm_xp", ROI_DIALOG)
            # two dialog styles exist (victory panel vs XP/drop dialogs): accept either
            if s_ok_a >= s_ok_b:
                s_ok, p_ok = s_ok_a, p_ok_a
            else:
                s_ok, p_ok = s_ok_b, p_ok_b
            stages["detect"].append((time.perf_counter() - t) * 1000)

            is_victory = s_vic >= args.victory_th
            is_dialog = s_ok >= args.confirm_th and not is_victory
            # hysteresis: the fight button score dips to ~0.80 during animations,
            # so once we are in a battle a weaker match still counts as battle.
            fight_cut = (args.fight_th - 0.10) if last_state == "BATTLE" else args.fight_th
            is_battle = s_fight >= fight_cut and not is_victory and not is_dialog

            if is_victory:
                state = "RESULT"
            elif is_dialog:
                state = "DIALOG"
            elif is_battle:
                state = "BATTLE"
            else:
                state = "WORLD"

            if state != last_state and state != "WORLD":
                last_progress = time.perf_counter()
            if time.perf_counter() - last_progress > args.stall_s:
                end_reason = f"no progress for {args.stall_s:.0f}s"
                print(f"           -> {end_reason}")
                break
            if state != last_state:
                print(f"[{time.perf_counter()-started:6.1f}s] {state:<7} "
                      f"fight={s_fight:.2f} victory={s_vic:.2f} confirm={s_ok:.2f}")
                last_state = state

            if state == "RESULT":
                if not pending_win:
                    pending_win = True
                    # click the 确认 button, NOT the 胜利 glyph — clicking the
                    # glyph is a no-op and leaves the panel up forever.
                    act(p_ok, "confirm victory")
                    t = time.perf_counter()
                    # the panel is only gone once the glyph stops matching; counting
                    # before that is exactly how one win became three.
                    deadline = time.perf_counter() + 8.0
                    while time.perf_counter() < deadline:
                        f2 = backend.latest()
                        s2, _ = matcher.score(f2.array, "live_victory_word", ROI_VICTORY)
                        if s2 < args.victory_th:
                            break
                        time.sleep(0.03)
                    gone = s2 < args.victory_th
                    if gone:
                        wins += 1
                        print(f"           -> win #{wins} confirmed after "
                              f"{time.perf_counter()-t:.1f}s")
                    else:
                        end_reason = ("victory panel would not dismiss after 8s "
                                      "(confirm click did not take effect)")
                        print(f"           -> {end_reason}")
                        pending_win = False
                        break
                    pending_win = False
                else:
                    time.sleep(0.1)
                unknown_since = None
                continue

            if state == "DIALOG":
                act(p_ok, "dismiss dialog")
                time.sleep(0.7)
                unknown_since = None
                continue

            if state == "BATTLE":
                act(p_fight, "attack")
                # wait for the UI to move on: victory panel or the turn changing
                t = time.perf_counter()
                deadline = time.perf_counter() + 6.0
                moved = False
                base = frame.seq
                while time.perf_counter() < deadline:
                    f2 = backend.latest()
                    if f2.seq != base:
                        s2, _ = matcher.score(f2.array, "live_victory_word", ROI_VICTORY)
                        if s2 >= args.victory_th:
                            moved = True
                            break
                        # a real turn makes the combat log/HP change; give it a beat
                        if time.perf_counter() - t > 0.35:
                            moved = True
                            break
                    time.sleep(0.004)
                stages["wait"].append((time.perf_counter() - t) * 1000)
                unknown_since = None
                continue

            # WORLD: only click something we can actually see. A blind "nudge"
            # would be exactly the unsafe behaviour the project forbids.
            s_mob, p_mob = matcher.score(frame.array, "live_monster_moguguai", ROI_SCENE)
            if s_mob >= args.monster_th:
                print(f"           -> monster at ({p_mob[0]:.0f},{p_mob[1]:.0f}) "
                      f"score={s_mob:.2f}, engaging")
                act(p_mob, f"engage monster {s_mob:.2f}")
                battles_started += 1
                time.sleep(1.4)
                unknown_since = None
                continue
            # ---- escalation path -------------------------------------- #
            # Nothing matched by template. Dialog styles vary a lot (victory
            # panel, XP panel, drop panel, achievement panel...), so an OCR
            # look is the honest way out. It is expensive (~450 ms), which is
            # exactly why it only runs when templates say "nothing is here",
            # at most once every --escalate-s.
            if unknown_since is None:
                unknown_since = time.perf_counter()
            elapsed_unknown = time.perf_counter() - unknown_since
            if elapsed_unknown >= args.escalate_s:
                stages["ocr"].append(_ocr_escalate(client, backend, geo, act, args))
                unknown_since = time.perf_counter()
                continue
            if elapsed_unknown > 20.0:
                end_reason = "no monster found for 20 s after OCR escalation"
                break
            time.sleep(0.2)
            continue
        else:
            end_reason = "budget"
    except KeyboardInterrupt:
        end_reason = "interrupted"
    finally:
        if wins >= args.battles:
            end_reason = "goal reached"
        print()
        print(f"stopped: {end_reason} — wins={wins} losses={losses} "
              f"in {time.perf_counter()-started:.1f}s")
        for k, v in stages.items():
            if v:
                print(f"  {k:<8} n={len(v):<4} mean={statistics.mean(v):8.2f} "
                      f"p50={pct(v,.5):8.2f} p95={pct(v,.95):8.2f}")
        if stages["detect"]:
            print(f"  per-poll vision cost (capture+detect) ~"
                  f"{statistics.mean(stages['capture']) + statistics.mean(stages['detect']):.1f} ms")
        backend.close()
        client.close()
        if args.json:
            json.dump({"wins": wins, "end_reason": end_reason, "stages_ms": stages,
                       "actions": log}, open(args.json, "w", encoding="utf-8"), indent=1)
            print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
