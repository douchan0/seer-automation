"""``python3 -m seerauto goal ...`` — the long-horizon Goal Runner CLI.

    goal start puni --planner ai --dry-run
    goal start puni --planner ai --allow-input --max-minutes 60 --max-battles 12 --max-spend 0
    goal status | goal pause | goal resume | goal stop | goal report

Design notes that matter in practice:

* ``--dry-run`` never sends input: it observes, plans and writes a report.
* Real input requires ``--allow-input`` *and* a non-dry run *and* the executor
  lock; without the flag every action skill refuses.
* ``pause``/``stop`` only touch control files, so they work from any shell and
  do not need the pid.
* ``resume`` does two different things depending on state, and says which:
  it un-pauses a live run, or it restarts a finished/paused run from its last
  checkpoint (re-observing the game first).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .runner.control import InstanceLock, LockHeld, RunControl
from .runner.runner import GoalRunner, RunConfig, open_runner
from .world.store import StoreError, WorldStore

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "data" / "reports"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _control(args: Any) -> RunControl:
    return RunControl(getattr(args, "pause_file", None) or "/tmp/seerauto.pause",
                      getattr(args, "stop_file", None) or "/tmp/seerauto.stop")


def _open_store(args: Any) -> WorldStore:
    return WorldStore(getattr(args, "db", None) or str(ROOT / "data" / "goalrunner.sqlite3"))


def _config_from_args(args: Any, **overrides: Any) -> RunConfig:
    cfg = RunConfig(
        goal=getattr(args, "goal_name", None) or "puni",
        planner=getattr(args, "planner", "deterministic"),
        dry_run=not getattr(args, "allow_input", False),
        allow_input=bool(getattr(args, "allow_input", False)),
        max_minutes=float(getattr(args, "max_minutes", 60.0)),
        max_battles=int(getattr(args, "max_battles", 12)),
        max_actions=int(getattr(args, "max_actions", 400)),
        max_spend=float(getattr(args, "max_spend", 0.0)),
        max_errors=int(getattr(args, "max_errors", 3)),
        db_path=getattr(args, "db", None) or str(ROOT / "data" / "goalrunner.sqlite3"),
        ai_dir=getattr(args, "ai_dir", None) or str(ROOT / "data" / "ai"),
        run_id=getattr(args, "run_id", None),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"run     {summary.get('run_id')}  status={summary.get('status')}")
    print(f"goal    {summary.get('goal')}  planner={summary.get('planner')}")
    print(f"reason  {summary.get('stop_reason')}")
    stats = summary.get("stats") or {}
    print(f"stats   battles={stats.get('battles')} actions={stats.get('actions')} "
          f"errors={stats.get('errors')} loops={stats.get('loops')} "
          f"elapsed={stats.get('elapsed_s')}s")
    if summary.get("planner_degraded"):
        print(f"degraded {summary['planner_degraded']}")
    world = summary.get("world") or {}
    if world:
        print(f"world   scene={world.get('scene')} source={world.get('scene_source')}")
        pet = world.get("active_pet") or {}
        if pet:
            print(f"        pet {pet.get('name')} Lv{pet.get('level')} "
                  f"hp {pet.get('hp')}/{pet.get('hp_max')}")
        if world.get("spt_defeated"):
            print(f"        spt defeated: {', '.join(world['spt_defeated'])}")
    nxt = summary.get("next_observations") or []
    if nxt:
        print("next    (unblocked by observing, not by guessing)")
        for cand in nxt[:3]:
            print(f"  - {cand.get('node')}: {cand.get('reasons', [''])[0]}")
    for note in summary.get("notes") or []:
        print(f"note    {note}")
    nodes = summary.get("nodes") or {}
    if nodes:
        print("nodes")
        for node_id, status in sorted(nodes.items()):
            proof = (summary.get("proofs") or {}).get(node_id) or ""
            print(f"  - {node_id}: {status}" + (f"  [{proof}]" if proof else ""))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_goal_start(args: Any) -> int:
    cfg = _config_from_args(args)
    lock = InstanceLock()
    try:
        lock.acquire()
    except LockHeld as exc:
        print(f"refused: {exc}", file=sys.stderr)
        print("hint: `python3 -m seerauto goal status` to see the live run, "
              "`goal stop` to stop it", file=sys.stderr)
        return 4

    if cfg.dry_run and cfg.allow_input:
        print("refused: --dry-run and --allow-input contradict each other", file=sys.stderr)
        return 2

    print(f"goal start: goal={cfg.goal} planner={cfg.planner} "
          f"input={'ENABLED' if cfg.allow_input else 'DISABLED (dry run)'}")
    print(f"budgets: {json.dumps(cfg.budgets, ensure_ascii=False)}")
    # SIGTERM must behave like Ctrl-C: write a checkpoint and leave the DB
    # consistent.  (A bare SIGTERM used to skip the finally block and leave the
    # run marked "running" with no process behind it.)
    def _on_term(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass
    try:
        runner = open_runner(cfg)
    except Exception as exc:  # noqa: BLE001 - e.g. game window missing
        print(f"cannot start: {type(exc).__name__}: {exc}", file=sys.stderr)
        lock.release()
        return 5
    runner._lock = lock  # hand the lock over so close() releases it
    try:
        summary = runner.run()
    finally:
        try:
            runner.close()
        except Exception:  # noqa: BLE001
            pass
    print()
    _print_summary(summary)
    return 0


def cmd_goal_status(args: Any) -> int:
    store = _open_store(args)
    try:
        run = store.get_run(args.run_id) if getattr(args, "run_id", None) else store.latest_run()
        if run is None:
            print("no goal run recorded yet")
            return 1
        run_id = run["run_id"]
        nodes = store.node_states(run_id)
        world = store.latest_world()
        events = store.events(run_id, limit=6)
        summary = {
            "run_id": run_id,
            "goal": run.get("goal"),
            "planner": run.get("planner"),
            "status": run.get("status"),
            "stop_reason": run.get("stop_reason"),
            "stats": run.get("stats"),
            "nodes": {k: v.get("status") for k, v in nodes.items()},
            "proofs": {k: v.get("proof") for k, v in nodes.items() if v.get("proof")},
            "world": None if world is None else world.summary(),
        }
        _print_summary(summary)
        if events:
            print("recent events")
            for ev in events:
                print(f"  {ev.get('ts')}  {ev.get('kind')}: {ev.get('message')}")
    finally:
        store.close()
    return 0


def cmd_goal_pause(args: Any) -> int:
    control = _control(args)
    control.request_pause()
    store = _open_store(args)
    try:
        run = store.latest_run()
        if run is not None and run.get("status") == "running":
            store.update_run(run["run_id"], status="pause-requested")
    finally:
        store.close()
    print(f"pause requested ({control.pause_file}); the run stops acting within one tick")
    return 0


def cmd_goal_resume(args: Any) -> int:
    control = _control(args)
    had_pause = control.paused()
    control.clear()
    lock = InstanceLock()
    try:
        lock.acquire()
    except LockHeld:
        print(f"un-paused a live run ({control.pause_file} removed)" if had_pause
              else "a live run holds the executor; nothing to resume")
        lock.release()
        return 0
    # no live process: restart from the last checkpoint, re-observing first
    store = _open_store(args)
    try:
        run = store.latest_run()
        if run is None:
            print("no previous run to resume")
            return 1
        if run.get("status") == "completed":
            print(f"run {run['run_id']} already completed; nothing to resume")
            return 0
        run_id = run["run_id"]
        nodes = {k: v.get("status") for k, v in store.node_states(run_id).items()}
        print(f"resuming run {run_id} (status was {run.get('status')}, stop_reason="
              f"{run.get('stop_reason')})")
        print(f"completed nodes: {[k for k, v in nodes.items() if v == 'completed']}")
        budgets = run.get("budgets") or {}
    finally:
        store.close()

    cfg = _config_from_args(args, run_id=run_id)
    if "max_minutes" in budgets and not getattr(args, "max_minutes", None):
        cfg.max_minutes = float(budgets["max_minutes"])
    if "max_battles" in budgets and not getattr(args, "max_battles", None):
        cfg.max_battles = int(budgets["max_battles"])
    try:
        runner = open_runner(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot resume: {type(exc).__name__}: {exc}", file=sys.stderr)
        lock.release()
        return 5
    runner._lock = lock
    try:
        summary = runner.run()
    finally:
        try:
            runner.close()
        except Exception:  # noqa: BLE001
            pass
    print()
    _print_summary(summary)
    return 0


def cmd_goal_stop(args: Any) -> int:
    control = _control(args)
    control.request_stop()
    store = _open_store(args)
    try:
        run = store.latest_run()
        if run is not None:
            store.append_event(run["run_id"], "run.stop_requested", "user asked to stop")
    finally:
        store.close()
    print(f"stop requested ({control.stop_file}); the run checkpoints and exits after "
          f"the current skill")
    return 0


def cmd_goal_report(args: Any) -> int:
    store = _open_store(args)
    try:
        run = store.get_run(args.run_id) if getattr(args, "run_id", None) else store.latest_run()
        if run is None:
            print("no goal run recorded", file=sys.stderr)
            return 1
        run_id = run["run_id"]
        nodes = store.node_states(run_id)
        world = store.latest_world()
        events = list(reversed(store.events(run_id, limit=200)))
        checkpoints = store.checkpoints(run_id, limit=10)
        ai = store.ai_cost(run_id)
    finally:
        store.close()

    if (args.format or "md") == "json":
        payload = {
            "run": run,
            "nodes": nodes,
            "world": None if world is None else world.to_dict(),
            "events": events,
            "checkpoints": checkpoints,
            "ai_cost": ai,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1)
    else:
        lines = [
            f"# Goal run `{run_id}` report",
            "",
            f"- goal: **{run.get('goal')}**",
            f"- planner: **{run.get('planner')}**",
            f"- status: **{run.get('status')}**",
            f"- stop reason: {run.get('stop_reason')}",
            f"- started: {run.get('started_at')}  updated: {run.get('updated_at')}",
            f"- budgets: `{json.dumps(run.get('budgets'), ensure_ascii=False)}`",
            f"- stats: `{json.dumps(run.get('stats'), ensure_ascii=False)}`",
            f"- ai calls: {ai.get('calls')} (failures {ai.get('failures')}, "
            f"cost {ai.get('cost_usd')})",
            "",
            "## Node states",
            "",
        ]
        for node_id, row in sorted(nodes.items()):
            proof = row.get("proof") or ""
            lines.append(f"- `{node_id}` -> **{row.get('status')}**"
                         + (f"\n  - proof: {proof}" if proof else ""))
        if world is not None:
            lines += ["", "## Last observed world", "", "```json",
                      json.dumps(world.summary(), ensure_ascii=False, indent=1), "```"]
        lines += ["", "## Checkpoints", ""]
        for ck in checkpoints:
            lines.append(f"- {ck.get('ts')} `{ck.get('label')}` verified={ck.get('verified')}")
        lines += ["", "## Event log", ""]
        for ev in events[-60:]:
            lines.append(f"- {ev.get('ts')} **{ev.get('kind')}** {ev.get('message')}")
        text = "\n".join(lines) + "\n"

    _write_and_print(args, text, run_id)
    return 0


def _write_and_print(args: Any, text: str, run_id: str) -> None:
    out = getattr(args, "out", None)
    if out:
        path = Path(out)
    elif (getattr(args, "format", None) or "md") == "md":
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"goal-{run_id}.md"
    else:
        path = REPORT_DIR / f"goal-{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n[report written to {path}]")


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #
def add_goal_subcommand(sub: Any) -> None:
    goal = sub.add_parser("goal", help="long-horizon goal runner (puni …)")
    gsub = goal.add_subparsers(dest="goal_command", required=True)

    common_db = dict(default=None, help="sqlite path (default data/goalrunner.sqlite3)")

    start = gsub.add_parser("start", help="start a goal run")
    start.add_argument("goal_name", nargs="?", default="puni")
    start.add_argument("--planner", default="deterministic",
                       choices=["deterministic", "ai", "bridge"])
    start.add_argument("--dry-run", action="store_true", default=False,
                       help="observe and plan only; never send input")
    start.add_argument("--allow-input", action="store_true", default=False,
                       help="REQUIRED for real game input (implies --dry-run is off)")
    start.add_argument("--max-minutes", type=float, default=60.0)
    start.add_argument("--max-battles", type=int, default=12)
    start.add_argument("--max-actions", type=int, default=400)
    start.add_argument("--max-spend", type=float, default=0.0)
    start.add_argument("--max-errors", type=int, default=3)
    start.add_argument("--db", **common_db)
    start.add_argument("--ai-dir", default=None)
    start.add_argument("--pause-file", default=None)
    start.add_argument("--stop-file", default=None)
    start.add_argument("--run-id", default=None)
    start.set_defaults(func=cmd_goal_start)

    status = gsub.add_parser("status", help="show the latest run")
    status.add_argument("--run-id", default=None)
    status.add_argument("--db", **common_db)
    status.set_defaults(func=cmd_goal_status)

    pause = gsub.add_parser("pause", help="pause the live run")
    pause.add_argument("--db", **common_db)
    pause.add_argument("--pause-file", default=None)
    pause.add_argument("--stop-file", default=None)
    pause.set_defaults(func=cmd_goal_pause)

    resume = gsub.add_parser("resume", help="resume a paused or interrupted run")
    resume.add_argument("--allow-input", action="store_true", default=False)
    resume.add_argument("--planner", default="deterministic",
                        choices=["deterministic", "ai", "bridge"])
    resume.add_argument("--max-minutes", type=float, default=None)
    resume.add_argument("--max-battles", type=int, default=None)
    resume.add_argument("--db", **common_db)
    resume.add_argument("--ai-dir", default=None)
    resume.add_argument("--pause-file", default=None)
    resume.add_argument("--stop-file", default=None)
    resume.add_argument("--run-id", default=None)
    resume.set_defaults(func=cmd_goal_resume)

    stop = gsub.add_parser("stop", help="ask the live run to stop after the current skill")
    stop.add_argument("--db", **common_db)
    stop.add_argument("--pause-file", default=None)
    stop.add_argument("--stop-file", default=None)
    stop.set_defaults(func=cmd_goal_stop)

    report = gsub.add_parser("report", help="write a run report (markdown or json)")
    report.add_argument("--run-id", default=None)
    report.add_argument("--format", default="md", choices=["md", "json"])
    report.add_argument("--out", default=None)
    report.add_argument("--db", **common_db)
    report.set_defaults(func=cmd_goal_report)


__all__ = ["add_goal_subcommand", "cmd_goal_start", "cmd_goal_status", "cmd_goal_pause",
           "cmd_goal_resume", "cmd_goal_stop", "cmd_goal_report"]
