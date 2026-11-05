#!/usr/bin/env python3
"""`farm.tower.repeat` — a standing 试炼之塔 grind, as distinct from the one-shot node.

Why this exists: `puni.prepare.tower_farm` is a goal node, and a goal node completes.
The first time a floor falls the node is done, so it can never be the thing that
produces the *millions* of experience points the account actually needs.  Farming is
a different shape of work: it repeats, it is measured, and it stops for a reason.

What one round is:

    1. read the painted floor number (before)
    2. fight exactly one floor guardian          <- game.farm_tower
    3. read any reward dialog's own numbers      <- parse_reward (measured XP)
    4. read the painted floor number again (after)

Everything the round produced or spent is appended to a JSONL ledger, so a night can
be audited afterwards without re-reading a log.  Kill it unrecoverably in the middle
and the ledger still holds every round that finished.

Stop conditions, all deliberate:

    --deadline-at / --minutes   absolute time, never rolled forward
    --max-rounds                batch budget
    --max-no-work               consecutive rounds that cleared nothing
    --max-failures              consecutive rounds that errored
    /tmp/seerauto.pause         cooperative pause: read-only, then wait
    /tmp/seerauto.stop          cooperative stop: finish the round, then exit

This tool drives the *foreground* input lane, because `tools/bg_probe.py` proved the
PID lane cannot reach the PepperFlash canvas (same coordinates, foreground works,
clickpid does not).  Do not run it while the machine is in use.

    PYTHONPATH=.pydeps python3 tools/farm_tower_repeat.py --max-rounds 3 --dry-run
    PYTHONPATH=.pydeps python3 tools/farm_tower_repeat.py \
        --deadline-at 2026-11-06T06:30:00+08:00 --max-rounds 200 --max-no-work 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.farm.ledger import (  # noqa: E402
    ROUND_OK,
    Guards,
    RoundRecord,
    TowerLedger,
)
from seerauto.skills.act import build_act_skills  # noqa: E402
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.world.model import GameWorldState  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_DIR = os.path.join(ROOT, "data", "overnight")
LEDGER_PATH = os.path.join(LEDGER_DIR, "tower_ledger.jsonl")
FRAME_DIR = os.path.join(LEDGER_DIR, "frames")
STATE_PATH = os.path.join(LEDGER_DIR, "tower_state.json")
PAUSE_FILE = "/tmp/seerauto.pause"
STOP_FILE = "/tmp/seerauto.stop"
#: Measured 2026-11-04: one floor pays this much 积累经验.  An *estimate*, used only
#: for the headline number — the ledger keeps measured reward dialogs separately.
XP_PER_FLOOR_ESTIMATE = 1300.0


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_previous_rounds(path: str = LEDGER_PATH) -> list[dict]:
    """Resume support: the ledger is append-only, so a restart can count from it."""

    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001 - a half-written last line is not fatal
                continue
    return out


def append_round(record: RoundRecord, path: str = LEDGER_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(FRAME_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def write_state(ledger: TowerLedger, extra: dict | None = None) -> None:
    os.makedirs(LEDGER_DIR, exist_ok=True)
    payload = ledger.summary()
    payload.update(extra or {})
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def parse_deadline(raw: str | None) -> datetime | None:
    if not raw:
        return None
    deadline = datetime.fromisoformat(raw)
    if deadline.tzinfo is not None:
        # The ledger compares against naive local time.
        deadline = deadline.astimezone().replace(tzinfo=None)
    return deadline


def read_rewards(session) -> list[dict]:
    """Any reward dialog currently on screen, parsed as (kind, amount) pairs."""

    try:
        from seerauto.skills.parsers import parse_reward

        return parse_reward(session.ocr(min_conf=0.25))
    except Exception:  # noqa: BLE001 - measurement is optional, never fatal
        return []


def travel_to_tower(skills, ctx, destination: str = "tutor_room") -> bool:
    """Get to the room that holds the tower before fighting anything.

    The guardian click is a fixed pixel in the tower; from any other scene it lands
    on scenery and the round dies with "guardian click did not start a battle".
    Arrival is checked by the skill itself, not by a sleep.
    """

    skill = skills.get("game.travel")
    if skill is not None:
        try:
            res = skill(ctx, destination=destination)
        except Exception as exc:  # noqa: BLE001
            log(f"travel raised: {exc}")
            res = None
        if res is not None:
            ok = bool((res.after or {}).get("arrived")) and res.status.value == "PASS"
            log(f"travel {destination}: {res.status.value} "
                f"{(res.proof or res.reason or '')[:90]}")
            if ok:
                return True
    # The map path is the cheap route; the dedicated navigation skill is the retry.
    # Both are verified skills, so a failure here means the client is not where the
    # tool thinks it is, and the caller should be told instead of clicking scenery.
    fallback = skills.get("game.goto_tutor_room")
    if fallback is not None and destination == "tutor_room":
        try:
            res = fallback(ctx)
        except Exception as exc:  # noqa: BLE001
            log(f"goto_tutor_room raised: {exc}")
            return False
        ok = res.status.value == "PASS"
        log(f"goto_tutor_room fallback: {res.status.value} "
            f"{(res.proof or res.reason or '')[:90]}")
        return ok
    return False


def enter_tower_and_read_floor(skills, ctx) -> int | None:
    """Enter the tower interior and read the painted floor number.

    This is not optional bookkeeping: ``game.tower_status`` is the only skill that
    clicks the tower entrance (``ReadTowerFloor.TOWER_ENTRY``).  ``game.travel``
    stops at the training *room*; clicking the guardian's fixed pixel from there hits
    scenery, which is exactly how four ledger rounds were lost on 2026-11-05 with
    "guardian click did not start a battle" while the client sat in the lobby.
    """

    skill = skills.get("game.tower_status")
    if skill is None:
        log("game.tower_status is not registered; cannot enter the tower")
        return None
    try:
        res = skill(ctx)
    except Exception as exc:  # noqa: BLE001
        log(f"tower_status raised: {exc}")
        return None
    changed = bool(getattr(res, "changed", False))
    log(f"enter tower: {res.status.value} {(res.proof or res.reason or '')[:100]}")
    if res.status.value not in ("PASS", "SKIPPED") and not changed:
        return None
    value = (getattr(res, "after", None) or {}).get("floors")
    if value is None:
        value = (getattr(res, "after", None) or {}).get("tower_floor")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def one_round(*, index: int, skills, ctx, floors: int, read_floor_wanted: bool,
              dry_run: bool, control=None) -> RoundRecord:
    """One floor-farming pass.  The floor read is opt-in because it is expensive.

    Reading the painted floor number costs a VLM round-trip (~40 s measured) and it
    is only INFERRED evidence, so it is not the per-round progress signal: a won
    battle is.  The read is used to bracket the session, where a before/after pair
    is worth its cost.
    """

    rec = RoundRecord(index=index, started_at=datetime.now().astimezone().isoformat(
        timespec="seconds"))
    try:
        if read_floor_wanted and not dry_run:
            rec.tower_floor_before = enter_tower_and_read_floor(skills, ctx)
        farm = skills.get("game.farm_tower")
        res = farm(ctx, floors=floors, max_seconds=240.0)
        rec.floors_won = int((res.after or {}).get("wins")
                             or (res.after or {}).get("floors_won") or 0)
        if res.reason:
            rec.notes.append(res.reason[:200])
        if res.status.value not in ("PASS", "SKIPPED"):
            # A refusal (no input / dry run / wrong scene) is BLOCKED, not "it ran and
            # found nothing".  Keeping them apart is what stops a NO_WORK guard from
            # firing on a configuration problem.
            if res.status.value in ("BLOCKED", "SKIPPED") or "refused" in (
                    res.reason or "").lower():
                rec.blocked = True
            rec.error = f"farm_tower {res.status.value}: {res.reason[:160]}"
        # Prefer what the battle itself measured; only fall back to the screen.
        rewards = list((res.after or {}).get("rewards") or [])
        if not rewards:
            rewards = read_rewards(ctx.session)
        rec.xp_events = rewards
        measured = float((res.after or {}).get("xp") or 0.0)
        from_dialogs = sum(float(r.get("amount") or 0) for r in rewards
                           if "经验" in str(r.get("kind") or ""))
        rec.xp_income = measured or from_dialogs
        if read_floor_wanted and not dry_run:
            rec.tower_floor_after = enter_tower_and_read_floor(skills, ctx)
        if control is not None and control.paused():
            # The round was cut short by the user, which is not a failure.
            rec.interrupted = True
        # Keep the frame every round: a ledger number nobody can look at is a claim,
        # not evidence.  One shot per round is ~0.5 s against a ~30 s round.
        try:
            path = os.path.join(FRAME_DIR, f"round_{index:03d}.png")
            ctx.session.client.send("shot", path=path)
            rec.notes.append(f"frame {path}")
        except Exception:  # noqa: BLE001 - evidence is best-effort, not fatal
            pass
        if rec.floors_won and not rec.xp_income:
            rec.notes.append(
                f"xp not measured on screen; estimate {rec.floors_won * XP_PER_FLOOR_ESTIMATE:.0f}")
    except Exception as exc:  # noqa: BLE001 - one bad round must not end the night
        rec.error = f"{type(exc).__name__}: {exc}"
    rec.ended_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return rec


def pause_requested(control) -> bool:
    return control.paused()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1, help="floors per round")
    ap.add_argument("--max-rounds", type=int, default=0, help="0 = until another bound")
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--deadline-at", default=None,
                    help="absolute ISO time with tz, e.g. 2026-11-06T06:30:00+08:00")
    ap.add_argument("--max-no-work", type=int, default=2)
    ap.add_argument("--max-failures", type=int, default=3)
    ap.add_argument("--destination", default="tutor_room",
                    help="map destination that holds the tower (default tutor_room)")
    ap.add_argument("--no-travel", action="store_true",
                    help="assume the client is already inside the tower")
    ap.add_argument("--floor-read-every", type=int, default=0,
                    help="read the painted floor number every N rounds (0 = only the "
                         "first and last round); each read is a slow VLM call")
    ap.add_argument("--dry-run", action="store_true",
                    help="observe and account only; never send input")
    ap.add_argument("--background", action="store_true",
                    help="use the PID lane (proven NOT to reach this client; kept "
                         "only so a caller gets a truthful refusal instead of silence)")
    ap.add_argument("--pause-file", default=PAUSE_FILE)
    ap.add_argument("--stop-file", default=STOP_FILE)
    args = ap.parse_args()

    deadline = parse_deadline(args.deadline_at)
    if deadline is not None and deadline <= datetime.now():
        ap.error("--deadline-at is not in the future; refusing to roll it forward")
    if args.minutes:
        from datetime import timedelta
        deadline = min(filter(None, [deadline, datetime.now() + timedelta(
            minutes=args.minutes)]), default=None)

    if args.background and not args.dry_run:
        log("REFUSED: the PID input lane cannot reach the PepperFlash canvas "
            "(evidence: data/overnight/bg_probe.json). Use the foreground lane "
            "while nobody is using the machine, or stay paused.")
        return 6

    from seerauto.live import LiveSession
    from seerauto.runner.control import RunControl

    control = RunControl(args.pause_file, args.stop_file)
    session = LiveSession(dry_run=args.dry_run,
                          enable_input=not args.dry_run,
                          background=args.background).open()
    ledger = TowerLedger(
        guards=Guards(max_rounds=args.max_rounds, max_minutes=args.minutes,
                      max_no_work=args.max_no_work, max_failures=args.max_failures),
        started_at=datetime.now())
    previous = load_previous_rounds()
    if previous:
        log(f"ledger already holds {len(previous)} rounds "
            f"({sum(int(r.get('floors_won') or 0) for r in previous)} floors won in total)")

    skills = {s.spec.id: s for s in build_act_skills()}
    world = GameWorldState()
    ctx = SkillContext(session=session, world=world, dry_run=args.dry_run,
                       allow_input=not args.dry_run, budget=Budget(max_duration_s=300.0,
                                                                   max_actions=120),
                       options={"control": control})
    index = len(previous)
    if not args.no_travel and not args.dry_run:
        if not travel_to_tower(skills, ctx, args.destination):
            log("cannot reach the tower; refusing to burn the night clicking scenery")
            ledger.stopped_because = "cannot reach the training room"
            write_state(ledger, {"dry_run": args.dry_run})
            session.close()
            return 11
    if not args.no_travel and not args.dry_run:
        floor_now = enter_tower_and_read_floor(skills, ctx)
        log(f"tower entry probe: floor={floor_now}")
    try:
        while True:
            if control.stop_requested():
                ledger.stopped_because = "stop requested"
                log("stop requested -> finishing without a new round")
                break
            if pause_requested(control):
                # 「暂停，保存」 is a contract: freeze inputs, save, and *wait*.
                # Exiting here (the first version did) turned a pause into a failure
                # with no checkpoint, and the caller could not tell the two apart.
                log("PAUSED: no new round starts while the pause file exists")
                ledger.stopped_because = "paused by user"
                write_state(ledger, {"dry_run": args.dry_run, "status": "paused"})
                waited = control.wait_while_paused()
                if waited <= 0:
                    log("pause released immediately; continuing")
                else:
                    log(f"RESUMED after {waited:.1f}s; re-observing before the next round")
                ledger.stopped_because = ""
                if not args.no_travel:
                    travel_to_tower(skills, ctx, args.destination)
                continue
            keep, why = ledger.should_continue(now=datetime.now(), deadline=deadline)
            if not keep:
                ledger.stopped_because = why
                log(f"stopping: {why}")
                break
            index += 1
            log(f"round {index}: farm {args.rounds} floor(s)")
            # Bracket the session, not every round: the read costs a VLM call and is
            # INFERRED evidence.  `floors_won` is the per-round truth.
            first_or_last = (index == 1
                             or (args.max_rounds and index == args.max_rounds)
                             or (args.floor_read_every
                                 and index % args.floor_read_every == 0))
            rec = one_round(index=index, skills=skills, ctx=ctx, floors=args.rounds,
                            read_floor_wanted=first_or_last, dry_run=args.dry_run,
                            control=control)
            ledger.add(rec)
            append_round(rec)
            write_state(ledger, {"dry_run": args.dry_run})
            if (not args.no_travel and not args.dry_run
                    and ledger.failure_streak == 2):
                log("two rounds failed in a row; re-travelling before the next attempt")
                travel_to_tower(skills, ctx, args.destination)
            log(f"round {index}: {rec.outcome} won={rec.floors_won} "
                f"xp={rec.xp_income:.0f} floor {rec.tower_floor_before}"
                f"->{rec.tower_floor_after}"
                + (f" error={rec.error[:80]}" if rec.error else ""))
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    write_state(ledger, {"dry_run": args.dry_run})
    s = ledger.summary()
    log(f"done: rounds={s['rounds']} productive={s['productive_rounds']} "
        f"floors={s['floors_won']} xp_measured={s['xp_income']:.0f} "
        f"stopped={s['stopped_because'] or 'none'}")
    if (s["stopped_because"] or "").startswith("paused"):
        return 20          # paused != failed; a caller must be able to tell them apart
    if s["stopped_because"] == "stop requested":
        return 20
    return 0 if s["floors_won"] or s["rounds"] == 0 else 10


if __name__ == "__main__":
    raise SystemExit(main())
