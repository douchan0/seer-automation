"""Bridge planner: the "AI in the loop" path used when a human or an agent
(e.g. a DeepSeek Harness session) plays the planner role.

Rendezvous protocol, one directory, plain files, no server::

    <dir>/request-0007.json    written by this process (atomic: tmp + replace)
    <dir>/response-0007.json   written by the supervisor (atomic, or not: the
                               poller tolerates a half-written file)

The request file is the full :class:`PlanRequest` JSON plus three convenience
keys (``seq``, ``created_at``, ``reply_with``), so a supervising agent can
answer without importing Python::

    {"goal": "puni", "world_summary": {...}, "graph_digest": [...], ...,
     "capabilities": [...], "budgets": {...}, "constraints": {...},
     "seq": 7, "created_at": "...", "reply_with": "response-0007.json"}

The response file is one decision object (validated by
:func:`~seerauto.planner.base.parse_decision`), optionally wrapped::

    {"seq": 7, "decision": { ... the contract object ... }}

Staleness is checked twice, because a stale answer is worse than no answer:

* the payload ``seq`` (when present) must equal the request ``seq``;
* the response file's ``mtime`` must not predate the request file.

One writing process per directory (sequence numbers are allocated by scanning
the directory; two concurrent planners would race for the same number).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .base import (
    PlanDecision,
    PlanError,
    PlanRequest,
    PlannerError,
    PlannerUnavailable,
    parse_decision,
)

REQUEST_RE = re.compile(r"^request-(\d{4,})\.json$")
RESPONSE_RE = re.compile(r"^response-(\d{4,})\.json$")
DEFAULT_TIMEOUT_S = 240.0
DEFAULT_POLL_S = 0.25

__all__ = [
    "BridgePlanner",
    "BridgeWatcher",
    "pending",
    "answer",
    "format_prompt",
    "next_seq",
    "request_name",
    "response_name",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_POLL_S",
]


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #
def request_name(seq: int) -> str:
    return f"request-{int(seq):04d}.json"


def response_name(seq: int) -> str:
    return f"response-{int(seq):04d}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write JSON so a concurrent reader never sees a half-written file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    return path


def _as_dir(directory: str | os.PathLike[str]) -> Path:
    if directory is None or str(directory).strip() == "":
        raise PlannerUnavailable(
            "bridge directory is empty; set BridgePlanner(directory=...) to the "
            "folder the supervising agent watches")
    return Path(directory).expanduser()


def next_seq(directory: str | os.PathLike[str]) -> int:
    """First unused sequence number in ``directory``."""

    folder = _as_dir(directory)
    highest = 0
    if folder.is_dir():
        for path in folder.iterdir():
            match = REQUEST_RE.match(path.name) or RESPONSE_RE.match(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def pending(directory: str | os.PathLike[str], *,
            unanswered_only: bool = True) -> list[str]:
    """Request file names in ``directory``, oldest sequence first.

    With ``unanswered_only`` (the default) a request is skipped once a response
    file exists that is at least as new as the request - i.e. exactly the same
    freshness rule the poller applies.
    """

    folder = _as_dir(directory)
    found: list[tuple[int, str]] = []
    if not folder.is_dir():
        return []
    for path in folder.iterdir():
        match = REQUEST_RE.match(path.name)
        if not match:
            continue
        seq = int(match.group(1))
        if unanswered_only:
            response_path = folder / response_name(seq)
            try:
                response_mtime = response_path.stat().st_mtime_ns
                request_mtime = path.stat().st_mtime_ns
            except OSError:
                pass
            else:
                if response_mtime >= request_mtime:
                    continue
        found.append((seq, path.name))
    found.sort(key=lambda item: item[0])
    return [name for _seq, name in found]


def answer(directory: str | os.PathLike[str], seq: int,
           payload: "PlanDecision | str | Mapping[str, Any]") -> Path:
    """Write ``response-<seq>.json`` for a pending request.

    The payload is validated *before* it touches the filesystem, so a typo in a
    hand-written answer fails loudly here instead of derailing the run.
    """

    folder = _as_dir(directory)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise PlannerError(f"bridge sequence must be a positive integer, got {seq!r}")
    decision = _coerce_decision(payload)
    path = folder / response_name(seq)
    return _atomic_write_json(path, {
        "seq": int(seq),
        "answered_at": _now_iso(),
        "decision": decision.to_dict(),
    })


def _coerce_decision(payload: "PlanDecision | str | Mapping[str, Any]") -> PlanDecision:
    if isinstance(payload, PlanDecision):
        decision = payload
    elif isinstance(payload, str):
        decision = parse_decision(payload)
    elif isinstance(payload, Mapping):
        decision = PlanDecision.from_raw(payload)
    else:
        raise PlannerError(
            "bridge answer must be a PlanDecision, JSON text or a mapping, got "
            f"{type(payload).__name__}")
    decision.normalize()
    decision.validate()
    return decision


# --------------------------------------------------------------------------- #
# human/LLM readable brief
# --------------------------------------------------------------------------- #
def format_prompt(req: PlanRequest, *, seq: int | None = None,
                  directory: Path | str | None = None) -> str:
    """Render a compact brief so a supervising agent can answer without Python."""

    if not isinstance(req, PlanRequest):
        raise PlannerError(
            f"format_prompt expects a PlanRequest, got {type(req).__name__}")
    world = req.world_summary if isinstance(req.world_summary, Mapping) else {}
    lines: list[str] = []
    lines.append("PLANNER BRIEF (bridge) - answer with exactly ONE JSON decision object")
    lines.append("=" * 72)
    lines.append(f"goal        : {req.goal or '<empty>'}")
    if req.run_id:
        lines.append(f"run_id      : {req.run_id}")
    lines.append(f"budgets     : {_fmt_mapping(req.budgets)}")
    lines.append(f"constraints : {_fmt_mapping(req.constraints)}")
    lines.append("")

    lines.append("WORLD (evidence-aware: a null value marked UNKNOWN means 'not observed', "
                 "not 'false')")
    lines.extend(_world_lines(world))
    lines.append("")

    ready, other = _split_nodes(req.graph_digest)
    lines.append(f"READY NODES ({len(ready)}, highest priority first)")
    if not ready:
        lines.append("  (none)")
    for index, node in enumerate(ready, start=1):
        capability = _node_capability(node)
        budget = node.get("budget") if isinstance(node.get("budget"), Mapping) else {}
        head = (f"  {index}. {node.get('id')}  priority={node.get('priority', 0)}  "
                f"capability={capability or '<none>'}")
        if budget:
            head += f"  budget={_fmt_mapping(budget)}"
        lines.append(head)
        if node.get("title"):
            lines.append(f"     title: {node.get('title')}")
    lines.append("")
    if other:
        lines.append(f"OTHER NODES ({len(other)}) - not selectable")
        for node in other:
            detail = node.get("blocked_reason") or ""
            suffix = f" :: {detail}" if detail else ""
            lines.append(f"  - {node.get('id')}  status={node.get('status')}"
                         f"  capability={_node_capability(node) or '<none>'}{suffix}")
        lines.append("")

    lines.append("CAPABILITIES (only these ids are legal for 'capability')")
    for spec in req.capabilities or ():
        if not isinstance(spec, Mapping):
            continue
        lines.append(f"  {spec.get('id')}  [{spec.get('kind', '?')}, "
                     f"verified={spec.get('verified', '?')}]  {spec.get('summary', '')}")
        lines.append(f"     max_actions={spec.get('max_actions')} "
                     f"max_duration_s={spec.get('max_duration_s')} "
                     f"requires_input={_yes(spec.get('requires_input'))} "
                     f"requires_approval={_yes(spec.get('requires_approval'))}")
        pre = [str(x) for x in (spec.get("preconditions") or ()) if str(x).strip()]
        post = [str(x) for x in (spec.get("postconditions") or ()) if str(x).strip()]
        if pre:
            lines.append(f"     preconditions: {', '.join(pre[:3])}")
        if post:
            lines.append(f"     postconditions: {', '.join(post[:3])}")
    lines.append("")

    if req.last_result:
        last = req.last_result
        lines.append("LAST RESULT")
        lines.append(f"  skill={last.get('skill')} status={last.get('status')} "
                     f"changed={last.get('changed')} reason={last.get('reason')}")
        if last.get("proof"):
            lines.append(f"  proof: {last.get('proof')}")
        lines.append("")
    if req.recent_events:
        lines.append("RECENT EVENTS (newest last)")
        for event in req.recent_events[-5:]:
            lines.append(f"  - {json.dumps(event, ensure_ascii=False)[:220]}")
        lines.append("")
    if req.completed_nodes:
        lines.append(f"COMPLETED NODES: {', '.join(req.completed_nodes)}")
        lines.append("")

    target = "response-<seq>.json"
    if seq is not None:
        target = response_name(seq)
        if directory is not None:
            target = str(Path(directory).expanduser() / target)
    lines.append(f"ANSWER: write one JSON object to {target}")
    lines.append(json.dumps({
        "goal": req.goal,
        "selected_node": "<id from READY NODES>",
        "capability": "<id from CAPABILITIES>",
        "reason": "<why, citing the evidence above>",
        "expected_observation": "<what proves it worked>",
        "max_actions": 40,
        "max_duration_s": 1800,
        "requires_approval": False,
    }, ensure_ascii=False, indent=2))
    lines.append("Refuse instead with {\"stop\": true, \"stop_reason\": \"...\"} or "
                 "{\"need_human\": true, \"human_question\": \"...\"}.")
    lines.append("Never answer with coordinates, pixel positions or per-frame actions.")
    return "\n".join(lines)


def _world_lines(world: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []

    def put(label: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        lines.append(f"  {label:<17}: {value}")

    put("scene", world.get("scene"))
    source = world.get("scene_source")
    if source:
        lines.append(f"  {'scene_source':<17}: {source}")
    put("logged_in", world.get("logged_in"))
    put("frontmost", world.get("frontmost"))
    pet = world.get("active_pet")
    if isinstance(pet, Mapping):
        lines.append(f"  {'active_pet':<17}: {pet.get('name')} "
                     f"Lv{pet.get('level')} {pet.get('hp')}/{pet.get('hp_max')}")
    pets = world.get("pets")
    if isinstance(pets, Mapping) and pets:
        rendered = ", ".join(
            f"{name} Lv{info.get('level')} ({info.get('source')})"
            for name, info in pets.items() if isinstance(info, Mapping))
        put("pets", rendered)
    resources = world.get("resources")
    if isinstance(resources, Mapping) and resources:
        put("resources", ", ".join(f"{k}={v}" for k, v in resources.items()))
    for key in ("spt_defeated", "spt_unknown", "unknowns", "notes"):
        value = world.get(key)
        if isinstance(value, (list, tuple)) and value:
            put(key, ", ".join(str(v) for v in value))
        elif isinstance(value, Mapping) and value:
            put(key, ", ".join(f"{k}={v}" for k, v in value.items()))
    return lines or ["  (no world facts provided)"]


def _split_nodes(digest: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    nodes = [node for node in (digest or ()) if isinstance(node, Mapping)]
    ready = [node for node in nodes
             if str(node.get("status") or "").strip().lower() == "ready"]

    def priority(node: Mapping[str, Any]) -> int:
        try:
            return int(node.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    ready.sort(key=priority, reverse=True)
    other = [node for node in nodes if not any(node is chosen for chosen in ready)]
    other.sort(key=priority, reverse=True)
    return ready, other


def _node_capability(node: Mapping[str, Any]) -> str:
    return str(node.get("capability") or node.get("skill") or "")


def _fmt_mapping(value: Mapping[str, Any] | None) -> str:
    if not value:
        return "(none)"
    return ", ".join(f"{k}={v}" for k, v in value.items())


def _yes(value: Any) -> str:
    return "yes" if value else "no"


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
@dataclass
class BridgePlanner:
    """Planner that writes a request file and waits for a human/agent answer."""

    directory: str | Path
    timeout_s: float = DEFAULT_TIMEOUT_S
    poll_s: float = DEFAULT_POLL_S
    name: str = "bridge"
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False, compare=False)

    # -- protocol ---------------------------------------------------------- #
    def decide(self, req: PlanRequest) -> PlanDecision:
        if not isinstance(req, PlanRequest):
            raise PlannerError(
                f"{self.name}: decide() expects a PlanRequest, got {type(req).__name__}")
        if not str(req.goal or "").strip():
            raise PlannerError(
                f"{self.name}: PlanRequest.goal is empty; refusing to plan without the "
                "user-facing goal")
        folder = self.path()
        if not folder.is_dir():
            raise PlannerUnavailable(
                f"bridge directory {folder} does not exist; create it (and let the "
                "supervising agent watch it) before the run")

        seq = next_seq(folder)
        request_path = _atomic_write_json(
            folder / request_name(seq),
            self.request_payload(req, seq),
        )
        request_mtime = request_path.stat().st_mtime_ns
        response_path = folder / response_name(seq)
        deadline = self.clock() + max(0.0, float(self.timeout_s))
        last_error = ""

        while True:
            text = self._read_fresh(response_path, request_mtime)
            if text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    # Tolerate a hand-written document (a fenced / prose-wrapped
                    # decision) the same way model output is tolerated.  A
                    # half-written JSON file fails this too and keeps polling.
                    try:
                        decision = parse_decision(text)
                    except PlanError as parse_exc:
                        last_error = (f"{response_path.name}: neither JSON nor a "
                                      f"decision document ({exc.msg}; {parse_exc})")
                    else:
                        self._validate_against_request(decision, req)
                        return decision
                else:
                    return self._decision_from_payload(payload, response_path, seq, req)

            remaining = deadline - self.clock()
            if remaining <= 0:
                detail = f" (last error: {last_error})" if last_error else ""
                raise PlannerError(
                    f"no answer for {request_path.name} within {self.timeout_s:g}s; "
                    f"expected {response_path.name} in {folder}{detail}")
            self.sleep(min(max(self.poll_s, 0.01), remaining))

    # -- helpers ----------------------------------------------------------- #
    def path(self) -> Path:
        return _as_dir(self.directory)

    def request_payload(self, req: PlanRequest, seq: int) -> dict[str, Any]:
        """The full PlanRequest plus the rendezvous keys."""

        payload = req.to_dict()
        payload["seq"] = int(seq)
        payload["created_at"] = _now_iso()
        payload["reply_with"] = response_name(seq)
        return payload

    def prompt(self, req: PlanRequest, seq: int | None = None) -> str:
        return format_prompt(req, seq=seq, directory=self.path())

    @staticmethod
    def _read_fresh(response_path: Path, request_mtime_ns: int) -> str | None:
        """Response text, or ``None`` when missing/stale/partially written."""

        try:
            stat = response_path.stat()
        except OSError:
            return None
        if stat.st_mtime_ns < request_mtime_ns:
            return None
        try:
            return response_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _decision_from_payload(self, payload: Any, response_path: Path, seq: int,
                               req: PlanRequest) -> PlanDecision:
        if not isinstance(payload, Mapping):
            raise PlannerError(
                f"{response_path.name} must contain one JSON object, got "
                f"{type(payload).__name__}")
        if "decision" in payload:
            file_seq = payload.get("seq")
            if file_seq is not None:
                try:
                    file_seq_int = int(file_seq)
                except (TypeError, ValueError) as exc:
                    raise PlannerError(
                        f"{response_path.name}: 'seq' must be an integer, got "
                        f"{file_seq!r}") from exc
                if file_seq_int != seq:
                    raise PlannerError(
                        f"{response_path.name} answers seq {file_seq_int}, not the "
                        f"request seq {seq}; refusing a stale answer")
            raw_decision = payload["decision"]
        else:
            raw_decision = payload  # a bare decision object (hand-written answer)

        decision = _coerce_decision(raw_decision)
        self._validate_against_request(decision, req)
        return decision

    @staticmethod
    def _validate_against_request(decision: PlanDecision, req: PlanRequest) -> None:
        decision.validate(
            known_nodes=req.graph_digest or [],
            known_capabilities=req.capabilities or [],
            budgets=req.budgets or None,
        )


class BridgeWatcher:
    """Helpers for the supervising side (a human, or an agent session)."""

    pending = staticmethod(pending)
    answer = staticmethod(answer)
    format_prompt = staticmethod(format_prompt)
    next_seq = staticmethod(next_seq)
    request_name = staticmethod(request_name)
    response_name = staticmethod(response_name)
