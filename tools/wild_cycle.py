#!/usr/bin/env python3
"""wild_cycle — the verified 蘑菇怪 grind, kept alive by healing on a timer.

Why this exists after the tower: the 试炼之塔 paid 25x more per floor and cleared 8 floors
live (第15→23层), but it is a *climb* and the guardian at floor 23+ wins every fight
(「很遗憾，这次挑战未能获胜」).  There is no verified way to restart lower, so the tower
cannot be the thing that finishes a multi-hour night.  The wild loop can:
26.8 s/battle, 106 XP each, 11 wins in a row on the Round-4 record.

The one thing that actually stops this loop is HP: at 0/117 no battle starts at all and
every skill reports "no engageable monster".  So the cycle heals *first* and re-heals on
a battle count, not on a feeling.

    PYTHONPATH=.pydeps python3 tools/wild_cycle.py \
        --deadline-at 2026-11-06T06:30:00+08:00 --battles 3 --heal-every 3

Exit codes: 0 progress, 10 no progress, 20 paused/stopped, 11 blocked.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.skills.act import build_act_skills  # noqa: E402
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.world.model import GameWorldState  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "overnight")
LOG = os.path.join(OUT, "wild_cycle.jsonl")
FRAMES = os.path.join(OUT, "frames")
PAUSE, MASTER, STOP = "/tmp/seerauto.pause", "/tmp/seerauto.master.pause", "/tmp/seerauto.stop"

#: The spot Round 4 proved, and the boss that lives there.
BOSS = "蘑菇怪"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def paused() -> bool:
    return os.path.exists(PAUSE) or os.path.exists(MASTER)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--battles", type=int, default=3, help="battles per pass")
    ap.add_argument("--heal-every", type=int, default=3, help="heal after this many passes")
    ap.add_argument("--max-passes", type=int, default=0)
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--deadline-at", default=None)
    ap.add_argument("--max-no-progress", type=int, default=3, help="passes with 0 wins")
    args = ap.parse_args()

    deadline = None
    if args.deadline_at:
        raw = datetime.fromisoformat(args.deadline_at)
        deadline = raw.astimezone().replace(tzinfo=None) if raw.tzinfo else raw
        if deadline <= datetime.now():
            ap.error("--deadline-at is not in the future")
    if args.minutes:
        cap = datetime.now() + timedelta(minutes=args.minutes)
        deadline = min(filter(None, [deadline, cap]), default=None)

    os.makedirs(FRAMES, exist_ok=True)
    from seerauto.live import LiveSession

    session = LiveSession(dry_run=False, enable_input=True).open()
    session.client.send("activate")
    time.sleep(0.8)
    skills = {s.spec.id: s for s in build_act_skills()}
    ctx = SkillContext(session=session, world=GameWorldState(), dry_run=False,
                       allow_input=True,
                       budget=Budget(max_duration_s=600, max_actions=300))
    started = datetime.now()
    total_wins = 0
    total_xp = 0.0
    dry_passes = 0
    passes = 0
    level_seen = None
    try:
        while True:
            if os.path.exists(STOP):
                log("stop requested")
                return 20
            if paused():
                log("paused by user; not starting another pass")
                return 20
            now = datetime.now()
            if deadline and now >= deadline:
                log(f"deadline {deadline:%H:%M} reached")
                break
            if args.max_passes and passes >= args.max_passes:
                log(f"pass budget {args.max_passes} reached")
                break
            passes += 1
            record = {"pass": passes, "at": now.astimezone().isoformat(timespec="seconds")}
            if passes == 1 or (passes - 1) % max(1, args.heal_every) == 0:
                h = skills["game.heal_pet"](ctx)
                hp = (h.after or {}).get("hp_after") or []
                lead = hp[0] if hp else {}
                record["heal"] = {"status": h.status.value, "hp": lead.get("hp"),
                                  "hp_max": lead.get("hp_max")}
                log(f"heal: {h.status.value} lead {lead.get('hp')}/{lead.get('hp_max')}")
                # A FAIL whose HP read shows a full pet is not a problem: the heal
                # button simply has nothing to do.  Only a genuinely low/unknown HP is.
                full = (lead.get("hp") is not None and lead.get("hp_max")
                        and lead["hp"] >= lead["hp_max"])
                if h.status.value != "PASS" and not full:
                    dry_passes += 1
                    record["outcome"] = "NO_WORK"
                    _append(record)
                    if dry_passes >= args.max_no_progress:
                        log("cannot heal; stopping rather than fight at 0 HP")
                        return 11
                    continue
            nav = skills["game.navigate_spt_boss"](ctx, boss=BOSS)
            record["navigate"] = nav.status.value
            if nav.status.value != "PASS":
                log(f"navigate {nav.status.value}: {(nav.reason or '')[:120]}")
                dry_passes += 1
                record["outcome"] = "NO_WORK"
                _append(record)
                if dry_passes >= args.max_no_progress:
                    return 11
                continue
            t0 = time.time()
            res = skills["game.run_battle"](ctx, battles=args.battles, monster=BOSS,
                                            max_seconds=360.0)
            wins = int((res.after or {}).get("wins") or 0)
            xp = float((res.after or {}).get("xp") or 0.0)
            total_wins += wins
            total_xp += xp
            record.update({"status": res.status.value, "wins": wins, "xp": xp,
                           "seconds": round(time.time() - t0, 1),
                           "reason": (res.reason or "")[:160],
                           "outcome": "PROGRESS" if wins else "NO_WORK"})
            log(f"pass {passes}: {wins} win(s) +{xp:.0f} xp in {time.time()-t0:.0f}s "
                f"(total {total_wins} wins, {total_xp:.0f} xp)")
            if wins == 0:
                dry_passes += 1
            else:
                dry_passes = 0
            lvl = skills["game.read_active_pet_level"](ctx)
            if lvl.status.value == "PASS":
                level_seen = lvl.after
                record["level"] = level_seen
                log(f"level: {level_seen}")
            try:
                path = os.path.join(FRAMES, f"wild_{passes:03d}.png")
                session.client.send("shot", path=path)
                record["frame"] = path
            except Exception:  # noqa: BLE001
                pass
            _append(record)
            if dry_passes >= args.max_no_progress:
                log(f"{dry_passes} passes produced nothing; stopping")
                break
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    summary = {"passes": passes, "wins": total_wins, "xp": total_xp,
               "level": level_seen,
               "minutes": round((datetime.now() - started).total_seconds() / 60, 1)}
    with open(os.path.join(OUT, "wild_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    log(f"done: {summary}")
    return 0 if total_wins else 10


def _append(record: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
