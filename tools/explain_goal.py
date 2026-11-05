#!/usr/bin/env python3
"""Why is the planner refusing?  Print the whole goal graph's verdict, not 5 lines of it.

`goal start` clips the refusal to a few blockers, which is enough for a log and useless
for deciding what to build next: the overnight run kept stopping with
`nothing schedulable` while 40 of 45 nodes were blocked for reasons nobody could see.

This tool never touches the game.  It reads the last verified world snapshot out of the
SQLite store and evaluates every node exactly the way the scheduler does, then groups
the blockers by *cause* so the next move is obvious:

    node status      completed / ready / missing_prereq / missing_capability / ...
    capability gaps  which unregistered capabilities block the most nodes
    ready list       what the runner would do right now, in priority order

    PYTHONPATH=.pydeps python3 tools/explain_goal.py
    PYTHONPATH=.pydeps python3 tools/explain_goal.py --goal puni --json data/overnight/plan.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.goals.graph import GoalGraph  # noqa: E402
from seerauto.runner.runner import RunConfig, open_runner  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "goalrunner.sqlite3")


def collect(goal: str, db: str) -> dict:
    """Evaluate the graph against the newest stored world snapshot."""

    graph = GoalGraph.load()
    cfg = RunConfig(goal=goal, db_path=db, dry_run=True, allow_input=False)
    runner = open_runner(cfg, log=lambda *_: None)
    try:
        world = runner.store.latest_world() if runner.store else None
        completed = runner.completed_nodes(world)
        rows = []
        for node in graph:
            status = graph.status_of(node.id, world=world, completed=completed)
            unmet = graph.unmet(node.id, world=world, completed=completed)
            rows.append({
                "id": node.id,
                "legacy_id": node.legacy_id,
                "title": node.title,
                "phase": node.phase,
                "priority": node.priority,
                "executable": node.executable,
                "status": status,
                "unmet": unmet,
            })
        ready = [n.id for n in graph.ready(world=world, completed=completed)]
        caps_available = sorted(graph.available_capabilities())
        return {
            "goal": goal,
            "world": {
                "scene": getattr(getattr(world, "scene", None), "value", None),
                "run_id": getattr(world, "run_id", None),
                "observed_at": str(getattr(world, "observed_at", "") or ""),
            } if world is not None else None,
            "nodes": rows,
            "ready": ready,
            "capabilities_registered": caps_available,
        }
    finally:
        runner.close()


def summarise(report: dict) -> dict:
    rows = report["nodes"]
    by_status = Counter(r["status"] for r in rows)
    # Which missing capability, if registered, would unblock the most nodes?
    cap_blockers: dict[str, list[str]] = defaultdict(list)
    state_blockers: dict[str, list[str]] = defaultdict(list)
    node_blockers: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        for reason in r["unmet"]:
            if reason.startswith("missing capability "):
                name = reason.split("'")[1] if "'" in reason else reason
                cap_blockers[name].append(r["id"])
            elif reason.startswith("state prerequisite "):
                name = reason.split("'")[1] if "'" in reason else reason
                state_blockers[name].append(r["id"])
            elif reason.startswith("prerequisite node "):
                name = reason.split("'")[1] if "'" in reason else reason
                node_blockers[name].append(r["id"])
    return {
        "count": len(rows),
        "by_status": dict(by_status.most_common()),
        "capability_gaps": sorted(
            ({"capability": k, "blocks": len(v), "nodes": v[:8]}
             for k, v in cap_blockers.items()), key=lambda x: -x["blocks"]),
        "state_gaps": sorted(
            ({"state": k, "blocks": len(v), "nodes": v[:8]}
             for k, v in state_blockers.items()), key=lambda x: -x["blocks"]),
        "node_gaps": sorted(
            ({"node": k, "blocks": len(v), "nodes": v[:8]}
             for k, v in node_blockers.items()), key=lambda x: -x["blocks"]),
        "ready": report["ready"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", default="puni")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", default=os.path.join(ROOT, "data", "overnight", "plan.json"))
    ap.add_argument("--all", action="store_true", help="print every node, not just blockers")
    args = ap.parse_args()

    report = collect(args.goal, args.db)
    summary = summarise(report)
    world = report["world"] or {}
    print(f"goal {report['goal']}  nodes={summary['count']}  "
          f"world_scene={world.get('scene')}  observed={world.get('observed_at')}")
    print("status: " + " ".join(f"{k}={v}" for k, v in summary["by_status"].items()))
    print(f"ready now: {summary['ready'] or 'NONE'}")
    print()

    if summary["capability_gaps"]:
        print("CAPABILITY GAPS (build these to unblock the most nodes):")
        for gap in summary["capability_gaps"][:12]:
            print(f"  {gap['capability']:<28} blocks {gap['blocks']:>2}  "
                  f"e.g. {', '.join(gap['nodes'][:3])}")
        print()
    if summary["state_gaps"]:
        print("STATE PREREQUISITES never yet observed:")
        for gap in summary["state_gaps"][:12]:
            print(f"  {gap['state']:<28} blocks {gap['blocks']:>2}  "
                  f"e.g. {', '.join(gap['nodes'][:3])}")
        print()
    if summary["node_gaps"]:
        print("NODE PREREQUISITES still open (top 10):")
        for gap in summary["node_gaps"][:10]:
            print(f"  {gap['node']:<34} blocks {gap['blocks']:>2}")
        print()

    if args.all:
        print("ALL NODES:")
        for r in report["nodes"]:
            print(f"  [{r['priority']}] {r['id']:<40} {r['status']:<18} {r['title'][:34]}")
            for reason in r["unmet"][:4]:
                print(f"        - {reason[:150]}")
        print()

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump({"report": report, "summary": summary}, fh, ensure_ascii=False, indent=1)
    print(f"detail -> {args.json}")
    return 0 if summary["ready"] else 10


if __name__ == "__main__":
    raise SystemExit(main())
