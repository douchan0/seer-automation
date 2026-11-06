#!/usr/bin/env python3
"""`overnight_control` — the operator/agent CLI over the dual-mode control plane.

    PYTHONPATH=.pydeps python3 tools/overnight_control.py status
    PYTHONPATH=.pydeps python3 tools/overnight_control.py init --policy ADAPTIVE
    PYTHONPATH=.pydeps python3 tools/overnight_control.py switch --to MAX_RESULTS \
        --reason NEED_RESEARCH --evidence data/overnight/20261106_dual_mode/events.jsonl
    PYTHONPATH=.pydeps python3 tools/overnight_control.py health --stall-s 600
    PYTHONPATH=.pydeps python3 tools/overnight_control.py health --watch --cadence 0.5
    PYTHONPATH=.pydeps python3 tools/overnight_control.py deadline --set 2026-11-07T07:00:00+08:00
    PYTHONPATH=.pydeps python3 tools/overnight_control.py progress --proof "Lv41" \
        --evidence data/overnight/frames/wild_027.png
    PYTHONPATH=.pydeps python3 tools/overnight_control.py event --kind phase --message "research done"

Every subcommand prints exactly one JSON object on stdout, so a caller (the DSH
agent, a cron job, a human) parses a schema instead of scraping prose.

Safety properties the plan depends on:

* every subcommand works with *no* state on disk — `status` prints JSON with nulls
  instead of raising, because a broken status command during a live night is worse
  than a useless one;
* `status` is read-only: it does not create the data directory, the ledgers or the
  lock file;
* a suppressed switch is a **successful, recorded decision** — exit 0, and the JSON
  says ``applied: false`` with the cooldown that refused it.  Refusing to flap is
  not an error;
* exit codes: 0 = the command did what was asked (including "suppressed, recorded"),
  2 = usage/validation error (argparse), 3 = the on-disk state could not satisfy the
  request (e.g. `switch` on an uninitialised night, malformed deadline).

This file is a thin orchestrator on purpose: all rules live in `seerauto/dual/` so
they are unit-testable without a game or a model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.dual.facts import (FactKind, collect, probe_processes)  # noqa: E402
from seerauto.dual.ledger import (CHECKPOINT_NAME, DEFAULT_DATA_DIR,  # noqa: E402
                                  LEDGERS, Ledger)
from seerauto.dual.modes import Mode, Policy, Signals, resolve  # noqa: E402
from seerauto.dual.outcome import canonical  # noqa: E402
from seerauto.dual.state import derive  # noqa: E402
from seerauto.dual.switch import ModeSwitchService, SwitchReason  # noqa: E402
from seerauto.dual.watchdog import (Watchdog, WatchdogConfig,  # noqa: E402
                                    parse_deadline, remaining_seconds)

EXIT_OK = 0
EXIT_STATE = 3


def emit(payload: Any) -> None:
    """One compact JSON object per invocation, UTF-8, on stdout."""

    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                     default=str), flush=True)


def ledger_for(args: argparse.Namespace) -> Ledger:
    return Ledger(args.data_dir, fsync=not args.no_fsync)


def checkpoint_for(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    checkpoint = ledger.load_checkpoint(path=args.checkpoint, default={}) or {}
    return checkpoint


def manifest_for(ledger: Ledger) -> dict[str, Any]:
    """The experiment manifest written before the night started, or {}."""

    path = ledger.data_dir / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_deadline(args: argparse.Namespace, checkpoint: dict[str, Any],
                     ledger: Ledger):
    """Explicit arg > checkpoint > experiment manifest; validated, never guessed."""

    for candidate in (getattr(args, "deadline_at", None), checkpoint.get("deadline"),
                      manifest_for(ledger).get("hard_deadline")):
        if candidate:
            return parse_deadline(candidate)
    return None


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _ledger_problems(ledger: Ledger) -> dict[str, list[dict[str, Any]]]:
    """Only the ledgers that have unparsable lines (usually just a truncated tail)."""

    found = {}
    for name in LEDGERS:
        problems = ledger.problems(name)
        if problems:
            found[name] = problems
    return found


def _last_event(ledger: Ledger) -> dict[str, Any] | None:
    record = ledger.last("events")
    if record is None:
        return None
    return {
        "ts": record.get("ts"),
        "kind": record.get("kind") or record.get("type"),
        "outcome": record.get("outcome"),
        "message": record.get("message") or record.get("reason") or record.get("wake"),
        "source": "events.jsonl",
    }


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    ledger = ledger_for(args).ensure()
    ledger.sweep_tmp()
    existing = ledger.load_checkpoint(path=args.checkpoint, default=None)
    if existing and not args.force:
        emit({"ok": True, "already_initialized": True,
              "checkpoint": str(ledger.checkpoint_path),
              "mode": existing.get("mode"), "policy": existing.get("policy"),
              "deadline": existing.get("deadline")})
        return EXIT_OK

    manifest = manifest_for(ledger)
    try:
        deadline = parse_deadline(args.deadline_at or manifest.get("hard_deadline"))
        # The experiment may have started before this init call (the manifest records
        # the real start); a checkpoint that claims a later start would understate
        # every "time without progress" number the watchdog reports.
        started = parse_deadline(args.started_at) if args.started_at else None
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_STATE
    now = started.timestamp() if started else time.time()
    mode = Mode.parse(args.mode)
    policy = Policy.parse(args.policy)
    state = {
        "experiment": manifest.get("experiment") or ledger.data_dir.name,
        "created_ts": _iso(now),
        "created_epoch": round(now, 3),
        "session_started_ts": _iso(now),
        "session_started_epoch": round(now, 3),
        "mode": mode.value,
        "mode_since_epoch": round(now, 3),
        "policy": policy.value,
        "deadline": deadline.isoformat() if deadline else None,
        "goal": {
            "id": args.goal_id,
            "status": args.goal_status,
            "started_ts": _iso(now),
            "started_epoch": round(now, 3),
        },
        "last_verified_progress_ts": None,
        "last_verified_progress": None,
        "last_switch_ts": None,
        "last_switch_epoch": None,
        "checkpoint_epoch": round(now, 3),
    }
    if existing:
        state["previous_mode"] = existing.get("mode")
        state["reinitialized_epoch"] = round(now, 3)
    ledger.save_checkpoint(state, path=args.checkpoint)
    ledger.append("events", {"kind": "session.init", "mode": mode.value,
                             "policy": policy.value,
                             "deadline": state["deadline"],
                             "goal": state["goal"],
                             "reinitialized": bool(existing)})
    emit({"ok": True, "already_initialized": False, "data_dir": str(ledger.data_dir),
          "checkpoint": str(ledger.checkpoint_path),
          "mode": mode.value, "policy": policy.value,
          "deadline": state["deadline"],
          "remaining_s": (round(remaining_seconds(deadline, now), 1)
                          if deadline else None),
          "ledgers": {name: str(ledger.path(name)) for name in LEDGERS}})
    return EXIT_OK


# --------------------------------------------------------------------------- #
# status (read-only)
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    now = time.time()
    checkpoint = checkpoint_for(args, ledger)
    night = derive(ledger, checkpoint=checkpoint, now=now)
    try:
        deadline = resolve_deadline(args, checkpoint, ledger)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_STATE
    pattern = args.supervisor_pattern
    facts = collect(ledger=ledger, checkpoint=checkpoint, now=now,
                    process_probe=lambda: probe_processes(pattern),
                    night=night)
    mode = checkpoint.get("mode")
    policy = checkpoint.get("policy")
    resolved: dict[str, Any] | None = None
    if policy:
        try:
            resolution = resolve(
                policy,
                Signals(
                    minutes_since_progress=(night.since_progress_s / 60.0
                                            if night.since_progress_s is not None
                                            else None),
                    consecutive_no_work=night.consecutive_no_work,
                    consecutive_unverified=night.consecutive_unverified,
                    blocked=night.blocked,
                    deadline_remaining_s=remaining_seconds(deadline, now),
                ),
                current=mode,
            )
            resolved = {"mode": resolution.mode.value,
                        "reason": resolution.reason.value if resolution.reason else None,
                        "rule": resolution.rule,
                        "because": list(resolution.because)}
        except ValueError as exc:  # pragma: no cover - bad checkpoint content
            resolved = {"error": str(exc)}
    last_progress = night.last_verified_progress or {}
    emit({
        "ok": True,
        "initialized": bool(checkpoint),
        "data_dir": str(ledger.data_dir),
        "mode": mode,
        "policy": policy,
        "resolved_mode": resolved,
        "deadline": deadline.isoformat() if deadline else None,
        "remaining_s": (round(remaining_seconds(deadline, now), 1)
                        if deadline else None),
        "deadline_source": ("arg" if getattr(args, "deadline_at", None)
                            else "checkpoint" if checkpoint.get("deadline")
                            else "manifest" if manifest_for(ledger).get("hard_deadline")
                            else None),
        "ledgers": ledger.sizes(),
        "ledger_problems": _ledger_problems(ledger),
        "last_event": _last_event(ledger),
        "last_verified_progress": (
            {"ts": last_progress.get("ts"),
             "age_s": round(night.since_progress_s, 1),
             "node": last_progress.get("node"),
             "proof": last_progress.get("proof"),
             "evidence": last_progress.get("evidence")}
            if night.last_verified_progress_ts is not None else None),
        "executor_heartbeat_age_s": (
            round(facts.get(FactKind.EXECUTOR_ALIVE).age_s, 1)
            if facts.get(FactKind.EXECUTOR_ALIVE).age_s is not None else None),
        "facts": facts.compact(),
        "unknown_facts": [kind.value for kind in facts.unknown],
        "night": {
            "batches": night.batch_count,
            "last_outcome": night.last_outcome.value if night.last_outcome else None,
            "consecutive_no_work": night.consecutive_no_work,
            "consecutive_unverified": night.consecutive_unverified,
            "blocked": night.blocked,
            "milestones": night.milestone_seq,
        },
    })
    return EXIT_OK


# --------------------------------------------------------------------------- #
# switch
# --------------------------------------------------------------------------- #
def cmd_switch(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    checkpoint = checkpoint_for(args, ledger)
    if not checkpoint and not args.allow_uninitialised:
        emit({"ok": False, "error": "the night is not initialised (run `init` first, "
                                   "or pass --allow-uninitialised)"})
        return EXIT_STATE
    service = ModeSwitchService(ledger, checkpoint_path=args.checkpoint,
                                reason_cooldown_s=args.reason_cooldown_s,
                                min_dwell_s=args.min_dwell_s)
    try:
        record = service.request(args.to, args.reason, evidence=args.evidence,
                                 note=args.note)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_STATE
    emit({"ok": True, **record})
    return EXIT_OK


# --------------------------------------------------------------------------- #
# checkpoint
# --------------------------------------------------------------------------- #
def cmd_checkpoint(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    checkpoint = checkpoint_for(args, ledger)
    if args.set:
        for item in args.set:
            if "=" not in item:
                emit({"ok": False, "error": f"--set wants KEY=VALUE, got {item!r}"})
                return EXIT_STATE
            key, value = item.split("=", 1)
            try:
                parsed: Any = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
            checkpoint[key] = parsed
        checkpoint["checkpoint_epoch"] = time.time()
        ledger.save_checkpoint(checkpoint, path=args.checkpoint)
        ledger.append("events", {"kind": "checkpoint", "set": args.set})
        emit({"ok": True, "updated": args.set, "checkpoint": checkpoint})
        return EXIT_OK
    emit({"ok": True, "initialized": bool(checkpoint),
          "path": str(ledger.checkpoint_path), "checkpoint": checkpoint})
    return EXIT_OK


# --------------------------------------------------------------------------- #
# health (one watchdog evaluation, or the loop)
# --------------------------------------------------------------------------- #
def watch_config(args: argparse.Namespace, ledger: Ledger) -> WatchdogConfig:
    checkpoint = ledger.load_checkpoint(path=args.checkpoint, default={}) or {}
    deadline = getattr(args, "deadline_at", None) or checkpoint.get("deadline")
    return WatchdogConfig(
        deadline=deadline,
        stall_s=args.stall_s,
        heartbeat_max_age_s=args.max_age_s,
        no_work_streak=args.no_work_streak,
        unverified_streak=args.unverified_streak,
        deadline_warn_s=args.deadline_warn_s,
        cadence_s=args.cadence,
        persist_every_s=args.persist_every,
        cooldown_s=args.cooldown,
        pause_files=tuple(args.pause_file) if args.pause_file else
        WatchdogConfig().pause_files,
        stop_files=tuple(args.stop_file) if args.stop_file else
        WatchdogConfig().stop_files,
        supervisor_pattern=args.supervisor_pattern,
        supervisor_max_age_s=args.supervisor_max_age_s,
        supervisor_log_file=args.supervisor_log,
        agent_turn_file=args.agent_turn_file,
        executor_heartbeat_file=args.executor_heartbeat_file,
        executor_lock_file=args.executor_lock_file,
        wild_cycle_ledger=args.wild_cycle_ledger,
    )


def cmd_health(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    config = watch_config(args, ledger)
    watchdog = Watchdog(
        ledger, config,
        process_probe=lambda: probe_processes(config.supervisor_pattern))
    if args.watch:
        def on_report(report: dict[str, Any]) -> None:
            emit(_slim_health(report, full=args.full))

        try:
            cycles = watchdog.run(cycles=args.cycles, on_report=on_report)
        except KeyboardInterrupt:
            emit({"ok": True, "stopped": "KeyboardInterrupt", "cycles": watchdog.cycles})
            return EXIT_OK
        emit({"ok": True, "cycles": cycles, "wakes_total": watchdog.wakes_total,
              "suppressed_total": watchdog.suppressed_total})
        return EXIT_OK

    report = watchdog.step()
    emit(_slim_health(report, full=args.full))
    return EXIT_OK


def _slim_health(report: dict[str, Any], *, full: bool) -> dict[str, Any]:
    payload = dict(report)
    if not full:
        payload.pop("config", None)
    return {"ok": True, **payload}


# --------------------------------------------------------------------------- #
# deadline
# --------------------------------------------------------------------------- #
def cmd_deadline(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    checkpoint = checkpoint_for(args, ledger)
    if args.set or args.clear:
        if args.clear:
            checkpoint["deadline"] = None
        else:
            try:
                moment = parse_deadline(args.set)
            except ValueError as exc:
                emit({"ok": False, "error": str(exc)})
                return EXIT_STATE
            checkpoint["deadline"] = moment.isoformat() if moment else None
        checkpoint["checkpoint_epoch"] = time.time()
        ledger.save_checkpoint(checkpoint, path=args.checkpoint)
        ledger.append("events", {"kind": "deadline.set",
                                 "deadline": checkpoint.get("deadline")})
    try:
        deadline = resolve_deadline(args, checkpoint, ledger)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_STATE
    now = time.time()
    remaining = remaining_seconds(deadline, now)
    emit({"ok": True,
          "deadline": deadline.isoformat() if deadline else None,
          "remaining_s": round(remaining, 1) if remaining is not None else None,
          "expired": (remaining is not None and remaining <= 0),
          "updated": bool(args.set or args.clear)})
    return EXIT_OK


# --------------------------------------------------------------------------- #
# progress / event / agent-turn — the writers the executor layer needs
# --------------------------------------------------------------------------- #
def cmd_progress(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    if not str(args.proof or "").strip():
        emit({"ok": False, "error": "verified progress needs a proof string "
                                   "(a status is not evidence)"})
        return EXIT_STATE
    checkpoint = checkpoint_for(args, ledger)
    now = time.time()
    record = ledger.append("milestones", {
        "kind": "progress",
        "node": args.node,
        "proof": args.proof,
        "evidence": args.evidence,
    })
    checkpoint["last_verified_progress_ts"] = record["ts_epoch"]
    checkpoint["last_verified_progress"] = {
        "ts": record["ts"], "node": args.node, "proof": args.proof,
        "evidence": args.evidence,
    }
    checkpoint["checkpoint_epoch"] = round(now, 3)
    ledger.save_checkpoint(checkpoint, path=args.checkpoint)
    # The events copy is for the timeline only: kind "progress.recorded" is not one
    # of state.PROGRESS_KINDS, so the milestone is counted once, in milestones.jsonl.
    ledger.append("events", {"kind": "progress.recorded", "node": args.node,
                             "proof": args.proof, "evidence": args.evidence})
    emit({"ok": True, "recorded": record, "milestone": True,
          "last_verified_progress_ts": record["ts_epoch"]})
    return EXIT_OK


def cmd_event(args: argparse.Namespace) -> int:
    ledger = ledger_for(args)
    payload: dict[str, Any] = {"kind": args.kind, "message": args.message,
                               "evidence": args.evidence}
    for item in args.data or []:
        if "=" not in item:
            emit({"ok": False, "error": f"--data wants KEY=VALUE, got {item!r}"})
            return EXIT_STATE
        key, value = item.split("=", 1)
        try:
            payload[key] = json.loads(value)
        except json.JSONDecodeError:
            payload[key] = value
    outcome_value = canonical(payload.get("outcome"))
    if outcome_value is not None:
        payload["outcome"] = outcome_value.value
    record = ledger.append("events", payload)
    emit({"ok": True, "recorded": record})
    return EXIT_OK


def cmd_agent_turn(args: argparse.Namespace) -> int:
    """Record whether the DSH layer is deciding — its own fact, never inferred."""

    ledger = ledger_for(args).ensure(create_ledgers=False)
    now = time.time()
    payload = {
        "state": args.state,
        "ts": _iso(now),
        "ts_epoch": round(now, 3),
        "note": args.note,
        "session": args.session,
    }
    target = ledger.data_dir / "agent_turn.json"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)
    ledger.append("events", {"kind": "agent.turn", "state": args.state,
                             "note": args.note})
    emit({"ok": True, "recorded": payload, "path": str(target)})
    return EXIT_OK


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="overnight_control",
        description="Dual-mode overnight control plane (no model calls, no game input)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help="evidence directory (default: %(default)s)")
    parser.add_argument("--checkpoint", default=None,
                        help="checkpoint path override (default: <data-dir>/"
                             + CHECKPOINT_NAME + ")")
    parser.add_argument("--no-fsync", action="store_true",
                        help="skip fsync on appends (tests only; not crash-safe)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the checkpoint and empty ledgers")
    p.add_argument("--mode", default=Mode.EFFICIENT.value,
                   choices=[m.value for m in Mode])
    p.add_argument("--policy", default=Policy.ADAPTIVE.value,
                   choices=[p.value for p in Policy])
    p.add_argument("--deadline-at", default=None,
                   help="ISO-8601 with offset; defaults to the manifest hard_deadline")
    p.add_argument("--started-at", default=None,
                   help="ISO-8601 with offset; defaults to now (use the real start)")
    p.add_argument("--goal-id", default="puni-nightly")
    p.add_argument("--goal-status", default="active")
    p.add_argument("--force", action="store_true",
                   help="re-initialise over an existing checkpoint")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="read-only compact JSON status")
    p.add_argument("--deadline-at", default=None)
    p.add_argument("--supervisor-pattern", default="tools/supervise.py")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("switch", help="request a mode switch (audited either way)")
    p.add_argument("--to", required=True, choices=[m.value for m in Mode])
    p.add_argument("--reason", required=True,
                   choices=[r.value for r in SwitchReason])
    p.add_argument("--evidence", required=True,
                   help="evidence pointer: path / ledger record / explicit NOT FOUND")
    p.add_argument("--note", default=None)
    p.add_argument("--reason-cooldown-s", type=float, default=900.0)
    p.add_argument("--min-dwell-s", type=float, default=1800.0)
    p.add_argument("--allow-uninitialised", action="store_true")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("checkpoint", help="print or patch the checkpoint")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("health", help="evaluate the watchdog once, or run the loop")
    p.add_argument("--watch", action="store_true", help="run the local loop")
    p.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    p.add_argument("--cadence", type=float, default=0.5)
    p.add_argument("--stall-s", type=float, default=900.0)
    p.add_argument("--max-age-s", type=float, default=120.0)
    p.add_argument("--no-work-streak", type=int, default=2)
    p.add_argument("--unverified-streak", type=int, default=3)
    p.add_argument("--deadline-warn-s", type=float, default=1800.0)
    p.add_argument("--deadline-at", default=None)
    p.add_argument("--persist-every", type=float, default=30.0)
    p.add_argument("--cooldown", type=float, default=300.0)
    p.add_argument("--pause-file", action="append", default=None)
    p.add_argument("--stop-file", action="append", default=None)
    p.add_argument("--supervisor-pattern", default="tools/supervise.py")
    p.add_argument("--supervisor-max-age-s", type=float, default=900.0)
    p.add_argument("--supervisor-log", default=None)
    p.add_argument("--agent-turn-file", default=None)
    p.add_argument("--executor-heartbeat-file", default=None)
    p.add_argument("--executor-lock-file", default=None)
    p.add_argument("--wild-cycle-ledger", default=None)
    p.add_argument("--full", action="store_true", help="include the config block")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("deadline", help="show or set the hard deadline")
    p.add_argument("--set", default=None, help="ISO-8601 with offset")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--deadline-at", default=None)
    p.set_defaults(func=cmd_deadline)

    p = sub.add_parser("progress", help="record verified progress (needs a proof)")
    p.add_argument("--node", default=None)
    p.add_argument("--proof", required=True)
    p.add_argument("--evidence", default=None)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("event", help="append one event to the timeline")
    p.add_argument("--kind", required=True)
    p.add_argument("--message", default=None)
    p.add_argument("--evidence", default=None)
    p.add_argument("--data", action="append", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("agent-turn", help="record that the DSH layer is deciding")
    p.add_argument("--state", required=True, choices=["deciding", "idle"])
    p.add_argument("--note", default=None)
    p.add_argument("--session", default=None)
    p.set_defaults(func=cmd_agent_turn)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:  # pragma: no cover - only when piped to `head`
        return EXIT_OK
    except (ValueError, OSError) as exc:
        # A status command that tracebacks during a live night is worse than one
        # that prints a machine-readable error: the caller can then decide.
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return EXIT_STATE


if __name__ == "__main__":
    sys.exit(main())
