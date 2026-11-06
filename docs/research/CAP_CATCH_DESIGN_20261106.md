# CAP-CATCH design & live test plan (2026-11-06)

> Status: **UNIT_ONLY**. No live throw has been attempted by this author. The live
> client is owned by the live agent; this document is the hand-off contract between
> the unit-tested code and the first real catch.
> Spec: `docs/research/20261106_CATCH_SPEC.md`. Task graph node:
> `docs/PUNI_TASK_GRAPH.md` → `CAP-CATCH` (was ❌).

## 1. What was built

| file | role |
|---|---|
| `seerauto/skills/catch.py` | calibration loader/validator, `CatchPet` skill, `CatchSession` state machine, catch ledger (append-only JSONL) |
| `seerauto/config/catch_calibration.json` | schema + placeholder values, shipped `UNCALIBRATED` (skill refuses to click) |
| `tools/catch_pet.py` | CLI: `--dry-run` / real run, budget + deadline flags, machine-readable result |
| `tests/test_catch_pet.py` | 26 unit tests (fake session, scripted OCR) |

Deliberately **not** changed: `seerauto/skills/act.py` (`build_act_skills`), the task
graph, the planner. Registering `game.catch_pet` as a capability the planner may pick
is a separate wiring step; the skill and CLI are usable directly tonight.

## 2. State machine (one attempt)

```
PRECHECK -> ITEM_PANEL -> THROW -> VERDICT -> CLEANUP -> VERIFY
```

| phase | action | predicate (two consecutive agreeing samples) | failure => |
|---|---|---|---|
| PRECHECK | OCR `battle.report_roi_px`, match `battle.opponent_regex` | same `(name, level)` twice | unreadable => `UNVERIFIED_ACTIVITY`, **no click**; wrong name/level => `BLOCKED`, **no throw** |
| ITEM_PANEL | one click at `battle.item_button_px` | ≥1 of `item_panel.markers` twice | panel absent => `BLOCKED`, **no second click** |
| THROW | click `capsule_slot_px`, then `use_button_px` | one of `selection_markers` twice before the throw click | selection unconfirmed => `BLOCKED`, **no use click** |
| VERDICT | OCR `battle.result_roi_px` | same class+word twice | no evidence => `UNVERIFIED_ACTIVITY`, **no second throw** |
| CLEANUP | click calibrated confirm words only | – | best-effort, never blocks a result |
| VERIFY | re-read capsule count if `inventory.capsule_count_roi_px` is set | – | unreadable => `inventory_verified=false` |

Session bounds (hard-coded ceilings a calibration/CLI value may only tighten):
`throws <= 5`, `reserve >= 1` capsule, `failures <= 3`, one target kind, absolute
deadline, `/tmp/seerauto.{pause,master.pause,stop}` checked at every cooperation
point (before each attempt and at each sample gap).

## 3. Calibration — what the live agent must fill

File: `seerauto/config/catch_calibration.json`. Coordinates are **game_px**
(window-local device pixels; 2400×1600 on tonight's 1200×800 pt window).
`reference.capture_px` must match the live capture or the skill refuses.

Required for `status=CALIBRATED` (P0, SPEC §5-A):

| field | how to measure (read-only) |
|---|---|
| `battle.report_roi_px` | `tools/ctl.py look` in a battle; box around the 战报 first line |
| `battle.opponent_regex` | must keep named groups `name` and optional `level`; validate against a real 战报 line |
| `battle.item_button_px` | `tools/ctl.py hover` scan right of the 战斗 button ROI `(1710,1130,2040,1260)` |
| `battle.item_panel.capsule_slot_px` | `ctl.py look` after opening 道具 (read-only), click only if the agent is authorized to open it |
| `battle.item_panel.use_button_px` | same panel: 使用/投掷 button |
| `battle.result_roi_px` | 战报 area that shows 捕捉成功/挣脱/逃跑 |
| `battle.result_keywords.{success,escaped}` | from the same sample; add `fled`/`defeated` if seen |

Already present but **unverified** placeholders (SPEC §5-B, P1): panel markers,
`selection_markers`, `inventory.capsule_count_roi_px` (null = unknown capsule count),
`targets.pippi.level_max` (3), budget values.

`battle.item_panel.markers`, `selection_markers`, `budget`, `safety.confirm_words`
and the target list live in the same file. `safety.confirm_words` is a **subset** of
the hard-coded allowlist (`确认/确定/我知道了/关闭/取消`) — the file cannot add a new
clickable word.

## 4. Safety boundaries (enforced in code, not playbook)

* Click reasons are limited to `item_button`, `capsule_slot`, `use_button`,
  `confirm:<one of the calibrated confirm words>`.
* Forbidden words are refused both as click targets and as calibration content:
  `放生 出售 交易 购买 丢弃 还原 重组 融合 赋形 收藏 展示 放回仓库 放入背包`.
* No code path buys capsules, sells, releases, re-arranges or evolves a pet.
* Capsule budget: `<= 5` throws per session, `>= 1` kept in reserve, stop after
  **3** failed attempts; "no budget" ends `BLOCKED` (restocking is a human decision).
* Unreadable evidence never becomes PASS: `UNVERIFIED_ACTIVITY` instead.

## 5. Evidence rules (what PASS means)

`PROGRESS` (skill `PASS`) requires **two consecutive agreeing result-text readings**
of a calibrated success keyword. The proof string carries that text; the ledger row
records `capsules_before/after` and `inventory_verified`. If the result text cannot be
read: `UNVERIFIED_ACTIVITY`, status `FAIL`, and **no second capsule is thrown** (it
may already be consumed).

Known honest limit: the shipped inventory ROI is `null`, so `inventory_verified=false`
until P1 is calibrated. A capture can then be PASS on result text alone — the SPEC's
primary evidence — but the capsule-delta cross-check is missing and is recorded as such.

## 6. Live test plan (live agent; not this author)

Acceptance levels (SPEC §7):

1. **REPLAY_PASS** — done in unit tests (scripted OCR, no client): the panel can be
   recognised and the verdict classified. `26 passed`.
2. **LIVE_ATTEMPTED (zero capsule spend)** — in a battle with 皮皮: fill the P0
   calibration, run `tools/catch_pet.py --dry-run` (must show `calibrated: true`),
   then a run that opens 道具 and stops before 使用 — e.g. temporarily set
   `use_button_px` to `null` so the skill refuses at that step, and confirm the panel
   layout + markers from the ledger evidence. Cost: 0 capsules.
3. **LIVE_PASS** — the first real catch, with the user's authorized capsule spend:
   - result text shows a success keyword (recorded in `catch_ledger.jsonl`), and
   - `capsules_before - capsules_after == throws_used` (needs P1 inventory ROI), or the
     storage/team count increased by 1 when inventory is unreadable.
   Until all of that is true, the capability stays `UNIT_ONLY` in every report.

Commands:

```bash
# 0) what is still uncalibrated (never opens a client)
PYTHONPATH=.pydeps python3 tools/catch_pet.py --dry-run

# 1) after the live agent filled the P0 fields and set status=CALIBRATED
PYTHONPATH=.pydeps python3 tools/catch_pet.py --dry-run   # expect calibrated:true

# 2) the real bounded attempt (already in battle with the target)
PYTHONPATH=.pydeps python3 tools/catch_pet.py --target pippi --max-capsules 5 \
    --allow-input --deadline-at 2026-11-07T07:00:00+08:00

# 3) read the evidence
python3 -c "import json;[print(json.loads(l)['kind'], json.loads(l).get('outcome')) \
    for l in open('data/overnight/catch_ledger.jsonl')]"
```

## 7. Open questions / TODO (do not encode as facts)

1. Whether 使用/投掷 is one click or two (select + use) — the session assumes
   select-then-use; a one-click-only UI will fail at the selection predicate and end
   `BLOCKED` without a second click. Adjust after the first live sample.
2. Success rate of 高级捕捉胶囊 on Lv1–2 皮皮 — unknown (SPEC §8.1).
3. Where a caught pet goes (team or storage) and whether a naming dialog appears —
   unknown; the session only dismisses calibrated confirm words, never a text input.
4. Capsule count in storage (whether the tower's 3 中级胶囊 per floor exist) — read it
   before budgeting; `capsules_before=null` means the ceiling is used, not a guess.
5. Planner/graph registration (`game.catch_pet` in `build_act_skills` + CAP-CATCH node)
   — not done here.
