# seerauto.planner — the AI seam (one node, one capability, never a coordinate)

```
User Goal -> AI Planner / Supervisor -> Goal Graph + World State -> Scheduler
          -> Verified Semantic Skills -> Deterministic Vision Executor -> Game
```

This package is the **only** place where a model may make a decision, and the
only decision it may make is: *which ready goal node, executed by which
registered capability, with which budget*. It never emits coordinates, key
presses, OCR thresholds or per-frame choices; the deterministic executor owns
those (`AGENTS.md` section 7).

No third-party dependency: the HTTP client is stdlib `urllib`, and nothing here
imports the capture layer, the goal graph or the world store.

## Modules

| File | Role |
|---|---|
| `base.py` | `PlanRequest` / `PlanDecision` / `Planner` protocol / `parse_decision` (strict schema) |
| `deterministic.py` | `DeterministicPlanner` — priority-ordered, no model, no network |
| `openai_compat.py` | `OpenAICompatPlanner` — `/chat/completions` over `urllib`, text only, capped calls |
| `bridge.py` | `BridgePlanner` — `request-<seq>.json` / `response-<seq>.json` rendezvous for a human or agent planner |

`build_planner("deterministic" | "ai" | "bridge", **kw)` is the factory; an
unknown name raises instead of silently disabling the AI layer.

## The decision contract

Exactly one JSON object, no extra top-level keys:

```json
{
  "goal": "puni",
  "selected_node": "puni.prepare.level_up",
  "capability": "game.run_battle",
  "reason": "当前精灵等级未达到目标",
  "expected_observation": "目标精灵等级或经验增加",
  "max_actions": 40,
  "max_duration_s": 1800,
  "requires_approval": false
}
```

* `max_actions` ∈ 1..200, `max_duration_s` ∈ 1..7200, `requires_approval` is a
  real JSON boolean.
* `selected_node` must exist in the digest, `capability` must be registered, and
  the budgets must fit inside the run budget.
* A refusal schedules no work: `{"stop": true, "stop_reason": "..."}` and/or
  `{"need_human": true, "human_question": "..."}` (the latter implies stop). A
  refusal names no node/capability and carries zero budgets.
* `parse_decision()` tolerates a ```` ```json ```` fence or surrounding prose;
  everything inside the object is checked strictly. A bad answer raises
  `PlanError`; it is never patched into something plausible.

## What the runner must build

`PlanRequest` (no images, no coordinates):

```
goal            "puni"
world_summary   GameWorldState.summary()
graph_digest    one entry per node:
                node.to_dict() + {"scheduler_status": graph.status_of(node_id, ...)}
capabilities    [SkillSpec.to_dict() for every registered skill]
completed_nodes ids the runner observed finishing
recent_events   tail of the event log
last_result     previous SkillResult.to_dict() or None
budgets         {"max_actions": 200, "max_duration_s": 3600}
                (the runner's {"max_battles", "max_minutes"} work too)
constraints     {"allow_input": false, "forbid_approval": false, "max_spend": 0}
run_id          optional
```

Digest aliases accepted (so no translation layer is needed):

* `skill` for `capability`;
* `scheduler_status` for `status` (it wins when both are present, because the
  goal file's own `status` is a static "todo/partial" label);
* `max_battles` / `max_minutes` / `max_seconds` inside a node `budget` (and at
  run level);
* `blocked_reason` may be a string *or* a list (`graph.unmet()[:3]`) — both are
  rendered into one line;
* an optional advisory `playbook: false` (the runner has no verified skill
  sequence for that node) is treated as missing work, not as "blocked".

`PlanRequest.build(goal, world, ...)` calls `world.summary()` duck-typed, so the
world store can be passed without importing it.

## Wiring

```python
from seerauto.planner import build_planner, PlanRequest

req = PlanRequest.build("puni", world,
                        graph_digest=digest,
                        capabilities=registry.specs(),
                        budgets={"max_actions": 200, "max_duration_s": 3600},
                        constraints={"allow_input": True},
                        run_id=run_id)
decision = build_planner("deterministic").decide(req)   # or "ai" / "bridge"

if decision.stop or decision.need_human:
    log(decision.stop_reason, decision.human_question)
else:
    log(decision.reason, decision.expected_observation)
    scheduler.run(decision.selected_node, decision.capability,
                  max_actions=decision.max_actions,
                  max_duration_s=decision.max_duration_s)
```

`decision.summary()` is a one-line log form; `deterministic` reasons always cite
the evidence they used (scheduler status, UNKNOWN world facts, `last_result`, or
the skipped higher-priority nodes).

## AI planner (opencode-go gateway)

| Env | Default | Note |
|---|---|---|
| `SEERAUTO_AI_BASE_URL` | `https://opencode.ai/inference/go/openai/v1` | `/chat/completions` is appended |
| `SEERAUTO_AI_API_KEY` | *(empty)* | required for `available()` |
| `SEERAUTO_AI_MODEL` | `deepseek-v4.1-flash` | |
| `SEERAUTO_AI_TIMEOUT_S` | 45 | per call |
| `SEERAUTO_AI_MAX_CALLS` | 20 | per process; the cap is enforced |
| `SEERAUTO_AI_SESSION` | random `seerauto-<hex>` | sent as `x-opencode-session` |
| `SEERAUTO_AI_JSON_MODE` | true | `response_format={"type":"json_object"}` |

`decide()` attaches a `CallMeta` (model, latency, prompt/response chars, token
counts when reported, `cost_usd`) to `decision.meta`; `decide_with_meta()`
returns both. `cost_usd` is `None` unless the model is in an operator-supplied
price table — vendor prices change and a stale table silently lies.
`available()` is pure configuration: no network, no key, no call.

## Bridge planner (a human or an agent session plays the planner)

```python
from seerauto.planner import BridgeWatcher

names  = BridgeWatcher.pending(bridge_dir)          # unanswered requests
brief  = BridgeWatcher.format_prompt(req, seq=7)    # readable brief, no Python needed
BridgeWatcher.answer(bridge_dir, 7, decision_dict)  # validates, then writes atomically
```

`BridgePlanner(directory, timeout_s=240)` writes `request-0007.json` (the full
`PlanRequest` plus `seq`, `created_at`, `reply_with`) and polls for
`response-0007.json`; the answer is parsed with `parse_decision`, checked against
the digest and the capability registry, then returned. Staleness is checked both
ways: the payload `seq` must match and the response file must not predate the
request file. Files are written with tmp + `os.replace`; a half-written answer is
retried, a malformed one fails loudly.

## Verification status (honest)

* `tests/test_planner.py` — 90 unit tests, all synthetic: schema happy/sad
  paths, deterministic policy, bridge round-trip/timeout/stale-seq, and the AI
  transport with `urllib.request.urlopen` monkeypatched. **No real network call
  is made by the test suite.**
* Deterministic planner exercised once against the real 42-node
  `seerauto/goals/puni.yaml` in a read-only smoke run (no pytest case, because
  the goal graph is a separate module): with an empty world the graph reports
  `unknown: 8, not_executable: 29, missing_prereq: 1, blocked: 4`, zero ready
  nodes, and the planner refuses with one line of unmet reasons; forcing
  `puni.prepare.level_up` ready yields `game.run_battle`, 15 actions / 900 s
  (the skill's own cap), matching the node and run budgets.
* **The real gateway was never called with a credential**: this environment had
  no `SEERAUTO_AI_API_KEY`/`OPENCODE_GO_API_KEY`, so the wire format is verified
  only against a fake transport. An unauthenticated probe of the endpoint
  returned `HTTP 401 {"error":{"type":"server_error","message":"Upstream request
  failed: Go requires a credential"}}` in 0.6 s — the endpoint exists and
  requires the bearer credential, which is all that can be established without
  a key. A 401 from `decide()` becomes a `PlannerError` naming the credential,
  never a made-up decision.
