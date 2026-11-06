#!/usr/bin/env python3
"""Measure DSH's own token spend for the dual-mode overnight experiment.

Nothing here is estimated.  Every number is read from files DSH itself wrote:

* ``$DSH_HOME/storages/session_projcache/sessions/<id>.json`` - DSH's per-session
  projection cache.  ``record.rows.tokenUsage.val.totals`` is the authoritative
  running total (``uncachedInputTokens`` / ``outputTokens`` / ``cacheReadTokens`` /
  ``cacheWriteTokens``); ``record.rows.sessionStats.val`` carries ``steps`` and
  ``turns``; ``record.rows.contextPressure.val`` carries ``surfaceTokens`` /
  ``contextWindow``.
* ``$DSH_HOME/sessions/<cwd-slug>/<id>/session.v4.jsonl[.zstd]`` - the raw event
  log.  Each ``assistant/message`` event carries ``data.usage`` (per-request
  input/output/cacheRead tokens) and the request-completion ``time`` (epoch ms).
  That is what makes the per-hour table reproducible from disk; summed over a
  settled session the raw log reconciles exactly with the procache totals (see
  the ``hours`` subcommand, which prints that reconciliation).

Read-only against ``$DSH_HOME``: this tool never writes there.

Usage:
    PYTHONPATH=.pydeps python3 tools/usage_meter.py snapshot --label EFFICIENT
    PYTHONPATH=.pydeps python3 tools/usage_meter.py report
    PYTHONPATH=.pydeps python3 tools/usage_meter.py baseline
    PYTHONPATH=.pydeps python3 tools/usage_meter.py hours
    PYTHONPATH=.pydeps python3 tools/usage_meter.py sessions

Snapshot convention (matters for the per-mode table): a snapshot closes the
interval that just ended, so ``--label`` must name the mode that was active
DURING that interval.  At a mode switch: snapshot first, then flip the mode.
The first snapshot of a session has no predecessor; its delta is measured from
the session start and is flagged ``basis=session_start``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "usage_snapshot.v1"
KIND = "usage_snapshot"

# procache field -> meter field
RAW_FIELDS = {
    "uncachedInputTokens": "uncached_input",
    "outputTokens": "output",
    "cacheReadTokens": "cache_read",
    "cacheWriteTokens": "cache_write",
}
METER_FIELDS = ("uncached_input", "output", "cache_read", "cache_write")

EVENT_FILENAMES = (
    "session.v4.jsonl.zstd",
    "session.v4.jsonl",
    "session.v3.jsonl.zstd",
    "session.v3.jsonl",
    "session.jsonl",
)

# Report thresholds.  They only decide whether a line is flagged, never how a
# number is computed.
CACHE_HIT_WARN = 0.60           # cacheRead / (cacheRead + uncached) below this is odd
SURFACE_GROWTH_WARN = 5000.0    # tokens of context added per request
SURFACE_PRESSURE_WARN = 0.80    # surfaceTokens / contextWindow

PRIOR_NIGHT_SESSION = "session-fb389015-5471-459e-837c-13d1813e9b52"

# Labels that are the experiment's mode windows.  Anything else (PREP, RECOVERY, ...)
# still gets its own row and still counts in the totals, but it is not compared against
# the prior-night baseline as if it were a mode.
MODE_LABELS = ("EFFICIENT", "MAX_RESULTS")


class MeterError(RuntimeError):
    """A precondition is missing; the message always names the path expected."""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def dsh_home() -> Path:
    """DSH state directory (read-only from here)."""
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def repo_root() -> Path:
    """Repository root = parent of tools/ (also the DSH session cwd we track)."""
    return Path(__file__).resolve().parents[1]


def experiment_dir(root: Path | None = None) -> Path:
    exp = os.environ.get("USAGE_METER_EXPERIMENT", "20261106_dual_mode")
    return (Path(root) if root else repo_root()) / "data" / "overnight" / exp


def default_usage_file(root: Path | None = None) -> Path:
    return Path(os.environ.get("USAGE_METER_FILE") or experiment_dir(root) / "model_usage.jsonl")


def default_baseline_file(root: Path | None = None) -> Path:
    return Path(os.environ.get("USAGE_METER_BASELINE") or experiment_dir(root) / "baseline_prior_night.json")


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone().isoformat(timespec="seconds")


def iso_ms(epoch_ms: int | None) -> str | None:
    if not epoch_ms:
        return None
    return iso(int(epoch_ms) / 1000.0)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def total_of(totals: dict) -> int:
    """Total tokens = uncached input + output + cache read + cache write."""
    return sum(_int(totals.get(field)) for field in METER_FIELDS)


def cache_hit_of(totals: dict) -> float:
    """Share of input tokens served from cache, 0.0 when no input at all."""
    uncached = _int(totals.get("uncached_input"))
    cached = _int(totals.get("cache_read"))
    denom = uncached + cached
    return (cached / denom) if denom else 0.0


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _human(number: float) -> str:
    """Compact magnitude for tables; raw integers stay available in --json."""
    n = float(number)
    for scale in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(n) >= scale[0]:
            return f"{n / scale[0]:.2f}{scale[1]}"
    return f"{n:.0f}"


def _val(rows: dict, name: str) -> dict:
    """Return ``rows[name]['val']`` defensively (any layer may be absent)."""
    row = rows.get(name)
    if not isinstance(row, dict):
        return {}
    val = row.get("val")
    return val if isinstance(val, dict) else {}


# ---------------------------------------------------------------------------
# procache sessions
# ---------------------------------------------------------------------------


def session_totals_from_doc(doc: dict) -> tuple[dict, list[str]]:
    """Extract the token totals from a procache document, naming missing rows."""
    missing: list[str] = []
    record = doc.get("record") if isinstance(doc.get("record"), dict) else {}
    rows = record.get("rows") if isinstance(record.get("rows"), dict) else {}
    usage = _val(rows, "tokenUsage")
    raw_totals = usage.get("totals")
    if not isinstance(raw_totals, dict):
        missing.append("tokenUsage.totals")
        raw_totals = {}
    totals = {field: _int(raw_totals.get(raw)) for raw, field in RAW_FIELDS.items()}
    return totals, missing


def parse_session_doc(doc: dict, path: Path) -> dict:
    """Turn one procache document into the flat record the rest of the tool uses."""
    record = doc.get("record") if isinstance(doc.get("record"), dict) else {}
    rows = record.get("rows") if isinstance(record.get("rows"), dict) else {}
    ident = record.get("identity") if isinstance(record.get("identity"), dict) else {}

    totals, missing = session_totals_from_doc(doc)
    stats = _val(rows, "sessionStats")
    pressure = _val(rows, "contextPressure")
    breakdown = _val(rows, "contextBreakdown")
    usage = _val(rows, "tokenUsage")

    if not stats:
        missing.append("sessionStats")
    if not pressure:
        missing.append("contextPressure")

    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        mtime = None

    return {
        "session": Path(path).stem,
        "path": str(Path(path)),
        "cwd": ident.get("cwd"),
        "created_at_ms": _int(ident.get("createdAt")) or None,
        "mtime": mtime,
        "mtime_iso": iso(mtime),
        "steps": _int(stats.get("steps")),
        "turns": _int(stats.get("turns")),
        "surface_tokens": _int(pressure.get("surfaceTokens")),
        "context_window": _int(pressure.get("contextWindow")),
        "pressure_tokens": _int(pressure.get("pressureTokens")),
        "breakdown": breakdown.get("breakdown") if isinstance(breakdown.get("breakdown"), dict) else {},
        "llm_ms": _int(stats.get("llmMs")),
        "tool_ms": _int(stats.get("toolMs")),
        "last": usage.get("last") if isinstance(usage.get("last"), dict) else {},
        "totals": totals,
        "total": total_of(totals),
        "cache_hit": cache_hit_of(totals),
        "missing": missing,
    }


def load_session_file(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise MeterError(f"session procache file not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MeterError(f"cannot parse procache JSON {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise MeterError(f"unexpected procache layout in {path}: top level is not an object")
    return parse_session_doc(doc, path)


def procache_dir(home: Path | None = None) -> Path:
    return (Path(home) if home else dsh_home()) / "storages" / "session_projcache" / "sessions"


def _procache_path(home: Path | None, session_id: str) -> Path | None:
    bare = session_id[8:] if session_id.startswith("session-") else session_id
    for name in (f"session-{bare}.json", f"{bare}.json"):
        candidate = procache_dir(home) / name
        if candidate.is_file():
            return candidate
    return None


def cwd_slug(cwd: str | None) -> str | None:
    """DSH's directory slug for a cwd, e.g. ``/a/b`` -> ``--a-b--``."""
    if not cwd:
        return None
    return "--" + str(cwd).strip("/").replace("/", "-") + "--"


def find_raw_log(home: Path | None, session_id: str, cwd: str | None = None) -> Path | None:
    """Locate the raw event log for a session (preferring the cwd's own bucket)."""
    root = (Path(home) if home else dsh_home()) / "sessions"
    if not root.is_dir():
        return None
    bare = session_id[8:] if session_id.startswith("session-") else session_id
    names = {session_id, bare, f"session-{bare}"}
    parents: list[Path] = []
    slug = cwd_slug(cwd)
    if slug and (root / slug).is_dir():
        parents.append(root / slug)
    for child in sorted(root.iterdir()):
        if child.is_dir() and child not in parents:
            parents.append(child)
    for parent in parents:
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and child.name in names:
                for filename in EVENT_FILENAMES:
                    candidate = child / filename
                    if candidate.is_file():
                        return candidate
    return None


# ---------------------------------------------------------------------------
# raw event log
# ---------------------------------------------------------------------------


def _iter_json_lines(stream):
    """Yield parsed JSON objects; a truncated/broken line is skipped, not fatal."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def read_events(path: Path):
    """Stream every event object of a raw session log (plain or zstd)."""
    path = Path(path)
    if not path.is_file():
        raise MeterError(f"raw session log not found: {path}")
    if path.suffix == ".zstd":
        binary = shutil.which("zstd") or ("/opt/homebrew/bin/zstd" if Path("/opt/homebrew/bin/zstd").is_file() else None)
        if not binary:
            raise MeterError("zstd binary not found; cannot decompress %s (brew install zstd)" % path)
        proc = subprocess.Popen(
            [binary, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        try:
            assert proc.stdout is not None
            yield from _iter_json_lines(proc.stdout)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            proc.wait()
        return
    with open(path, encoding="utf-8", errors="replace") as stream:
        yield from _iter_json_lines(stream)


def first_event(path: Path) -> dict:
    """First event only - cheap enough to run once per candidate session."""
    path = Path(path)
    if path.suffix == ".zstd":
        binary = shutil.which("zstd") or ("/opt/homebrew/bin/zstd" if Path("/opt/homebrew/bin/zstd").is_file() else None)
        if not binary:
            return {}
        proc = subprocess.Popen([binary, "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            proc.kill()
            proc.wait()
    else:
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                line = stream.readline()
        except OSError:
            return {}
    try:
        event = json.loads(line.strip())
    except (json.JSONDecodeError, AttributeError):
        return {}
    return event if isinstance(event, dict) else {}


def session_origin(home: Path | None, session_id: str, cwd: str | None) -> dict:
    """Delegation facts taken from the raw log's session header event."""
    raw = find_raw_log(home, session_id, cwd)
    if raw is None:
        return {"raw_log": None, "depth": None, "parent": None, "origin": None}
    header = first_event(raw)
    return {
        "raw_log": str(raw),
        "depth": header.get("delegationDepth"),
        "parent": header.get("parentSession"),
        "origin": header.get("origin"),
    }


def usage_events(path: Path) -> list[dict]:
    """All per-request usage records of a raw log: one dict per LLM request."""
    out: list[dict] = []
    for event in read_events(path):
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        if not usage:
            continue
        out.append(
            {
                "type": event.get("type"),
                "time_ms": _int(event.get("time")) or None,
                "turn": data.get("turn"),
                "step": data.get("step"),
                "uncached_input": _int(usage.get("inputTokens")),
                "output": _int(usage.get("outputTokens")),
                "cache_read": _int(usage.get("cacheReadTokens")),
                "cache_write": _int(usage.get("cacheWriteTokens")),
                "total": _int(usage.get("totalTokens")),
            }
        )
    return out


def request_stats(events: list[dict]) -> dict:
    """Per-request distribution: mean/median/p90/p95 for prompt and total tokens."""
    if not events:
        return {"requests": 0}
    prompts = sorted(e["uncached_input"] + e["cache_read"] for e in events)
    totals = sorted(e["total"] or (e["uncached_input"] + e["cache_read"] + e["output"]) for e in events)
    outputs = sorted(e["output"] for e in events)

    def quantile(values: list[int], q: float) -> float:
        if not values:
            return 0.0
        pos = (len(values) - 1) * q
        low, high = int(pos), min(int(pos) + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (pos - low)

    return {
        "requests": len(events),
        "prompt_mean": round(sum(prompts) / len(prompts), 1),
        "prompt_median": round(quantile(prompts, 0.5), 1),
        "prompt_p90": round(quantile(prompts, 0.9), 1),
        "prompt_p95": round(quantile(prompts, 0.95), 1),
        "prompt_max": prompts[-1],
        "total_mean": round(sum(totals) / len(totals), 1),
        "total_median": round(quantile(totals, 0.5), 1),
        "output_mean": round(sum(outputs) / len(outputs), 1),
        "output_median": round(quantile(outputs, 0.5), 1),
    }


def hour_buckets(events: list[dict], day: str | None = None) -> dict:
    """Bucket per-request usage by local completion hour.

    Returns ``{"rows": [...], "sum": {...}, "requests": n, "malformed": 0}``.
    ``day`` filters to one local date (``YYYY-MM-DD``).
    """
    rows: dict[str, dict] = {}
    total = {field: 0 for field in METER_FIELDS}
    requests = 0
    for event in events:
        if not event.get("time_ms"):
            continue
        stamp = iso(event["time_ms"] / 1000.0)
        if not stamp:
            continue
        local = datetime.fromisoformat(stamp)
        key = local.strftime("%Y-%m-%d %H")
        if day and not key.startswith(day):
            continue
        row = rows.setdefault(
            key,
            {
                "hour": key,
                "requests": 0,
                "uncached_input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "total": 0,
            },
        )
        row["requests"] += 1
        for field in METER_FIELDS:
            row[field] += event[field]
            total[field] += event[field]
        row["total"] += event.get("total") or (event["uncached_input"] + event["cache_read"] + event["output"])
        requests += 1
    for row in rows.values():
        row["cache_hit"] = cache_hit_of(row)
        row["avg_total_per_request"] = row["total"] / row["requests"] if row["requests"] else 0.0
    ordered = [rows[key] for key in sorted(rows)]
    return {"rows": ordered, "sum": total, "requests": requests}


# ---------------------------------------------------------------------------
# session discovery
# ---------------------------------------------------------------------------


def _same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def list_sessions(home: Path | None = None, root: Path | None = None, all_cwds: bool = False) -> list[dict]:
    """All sessions DSH's procache knows, newest first, annotated with delegation info."""
    repo = str((Path(root) if root else repo_root()).resolve())
    directory = procache_dir(home)
    if not directory.is_dir():
        raise MeterError(f"DSH procache directory not found: {directory}")
    sessions: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        session = parse_session_doc(doc, path)
        session["in_repo"] = _same_path(session.get("cwd"), repo)
        if not session["in_repo"] and not all_cwds:
            continue
        sessions.append(session)
    for session in sessions:
        session.update(session_origin(home, session["session"], session.get("cwd")))
        session["delegated"] = bool(session.get("parent")) or bool(session.get("depth"))
    sessions.sort(key=lambda s: (s.get("mtime") or 0), reverse=True)
    return sessions


def resolve_session(home: Path | None = None, root: Path | None = None, spec: str | None = "auto") -> dict:
    """Resolve ``auto`` / a session id / a procache path to a parsed session.

    ``auto`` means: newest **top-level** session (no ``parentSession`` in its raw
    log header) whose cwd is this repo.  Subagent sessions share the repo cwd, so
    preferring the bare "newest file" would silently measure a teammate instead
    of the session the night's accounting is about.  ``--session newest`` returns
    the literal newest repo session.
    """
    spec = (spec or "auto").strip()
    if spec not in ("auto", "newest"):
        candidate = Path(spec).expanduser()
        if candidate.is_file():
            return load_session_file(candidate)
        path = _procache_path(home, spec)
        if path is None:
            known = [s["session"] for s in list_sessions(home, root)]
            raise MeterError(f"session not found: {spec}; repo sessions: {', '.join(known[:8]) or 'none'}")
        return load_session_file(path)

    sessions = list_sessions(home, root)
    if not sessions:
        raise MeterError(f"no DSH sessions with cwd under {(Path(root) if root else repo_root())}")
    if spec == "auto":
        top_level = [s for s in sessions if s.get("depth") == 0] or [s for s in sessions if not s.get("parent")]
        if top_level:
            return top_level[0]
    return sessions[0]


def delegate_usage(home: Path | None, parent_session: str, cwd: str | None) -> dict:
    """Tokens spent by subagent sessions spawned by ``parent_session``.

    DSH keeps a subagent's usage in its own session file, so the parent's totals
    do not include it.  We surface it separately instead of silently dropping it.
    """
    root = (Path(home) if home else dsh_home()) / "sessions"
    empty = {"count": 0, "session_ids": [], "steps": 0, "totals": {field: 0 for field in METER_FIELDS}, "total": 0}
    slug = cwd_slug(cwd)
    if not slug or not (root / slug).is_dir():
        return empty
    children: list[str] = []
    for child in sorted((root / slug).iterdir()):
        if not child.is_dir():
            continue
        raw = None
        for filename in EVENT_FILENAMES:
            if (child / filename).is_file():
                raw = child / filename
                break
        if raw is None:
            continue
        header = first_event(raw)
        if header.get("parentSession") == parent_session:
            children.append(child.name)
    totals = {field: 0 for field in METER_FIELDS}
    steps = 0
    found: list[str] = []
    for child_id in children:
        path = _procache_path(home, child_id)
        if path is None:
            continue
        try:
            session = load_session_file(path)
        except MeterError:
            continue
        found.append(child_id)
        steps += _int(session.get("steps"))
        for field in METER_FIELDS:
            totals[field] += session["totals"][field]
    payload = dict(empty)
    payload.update({"count": len(found), "session_ids": found, "steps": steps, "totals": totals, "total": total_of(totals)})
    payload["cache_hit"] = cache_hit_of(totals)
    return payload


# ---------------------------------------------------------------------------
# snapshot records
# ---------------------------------------------------------------------------


def read_usage_records(path: Path) -> tuple[list[dict], dict]:
    """Read model_usage.jsonl; foreign ``kind`` lines are counted, not parsed."""
    path = Path(path)
    meta = {"path": str(path), "lines": 0, "records": 0, "foreign": 0, "malformed": 0}
    records: list[dict] = []
    if not path.is_file():
        return records, meta
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            meta["lines"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                meta["malformed"] += 1
                continue
            if not isinstance(record, dict):
                meta["malformed"] += 1
                continue
            if record.get("kind") not in (None, KIND):
                meta["foreign"] += 1
                continue
            if not isinstance(record.get("totals"), dict):
                meta["malformed"] += 1
                continue
            records.append(record)
    records.sort(key=lambda r: _int(r.get("epoch_ms")))
    meta["records"] = len(records)
    return records, meta


def append_jsonl(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n"
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()


def last_snapshot_for(records: list[dict], session_id: str) -> dict | None:
    for record in reversed(records):
        if record.get("session") == session_id:
            return record
    return None


def build_snapshot_record(
    session: dict,
    previous: dict | None = None,
    label: str = "",
    note: str = "",
    delegates: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Pure function: one JSONL snapshot line for a parsed procache session."""
    now = now or _now()
    epoch_ms = int(now.timestamp() * 1000)
    totals = {field: _int(session.get("totals", {}).get(field)) for field in METER_FIELDS}
    steps = _int(session.get("steps"))
    turns = _int(session.get("turns"))
    requests_delta = steps
    basis = "session_start"
    since_ts = None
    seconds = 0.0
    if session.get("created_at_ms"):
        seconds = max(0.0, (epoch_ms - _int(session["created_at_ms"])) / 1000.0)
    delta = dict(totals)
    delta_steps = steps
    if previous and previous.get("session") == session.get("session"):
        prev_totals = previous.get("totals") if isinstance(previous.get("totals"), dict) else {}
        delta = {field: totals[field] - _int(prev_totals.get(field)) for field in METER_FIELDS}
        delta_steps = steps - _int(previous.get("steps"))
        basis = "previous_snapshot"
        since_ts = previous.get("ts")
        if previous.get("epoch_ms"):
            seconds = max(0.0, (epoch_ms - _int(previous["epoch_ms"])) / 1000.0)
    regressed = any(value < 0 for value in delta.values()) or delta_steps < 0
    delta = {field: max(0, value) for field, value in delta.items()}
    delta_steps = max(0, delta_steps)
    delta_total = total_of(delta)
    delta_cache_hit = cache_hit_of(delta) if delta_total else 0.0

    delegate_payload = delegates or {"count": 0, "session_ids": [], "totals": {field: 0 for field in METER_FIELDS}, "total": 0}

    return {
        "schema": SCHEMA,
        "kind": KIND,
        "ts": now.isoformat(timespec="seconds"),
        "epoch_ms": epoch_ms,
        "label": label or "UNLABELED",
        "note": note or "",
        "session": session.get("session"),
        "cwd": session.get("cwd"),
        "source": session.get("path"),
        "raw_log": session.get("raw_log"),
        "steps": steps,
        "turns": turns,
        "surface_tokens": _int(session.get("surface_tokens")),
        "context_window": _int(session.get("context_window")),
        "pressure_tokens": _int(session.get("pressure_tokens")),
        "session_cache_hit": cache_hit_of(totals),
        "context_breakdown": {
            "system": _int((session.get("breakdown") or {}).get("systemTokens")),
            "tools": _int((session.get("breakdown") or {}).get("toolsTokens")),
            "messages": _int((session.get("breakdown") or {}).get("messageTokens")),
        },
        "totals": totals,
        "total": total_of(totals),
        "delta": {
            "basis": basis,
            "since_ts": since_ts,
            "seconds": round(seconds, 1),
            "requests": delta_steps,
            "uncached_input": delta["uncached_input"],
            "output": delta["output"],
            "cache_read": delta["cache_read"],
            "cache_write": delta["cache_write"],
            "total": delta_total,
            "cache_hit": round(delta_cache_hit, 6),
            "avg_total_per_request": round(delta_total / delta_steps, 1) if delta_steps else 0.0,
            "avg_total_per_hour": round(delta_total / (seconds / 3600.0), 1) if seconds > 0 else 0.0,
            "totals_regressed": regressed,
        },
        "delegates": {
            "count": _int(delegate_payload.get("count")),
            "session_ids": list(delegate_payload.get("session_ids") or []),
            "steps": _int(delegate_payload.get("steps")),
            "totals": {field: _int((delegate_payload.get("totals") or {}).get(field)) for field in METER_FIELDS},
            "total": _int(delegate_payload.get("total")),
        },
        "missing": list(session.get("missing") or []),
    }


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _delta_of(record: dict) -> dict:
    delta = record.get("delta") if isinstance(record.get("delta"), dict) else {}
    return {
        "requests": _int(delta.get("requests")),
        "uncached_input": _int(delta.get("uncached_input")),
        "output": _int(delta.get("output")),
        "cache_read": _int(delta.get("cache_read")),
        "cache_write": _int(delta.get("cache_write")),
        "total": _int(delta.get("total")),
        "seconds": float(delta.get("seconds") or 0.0),
        "basis": delta.get("basis") or "unknown",
        "since_ts": delta.get("since_ts"),
    }


def _new_bucket(label: str) -> dict:
    return {
        "label": label,
        "snapshots": 0,
        "from_session_start": 0,
        "requests": 0,
        "uncached_input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
        "seconds": 0.0,
        "first_ts": None,
        "last_ts": None,
        "surface_first": None,
        "surface_last": None,
    }


def _add_to_bucket(bucket: dict, delta: dict, record: dict, surface_start: int | None = None) -> None:
    bucket["snapshots"] += 1
    if delta["basis"] == "session_start":
        bucket["from_session_start"] += 1
    for field in ("requests", "uncached_input", "output", "cache_read", "cache_write", "total"):
        bucket[field] += delta[field]
    bucket["seconds"] += delta["seconds"]
    bucket["first_ts"] = bucket["first_ts"] or delta.get("since_ts") or record.get("ts")
    bucket["last_ts"] = record.get("ts")
    if bucket["surface_first"] is None:
        # Surface at the start of the interval: 0 for "since session start",
        # otherwise the previous snapshot's reading.
        bucket["surface_first"] = 0 if surface_start is None else _int(surface_start)
    bucket["surface_last"] = _int(record.get("surface_tokens"))


def aggregate_report(records: list[dict]) -> dict:
    """Per-label and overall intervals, plus integrity checks over the snapshot chain."""
    per_label: dict[str, dict] = {}
    overall = _new_bucket("OVERALL")
    session_changes: list[dict] = []
    zero_deltas: list[str] = []
    delegate_buckets: dict[str, dict] = {}
    previous: dict | None = None

    for record in records:
        label = str(record.get("label") or "UNLABELED")
        delta = _delta_of(record)
        bucket = per_label.setdefault(label, _new_bucket(label))
        surface_start = (previous or {}).get("surface_tokens") if (previous and previous.get("session") == record.get("session")) else None
        _add_to_bucket(bucket, delta, record, surface_start)
        _add_to_bucket(overall, delta, record, surface_start)

        delegate_totals = (record.get("delegates") or {}).get("totals") or {}
        if previous is not None:
            prev_delegates = (previous.get("delegates") or {}).get("totals") or {}
            delegate_delta = {
                "requests": _int((record.get("delegates") or {}).get("steps"))
                - _int((previous.get("delegates") or {}).get("steps")),
                "uncached_input": _int(delegate_totals.get("uncached_input")) - _int(prev_delegates.get("uncached_input")),
                "output": _int(delegate_totals.get("output")) - _int(prev_delegates.get("output")),
                "cache_read": _int(delegate_totals.get("cache_read")) - _int(prev_delegates.get("cache_read")),
                "cache_write": _int(delegate_totals.get("cache_write")) - _int(prev_delegates.get("cache_write")),
                "total": total_of({field: _int(delegate_totals.get(field)) for field in METER_FIELDS})
                - total_of({field: _int(prev_delegates.get(field)) for field in METER_FIELDS}),
                "seconds": delta["seconds"],
                "basis": "previous_snapshot",
            }
            delegate_delta = {key: max(0, value) if key != "basis" else value for key, value in delegate_delta.items()}
            dbucket = delegate_buckets.setdefault(label, _new_bucket(label))
            _add_to_bucket(dbucket, delegate_delta, record)

        if previous is not None and previous.get("session") != record.get("session"):
            session_changes.append({"from": previous.get("session"), "to": record.get("session"), "ts": record.get("ts")})
        if delta["requests"] == 0:
            zero_deltas.append(record.get("ts"))
        previous = record

    def finalise(bucket: dict) -> dict:
        requests = bucket["requests"]
        seconds = bucket["seconds"]
        out = dict(bucket)
        out["avg_total_per_request"] = round(bucket["total"] / requests, 1) if requests else 0.0
        out["avg_total_per_hour"] = round(bucket["total"] / (seconds / 3600.0), 1) if seconds > 0 else 0.0
        out["avg_requests_per_hour"] = round(requests / (seconds / 3600.0), 2) if seconds > 0 else 0.0
        out["cache_hit"] = round(cache_hit_of(bucket), 6)
        return out

    labels = {label: finalise(bucket) for label, bucket in per_label.items()}
    overall_final = finalise(overall)
    delegates = {label: finalise(bucket) for label, bucket in delegate_buckets.items()}
    return {
        "labels": labels,
        "overall": overall_final,
        "delegates": delegates,
        "session_changes": session_changes,
        "zero_deltas": zero_deltas,
        "integrity": verify_chain(records),
    }


def verify_chain(records: list[dict]) -> dict:
    """Check that per-session deltas add up to the measured totals."""
    out: list[dict] = []
    by_session: dict[str, list[dict]] = {}
    for record in records:
        by_session.setdefault(str(record.get("session")), []).append(record)
    for session_id, chain in by_session.items():
        first = chain[0]
        last = chain[-1]
        summed = {field: 0 for field in METER_FIELDS}
        for record in chain:
            delta = _delta_of(record)
            for field in METER_FIELDS:
                summed[field] += delta[field]
        first_totals = first.get("totals") if isinstance(first.get("totals"), dict) else {}
        last_totals = last.get("totals") if isinstance(last.get("totals"), dict) else {}
        # delta of the first record already runs from session start, so the identity is:
        # sum(deltas) == last totals - session_start totals, and if the first record
        # starts at session start, == last totals.
        start_basis = _delta_of(first)["basis"] == "session_start"
        expected = {field: _int(last_totals.get(field)) for field in METER_FIELDS}
        if start_basis:
            identity_ok = all(summed[field] == expected[field] for field in METER_FIELDS)
        else:
            identity_ok = True  # unknown origin; nothing to compare against
        out.append(
            {
                "session": session_id,
                "snapshots": len(chain),
                "first_ts": first.get("ts"),
                "last_ts": last.get("ts"),
                "summed_total": total_of(summed),
                "last_total": total_of(expected),
                "origin_known": start_basis,
                "identity_ok": identity_ok,
                "steps": _int(last.get("steps")),
            }
        )
    return {"sessions": out}


def anomalies(report: dict, baseline: dict | None = None) -> list[dict]:
    """Facts worth a second look; each item says what was compared and to what."""
    flags: list[dict] = []
    for label, bucket in report["labels"].items():
        if bucket["snapshots"] and bucket["requests"]:
            if bucket["cache_hit"] < CACHE_HIT_WARN:
                flags.append(
                    {
                        "level": "warn",
                        "label": label,
                        "what": "cache hit below %.0f%%" % (CACHE_HIT_WARN * 100),
                        "value": _pct(bucket["cache_hit"]),
                    }
                )
        if bucket["surface_first"] is not None and bucket["surface_last"] is not None and bucket["requests"]:
            growth = (bucket["surface_last"] - bucket["surface_first"]) / bucket["requests"]
            if growth > SURFACE_GROWTH_WARN:
                flags.append(
                    {
                        "level": "warn",
                        "label": label,
                        "what": "context surface grew faster than %.0f tokens/request" % SURFACE_GROWTH_WARN,
                        "value": f"+{growth:.0f}/request ({bucket['surface_first']} -> {bucket['surface_last']})",
                    }
                )
    for label, bucket in report["labels"].items():
        if bucket["snapshots"] and bucket["requests"] and (bucket["seconds"] < 300 or bucket["requests"] < 10):
            flags.append(
                {
                    "level": "info",
                    "label": label,
                    "what": "thin interval: per-request and per-hour averages are noisy here",
                    "value": f"{bucket['requests']} requests over {bucket['seconds'] / 60:.1f} min",
                }
            )
    for change in report.get("session_changes") or []:
        flags.append(
            {
                "level": "warn",
                "what": "measured session changed between snapshots",
                "value": f"{change.get('from')} -> {change.get('to')} at {change.get('ts')}",
            }
        )
    if report.get("zero_deltas"):
        flags.append(
            {
                "level": "info",
                "what": "snapshots with zero requests (session idle or duplicated snapshot)",
                "value": f"{len(report['zero_deltas'])} at {', '.join(str(x) for x in report['zero_deltas'][:4])}",
            }
        )
    for integrity in report.get("integrity", {}).get("sessions", []):
        if not integrity["identity_ok"]:
            flags.append(
                {
                    "level": "error",
                    "what": "delta chain does not add up to measured totals",
                    "value": f"{integrity['session']}: summed {integrity['summed_total']} != last {integrity['last_total']}",
                }
            )
        if integrity.get("origin_known") is not True:
            flags.append(
                {
                    "level": "info",
                    "what": "first snapshot of session did not start at session start",
                    "value": f"{integrity['session']} origin_unknown; early tokens not attributable to a label",
                }
            )
    overall = report.get("overall") or {}
    if baseline and baseline.get("total_per_request"):
        baseline_per_request = float(baseline["total_per_request"])
        for label in MODE_LABELS:
            bucket = (report.get("labels") or {}).get(label)
            if not bucket or not bucket.get("requests"):
                continue
            change = (bucket["avg_total_per_request"] - baseline_per_request) / baseline_per_request
            flags.append(
                {
                    "level": "info",
                    "label": label,
                    "what": "avg total/request vs prior-night baseline",
                    "value": f"{bucket['avg_total_per_request']:.0f} vs {baseline_per_request:.0f} "
                    f"({change * 100:+.1f}% vs baseline) on {bucket['requests']} requests",
                }
            )
    surface = overall.get("surface_last") or 0
    if surface:
        flags.append({"level": "info", "what": "latest measured surfaceTokens", "value": _human(surface)})
    return flags


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------


def first_n_totals(events: list[dict], n: int) -> dict:
    """Cumulative usage of the first ``n`` requests (like-for-like comparisons)."""
    sums = {field: 0 for field in METER_FIELDS}
    total = 0
    count = 0
    for event in events[:n]:
        for field in METER_FIELDS:
            sums[field] += event[field]
        total += event["total"] or (event["uncached_input"] + event["cache_read"] + event["output"])
        count += 1
    return {"requests": count, "totals": sums, "total": total, "total_per_request": round(total / count, 1) if count else 0.0}


def build_baseline(session: dict, raw_log: Path | None = None, events: list[dict] | None = None) -> dict:
    """Baseline JSON for a reference session, every field traceable to a file."""
    steps = _int(session.get("steps"))
    totals = session.get("totals") or {}
    payload = {
        "schema": "usage_baseline.v1",
        "generated_at": _now().isoformat(timespec="seconds"),
        "session": session.get("session"),
        "cwd": session.get("cwd"),
        "created_at": iso_ms(session.get("created_at_ms")),
        "source": session.get("path"),
        "raw_log": str(raw_log) if raw_log else None,
        "turns": _int(session.get("turns")),
        "steps": steps,
        "requests": steps,
        "totals": {field: _int(totals.get(field)) for field in METER_FIELDS},
        "total": total_of(totals),
        "total_per_request": round(total_of(totals) / steps, 1) if steps else 0.0,
        "uncached_per_request": round(_int(totals.get("uncached_input")) / steps, 1) if steps else 0.0,
        "cache_read_per_request": round(_int(totals.get("cache_read")) / steps, 1) if steps else 0.0,
        "cache_hit": round(cache_hit_of(totals), 6),
        "surface_tokens": _int(session.get("surface_tokens")),
        "context_window": _int(session.get("context_window")),
        "pressure_tokens": _int(session.get("pressure_tokens")),
        "surface_window_ratio": round(session["surface_tokens"] / session["context_window"], 4) if session.get("context_window") else None,
        "per_request": {},
        "hours": [],
        "reconcile": {"ok": None, "evidence": "raw log unavailable"},
    }
    if raw_log:
        if events is None:
            events = usage_events(raw_log)
        payload["per_request"] = request_stats(events)
        buckets = hour_buckets(events)
        payload["hours"] = buckets["rows"]
        sums = {field: 0 for field in METER_FIELDS}
        for event in events:
            for field in METER_FIELDS:
                sums[field] += event[field]
        procache = {field: _int(totals.get(field)) for field in METER_FIELDS}
        payload["reconcile"] = {
            "ok": sums == procache,
            "raw_sum": sums,
            "procache_sum": procache,
            "evidence": f"{raw_log} vs {session.get('path')}",
        }
    return payload


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------


def print_table(headers: list[str], rows: list[list[str]], align_right: set[int] | None = None) -> None:
    align_right = align_right or set()
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def render(cells: list[str], fills: str = " ") -> str:
        parts = []
        for index, cell in enumerate(cells):
            text = str(cell)
            parts.append(text.rjust(widths[index], fills) if index in align_right else text.ljust(widths[index], fills))
        return "  ".join(parts).rstrip()

    print(render(headers))
    print(render(["-" * w for w in widths]))
    for row in rows:
        print(render([str(cell) for cell in row]))


def _delta_row(label: str, bucket: dict) -> list[str]:
    return [
        label,
        str(bucket.get("snapshots", 0)),
        str(bucket.get("requests", 0)),
        _human(bucket.get("total", 0)),
        f"{bucket.get('avg_total_per_request', 0):,.0f}",
        f"{bucket.get('avg_total_per_hour', 0):,.0f}",
        _pct(bucket.get("cache_hit", 0.0)),
        f"{bucket.get('surface_first') or 0:,}",
        f"{bucket.get('surface_last') or 0:,}",
    ]


REPORT_HEADERS = [
    "label",
    "snaps",
    "requests",
    "total",
    "avg/req",
    "avg/hour",
    "cache hit",
    "surface first",
    "surface last",
]


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_snapshot(args) -> int:
    home = dsh_home()
    root = repo_root()
    session = resolve_session(home, root, args.session)
    origin = session_origin(home, session["session"], session.get("cwd"))
    session.update(origin)
    delegates = delegate_usage(home, session["session"], session.get("cwd"))
    usage_file = Path(args.file) if args.file else default_usage_file(root)
    records, meta = read_usage_records(usage_file)
    previous = last_snapshot_for(records, session["session"])
    record = build_snapshot_record(session, previous, label=args.label, note=args.note, delegates=delegates)
    append_jsonl(usage_file, record)

    delta = record["delta"]
    print(f"snapshot  label={record['label']}  session={record['session']}  {record['ts']}")
    print(f"  source   {record['source']}")
    if record.get("raw_log"):
        print(f"  raw log  {record['raw_log']}")
    print(
        "  delta    {basis} ({since}): {req} requests, {total} total, {unc} uncached, {cr} cache-read, {out} output".format(
            basis=delta["basis"],
            since=delta.get("since_ts") or "session start",
            req=delta["requests"],
            total=_human(delta["total"]),
            unc=_human(delta["uncached_input"]),
            cr=_human(delta["cache_read"]),
            out=_human(delta["output"]),
        )
    )
    if delta["requests"]:
        print(
            f"           avg/request {delta['avg_total_per_request']:,.0f}  avg/hour {delta['avg_total_per_hour']:,.0f}  "
            f"cache hit {_pct(delta['cache_hit'])}  elapsed {delta['seconds'] / 60:.1f} min"
        )
    print(
        f"  session  steps={record['steps']} turns={record['turns']} surfaceTokens={record['surface_tokens']:,} "
        f"cache hit {_pct(record['session_cache_hit'])}  totals total {_human(record['total'])}"
    )
    if delegates.get("count"):
        print(f"  subagents {delegates['count']} session(s), cumulative {_human(delegates['total'])} total ({', '.join(delegates['session_ids'])})")
    if record["missing"]:
        print(f"  WARN missing procache rows: {', '.join(record['missing'])}")
    if delta.get("totals_regressed"):
        print("  WARN totals went backwards; delta clamped to zero (session file rewritten?)")
    print(f"  wrote    {usage_file}  ({meta['records'] + 1} usage record(s) in file)")
    return 0


def _baseline_for_report(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cmd_report(args) -> int:
    root = repo_root()
    usage_file = Path(args.file) if args.file else default_usage_file(root)
    baseline_file = Path(args.baseline) if args.baseline else default_baseline_file(root)
    records, meta = read_usage_records(usage_file)
    report = aggregate_report(records)
    baseline = _baseline_for_report(baseline_file)
    flags = anomalies(report, baseline)
    report["flags"] = flags
    report["sources"] = {
        "usage_file": str(usage_file),
        "baseline_file": str(baseline_file) if baseline_file.is_file() else None,
        "sessions": sorted({str(r.get("source")) for r in records if r.get("source")}),
        "read_meta": meta,
        "baseline_session": (baseline or {}).get("session"),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"usage meter report  ({len(records)} snapshot record(s))")
    print(f"  source       {usage_file}")
    if not records:
        print("  no snapshots yet: run `usage_meter.py snapshot --label <MODE>` at least twice to see deltas")
        return 0
    print(f"  parsed       {meta['records']} usage lines, {meta['foreign']} foreign kind, {meta['malformed']} malformed")
    for source in report["sources"]["sessions"]:
        print(f"  session file {source}")
    print()
    rows = [_delta_row(label, bucket) for label, bucket in report["labels"].items()]
    rows.append(_delta_row("OVERALL", report["overall"]))
    print_table(REPORT_HEADERS, rows, align_right={1, 2, 3, 4, 5, 6, 7, 8})

    latest_breakdown = None
    for record in reversed(records):
        breakdown = record.get("context_breakdown") or {}
        if any(_int(breakdown.get(key)) for key in ("system", "tools", "messages")):
            latest_breakdown = breakdown
            break
    if latest_breakdown:
        print(
            "\ncontext at last snapshot: "
            f"system {latest_breakdown.get('system', 0):,} + tools {latest_breakdown.get('tools', 0):,} "
            f"+ messages {latest_breakdown.get('messages', 0):,} tokens"
        )

    if report["delegates"]:
        print()
        print("subagent sessions (additional spend, not included in the parent totals above)")
        delegate_rows = [_delta_row(label, bucket) for label, bucket in report["delegates"].items()]
        print_table(REPORT_HEADERS, delegate_rows, align_right={1, 2, 3, 4, 5, 6, 7, 8})

    if baseline and baseline.get("total_per_request"):
        print()
        print("prior-night baseline comparison  ({})".format(baseline.get("session")))
        print(f"  baseline source  {baseline.get('source')}")
        for label, bucket in report["labels"].items():
            if not bucket["requests"]:
                continue
            if label not in MODE_LABELS:
                print(
                    f"  {label}: {bucket['avg_total_per_request']:,.0f} total/request over {bucket['requests']} "
                    "requests (not a mode window; excluded from the comparison)"
                )
                continue
            per_request = bucket["avg_total_per_request"]
            savings = 1.0 - (per_request / float(baseline["total_per_request"])) if baseline["total_per_request"] else 0.0
            print(
                f"  {label}: {per_request:,.0f} total/request vs {float(baseline['total_per_request']):,.0f} "
                f"-> {abs(savings) * 100:.1f}% {'lower' if savings >= 0 else 'higher'} than baseline "
                f"(x{per_request / float(baseline['total_per_request']):.3f})"
            )
            if bucket["requests"] < 50:
                print(
                    f"    note: {bucket['requests']} requests so far vs {baseline.get('requests')} in the baseline; "
                    "compare again once the sample is comparable"
                )
    print()
    if flags:
        print("flags")
        for flag in flags:
            print(f"  [{flag['level']}] {flag['what']}: {flag['value']}")
    else:
        print("flags: none")
    integrity = report["integrity"]["sessions"]
    if integrity:
        print()
        print("integrity (per measured session)")
        for item in integrity:
            print(
                f"  {item['session']}: snapshots={item['snapshots']} steps={item['steps']} "
                f"summed={_human(item['summed_total'])} last={_human(item['last_total'])} "
                f"identity={'OK' if item['identity_ok'] else 'MISMATCH'} origin={'session_start' if item['origin_known'] else 'unknown'}"
            )
    return 0


def cmd_hours(args) -> int:
    home = dsh_home()
    root = repo_root()
    session = resolve_session(home, root, args.session)
    raw = find_raw_log(home, session["session"], session.get("cwd"))
    if raw is None:
        raise MeterError(
            f"raw event log for {session['session']} not found under {dsh_home() / 'sessions'}; "
            "per-hour buckets need session.v4.jsonl.zstd"
        )
    events = usage_events(raw)
    buckets = hour_buckets(events, day=args.date)
    sums = buckets["sum"]
    procache = {field: _int((session.get("totals") or {}).get(field)) for field in METER_FIELDS}
    ok = sums == procache if not args.date else None
    payload = {
        "session": session["session"],
        "source": session.get("path"),
        "raw_log": str(raw),
        "day": args.date,
        "requests": buckets["requests"],
        "hours": buckets["rows"],
        "sum": sums,
        "sum_total": total_of(sums),
        "procache_totals": procache,
        "procache_total": total_of(procache),
        "reconcile_ok": ok,
        "note": "hour = local completion time of the request (assistant/message event time)",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"per-hour token usage  session={session['session']}")
    print(f"  source   {session.get('path')}")
    print(f"  raw log  {raw}")
    headers = ["hour", "requests", "uncached", "output", "cache read", "total", "avg/req", "cache hit"]
    rows = []
    for row in buckets["rows"]:
        rows.append(
            [
                row["hour"],
                str(row["requests"]),
                _human(row["uncached_input"]),
                _human(row["output"]),
                _human(row["cache_read"]),
                _human(row["total"]),
                f"{row['avg_total_per_request']:,.0f}",
                _pct(row["cache_hit"]),
            ]
        )
    rows.append(
        [
            "TOTAL",
            str(buckets["requests"]),
            _human(sums["uncached_input"]),
            _human(sums["output"]),
            _human(sums["cache_read"]),
            _human(total_of(sums)),
            f"{(total_of(sums) / buckets['requests'] if buckets['requests'] else 0):,.0f}",
            _pct(cache_hit_of(sums)),
        ]
    )
    print_table(headers, rows, align_right={1, 2, 3, 4, 5, 6, 7})
    print()
    if args.date:
        print(f"  filter    local day {args.date}")
        return 0
    print(
        "  reconcile raw log sum vs procache totals: {} (raw {} vs procache {})".format(
            "OK" if ok else "MISMATCH", _human(total_of(sums)), _human(total_of(procache))
        )
    )
    if not ok:
        print("  note      a live session can lag the procache by the in-flight request; a settled session must match exactly")
    return 0


def cmd_baseline(args) -> int:
    home = dsh_home()
    root = repo_root()
    spec = args.session or PRIOR_NIGHT_SESSION
    baseline_session = resolve_session(home, root, spec)
    raw = find_raw_log(home, baseline_session["session"], baseline_session.get("cwd"))
    events = usage_events(raw) if raw else []
    payload = build_baseline(baseline_session, raw, events)
    out = Path(args.out) if args.out else default_baseline_file(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"prior-night baseline  session={payload['session']}")
    print(f"  source   {payload['source']}")
    if payload.get("raw_log"):
        print(f"  raw log  {payload['raw_log']}")
    print(
        f"  totals   {payload['total']:,} total = {payload['totals']['uncached_input']:,} uncached + "
        f"{payload['totals']['output']:,} output + {payload['totals']['cache_read']:,} cache-read"
    )
    print(
        f"  requests {payload['requests']} steps over {payload['turns']} turn(s); "
        f"{payload['total_per_request']:,.1f} total/request; cache hit {_pct(payload['cache_hit'])}"
    )
    print(f"  surface  {payload['surface_tokens']:,} of {payload['context_window']:,} context window")
    if payload.get("per_request"):
        stats = payload["per_request"]
        print(
            f"  prompt   mean {stats['prompt_mean']:,.0f}, median {stats['prompt_median']:,.0f}, "
            f"p90 {stats['prompt_p90']:,.0f}, max {stats['prompt_max']:,} tokens/request"
        )
    reconcile = payload.get("reconcile") or {}
    if reconcile.get("ok") is not None:
        print(f"  reconcile raw log vs procache: {'OK' if reconcile['ok'] else 'MISMATCH'}")
    print(f"  wrote    {out}")

    if args.no_compare:
        return 0
    try:
        current = resolve_session(home, root, "auto")
    except MeterError as exc:
        print(f"\n  live comparison skipped: {exc}")
        return 0
    requests = _int(current.get("steps"))
    per_request = current["total"] / requests if requests else 0.0
    baseline_per_request = float(payload.get("total_per_request") or 0)
    savings = 1.0 - (per_request / baseline_per_request) if baseline_per_request else 0.0
    print()
    print(f"live comparison  session={current['session']}")
    print(f"  source   {current.get('path')}")
    print(
        f"  live     {current['total']:,} total over {requests} request(s) = {per_request:,.1f} total/request; "
        f"cache hit {_pct(current['cache_hit'])}; surface {current['surface_tokens']:,}"
    )
    print(
        f"  savings  {abs(savings) * 100:.1f}% {'lower' if savings >= 0 else 'higher'} vs prior-night baseline on total/request "
        f"({baseline_per_request:,.1f} -> {per_request:,.1f})"
    )
    if requests < 100:
        print(
            f"  caveat   only {requests} request(s) so far; a fresh session always looks cheap because the prompt "
            "grows with every request.  The like-for-like block below is the honest early comparison."
        )
    # Like-for-like: compare the first N requests of both sessions, because tokens/request
    # grow with the conversation.  Totals from the same request index are directly comparable.
    current_raw = find_raw_log(home, current["session"], current.get("cwd"))
    baseline_events = events
    if current_raw and baseline_events:
        current_events = usage_events(current_raw)
        n = min(len(baseline_events), len(current_events))
        if n:
            base_n = first_n_totals(baseline_events, n)
            cur_n = first_n_totals(current_events, n)
            ratio = (cur_n["total"] / base_n["total"]) if base_n["total"] else 0.0
            print()
            print(f"like-for-like  first {n} request(s) of each session")
            print(
                f"  baseline {base_n['total']:,} total = {base_n['total_per_request']:,.1f}/request "
                f"({raw})"
            )
            print(
                f"  live     {cur_n['total']:,} total = {cur_n['total_per_request']:,.1f}/request "
                f"({current_raw})"
            )
            print(
                f"  result   {abs(1.0 - ratio) * 100:.1f}% {'lower' if ratio <= 1 else 'higher'} "
                f"(x{ratio:.3f}) at the same request index"
            )
    return 0


def cmd_sessions(args) -> int:
    home = dsh_home()
    root = repo_root()
    sessions = list_sessions(home, root, all_cwds=args.all)
    if not sessions:
        raise MeterError(f"no sessions with cwd under {root}; try --all")
    headers = ["session", "depth", "parent", "steps", "total", "cache hit", "surface", "mtime"]
    rows = []
    for session in sessions:
        rows.append(
            [
                session["session"],
                "root" if session.get("depth") == 0 else ("sub" if session.get("delegated") else "?"),
                (session.get("parent") or "-"),
                str(session.get("steps")),
                _human(session.get("total")),
                _pct(session.get("cache_hit", 0.0)),
                f"{session.get('surface_tokens') or 0:,}",
                session.get("mtime_iso") or "-",
            ]
        )
    print(f"DSh sessions for cwd {root}  ({len(sessions)})")
    print(f"  procache {procache_dir(home)}")
    print_table(headers, rows, align_right={3, 4, 5, 6})
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure DSH token usage for the dual-mode experiment (read-only on $DSH_HOME).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "snapshot convention: a snapshot closes the interval that just ended, so --label is the mode\n"
            "that was active DURING that interval.  At a mode switch: snapshot first, then flip the mode.\n\n"
            "examples:\n"
            "  python3 tools/usage_meter.py snapshot --label EFFICIENT\n"
            "  python3 tools/usage_meter.py report\n"
            "  python3 tools/usage_meter.py baseline\n"
            "  python3 tools/usage_meter.py hours --session %s\n" % PRIOR_NIGHT_SESSION
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="append one usage snapshot to model_usage.jsonl")
    snapshot.add_argument("--label", required=True, help="mode active during the interval that ends here (EFFICIENT / MAX_RESULTS)")
    snapshot.add_argument("--session", default="auto", help="session id, procache path, or auto (default: newest top-level repo session)")
    snapshot.add_argument("--note", default="", help="free-text note stored with the snapshot")
    snapshot.add_argument("--file", default=None, help="override model_usage.jsonl path")
    snapshot.set_defaults(func=cmd_snapshot)

    report = sub.add_parser("report", help="per-mode aggregate table from model_usage.jsonl")
    report.add_argument("--file", default=None, help="override model_usage.jsonl path")
    report.add_argument("--baseline", default=None, help="override baseline_prior_night.json path")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    report.set_defaults(func=cmd_report)

    baseline = sub.add_parser("baseline", help="write the prior-night baseline JSON and compare with the live session")
    baseline.add_argument("--session", default=PRIOR_NIGHT_SESSION, help="reference session id (default: the 2026-11-05 night session)")
    baseline.add_argument("--out", default=None, help="override baseline JSON output path")
    baseline.add_argument("--no-compare", action="store_true", help="only write the file, do not compare with the live session")
    baseline.set_defaults(func=cmd_baseline)

    hours = sub.add_parser("hours", help="per-hour buckets from the raw event log, reconciled against procache totals")
    hours.add_argument("--session", default="auto", help="session id, procache path, or auto")
    hours.add_argument("--date", default=None, help="filter to one local day (YYYY-MM-DD)")
    hours.add_argument("--json", action="store_true", help="machine-readable output")
    hours.set_defaults(func=cmd_hours)

    sessions = sub.add_parser("sessions", help="list DSH sessions for this repo (depth/parent/totals)")
    sessions.add_argument("--all", action="store_true", help="include sessions from other working directories")
    sessions.set_defaults(func=cmd_sessions)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except MeterError as exc:
        print(f"usage_meter: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
