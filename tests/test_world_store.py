"""Persistence tests for :class:`seerauto.world.store.WorldStore`.

What actually matters here is *not* that SQLite works, but the three promises
the goal runner is being written against:

* a fully populated world survives save -> load unchanged (provenance included),
  because "level 15" without its evidence is exactly the bug Round 5 exists to
  fix;
* every read survives a corrupt row: a crash mid-write must never turn the
  recovery path itself into an exception;
* writes are atomic and serialised, so the flattened fact index can never
  disagree with the snapshot it belongs to, even with several threads.

Everything is offline: no game client, no daemon.
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from seerauto.world import StoreError, WorldStore
from seerauto.world.model import (
    Evidence,
    Fact,
    GameWorldState,
    PetState,
    ResourceState,
    Scene,
    Source,
    SptEntry,
    WindowState,
)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
RUN_ID = "0123456789ab"
T0 = "2026-11-01T10:00:00+08:00"


def full_world(*, level: int = 15, observed_at: str = T0,
               run_id: str | None = RUN_ID, scene: str = Scene.WORLD.value) -> GameWorldState:
    """A world with every sub-record populated and honest evidence attached."""

    evidence = Evidence(
        Source.LIVE_VERIFIED,
        observed_at=observed_at,
        origin="ocr.live",
        confidence=0.9,
        ref="artifacts/live/frame_0001.png",
    )
    world = GameWorldState(captured_at=observed_at, run_id=run_id)
    world.window = WindowState(
        window_id=4210, x=225.0, y=53.0, w=1200.0, h=800.0, scale=2.0,
        frontmost=True, title="赛尔号XIN计划", evidence=evidence,
    )
    world.pets["烈火猴"] = PetState(
        name="烈火猴", level=level, hp=120, hp_max=200,
        pp={"火焰拳": (10, 20), "火之刃": (5, 15)},
        skills=("火焰拳", "火之刃"), active=True, evidence=evidence,
        extra={"nature": "胆小"},
    )
    world.active_pet = "烈火猴"
    world.resources["赛尔豆"] = ResourceState("赛尔豆", 12345.0, "个", evidence)
    world.spt["蘑菇怪"] = SptEntry(
        boss_id="spt-001", name="蘑菇怪", location="克洛斯星", defeated=True,
        reward_unlock="交易行", evidence=evidence,
    )
    world.set_fact("session.scene", scene, evidence)
    world.set_fact("session.logged_in", True, evidence)
    world.unlocked["trade_market"] = Fact(True, evidence)
    world.dailies["daily_battle"] = Fact(3, evidence)
    world.mark_unknown("session.server")
    world.notes.append("round 5 smoke")
    world.observations["scene_scores"] = {"fight": 0.12, "victory": 0.03}
    return world


def raw_rows(path: str | Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(os.fspath(path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# world round trip
# --------------------------------------------------------------------------- #
def test_world_round_trips_through_sqlite():
    store = WorldStore()  # in-memory
    world = full_world()

    snapshot_id = store.save_world(world, note="after login")
    assert snapshot_id > 0

    loaded = store.latest_world()
    assert loaded is not None
    # Full structural equality: provenance, evidence and observations included.
    assert loaded.to_dict() == world.to_dict()

    # Key fields spelled out, so a failure names the broken record.
    assert loaded.pets["烈火猴"].level == 15
    assert loaded.pets["烈火猴"].pp["火焰拳"] == (10, 20)
    assert loaded.pets["烈火猴"].evidence.source is Source.LIVE_VERIFIED
    assert loaded.active_pet == "烈火猴"
    assert loaded.resources["赛尔豆"].amount == 12345.0
    assert loaded.spt["蘑菇怪"].defeated is True
    assert loaded.spt["蘑菇怪"].reward_unlock == "交易行"
    assert loaded.window is not None and loaded.window.frontmost is True
    assert loaded.window.title == "赛尔号XIN计划"
    assert loaded.fact("session.logged_in") is True
    assert loaded.evidence_for("session.scene").source is Source.LIVE_VERIFIED
    assert loaded.facts["session.scene"].value == "WORLD"
    assert loaded.unknowns == ["session.server"]
    assert loaded.notes == ["round 5 smoke"]

    # Fetch by id, and a missing id is None rather than an exception.
    assert store.world_snapshot(snapshot_id).to_dict() == world.to_dict()
    assert store.world_snapshot(999_999) is None

    history = store.world_history()
    assert [h["id"] for h in history] == [snapshot_id]
    assert history[0]["captured_at"] == T0
    assert history[0]["run_id"] == RUN_ID
    assert history[0]["scene"] == "WORLD"
    assert history[0]["note"] == "after login"
    store.close()


def test_save_world_stamps_run_id_without_mutating_the_caller():
    store = WorldStore()
    world = full_world(run_id=None)
    snapshot_id = store.save_world(world, run_id="fedcba987654")

    assert world.run_id is None  # caller's object untouched
    loaded = store.latest_world()
    assert loaded is not None and loaded.run_id == "fedcba987654"
    assert store.world_history()[0]["run_id"] == "fedcba987654"
    assert store.world_snapshot(snapshot_id).run_id == "fedcba987654"
    store.close()


def test_world_history_is_newest_first_and_limited():
    store = WorldStore()
    ids = [
        store.save_world(full_world(observed_at=f"2026-11-01T1{hour}:00:00+08:00", level=hour))
        for hour in (0, 1, 2)
    ]
    assert ids == sorted(ids)

    history = store.world_history()
    assert [h["id"] for h in history] == list(reversed(ids))
    assert [h["captured_at"] for h in history] == [
        "2026-11-01T12:00:00+08:00",
        "2026-11-01T11:00:00+08:00",
        "2026-11-01T10:00:00+08:00",
    ]
    assert [h["id"] for h in store.world_history(limit=2)] == [ids[2], ids[1]]
    assert store.world_history(limit=0) == []
    store.close()


# --------------------------------------------------------------------------- #
# flattened fact history
# --------------------------------------------------------------------------- #
def test_fact_history_of_a_pet_level_is_ordered_and_limited():
    store = WorldStore()
    for level, stamp in ((15, "2026-11-01T10:00:00+08:00"),
                         (16, "2026-11-01T11:00:00+08:00"),
                         (17, "2026-11-01T12:00:00+08:00")):
        store.save_world(full_world(level=level, observed_at=stamp))

    history = store.fact_history("pets.烈火猴.level")
    assert [row["value"] for row in history] == [17, 16, 15]
    assert [row["observed_at"] for row in history] == [
        "2026-11-01T12:00:00+08:00",
        "2026-11-01T11:00:00+08:00",
        "2026-11-01T10:00:00+08:00",
    ]
    # evidence travels with the value, otherwise a level is unusable
    assert history[0]["source"] == "LIVE_VERIFIED"
    assert history[0]["confidence"] == pytest.approx(0.9)
    assert history[0]["origin"] == "ocr.live"
    assert history[0]["ref"] == "artifacts/live/frame_0001.png"
    assert history[0]["snapshot_id"] > history[-1]["snapshot_id"]

    assert [row["value"] for row in store.fact_history("pets.烈火猴.level", limit=2)] == [17, 16]
    assert store.fact_history("pets.烈火猴.level", limit=0) == []
    # derived rows for resources / spt / explicit facts all exist
    assert store.fact_history("resources.赛尔豆")[0]["value"] == 12345.0
    assert store.fact_history("spt.蘑菇怪.defeated")[0]["value"] is True
    assert store.fact_history("session.logged_in")[0]["value"] is True
    # unknown path is an empty history, never an error
    assert store.fact_history("pets.不存在.level") == []
    store.close()


# --------------------------------------------------------------------------- #
# run lifecycle
# --------------------------------------------------------------------------- #
def test_run_lifecycle():
    store = WorldStore()
    budgets = {"max_actions": 40, "max_duration_s": 300}
    run_id = store.start_run("击败蘑菇怪", "rule", budgets=budgets)

    assert len(run_id) == 12 and int(run_id, 16) >= 0

    run = store.get_run(run_id)
    assert run is not None
    assert run["goal"] == "击败蘑菇怪"
    assert run["planner"] == "rule"
    assert run["status"] == "running"
    assert run["budgets"] == budgets
    assert run["stats"] == {}
    assert run["options"] == {}
    assert run["node_state"] == "pending"
    assert run["stop_reason"] is None
    assert run["started_at"] == run["updated_at"]
    assert store.active_run()["run_id"] == run_id

    store.update_run(run_id, status="paused", stats={"turns": 3},
                     current_node="fight_boss", node_state="running")
    run = store.get_run(run_id)
    assert run["status"] == "paused"
    assert run["stats"] == {"turns": 3}
    assert run["current_node"] == "fight_boss"
    assert run["node_state"] == "running"
    assert run["updated_at"] >= run["started_at"]
    assert store.active_run()["run_id"] == run_id  # paused still holds the lane

    # a partial update must not wipe the other JSON columns
    store.update_run(run_id, budgets={"max_actions": 1})
    run = store.get_run(run_id)
    assert run["budgets"] == {"max_actions": 1}
    assert run["stats"] == {"turns": 3}

    store.update_run(run_id, status="stopped", stop_reason="budget exhausted")
    assert store.active_run() is None
    assert store.get_run(run_id)["stop_reason"] == "budget exhausted"
    assert store.latest_run()["run_id"] == run_id

    with pytest.raises(ValueError) as excinfo:
        store.update_run(run_id, nonexistent_field=1)
    assert "nonexistent_field" in str(excinfo.value)
    assert store.get_run(run_id)["status"] == "stopped"  # nothing was written

    assert store.get_run("does-not-exist") is None
    assert store.latest_run(goal="something else") is None
    store.close()


def test_latest_run_filters_by_goal_and_lists_newest_first():
    store = WorldStore()
    first = store.start_run("收集十只精灵", "rule", budgets={}, options={"dry_run": True})
    second = store.start_run("击败蘑菇怪", "llm", budgets={})

    assert store.get_run(first)["options"] == {"dry_run": True}
    assert store.latest_run()["run_id"] == second
    assert store.latest_run(goal="收集十只精灵")["run_id"] == first
    assert store.latest_run(goal="击败蘑菇怪")["run_id"] == second
    assert store.latest_run(goal="没跑过的目标") is None
    assert [r["run_id"] for r in store.list_runs()] == [second, first]
    assert [r["run_id"] for r in store.list_runs(limit=1)] == [second]
    assert store.list_runs(limit=0) == []
    store.close()


# --------------------------------------------------------------------------- #
# goal nodes
# --------------------------------------------------------------------------- #
def test_node_state_upsert_and_attempts():
    store = WorldStore()
    run_id = store.start_run("击败蘑菇怪", "rule", budgets={})

    store.set_node_state(run_id, "speak_to_ni", "running", proof="panel open")
    store.set_node_state(run_id, "speak_to_ni", "failed", proof="no reply",
                         detail={"expected": "对话框打开", "attempt": 1})

    states = store.node_states(run_id)
    assert set(states) == {"speak_to_ni"}  # upsert, not a second row
    assert states["speak_to_ni"]["status"] == "failed"
    assert states["speak_to_ni"]["proof"] == "no reply"
    assert states["speak_to_ni"]["detail"] == {"expected": "对话框打开", "attempt": 1}
    assert states["speak_to_ni"]["attempts"] == 0

    assert store.bump_attempt(run_id, "speak_to_ni") == 1
    assert store.bump_attempt(run_id, "speak_to_ni") == 2
    assert store.node_states(run_id)["speak_to_ni"]["attempts"] == 2
    # bumping an unseen node creates it as pending
    assert store.bump_attempt(run_id, "fight_boss") == 1
    states = store.node_states(run_id)
    assert list(states) == ["fight_boss", "speak_to_ni"]  # stable ordering
    assert states["fight_boss"]["status"] == "pending"

    assert store.node_states("another-run") == {}
    store.close()


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def test_events_filter_by_run_and_kind_with_tail_ordering():
    store = WorldStore()
    run_a = store.start_run("goal a", "rule", budgets={})
    run_b = store.start_run("goal b", "rule", budgets={})

    e_start = store.append_event(run_a, "node.start", "开始：走进克洛斯星")
    e_fail = store.append_event(run_a, "node.fail", "没有反应", {"node": "enter"})
    e_stop = store.append_event(run_b, "run.stop", "预算用尽")
    e_free = store.append_event(None, "store.note", "no run")

    assert e_start < e_fail < e_stop < e_free  # append-only, monotonic ids
    assert [e["id"] for e in store.events()] == [e_free, e_stop, e_fail, e_start]
    assert [e["id"] for e in store.events(run_id=run_a)] == [e_fail, e_start]
    assert [e["id"] for e in store.events(kind="node.start")] == [e_start]
    assert [e["id"] for e in store.events(run_id=run_a, kind="node.fail")] == [e_fail]
    assert [e["id"] for e in store.events(limit=2)] == [e_free, e_stop]
    assert store.events(limit=0) == []
    assert store.events(run_id="missing") == []

    newest = store.events(run_id=run_a)[0]
    assert newest["payload"] == {"node": "enter"}
    assert newest["message"] == "没有反应"
    assert newest["ts"]
    assert store.events(run_id=run_a)[1]["payload"] == {}
    store.close()


# --------------------------------------------------------------------------- #
# checkpoints
# --------------------------------------------------------------------------- #
def test_checkpoint_stores_the_world_and_last_checkpoint_decodes_it():
    store = WorldStore()
    run_id = store.start_run("击败蘑菇怪", "rule", budgets={})
    world = full_world()

    first = store.checkpoint(run_id, "before_boss", world, payload={"node": "fight_boss"})
    last = store.last_checkpoint(run_id)
    assert last is not None
    assert last["id"] == first
    assert last["label"] == "before_boss"
    assert last["payload"] == {"node": "fight_boss"}
    assert last["verified"] is True
    assert last["world"] is not None
    assert last["world"].to_dict() == world.to_dict()

    second = store.checkpoint(run_id, "after_boss", full_world(level=16), verified=False)
    listed = store.checkpoints(run_id)
    assert [c["id"] for c in listed] == [second, first]
    assert listed[0]["verified"] is False
    assert listed[0]["world"].pets["烈火猴"].level == 16
    assert [c["id"] for c in store.checkpoints(run_id, limit=1)] == [second]
    assert store.checkpoints("another-run") == []
    assert store.last_checkpoint("another-run") is None
    store.close()


# --------------------------------------------------------------------------- #
# AI accounting
# --------------------------------------------------------------------------- #
def test_ai_cost_aggregates_calls_costs_and_failures():
    store = WorldStore()
    run_id = store.start_run("用 AI 规划", "llm", budgets={})

    store.record_ai_call(run_id, provider="deepseek", model="v4.1", prompt_chars=1200,
                         response_chars=300, latency_ms=1234.5, status="ok", cost_usd=0.02)
    store.record_ai_call(run_id, provider="deepseek", model="v4.1", prompt_chars=900,
                         response_chars=0, latency_ms=8000.0, status="error",
                         error="timeout")  # no cost reported
    store.record_ai_call(None, provider="local", model="rule", prompt_chars=0,
                         response_chars=0, latency_ms=0.5, status="success", cost_usd=0.0)

    total = store.ai_cost()
    assert total["calls"] == 3
    assert total["cost_usd"] == pytest.approx(0.02)
    assert total["failures"] == 1

    per_run = store.ai_cost(run_id)
    assert per_run["calls"] == 2
    assert per_run["cost_usd"] == pytest.approx(0.02)
    assert per_run["failures"] == 1

    assert store.ai_cost("missing-run") == {"calls": 0, "cost_usd": 0.0, "failures": 0}
    store.close()


# --------------------------------------------------------------------------- #
# durability
# --------------------------------------------------------------------------- #
def test_state_persists_across_close_and_reopen(tmp_path: Path):
    db = tmp_path / "nested" / "run.db"  # parent dirs are created on demand
    store = WorldStore(db)
    world = full_world()
    snapshot_id = store.save_world(world)
    run_id = store.start_run("击败蘑菇怪", "rule", budgets={"max_actions": 5},
                             options={"dry_run": True})
    store.set_node_state(run_id, "fight_boss", "done", proof="level 15 -> 16")
    store.append_event(run_id, "run.start", "hello")
    store.checkpoint(run_id, "start", world)
    store.record_ai_call(run_id, provider="p", model="m", prompt_chars=1,
                         response_chars=1, latency_ms=2.0, status="ok", cost_usd=0.5)
    store.close()
    store.close()  # idempotent

    reopened = WorldStore(db)
    assert reopened.path == store.path
    assert os.path.exists(db)

    assert reopened.latest_world().to_dict() == world.to_dict()
    assert reopened.world_snapshot(snapshot_id).to_dict() == world.to_dict()
    assert reopened.get_run(run_id)["budgets"] == {"max_actions": 5}
    assert reopened.get_run(run_id)["options"] == {"dry_run": True}
    assert reopened.node_states(run_id)["fight_boss"]["proof"] == "level 15 -> 16"
    assert [e["kind"] for e in reopened.events(run_id)] == ["run.start"]
    assert reopened.last_checkpoint(run_id)["world"].to_dict() == world.to_dict()
    assert reopened.ai_cost(run_id)["calls"] == 1
    reopened.close()

    # WAL is a persistent property of the database file, not just of the handle.
    assert raw_rows(db, "PRAGMA journal_mode")[0][0].lower() == "wal"
    # No stray journal left mid-flight after a clean close.
    assert not os.path.exists(str(db) + "-journal")


def test_a_second_connection_sees_committed_state(tmp_path: Path):
    """The CLI reads the same DB file while the runner keeps writing (WAL)."""

    db = tmp_path / "shared.db"
    writer = WorldStore(db)
    world = full_world()
    writer.save_world(world)
    run_id = writer.start_run("共享库", "rule", budgets={"max_actions": 3})

    reader = WorldStore(db)  # second handle, as ``seerauto goals`` would open
    assert reader.latest_world().to_dict() == world.to_dict()
    assert reader.get_run(run_id)["status"] == "running"
    assert reader.world_history()[0]["run_id"] == RUN_ID

    writer.append_event(run_id, "node.start", "writer")
    assert [e["kind"] for e in reader.events(run_id)] == ["node.start"]
    # both handles may write; ids stay monotonic across connections
    reader.append_event(run_id, "cli.note", "reader")
    assert [e["kind"] for e in writer.events(run_id)] == ["cli.note", "node.start"]

    reader.close()
    writer.close()


def test_save_world_is_atomic_when_a_fact_row_fails():
    """The snapshot and its fact rows are one transaction: no half a world."""

    store = WorldStore()
    store._conn.execute(
        "CREATE TRIGGER reject_facts BEFORE INSERT ON facts"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(StoreError):
        store.save_world(full_world())

    assert store.world_history() == []          # the snapshot row rolled back too
    assert store.fact_history("session.logged_in") == []

    store._conn.execute("DROP TRIGGER reject_facts")
    assert store.save_world(full_world()) > 0
    assert len(store.world_history()) == 1
    store.close()


def test_corrupt_json_never_crashes_reads(tmp_path: Path):
    db = tmp_path / "corrupt.db"
    store = WorldStore(db)
    world = full_world()
    snapshot_id = store.save_world(world)
    run_id = store.start_run("击败蘑菇怪", "rule", budgets={"max_actions": 1})
    store.update_run(run_id, stats={"turns": 2})
    checkpoint_id = store.checkpoint(run_id, "anchor", world)
    store.close()

    # Simulate a torn/partial write: valid rows, unparsable payloads.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE world_snapshots SET body_json = ? WHERE id = ?",
                 ("{not json at all", snapshot_id))
    conn.execute("UPDATE goal_runs SET stats_json = ? WHERE run_id = ?",
                 ("<xml/>", run_id))
    conn.execute("UPDATE checkpoints SET world_json = ? WHERE id = ?",
                 ("not-json", checkpoint_id))
    conn.execute("UPDATE facts SET value_json = ? WHERE snapshot_id = ? AND path = ?",
                 ("\x00garbage", snapshot_id, "pets.烈火猴.level"))
    conn.commit()
    conn.close()

    reopened = WorldStore(db)
    assert reopened.latest_world() is None            # no decodable snapshot
    assert reopened.world_snapshot(snapshot_id) is None
    assert reopened.get_run(run_id)["stats"] == {}    # falls back to the empty dict
    assert reopened.get_run(run_id)["status"] == "running"  # the rest is intact
    last = reopened.last_checkpoint(run_id)           # metadata still usable
    assert last is not None
    assert last["label"] == "anchor"
    assert last["world"] is None
    assert reopened.fact_history("pets.烈火猴.level")[0]["value"] is None

    # And the store is still writable: the next snapshot recovers the run.
    fresh = full_world(level=16)
    assert reopened.save_world(fresh) > snapshot_id
    assert reopened.latest_world().pets["烈火猴"].level == 16
    reopened.close()


def test_empty_store_reads_are_benign():
    store = WorldStore()
    assert store.latest_world() is None
    assert store.world_snapshot(1) is None
    assert store.world_history() == []
    assert store.fact_history("pets.烈火猴.level") == []
    assert store.get_run("nope") is None
    assert store.latest_run() is None
    assert store.active_run() is None
    assert store.list_runs() == []
    assert store.node_states("nope") == {}
    assert store.events() == []
    assert store.last_checkpoint("nope") is None
    assert store.checkpoints("nope") == []
    assert store.ai_cost() == {"calls": 0, "cost_usd": 0.0, "failures": 0}

    # An untouched world still persists: no scene fact must not crash the store.
    from seerauto.world.model import empty_world

    snapshot_id = store.save_world(empty_world())
    assert snapshot_id > 0
    assert store.world_history()[0]["scene"] == "UNKNOWN"
    assert store.latest_world() is not None
    # ... and a Scene enum stored as the fact value is unwrapped, not blanked.
    world = full_world()
    world.set_fact("session.scene", Scene.BATTLE,
                   Evidence.live("template", observed_at=T0))
    store.save_world(world)
    assert store.world_history()[0]["scene"] == "BATTLE"
    store.close()


def test_exotic_payloads_are_coerced_not_dropped():
    """Observations may carry sets/tuples; losing the whole row would be worse."""

    store = WorldStore()
    world = GameWorldState(captured_at=T0)
    world.set_fact("misc.tags", {"a", "b"}, Evidence.live("probe"))
    snapshot_id = store.save_world(world)
    assert snapshot_id > 0

    history = store.fact_history("misc.tags")
    assert sorted(history[0]["value"]) == ["a", "b"]
    assert store.latest_world().facts["misc.tags"].value in (["a", "b"], ["b", "a"])
    store.close()


def test_json_columns_are_utf8_and_key_sorted(tmp_path: Path):
    db = tmp_path / "json.db"
    store = WorldStore(db)
    store.save_world(full_world())
    store.start_run("击败蘑菇怪", "rule", budgets={"zeta": 1, "alpha": 2})
    store.close()

    body = raw_rows(db, "SELECT body_json FROM world_snapshots")[0][0]
    assert "烈火猴" in body          # ensure_ascii=False keeps CJK readable
    assert "\\u" not in body
    budget = raw_rows(db, "SELECT budget_json FROM goal_runs")[0][0]
    assert budget == '{"alpha": 2, "zeta": 1}'  # sort_keys=True
    assert json.loads(body)["pets"]["烈火猴"]["level"] == 15


# --------------------------------------------------------------------------- #
# lifecycle + threads
# --------------------------------------------------------------------------- #
def test_closed_store_raises_store_error_and_context_manager_closes():
    store = WorldStore()
    store.close()

    with pytest.raises(StoreError):
        store.latest_world()
    with pytest.raises(StoreError):
        store.append_event(None, "x")
    store.close()  # second close is a no-op

    with WorldStore() as scoped:
        scoped.append_event(None, "boot")
        assert scoped.ai_cost()["calls"] == 0
    with pytest.raises(StoreError):
        scoped.ai_cost()


def test_concurrent_writers_are_serialised():
    store = WorldStore()
    run_id = store.start_run("并发写入", "rule", budgets={})

    def worker(n: int) -> list[int]:
        ids = [store.append_event(run_id, "tick", f"w{n}-{i}") for i in range(20)]
        ids.append(store.save_world(full_world(level=n), note=f"w{n}"))
        return ids

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))

    event_ids = [i for batch in results for i in batch[:-1]]
    assert len(set(event_ids)) == 80
    assert len(store.events(run_id=run_id, limit=200)) == 80
    assert len(store.world_history(limit=10)) == 4
    store.close()
