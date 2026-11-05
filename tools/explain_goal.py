#!/usr/bin/env python3
"""Why is the planner refusing?  Print the whole goal graph's verdict, not 5 lines of it.

`goal start` clips the refusal to a few blockers, which is enough for a log and useless
for deciding what to build next: the overnight run kept stopping with
`nothing schedulable` while most of the graph was blocked for reasons nobody could see.

This tool **never touches the game**.  It does not open a `LiveSession` and therefore
never starts a second `seerdx` capture stream — an independent review caught the first
version doing exactly that through `open_runner`, which would have broken the stream of
a live run *at the moment the run needed diagnosing* (AGENTS.md 5.5.4).

Blockers are reported in two separate columns, because they mean very different work:

    unblocks   this capability is the ONLY thing standing between the node and a run
    blocks     the node also waits on prerequisite nodes, so adding the capability
               alone will not start it

Only `unblocks` is a sensible "build this next" ranking; summing both overstated the
leverage of the top gap by roughly 7x in the first version.

    PYTHONPATH=.pydeps python3 tools/explain_goal.py
    PYTHONPATH=.pydeps python3 tools/explain_goal.py --all --json data/overnight/plan.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.goals.graph import GoalGraph  # noqa: E402
from seerauto.world.store import WorldStore  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "goalrunner.sqlite3")

#: Statuses that cannot be scheduled this run by definition.  Counting their missing
#: capabilities as "work to do" is what inflated the first version's numbers.
NOT_SCHEDULABLE = frozenset({"completed", "not_executable", "blocked"})


def collect(goal: str, db: str) -> dict:
    """Evaluate the graph against the newest stored world snapshot.  No game access."""

    graph = GoalGraph.load()
    store = WorldStore(db)
    try:
        world = store.latest_world()
        latest = store.latest_run()
        completed: set[str] = set()
        if latest and latest.get("run_id"):
            for node_id, row in store.node_states(latest["run_id"]).items():
                if str(row.get("status")) == "completed":
                    completed.add(node_id)
        if world is not None:
            for node in graph:
                if graph.status_of(node.id, world=world, completed=completed) == "completed":
                    completed.add(node.id)
    finally:
        store.close()

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
    return {
        "goal": goal,
        "world": {
            "scene": getattr(getattr(world, "scene", None), "value", None),
            "run_id": getattr(world, "run_id", None),
        } if world is not None else None,
        "nodes": rows,
        "ready": ready,
        "completed_nodes": sorted(completed),
        "capabilities_registered": sorted(graph.available_capabilities()),
    }


def summarise(report: dict) -> dict:
    rows = report["nodes"]
    by_status = Counter(r["status"] for r in rows)
    unblocks: dict[str, list[str]] = defaultdict(list)
    blocks: dict[str, list[str]] = defaultdict(list)
    state_gaps: dict[str, list[str]] = defaultdict(list)
    node_gaps: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["status"] in NOT_SCHEDULABLE:
            continue
        caps = [x for x in r["unmet"] if x.startswith("missing capability ")]
        other = [x for x in r["unmet"] if not x.startswith("missing capability ")]
        for reason in caps:
            name = reason.split("'")[1] if "'" in reason else reason
            (blocks if other else unblocks)[name].append(r["id"])
        for reason in other:
            if reason.startswith("state prerequisite "):
                name = reason.split("'")[1] if "'" in reason else reason
                state_gaps[name].append(r["id"])
            elif reason.startswith("prerequisite node "):
                name = reason.split("'")[1] if "'" in reason else reason
                node_gaps[name].append(r["id"])

    def ranked(d):
        return sorted(({"name": k, "count": len(v), "nodes": v[:8]}
                       for k, v in d.items()), key=lambda x: -x["count"])

    schedulable_missing = sum(1 for r in rows if r["status"] == "missing_capability")
    return {
        "count": len(rows),
        "by_status": dict(by_status.most_common()),
        "schedulable_but_missing_capability": schedulable_missing,
        "capability_unblocks": ranked(unblocks),
        "capability_blocks": ranked(blocks),
        "state_gaps": ranked(state_gaps),
        "node_gaps": ranked(node_gaps),
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
          f"world_scene={world.get('scene')}  run={world.get('run_id')}")
    print("status: " + " ".join(f"{k}={v}" for k, v in summary["by_status"].items()))
    print(f"completed: {report.get('completed_nodes') or 'none'}")
    print(f"ready now: {summary['ready'] or 'NONE'}")
    print()

    if summary["capability_unblocks"]:
        print("CAPABILITY GAPS — adding one of these would let a node run immediately:")
        for gap in summary["capability_unblocks"][:12]:
            print(f"  {gap['name']:<28} unblocks {gap['count']:>2}  "
                  f"e.g. {', '.join(gap['nodes'][:3])}")
        print()
    if summary["capability_blocks"]:
        print("also missing a capability, but they wait on prerequisites too (lower priority):")
        for gap in summary["capability_blocks"][:8]:
            print(f"  {gap['name']:<28} on {gap['count']:>2} further node(s)")
        print()
    if summary["state_gaps"]:
        print("STATE PREREQUISITES never yet observed:")
        for gap in summary["state_gaps"][:12]:
            print(f"  {gap['name']:<28} blocks {gap['count']:>2}  "
                  f"e.g. {', '.join(gap['nodes'][:3])}")
        print()
    if summary["node_gaps"]:
        print("NODE PREREQUISITES still open (top 10):")
        for gap in summary["node_gaps"][:10]:
            print(f"  {gap['name']:<34} blocks {gap['count']:>2}")
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
