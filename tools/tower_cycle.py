#!/usr/bin/env python3
"""tower_cycle — keep the 试炼之塔 grind alive across rooms, heals and ejections.

What the live session taught (2026-11-05/06), each point observed on screen:

  * The tower refuses a challenge when the lead pet is not at full HP:
    「精灵不符合排位要求，数量不足或血量异常」.  Healing to full made the very same
    floor clear immediately, so HP is the gate — not level.
  * A floor pays real progress: 8 floors cleared (第15→23层) and the pet went from
    Lv36 to Lv37 with max HP 108→117.
  * After a while the run is ejected back to a station room.  That room has a bottom
    bar, so the pet can be healed there **without restarting the client** — restarting
    is what produced the 500033 / 「此次连接已经断开」 loop earlier in the night.
  * The tower is entered either from the training room's machine (1195,1150) or from
    the orb that stands in the station arcade (1434,875).  Which one is reachable
    depends on where the ejection dropped us, so both are tried, and success is the
    dialog appearing — not a coordinate.

So one cycle is: heal (if a bottom bar is reachable) → open the tower dialog →
「继续挑战」 → fight K floors → book the result.  Repeat until the guards stop it.

    PYTHONPATH=.pydeps python3 tools/tower_cycle.py --cycles 20 --floors 3
    PYTHONPATH=.pydeps python3 tools/tower_cycle.py --deadline-at 2026-11-06T06:30:00+08:00

Exit codes: 0 progress, 10 no progress, 20 paused/stopped by the user, 11 blocked.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.farm.ledger import Guards, RoundRecord, TowerLedger  # noqa: E402
from seerauto.skills.act import (  # noqa: E402
    _bottom_bar_visible,
    build_act_skills,
)
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.world.model import GameWorldState  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "overnight")
LEDGER = os.path.join(OUT, "tower_ledger.jsonl")
STATE = os.path.join(OUT, "tower_state.json")
FRAMES = os.path.join(OUT, "frames")
PAUSE = "/tmp/seerauto.pause"
MASTER_PAUSE = "/tmp/seerauto.master.pause"
STOP = "/tmp/seerauto.stop"

#: Entrance points for the tower dialog, in the rooms it has been found in.
ENTRANCES = ((1195, 1150), (1434, 875))
DIALOG_MARKERS = ("挑战试炼之塔", "继续挑战")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def paused() -> bool:
    return os.path.exists(PAUSE) or os.path.exists(MASTER_PAUSE)


def open_tower_dialog(skills, ctx) -> bool:
    """Get the 挑战试炼之塔 dialog up, from wherever we are."""

    def dialog_up() -> bool:
        try:
            text = " ".join(str(i.get("text")) for i in ctx.session.ocr(min_conf=0.2))
        except Exception:  # noqa: BLE001
            return False
        return any(m in text for m in DIALOG_MARKERS)

    if dialog_up():
        return True
    # Try the CURRENT room first: a tower ejection drops the character next to the orb
    # that reopens the dialog, so travelling away from it (the first version did) is
    # exactly the wrong move.
    for point in ENTRANCES:
        if dialog_up():
            return True
        try:
            ctx.session.click_px(point[0], point[1], f"点击试炼之塔入口 {point}")
        except Exception as exc:  # noqa: BLE001
            log(f"entrance {point} refused: {exc}")
            continue
        time.sleep(2.2)
        if dialog_up():
            return True
    # Last resort: go to the training room, which has a map and an entrance.
    try:
        skills["game.travel"](ctx, destination="tutor_room")
    except Exception as exc:  # noqa: BLE001
        log(f"travel: {exc}")
    for point in ENTRANCES:
        if dialog_up():
            return True
        try:
            ctx.session.click_px(point[0], point[1], f"点击试炼之塔入口 {point}")
        except Exception as exc:  # noqa: BLE001
            log(f"entrance {point} refused: {exc}")
            continue
        time.sleep(2.2)
    return dialog_up()


def one_cycle(skills, ctx, floors: int, max_seconds: float, index: int) -> RoundRecord:
    rec = RoundRecord(index=index, started_at=datetime.now().astimezone().isoformat(
        timespec="seconds"))
    try:
        if _bottom_bar_visible(ctx.session):
            heal = skills.get("game.heal_pet")
            if heal is not None:
                res = heal(ctx)
                hp = (res.after or {}).get("hp_after") or []
                lead = hp[0] if hp else {}
                log(f"heal: {res.status.value} lead hp {lead.get('hp')}/{lead.get('hp_max')}")
                if lead and lead.get("hp") is not None and lead.get("hp_max"):
                    if lead["hp"] < lead["hp_max"]:
                        rec.notes.append("lead pet still not at full HP after healing")
        if not open_tower_dialog(skills, ctx):
            rec.blocked = True
            rec.error = "tower dialog did not open (no known entrance in this room)"
            return rec
        enter = skills["game.enter_tower"](ctx)
        if enter.status.value == "BLOCKED":
            rec.blocked = True
            rec.error = f"enter_tower blocked: {enter.reason[:120]}"
            return rec
        if not _bottom_bar_visible(ctx.session):
            log("in the tower interior; farming")
        res = skills["game.farm_tower"](ctx, floors=floors, max_seconds=max_seconds)
        rec.floors_won = int((res.after or {}).get("wins")
                             or (res.after or {}).get("floors_won") or 0)
        rec.xp_events = list((res.after or {}).get("rewards") or [])
        rec.xp_income = float((res.after or {}).get("xp") or 0.0)
        rec.tower_floor_after = (res.after or {}).get("tower_floor")
        if res.reason:
            rec.notes.append(res.reason[:180])
        if res.status.value not in ("PASS",):
            rec.blocked = res.status.value == "BLOCKED"
        try:
            path = os.path.join(FRAMES, f"cycle_{index:03d}.png")
            ctx.session.client.send("shot", path=path)
            rec.notes.append(f"frame {path}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        rec.error = f"{type(exc).__name__}: {exc}"
    rec.ended_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=0, help="0 = until another bound")
    ap.add_argument("--floors", type=int, default=3, help="floors per cycle")
    ap.add_argument("--seconds", type=float, default=420.0, help="time cap per cycle")
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--deadline-at", default=None)
    ap.add_argument("--max-no-work", type=int, default=3)
    ap.add_argument("--max-failures", type=int, default=4)
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
                       budget=Budget(max_duration_s=args.seconds, max_actions=200))
    ledger = TowerLedger(guards=Guards(max_rounds=args.cycles,
                                       max_no_work=args.max_no_work,
                                       max_failures=args.max_failures),
                         started_at=datetime.now())
    # Continue the numbering from whatever is already in the ledger.
    index = 0
    if os.path.exists(LEDGER):
        index = sum(1 for line in open(LEDGER, encoding="utf-8") if line.strip())
    try:
        while True:
            if os.path.exists(STOP):
                ledger.stopped_because = "stop requested"
                break
            if paused():
                ledger.stopped_because = "paused by user"
                log("paused: no new cycle starts")
                break
            keep, why = ledger.should_continue(now=datetime.now(), deadline=deadline)
            if not keep:
                ledger.stopped_because = why
                log(f"stopping: {why}")
                break
            index += 1
            log(f"cycle {index}: heal → tower → {args.floors} floor(s)")
            rec = one_cycle(skills, ctx, args.floors, args.seconds, index)
            ledger.add(rec)
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
            with open(STATE, "w", encoding="utf-8") as fh:
                payload = ledger.summary()
                payload["updated_at"] = datetime.now().astimezone().isoformat(
                    timespec="seconds")
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            log(f"cycle {index}: {rec.outcome} floors={rec.floors_won} "
                f"at={rec.tower_floor_after}"
                + (f" err={rec.error[:70]}" if rec.error else ""))
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    s = ledger.summary()
    log(f"done: cycles={s['rounds']} floors={s['floors_won']} "
        f"stopped={s['stopped_because'] or 'none'}")
    if (s["stopped_because"] or "").startswith(("paused", "stop")):
        return 20
    return 0 if s["floors_won"] else 10


if __name__ == "__main__":
    raise SystemExit(main())
