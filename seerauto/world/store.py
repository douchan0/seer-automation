"""Crash-safe persistence for the goal runner (stdlib ``sqlite3`` only).

The runner is a long-horizon loop: it observes the client, decides, acts and
must survive a crash between any two of those steps.  This module is the only
place that writes that state to disk, and it is deliberately boring:

* every multi-row write is one transaction, so a world snapshot and its
  flattened fact index can never disagree;
* reads never raise on a malformed payload -- a corrupt blob decodes to
  ``None`` / ``{}`` and the caller decides.  Refusing to read is how a
  recovery path turns one bad row into a lost run;
* ``run_id`` columns carry **no** ``FOREIGN KEY``: the store is a crash log and
  must accept an event, a node state or a checkpoint whose parent run row is
  still being reconstructed after a crash.  ``PRAGMA foreign_keys=ON`` is set
  anyway so any future FK is actually enforced;
* a file DB runs in WAL with ``synchronous=NORMAL``: committed data survives a
  process crash (the WAL is replayed on the next open) without paying an fsync
  per event in a live loop.  Losing the very last transaction to a power cut is
  acceptable; corrupting the DB is not.

JSON columns are always written with ``ensure_ascii=False, sort_keys=True`` so
CJK fact paths (``pets.烈火猴.level``) stay readable in the raw file and diffs
of two snapshots are stable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from .model import Evidence, GameWorldState, Scene, now_iso

__all__ = ["WorldStore", "StoreError"]


class StoreError(RuntimeError):
    """The store cannot be opened, was closed, or broke an internal invariant."""


#: ``active_run`` treats these statuses as "still alive".
_ACTIVE_STATUSES: tuple[str, ...] = ("running", "paused")

#: ``record_ai_call`` statuses that ``ai_cost`` does not count as failures.
_AI_OK_STATUSES: tuple[str, ...] = ("ok", "success")

#: Accepted ``update_run`` keyword -> column.  Anything else is a programming
#: error and raises ``ValueError`` instead of silently updating nothing.
_RUN_UPDATE_COLUMNS: dict[str, str] = {
    "goal": "goal",
    "planner": "planner",
    "status": "status",
    "started_at": "started_at",
    "updated_at": "updated_at",
    "current_node": "current_node",
    "node_state": "node_state",
    "stop_reason": "stop_reason",
    "last_error": "last_error",
    "next_recovery_action": "next_recovery_action",
    # JSON payloads: accept the decoded key *and* the raw column name, because
    # ``get_run`` hands back ``budgets`` / ``stats`` / ``options``.
    "budget_json": "budget_json",
    "budgets": "budget_json",
    "budget": "budget_json",
    "stats_json": "stats_json",
    "stats": "stats_json",
    "options_json": "options_json",
    "options": "options_json",
}

_JSON_RUN_COLUMNS = frozenset({"budget_json", "stats_json", "options_json"})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    captured_at TEXT NOT NULL,
    scene       TEXT NOT NULL DEFAULT 'UNKNOWN',
    note        TEXT NOT NULL DEFAULT '',
    body_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_world_snapshots_run  ON world_snapshots(run_id, id);
CREATE INDEX IF NOT EXISTS idx_world_snapshots_time ON world_snapshots(captured_at);

-- Optional flattened view: one row per fact per snapshot, for
-- ``fact_history('pets.烈火猴.level')`` without decoding every body.
CREATE TABLE IF NOT EXISTS facts (
    snapshot_id INTEGER NOT NULL,
    path        TEXT NOT NULL,
    value_json  TEXT,
    source      TEXT NOT NULL DEFAULT 'UNKNOWN',
    confidence  REAL NOT NULL DEFAULT 0.0,
    observed_at TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT '',
    ref         TEXT,
    PRIMARY KEY (snapshot_id, path)
);
CREATE INDEX IF NOT EXISTS idx_facts_path ON facts(path, observed_at);

CREATE TABLE IF NOT EXISTS goal_runs (
    run_id               TEXT PRIMARY KEY,
    goal                 TEXT NOT NULL,
    planner              TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'running',
    started_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    current_node         TEXT,
    node_state           TEXT NOT NULL DEFAULT 'pending',
    budget_json          TEXT NOT NULL DEFAULT '{}',
    stats_json           TEXT NOT NULL DEFAULT '{}',
    options_json         TEXT NOT NULL DEFAULT '{}',
    stop_reason          TEXT,
    last_error           TEXT,
    next_recovery_action TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_runs_status ON goal_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_goal_runs_goal   ON goal_runs(goal, started_at);

CREATE TABLE IF NOT EXISTS node_states (
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    proof       TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    attempts    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, node_id)
);

-- Append-only: there is deliberately no update/delete API for events.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run  ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL,
    world_json   TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    verified     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, id);

CREATE TABLE IF NOT EXISTS ai_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT,
    ts             TEXT NOT NULL,
    provider       TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    prompt_chars   INTEGER NOT NULL DEFAULT 0,
    response_chars INTEGER NOT NULL DEFAULT 0,
    latency_ms     REAL NOT NULL DEFAULT 0.0,
    status         TEXT NOT NULL DEFAULT 'ok',
    error          TEXT,
    cost_usd       REAL
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_run  ON ai_calls(run_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_calls_time ON ai_calls(ts);
"""


# --------------------------------------------------------------------------- #
# JSON plumbing
# --------------------------------------------------------------------------- #
def _dumps(value: Any) -> str:
    """Stable JSON text.  Degrades exotic payloads instead of losing the row."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        # numpy scalars, sets, datetimes... -- the observation is still worth
        # keeping, and the model already knows how to flatten those.
        from .model import _jsonable

        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _loads(raw: Any) -> Any:
    """Decode a JSON column, ``None`` when the blob is not JSON at all."""

    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _json_dict(raw: Any) -> dict[str, Any]:
    value = _loads(raw)
    return dict(value) if isinstance(value, Mapping) else {}


def _limit(limit: Any) -> int:
    """SQLite treats a negative LIMIT as 'no limit'; never expose that."""

    try:
        return max(0, int(limit))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# world <-> fact rows
# --------------------------------------------------------------------------- #
def _fact_rows(state: GameWorldState) -> list[tuple[str, str, str, float, str, str, str | None]]:
    """Flatten one world into ``(path, value_json, source, confidence, ...)``.

    Derived paths (``pets.<name>.level``, ``resources.<name>``,
    ``spt.<name>.defeated``) are produced here because the goal-runner's most
    common history question -- "has this pet levelled up?" -- is not a
    ``state.facts`` entry.  Explicit facts are added last so they win on a
    path collision.
    """

    rows: dict[str, tuple[str, str, str, float, str, str, str | None]] = {}

    def add(path: str, value: Any, evidence: Evidence) -> None:
        rows[path] = (
            path,
            _dumps(value),
            evidence.source.value,
            round(float(evidence.confidence), 3),
            evidence.observed_at,
            evidence.origin,
            evidence.ref,
        )

    for pet in state.pets.values():
        add(f"pets.{pet.name}.level", pet.level, pet.evidence)
        add(f"pets.{pet.name}.hp", pet.hp, pet.evidence)
        add(f"pets.{pet.name}.hp_max", pet.hp_max, pet.evidence)
        add(f"pets.{pet.name}.active", pet.active, pet.evidence)
    for resource in state.resources.values():
        add(f"resources.{resource.name}", resource.amount, resource.evidence)
    for entry in state.spt.values():
        add(f"spt.{entry.name or entry.boss_id}.defeated", entry.defeated, entry.evidence)
    for path, fact in state.unlocked.items():
        add(path, fact.value, fact.evidence)
    for path, fact in state.dailies.items():
        add(path, fact.value, fact.evidence)
    for path, fact in state.facts.items():
        add(path, fact.value, fact.evidence)
    return list(rows.values())


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["budgets"] = _json_dict(out.pop("budget_json", None))
    out["stats"] = _json_dict(out.pop("stats_json", None))
    out["options"] = _json_dict(out.pop("options_json", None))
    return out


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "run_id": row["run_id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "message": row["message"],
        "payload": _json_dict(row["payload_json"]),
    }


def _checkpoint_dict(row: sqlite3.Row) -> dict[str, Any]:
    body = _loads(row["world_json"])
    world: GameWorldState | None = None
    if isinstance(body, Mapping):
        try:
            world = GameWorldState.from_dict(body)
        except Exception:  # noqa: BLE001 - a bad anchor must not kill recovery
            world = None
    return {
        "id": int(row["id"]),
        "run_id": row["run_id"],
        "label": row["label"],
        "ts": row["ts"],
        "payload": _json_dict(row["payload_json"]),
        "verified": bool(row["verified"]),
        "world": world,
    }


class WorldStore:
    """One SQLite file (or an in-memory DB) holding the runner's whole history.

    A single connection is shared by one process; ``check_same_thread=False``
    plus an ``RLock`` makes the observer thread and the runner thread safe to
    interleave, and ``busy_timeout`` lets a separate CLI reader coexist.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        raw = None if path is None else os.fspath(path)
        # ``None`` / ``":memory:"`` are the ephemeral case used by tests and
        # dry runs; anything else is a real file we create if missing.
        if raw in (None, "", ":memory:"):
            self.path: str | None = None
        else:
            self.path = os.path.abspath(os.path.expanduser(raw))
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

        try:
            if self.path is not None:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self._conn = self._connect()
            self._conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            self.close()
            raise StoreError(f"cannot open store {self.path or ':memory:'}: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path or ":memory:",
            timeout=5.0,
            # The connection is shared across threads; every use is serialised
            # by ``self._lock`` (sqlite3.threadsafety is 1 on this Python).
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if self.path is not None:
            # WAL is persistent per database file and must be set outside any
            # transaction; harmless (returns 'memory') for an in-memory DB.
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- lifecycle --------------------------------------------------------- #
    def _db(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise StoreError("store is closed")
        return conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:  # noqa: BLE001 - closing is best effort
                    pass

    def __enter__(self) -> "WorldStore":
        self._db()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"WorldStore({self.path or ':memory:'}{', closed' if self._conn is None else ''})"

    # ------------------------------------------------------------------ #
    # world
    # ------------------------------------------------------------------ #
    def save_world(self, state: GameWorldState, *, run_id: str | None = None,
                   note: str = "") -> int:
        """Persist one snapshot plus its flattened facts; returns the snapshot id.

        The serialised body and the indexed ``run_id`` column are kept in
        agreement (an explicit ``run_id`` argument wins and is stamped into the
        body) so ``latest_world()`` never reports a different run than
        ``world_history()``.
        """

        body = state.to_dict()
        effective_run = run_id or state.run_id
        body["run_id"] = effective_run
        captured_at = str(body.get("captured_at") or now_iso())
        # ``str(Scene.WORLD)`` is "Scene.WORLD" on 3.10, so an observer that
        # stored the enum itself must be unwrapped before parsing.
        scene_value = state.fact("session.scene")
        scene = (scene_value.value if isinstance(scene_value, Scene)
                 else Scene.parse(scene_value).value)
        rows = _fact_rows(state)

        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO world_snapshots (run_id, captured_at, scene, note, body_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (effective_run, captured_at, scene, str(note or ""), _dumps(body)),
            )
            snapshot_id = int(cur.lastrowid or 0)
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO facts (snapshot_id, path, value_json, source,"
                    " confidence, observed_at, origin, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(snapshot_id, *row) for row in rows],
                )
        return snapshot_id

    def latest_world(self) -> GameWorldState | None:
        """Newest snapshot; ``None`` when there is none or its body is unreadable."""

        with self._lock:
            row = self._db().execute(
                "SELECT body_json FROM world_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._decode_world(row["body_json"])

    def world_snapshot(self, snapshot_id: int) -> GameWorldState | None:
        """One snapshot by id; ``None`` when it is absent or unreadable."""

        with self._lock:
            row = self._db().execute(
                "SELECT body_json FROM world_snapshots WHERE id = ?", (int(snapshot_id),)
            ).fetchone()
        return None if row is None else self._decode_world(row["body_json"])

    @staticmethod
    def _decode_world(raw: Any) -> GameWorldState | None:
        body = _loads(raw)
        if not isinstance(body, Mapping):
            return None
        try:
            return GameWorldState.from_dict(body)
        except Exception:  # noqa: BLE001 - see module docstring: reads never raise
            return None

    def world_history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, run_id, captured_at, scene, note FROM world_snapshots"
            " ORDER BY id DESC LIMIT ?",
            (_limit(limit),),
        )
        return [
            {
                "id": int(r["id"]),
                "run_id": r["run_id"],
                "captured_at": r["captured_at"],
                "scene": r["scene"],
                "note": r["note"],
            }
            for r in rows
        ]

    def fact_history(self, path: str, limit: int = 50) -> list[dict[str, Any]]:
        """Every recorded value of one fact, newest observation first."""

        rows = self._query(
            "SELECT f.path, f.value_json, f.source, f.confidence, f.observed_at,"
            " f.origin, f.ref, f.snapshot_id, s.captured_at"
            " FROM facts f JOIN world_snapshots s ON s.id = f.snapshot_id"
            " WHERE f.path = ?"
            " ORDER BY f.observed_at DESC, f.snapshot_id DESC LIMIT ?",
            (str(path), _limit(limit)),
        )
        return [
            {
                "path": r["path"],
                "value": _loads(r["value_json"]),
                "source": r["source"],
                "confidence": float(r["confidence"]),
                "observed_at": r["observed_at"],
                "origin": r["origin"],
                "ref": r["ref"],
                "snapshot_id": int(r["snapshot_id"]),
                "captured_at": r["captured_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # runs
    # ------------------------------------------------------------------ #
    def start_run(self, goal: str, planner: str, *, budgets: dict[str, Any],
                  options: dict[str, Any] | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        ts = now_iso()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO goal_runs (run_id, goal, planner, status, started_at,"
                " updated_at, node_state, budget_json, stats_json, options_json)"
                " VALUES (?, ?, ?, 'running', ?, ?, 'pending', ?, '{}', ?)",
                (run_id, str(goal), str(planner), ts, ts,
                 _dumps(dict(budgets or {})), _dumps(dict(options or {}))),
            )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute(
                "SELECT * FROM goal_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return None if row is None else _run_dict(row)

    def update_run(self, run_id: str, **fields: Any) -> None:
        """Update whitelisted run fields; ``updated_at`` is touched for you."""

        columns: dict[str, Any] = {}
        unknown: list[str] = []
        for name, value in fields.items():
            column = _RUN_UPDATE_COLUMNS.get(name)
            if column is None:
                unknown.append(name)
                continue
            columns[column] = _dumps(value) if column in _JSON_RUN_COLUMNS else value
        if unknown:
            raise ValueError(
                f"update_run: unknown field(s) {sorted(unknown)}; accepted: "
                f"{sorted(set(_RUN_UPDATE_COLUMNS) | {'updated_at'})}")
        # A bare update is still a heartbeat, so include it explicitly.
        columns.setdefault("updated_at", now_iso())

        assignments = ", ".join(f"{column} = ?" for column in columns)
        with self._transaction() as conn:
            conn.execute(
                f"UPDATE goal_runs SET {assignments} WHERE run_id = ?",
                (*columns.values(), str(run_id)),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM goal_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (_limit(limit),),
        )
        return [_run_dict(r) for r in rows]

    def latest_run(self, *, goal: str | None = None) -> dict[str, Any] | None:
        if goal is None:
            rows = self._query(
                "SELECT * FROM goal_runs ORDER BY started_at DESC, rowid DESC LIMIT 1", ())
        else:
            rows = self._query(
                "SELECT * FROM goal_runs WHERE goal = ?"
                " ORDER BY started_at DESC, rowid DESC LIMIT 1", (str(goal),))
        return None if not rows else _run_dict(rows[0])

    def active_run(self) -> dict[str, Any] | None:
        placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
        rows = self._query(
            f"SELECT * FROM goal_runs WHERE status IN ({placeholders})"
            " ORDER BY updated_at DESC, started_at DESC, rowid DESC LIMIT 1",
            _ACTIVE_STATUSES,
        )
        return None if not rows else _run_dict(rows[0])

    # ------------------------------------------------------------------ #
    # goal nodes
    # ------------------------------------------------------------------ #
    def set_node_state(self, run_id: str, node_id: str, status: str, *,
                       proof: str = "", detail: dict[str, Any] | None = None) -> None:
        ts = now_iso()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO node_states (run_id, node_id, status, updated_at, proof,"
                " detail_json, attempts) VALUES (?, ?, ?, ?, ?, ?, 0)"
                " ON CONFLICT(run_id, node_id) DO UPDATE SET"
                " status = excluded.status, updated_at = excluded.updated_at,"
                " proof = excluded.proof, detail_json = excluded.detail_json",
                (str(run_id), str(node_id), str(status), ts, str(proof or ""),
                 _dumps(detail or {})),
            )

    def node_states(self, run_id: str) -> dict[str, dict[str, Any]]:
        rows = self._query(
            "SELECT node_id, status, updated_at, proof, detail_json, attempts"
            " FROM node_states WHERE run_id = ? ORDER BY node_id",
            (str(run_id),),
        )
        return {
            r["node_id"]: {
                "status": r["status"],
                "updated_at": r["updated_at"],
                "proof": r["proof"],
                "detail": _json_dict(r["detail_json"]),
                "attempts": int(r["attempts"]),
            }
            for r in rows
        }

    def bump_attempt(self, run_id: str, node_id: str) -> int:
        """Increment and return the attempt counter (creates the row if absent)."""

        ts = now_iso()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO node_states (run_id, node_id, status, updated_at, proof,"
                " detail_json, attempts) VALUES (?, ?, 'pending', ?, '', '{}', 1)"
                " ON CONFLICT(run_id, node_id) DO UPDATE SET"
                " attempts = attempts + 1, updated_at = excluded.updated_at",
                (str(run_id), str(node_id), ts),
            )
            row = conn.execute(
                "SELECT attempts FROM node_states WHERE run_id = ? AND node_id = ?",
                (str(run_id), str(node_id)),
            ).fetchone()
        if row is None:
            raise StoreError(f"bump_attempt: node {node_id!r} of run {run_id!r} vanished")
        return int(row["attempts"])

    # ------------------------------------------------------------------ #
    # events / checkpoints
    # ------------------------------------------------------------------ #
    def append_event(self, run_id: str | None, kind: str, message: str = "",
                     payload: dict[str, Any] | None = None) -> int:
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO events (run_id, ts, kind, message, payload_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, now_iso(), str(kind), str(message or ""), _dumps(payload or {})),
            )
            event_id = int(cur.lastrowid or 0)
        return event_id

    def events(self, run_id: str | None = None, limit: int = 200,
               kind: str | None = None) -> list[dict[str, Any]]:
        """Newest first -- a tail of the log, optionally one run / one kind."""

        sql = "SELECT id, run_id, ts, kind, message, payload_json FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(_limit(limit))
        return [_event_dict(r) for r in self._query(sql, params)]

    def checkpoint(self, run_id: str, label: str, world: GameWorldState, *,
                   payload: dict[str, Any] | None = None, verified: bool = True) -> int:
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO checkpoints (run_id, label, ts, world_json, payload_json,"
                " verified) VALUES (?, ?, ?, ?, ?, ?)",
                (str(run_id), str(label or ""), now_iso(), _dumps(world.to_dict()),
                 _dumps(payload or {}), 1 if verified else 0),
            )
            checkpoint_id = int(cur.lastrowid or 0)
        return checkpoint_id

    def last_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Newest recovery anchor of ``run_id``, with its world decoded."""

        rows = self._query(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (str(run_id),),
        )
        return None if not rows else _checkpoint_dict(rows[0])

    def checkpoints(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY id DESC LIMIT ?",
            (str(run_id), _limit(limit)),
        )
        return [_checkpoint_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # AI accounting
    # ------------------------------------------------------------------ #
    def record_ai_call(self, run_id: str | None, *, provider: str, model: str,
                       prompt_chars: int, response_chars: int, latency_ms: float,
                       status: str, error: str | None = None,
                       cost_usd: float | None = None) -> int:
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO ai_calls (run_id, ts, provider, model, prompt_chars,"
                " response_chars, latency_ms, status, error, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, now_iso(), str(provider), str(model), int(prompt_chars),
                 int(response_chars), float(latency_ms), str(status), error,
                 None if cost_usd is None else float(cost_usd)),
            )
            call_id = int(cur.lastrowid or 0)
        return call_id

    def ai_cost(self, run_id: str | None = None) -> dict[str, Any]:
        """``{calls, cost_usd, failures}`` for one run or the whole store."""

        placeholders = ", ".join("?" for _ in _AI_OK_STATUSES)
        sql = (
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(COALESCE(cost_usd, 0.0)), 0.0) AS cost_usd,"
            f" COALESCE(SUM(CASE WHEN status IN ({placeholders}) THEN 0 ELSE 1 END), 0)"
            " AS failures FROM ai_calls"
        )
        params: list[Any] = list(_AI_OK_STATUSES)
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params.append(run_id)
        rows = self._query(sql, params)
        row = rows[0]
        return {
            "calls": int(row["calls"]),
            "cost_usd": round(float(row["cost_usd"]), 6),
            "failures": int(row["failures"]),
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; SQLite failures surface as :class:`StoreError`.

        The lock is held for the whole transaction -- including the commit --
        so a concurrent reader can never observe this connection's uncommitted
        rows (they share it) and two writers cannot interleave their DML.
        """

        with self._lock:
            conn = self._db()
            try:
                with conn:
                    yield conn
            except sqlite3.Error as exc:
                raise StoreError(
                    f"write failed on {self.path or ':memory:'}: {exc}") from exc

    def _query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db().execute(sql, params).fetchall())
