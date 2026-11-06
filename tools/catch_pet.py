#!/usr/bin/env python3
"""`catch_pet` — one bounded CAP-CATCH session (throw capsules, prove the result).

    # what is calibrated yet?  (never opens a client, never clicks)
    PYTHONPATH=.pydeps python3 tools/catch_pet.py --dry-run
    PYTHONPATH=.pydeps python3 tools/catch_pet.py --dry-run --target pippi

    # the real thing, only after the live agent filled in the calibration
    PYTHONPATH=.pydeps python3 tools/catch_pet.py --target pippi --max-capsules 5 \
        --allow-input --deadline-at 2026-11-07T07:00:00+08:00

The skill (`seerauto.skills.catch.CatchPet`) refuses unless the calibration is
complete and `CALIBRATED`, refuses unless the live capture matches the calibrated
`reference.capture_px`, and never throws without result-text evidence.  This CLI is
only glue: argparse, a LiveSession, the ledger path, and a machine-readable result.

The session assumes the character is **already in a battle with the target** (the
verified battle skills own navigation/hunt; SPEC §2 Phase 1 reuses them).  If the
battle report does not parse, it does not click.

Safety: there is deliberately no flag for buying capsules, releasing a pet, selling,
trading or moving pets out of the bag.  The skill's click allowlist and word denylist
make those actions unreachable from here even if a UI happens to show them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.dual.outcome import Outcome  # noqa: E402
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.skills.catch import (DEFAULT_CALIBRATION, DEFAULT_LEDGER,  # noqa: E402
                                   HARD_MAX_THROWS, CatchLimits, CatchPet,
                                   CalibrationError, calibration_problems,
                                   load_calibration)
from seerauto.world.model import GameWorldState  # noqa: E402

EXIT_CODES = {
    Outcome.PROGRESS.value: 0,
    Outcome.NO_WORK.value: 3,
    Outcome.FAILED.value: 1,
    Outcome.UNVERIFIED_ACTIVITY.value: 4,
    Outcome.BLOCKED.value: 7,
    Outcome.DEADLINE_REACHED.value: 124,
    Outcome.PAUSED.value: 130,
    Outcome.INTERRUPTED.value: 130,
}

PLAN = [
    "PRECHECK: two consecutive OCR readings of the battle report; confirm target "
    "name and level from the calibrated regex; wrong/unreadable target => no throw",
    "ITEM_PANEL: click the calibrated 道具 button once; require two consecutive "
    "panel-marker readings; no repeat click if it does not open",
    "THROW: click the calibrated capsule slot, require the selection marker, then "
    "click the calibrated 使用/投掷 button once",
    "VERDICT: two consecutive agreeing result-text readings from the calibrated ROI "
    "(success / escaped / fled / defeated); no evidence => UNVERIFIED_ACTIVITY",
    "CLEANUP: dismiss result dialogs by clicking only calibrated confirm words",
    "VERIFY: re-read the capsule count when the inventory ROI is calibrated; record "
    "the delta in data/overnight/catch_ledger.jsonl",
]


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def dry_run_payload(args: argparse.Namespace) -> dict:
    info = calibration_problems(args.calibration)
    targets = info.get("targets") or []
    target_ok = not targets or args.target in targets
    payload = {
        "dry_run": True,
        "starts_nothing": True,
        "calibration": info,
        "target": args.target,
        "target_known": target_ok,
        "max_capsules_requested": args.max_capsules,
        "max_capsules_effective": min(int(args.max_capsules), HARD_MAX_THROWS),
        "plan": PLAN,
        "safety": {
            "click_allowlist": ["道具 button", "capsule slot", "使用/投掷 button",
                                "known confirm words only"],
            "never": ["放生", "出售", "交易", "购买/商城", "丢弃", "还原", "重组",
                      "融合", "赋形", "收藏", "展示", "放回仓库/放入背包"],
            "capsule_budget": f"<= {HARD_MAX_THROWS} throws, >= 1 capsule kept in "
                              f"reserve, stop after 3 failed attempts",
        },
        "ledger": str(args.ledger),
    }
    if info.get("calibrated"):
        payload["command"] = (
            f"PYTHONPATH=.pydeps python3 tools/catch_pet.py --target {args.target} "
            f"--max-capsules {min(int(args.max_capsules), HARD_MAX_THROWS)} "
            f"--allow-input")
    else:
        payload["blocked_reason"] = ("calibration is not CALIBRATED; fill the "
                                     "required_fields in the calibration file and "
                                     "set status=CALIBRATED")
    return payload


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Bounded CAP-CATCH session: 道具 -> 胶囊 -> 投掷 -> evidence")
    ap.add_argument("--target", default="pippi",
                    help="calibrated target key (default pippi)")
    ap.add_argument("--max-capsules", type=int, default=HARD_MAX_THROWS,
                    help=f"hard cap on throws this session (<= {HARD_MAX_THROWS})")
    ap.add_argument("--deadline-at", default=None,
                    help="absolute ISO-8601 deadline with offset")
    ap.add_argument("--calibration", default=str(DEFAULT_CALIBRATION))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--allow-input", action="store_true",
                    help="required for the real path: this clicks and spends capsules")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan + calibration gaps and exit; starts nothing")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_capsules < 1 or args.max_capsules > HARD_MAX_THROWS:
        emit({"ok": False, "error": f"--max-capsules must be 1..{HARD_MAX_THROWS}, "
                                    f"got {args.max_capsules}"})
        return 2

    if args.dry_run:
        emit(dry_run_payload(args))
        return 0

    try:
        calibration = load_calibration(args.calibration, require_calibrated=True)
    except CalibrationError as exc:
        emit({"ok": False, "error": "catch calibration unusable",
              "problems": exc.problems, "calibration": str(exc.path or ""),
              "hint": "run --dry-run for the required fields; do not guess coordinates"})
        return 3
    if not args.allow_input:
        emit({"ok": False,
              "error": "refusing to click: pass --allow-input to spend capsules",
              "calibration_ok": True})
        return 3
    deadline_epoch = None
    if args.deadline_at:
        try:
            deadline = datetime.fromisoformat(args.deadline_at)
        except ValueError as exc:
            emit({"ok": False, "error": f"--deadline-at is not ISO-8601: {exc}"})
            return 2
        if deadline.tzinfo is None:
            emit({"ok": False, "error": "--deadline-at must carry a timezone offset"})
            return 2
        deadline_epoch = deadline.timestamp()

    try:
        from seerauto.live import LiveSession
    except Exception as exc:  # noqa: BLE001 - vision deps missing is a clear report
        emit({"ok": False, "error": f"cannot import the live session: {exc}"})
        return 3

    session = None
    try:
        session = LiveSession(dry_run=False, enable_input=True).open()
        budget = Budget(max_duration_s=240.0,
                        max_actions=max(6, min(args.max_capsules, HARD_MAX_THROWS) * 4))
        ctx = SkillContext(session=session, world=GameWorldState(), dry_run=False,
                           allow_input=True, budget=budget)
        skill = CatchPet(
            calibration,
            limits=CatchLimits(
                max_throws=min(args.max_capsules, HARD_MAX_THROWS),
                reserve=max(1, calibration.reserve),
                max_failures=calibration.max_failures,
                deadline_epoch=deadline_epoch),
            ledger_path=args.ledger or None)
        result = skill(ctx, target=args.target)
        payload = {
            "ok": result.status.value == "PASS",
            "skill": result.skill,
            "status": result.status.value,
            "reason": result.reason,
            "proof": result.proof,
            "actions": result.actions,
            **{key: value for key, value in result.data.items()
               if key in ("outcome", "target", "target_name", "target_level",
                          "captures", "throws_used", "failures", "capsules_before",
                          "capsules_after", "inventory_verified",
                          "inventory_mismatch", "elapsed_s", "attempts", "ledger")},
        }
        payload["ok"] = payload.get("outcome") == Outcome.PROGRESS.value
        emit(payload)
        return EXIT_CODES.get(str(payload.get("outcome")), 1)
    except Exception as exc:  # noqa: BLE001 - a failed live run must be machine-readable
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 3
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
