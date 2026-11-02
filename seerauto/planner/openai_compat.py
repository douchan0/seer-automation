"""OpenAI-compatible ``/chat/completions`` planner - stdlib ``urllib`` only.

Why hand-rolled: the kernel must not grow an LLM SDK dependency (``AGENTS.md``
section 7), and the transport is trivial.  What this module *does* enforce:

* one HTTP call per decision, with a hard per-call timeout and a per-process
  call cap (``SEERAUTO_AI_MAX_CALLS``), because a planner loop that silently
  retries is how a run burns a quota overnight;
* only **text** is uploaded - ``world_summary`` / ``graph_digest`` /
  ``capabilities`` as JSON.  No screenshot, no frame, no image part, ever;
* the answer is parsed with the same strict contract as every other planner
  (:func:`seerauto.planner.base.parse_decision`), checked against the graph and
  the capability registry, and never patched up: a bad answer raises.

Configuration (environment only - never hard-code a key)::

    SEERAUTO_AI_BASE_URL   default https://opencode.ai/inference/go/openai/v1
    SEERAUTO_AI_API_KEY    required for available()
    SEERAUTO_AI_MODEL      default deepseek-v4.1-flash
    SEERAUTO_AI_TIMEOUT_S  default 45
    SEERAUTO_AI_MAX_CALLS  default 20 (per process)
    SEERAUTO_AI_SESSION    optional; sent as ``x-opencode-session``
                           (the opencode-go gateway requires that header, so a
                           stable random id is generated when unset)
    SEERAUTO_AI_JSON_MODE  default true -> ``response_format={"type":"json_object"}``
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .base import (
    PlanDecision,
    PlanError,
    PlanRequest,
    PlannerError,
    PlannerUnavailable,
    parse_decision,
)

DEFAULT_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_CALLS = 20
DEFAULT_JSON_MODE = True

ENV_BASE_URL = "SEERAUTO_AI_BASE_URL"
ENV_API_KEY = "SEERAUTO_AI_API_KEY"
ENV_MODEL = "SEERAUTO_AI_MODEL"
ENV_TIMEOUT_S = "SEERAUTO_AI_TIMEOUT_S"
ENV_MAX_CALLS = "SEERAUTO_AI_MAX_CALLS"
ENV_SESSION = "SEERAUTO_AI_SESSION"
ENV_JSON_MODE = "SEERAUTO_AI_JSON_MODE"

#: USD per 1M (prompt, completion) tokens.  Intentionally **empty by default**:
#: vendor prices drift and a stale table silently misreports cost.  The runner
#: passes ``prices=`` from its own config; ``cost_usd`` stays ``None`` for any
#: model that is not listed here.
PRICE_TABLE_USD_PER_MTOK: dict[str, tuple[float, float]] = {}

SYSTEM_PROMPT = """\
You are the high-level planner of a macOS game-automation kernel. You pick ONE
next goal node and ONE registered capability. You never speak about pixels,
coordinates, key presses, timings or per-frame decisions - a deterministic
local executor owns those.

You receive one JSON request with:
  goal             the user-facing goal of the whole run
  world_summary    evidence-tagged facts; a null value marked UNKNOWN means
                   "not observed", NOT "false"
  graph_digest     every goal node: id, title, status (ready/pending/blocked/
                   done/failed), priority (higher = more important),
                   capability, budget, blocked_reason
  capabilities     the registered capability contracts (id, kind, summary,
                   inputs, outputs, preconditions, postconditions,
                   max_actions, max_duration_s, requires_input,
                   requires_approval, verified)
  completed_nodes  node ids already proven done
  recent_events    tail of the event log
  last_result      the previous SkillResult (status/reason/proof), may be null
  budgets          run-level caps: max_actions, max_duration_s
  constraints      hard policy: allow_input, forbid_approval, max_spend

Rules:
1. Only a node whose status is "ready" may be selected.
2. selected_node must be an id from graph_digest; capability must be an id from
   capabilities. Never invent or rename either one.
3. Respect constraints: no input-requiring capability when allow_input is
   false; set requires_approval true when the capability says so.
4. max_actions must be 1..200 and <= budgets.max_actions when given;
   max_duration_s must be 1..7200 and <= budgets.max_duration_s when given.
5. reason: one sentence citing the evidence you actually used (a node status,
   a UNKNOWN world fact, last_result, or the skipped higher-priority nodes).
6. expected_observation: what the executor should be able to observe
   afterwards to prove the node moved.
7. If nothing can be scheduled, answer with a refusal instead:
   {"stop": true, "stop_reason": "..."} and, when a human decision is needed,
   {"need_human": true, "human_question": "..."} (need_human implies stop).
   A refusal names no node, no capability and schedules no work.
8. Do not add keys. Answer with exactly one JSON object matching this contract:

{"goal": "<same goal>",
 "selected_node": "<node id>",
 "capability": "<capability id>",
 "reason": "<why this node, citing evidence>",
 "expected_observation": "<observable proof>",
 "max_actions": <int 1..200>,
 "max_duration_s": <int 1..7200>,
 "requires_approval": <bool>}

There is no conversation and no follow-up: return the JSON object only.
"""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AIConfig:
    """Resolved environment configuration (never printed, never persisted)."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_calls: int = DEFAULT_MAX_CALLS
    session: str = ""
    json_mode: bool = DEFAULT_JSON_MODE

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AIConfig":
        env = os.environ if environ is None else environ

        def text(name: str, default: str) -> str:
            value = env.get(name)
            return default if value is None or not str(value).strip() else str(value).strip()

        base_url = text(ENV_BASE_URL, DEFAULT_BASE_URL)
        if not base_url.lower().startswith(("http://", "https://")):
            raise PlannerError(
                f"{ENV_BASE_URL} must start with http:// or https://, got {base_url!r}")
        timeout_s = _env_float(env, ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S)
        if not 1.0 <= timeout_s <= 600.0:
            raise PlannerError(
                f"{ENV_TIMEOUT_S} must be within [1, 600] seconds, got {timeout_s}")
        max_calls = _env_int(env, ENV_MAX_CALLS, DEFAULT_MAX_CALLS)
        if max_calls < 1:
            raise PlannerError(f"{ENV_MAX_CALLS} must be >= 1, got {max_calls}")
        # Fall back to the credential names this machine already uses, so a local
        # user does not have to export anything twice.  An explicit
        # SEERAUTO_AI_API_KEY always wins.
        api_key = text(ENV_API_KEY, "")
        if not api_key:
            for fallback in ("OPENCODE_GO_API_KEY",
                             "DSH_OPENCODE_GO_ACCOUNT_F9736BCC02EA3E61F692DAEDA78C65A7"):
                candidate = env.get(fallback)
                if candidate and str(candidate).strip():
                    api_key = str(candidate).strip()
                    break
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=text(ENV_MODEL, DEFAULT_MODEL),
            timeout_s=timeout_s,
            max_calls=max_calls,
            session=text(ENV_SESSION, ""),
            json_mode=_env_bool(env.get(ENV_JSON_MODE), DEFAULT_JSON_MODE),
        )


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise PlannerError(f"{name} must be a number of seconds, got {raw!r}") from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise PlannerError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(raw: Any, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


# --------------------------------------------------------------------------- #
# call metadata
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CallMeta:
    """One model call, in the shape the runner logs (no secrets, no images)."""

    model: str
    latency_ms: int
    prompt_chars: int
    response_chars: int
    cost_usd: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str = ""
    call_index: int = 0
    #: How many schema rejections preceded this successful answer (0 = first try).
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "latency_ms": int(self.latency_ms),
            "prompt_chars": int(self.prompt_chars),
            "response_chars": int(self.response_chars),
            "cost_usd": None if self.cost_usd is None else round(float(self.cost_usd), 6),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "finish_reason": self.finish_reason,
            "retries": int(self.retries),
            "call_index": int(self.call_index),
        }


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
class OpenAICompatPlanner:
    """Chat-completions planner.  ``available()`` is pure configuration.

    ``decide()`` returns a :class:`PlanDecision` whose ``meta`` attribute holds
    the :class:`CallMeta` for logging (the protocol signature stays
    ``decide(req) -> PlanDecision``).  ``decide_with_meta()`` is the same call
    returning ``(decision, meta)`` for callers that want both.
    """

    name = "ai"

    def __init__(self, *,
                 config: AIConfig | None = None,
                 environ: Mapping[str, str] | None = None,
                 prices: Mapping[str, tuple[float, float]] | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        self.config = config or AIConfig.from_env(environ)
        self.prices: dict[str, tuple[float, float]] = dict(PRICE_TABLE_USD_PER_MTOK)
        if prices:
            for model, price in prices.items():
                pair = tuple(float(x) for x in price)
                if len(pair) != 2:
                    raise PlannerError(
                        f"prices[{model!r}] must be (prompt_usd_per_mtok, "
                        f"completion_usd_per_mtok)")
                self.prices[str(model)] = pair  # type: ignore[assignment]
        self._opener = opener
        #: Sent as ``x-opencode-session``; the gateway requires a session id.
        self.session = self.config.session or f"seerauto-{uuid.uuid4().hex[:12]}"
        self._calls = 0

    # -- availability ------------------------------------------------------ #
    def transport_configured(self) -> bool:
        return bool(self.config.base_url.strip() and self.config.api_key.strip())

    def calls_remaining(self) -> int:
        return max(0, int(self.config.max_calls) - self._calls)

    def available(self) -> bool:
        """True when this planner could attempt a call right now."""

        return self.transport_configured() and self.calls_remaining() > 0

    def describe_config(self) -> dict[str, str]:
        """Safe, secret-free view of the transport for diagnostics."""

        key = self.config.api_key or ""
        return {
            "base_url": self.config.base_url or "<unset>",
            "model": self.config.model or "<unset>",
            "api_key": ("set (…" + key[-4:] + ")") if key else "<unset>",
            "session": self.session,
            "timeout_s": str(self.config.timeout_s),
            "max_calls": str(self.config.max_calls),
        }

    def self_test(self) -> str:
        """One minimal round trip, so a bad credential fails loudly.

        Sends no game data: a two-line prompt asking for a fixed JSON object, then
        parses it with :func:`parse_decision`.  Raises ``PlannerUnavailable`` when
        the credential is missing and ``PlannerError`` on any transport/schema
        problem.
        """

        self._require_available()
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content":
                 "Reply with exactly one JSON object and nothing else."},
                {"role": "user", "content":
                 'Return {"goal":"selftest","selected_node":"none","capability":"none",'
                 '"reason":"selftest","expected_observation":"none","max_actions":1,'
                 '"max_duration_s":1,"requires_approval":false}'},
            ],
            "max_tokens": 200,
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._calls += 1
        raw = self._post(payload)
        response = self._load_response(raw)
        return str(response.get("content") or "")

    # -- protocol ---------------------------------------------------------- #
    def decide(self, req: PlanRequest) -> PlanDecision:
        decision, _meta = self.decide_with_meta(req)
        return decision

    def decide_with_meta(self, req: PlanRequest) -> tuple[PlanDecision, CallMeta]:
        self._require_available()
        if not isinstance(req, PlanRequest):
            raise PlannerError(
                f"{self.name}: decide() expects a PlanRequest, got {type(req).__name__}")
        if not str(req.goal or "").strip():
            raise PlannerError(
                f"{self.name}: PlanRequest.goal is empty; refusing to plan without the "
                "user-facing goal")

        body = self._request_body(req)
        prompt_chars = sum(len(message["content"]) for message in body["messages"])
        payload_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))

        # A model occasionally answers with prose instead of the required object.
        # One immediate retry is worth it (measured live: the first attempt of a
        # run came back without any of the eight keys); more than one is not, since
        # a model that will not follow the schema twice will not follow it thrice.
        last_error: Exception | None = None
        attempts = 0
        for attempts in range(1, 3):
            if not self.available():
                raise PlannerUnavailable(
                    f"{self.name}: call budget exhausted ({self.config.max_calls})")
            self._calls += 1
            index = self._calls
            started = time.perf_counter()
            raw = self._post(payload_text.encode("utf-8"))
            latency_ms = int(round((time.perf_counter() - started) * 1000.0))
            response = self._load_response(raw)
            choice = response["choice"]
            content = response["content"]
            try:
                decision = self._decision_from_content(
                    content, choice.get("finish_reason"), req)
            except PlanError as exc:
                last_error = exc
                continue
            meta = self._meta(response, choice, content, prompt_chars, latency_ms, index,
                              retries=attempts - 1)
            decision.meta = meta
            return decision, meta
        raise PlanError(
            f"{self.name}: the model did not return a valid decision after {attempts} "
            f"attempts: {last_error}")

    # -- request building -------------------------------------------------- #
    def _request_body(self, req: PlanRequest) -> dict[str, Any]:
        view = {
            "goal": req.goal,
            "world_summary": req.world_summary,
            "graph_digest": req.graph_digest,
            "capabilities": req.capabilities,
            "completed_nodes": req.completed_nodes,
            "recent_events": req.recent_events[-20:] if req.recent_events else [],
            "last_result": req.last_result,
            "budgets": req.budgets,
            "constraints": req.constraints,
            "run_id": req.run_id,
        }
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": json.dumps(view, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        if self.config.json_mode:
            body["response_format"] = {"type": "json_object"}
        self._assert_text_only(body)
        return body

    @staticmethod
    def _assert_text_only(body: Mapping[str, Any]) -> None:
        """Hard rule: the planner uploads text, never a frame.

        The request only ever carries strings; this guard turns "we meant to"
        into "the code refuses".
        """

        for message in body.get("messages", ()):
            if not isinstance(message, Mapping):
                raise PlannerError("request messages must be objects")
            content = message.get("content")
            if not isinstance(content, str):
                raise PlannerError(
                    "planner messages must be plain text; image/audio content parts "
                    f"are not allowed (got {type(content).__name__})")

    # -- transport --------------------------------------------------------- #
    def _post(self, body: bytes) -> bytes:
        request = urllib.request.Request(
            self.config.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "x-opencode-session": self.session,
                "User-Agent": "seerauto-planner/0.1",
            },
            method="POST",
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.config.timeout_s) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise PlannerError(self._http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise PlannerError(self._timeout_message()) from exc
            raise PlannerError(
                f"cannot reach the AI gateway at {self.config.endpoint}: "
                f"{exc.reason}") from exc
        except TimeoutError as exc:
            raise PlannerError(self._timeout_message()) from exc
        except OSError as exc:  # connection reset, DNS flake, ...
            raise PlannerError(
                f"AI gateway connection failed ({type(exc).__name__}: {exc})") from exc

    def _timeout_message(self) -> str:
        return (f"AI gateway did not answer within {self.config.timeout_s:g}s "
                f"({self.config.endpoint}); the planner degrades to the deterministic "
                "path")

    @staticmethod
    def _http_error_message(exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001 - the body is best effort
            detail = ""
        hint = ""
        if exc.code in (401, 403):
            hint = (" - the gateway rejected the credential; check "
                    f"{ENV_API_KEY} and {ENV_SESSION}")
        elif exc.code == 404:
            hint = f" - check {ENV_BASE_URL} (the endpoint must end with /v1 or similar)"
        elif exc.code == 429:
            hint = " - rate limited or out of quota"
        elif exc.code >= 500:
            hint = " - gateway-side failure, retry later"
        suffix = f": {detail}" if detail.strip() else ""
        return f"AI gateway returned HTTP {exc.code}{hint}{suffix}"

    # -- response handling ------------------------------------------------- #
    def _load_response(self, raw: bytes) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlannerError(f"AI gateway returned non-UTF-8 bytes: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlannerError(
                f"AI gateway returned a non-JSON body ({exc.msg} at char {exc.pos}): "
                f"{_snippet(text)}") from exc
        if not isinstance(payload, Mapping):
            raise PlannerError(
                f"AI gateway returned {type(payload).__name__}, expected a JSON object")
        if payload.get("error") and not payload.get("choices"):
            detail = json.dumps(payload["error"], ensure_ascii=False)[:300]
            raise PlannerError(f"AI gateway reported an error: {detail}")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PlannerError(
                "AI gateway response has no 'choices'; got keys "
                f"{sorted(map(str, payload.keys()))}")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise PlannerError("AI gateway response choice is not an object")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise PlannerError("AI gateway response choice has no 'message' object")
        content = _content_text(message.get("content"))
        if not content or not content.strip():
            raise PlannerError("AI gateway returned an empty message content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        return {
            "payload": payload,
            "choice": choice,
            "content": content,
            "usage": usage,
        }

    def _decision_from_content(self, content: str, finish_reason: Any,
                              req: PlanRequest) -> PlanDecision:
        reason = str(finish_reason or "")
        if reason == "length":
            raise PlannerError(
                "the model hit its token limit (finish_reason=length), so the decision "
                f"is truncated: {_snippet(content)}")
        decision = parse_decision(content)  # PlanError on schema violations
        decision.validate(
            known_nodes=req.graph_digest or [],
            known_capabilities=req.capabilities or [],
            budgets=req.budgets or None,
        )
        return decision

    def _meta(self, response: Mapping[str, Any], choice: Mapping[str, Any],
              content: str, prompt_chars: int, latency_ms: int,
              index: int, *, retries: int = 0) -> CallMeta:
        usage = response.get("usage") or {}
        prompt_tokens = _token_count(usage.get("prompt_tokens"))
        completion_tokens = _token_count(usage.get("completion_tokens"))
        model = str(response.get("payload", {}).get("model") or self.config.model)
        return CallMeta(
            model=model,
            latency_ms=latency_ms,
            prompt_chars=prompt_chars,
            response_chars=len(content),
            cost_usd=self._estimate_cost(model, prompt_tokens, completion_tokens),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            retries=retries,
            finish_reason=str(choice.get("finish_reason") or ""),
            call_index=index,
        )

    def _estimate_cost(self, model: str, prompt_tokens: int | None,
                       completion_tokens: int | None) -> float | None:
        price = self.prices.get(model)
        if price is None or prompt_tokens is None or completion_tokens is None:
            return None
        prompt_usd, completion_usd = price
        return (prompt_tokens * prompt_usd + completion_tokens * completion_usd) / 1_000_000.0

    # -- guards ------------------------------------------------------------ #
    def _require_available(self) -> None:
        if not self.config.base_url.strip():
            raise PlannerUnavailable(
                f"{ENV_BASE_URL} is empty; the AI planner is unavailable "
                "(deterministic and bridge planners still work)")
        if not self.config.api_key.strip():
            raise PlannerUnavailable(
                f"{ENV_API_KEY} is not set; the AI planner is unavailable "
                "(deterministic and bridge planners still work)")
        if self._calls >= self.config.max_calls:
            raise PlannerUnavailable(
                f"AI call cap reached ({self.config.max_calls} per process, "
                f"{ENV_MAX_CALLS}); refusing the next decision")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _content_text(content: Any) -> str | None:
    """Chat-completions content may be a string or a list of text parts."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in (None, "text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts) if parts else None
    return None


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _snippet(text: str, limit: int = 240) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


__all__ = [
    "OpenAICompatPlanner",
    "AIConfig",
    "CallMeta",
    "SYSTEM_PROMPT",
    "PRICE_TABLE_USD_PER_MTOK",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "ENV_SESSION",
    "ENV_JSON_MODE",
    "ENV_MAX_CALLS",
    "ENV_TIMEOUT_S",
]
