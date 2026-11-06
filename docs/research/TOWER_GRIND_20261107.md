# 试炼之塔无人值守刷塔器 (tower_grind) — 2026-11-07

> Status: **UNIT_ONLY**. No live cycle has been run by this author; the live client
> belongs to the operator. No XP/hour number is claimed until a real multi-cycle
> measurement exists (the single live sample was floor 1 / 1710 XP, n=1).
> Evidence for the path: `data/overnight/20261106_dual_mode/live/`,
> `world_facts_20261106.jsonl`, task-6.

## 1. What was built

| file | role |
|---|---|
| `tools/tower_grind.py` | standalone state machine + CLI (+ `--dry-run`), calibration loader, `data/overnight/tower_grind.jsonl` ledger |
| `seerauto/config/tower_calibration.json` | schema/example, shipped **UNCALIBRATED**, LIVE-VERIFIED anchors pre-filled |
| `tests/test_tower_grind.py` | 16 unit tests (scripted OCR, fake battle runner) |
| this doc | hand-off contract + live acceptance plan |

Not modified (kept as evidence): `tools/farm_tower_repeat.py`, `tools/tower_cycle.py`,
`seerauto/` — the battle layer is *called*, never edited.

## 2. Why this path (the root cause of the old BLOCKED)

`farm_tower_repeat.py` was BLOCKED all night because it assumed a 「战斗」 button in the
tower. There is none: a fight starts by clicking the **guardian's body at (1190,760)
twice** (walk, then engage). The dialog's 「你当前到达第 N 层」 is the historical
highest; the current floor is the **painted number on the ground** (01, 02, …), which
also auto-advances after a win. The runner is built exactly around those two facts.

## 3. State machine (per cycle)

```
DIALOG -> PICK_FLOOR -> INSIDE -> ENGAGE -> BATTLE -> RESULT -> NEXT
```

| phase | action | evidence / predicate |
|---|---|---|
| DIALOG | recognise tower panel by `tower.dialog.markers` | two consecutive agreeing readings |
| PICK_FLOOR | click `dialog.pick_floor_px`, then preset (default 1) | floor number readable afterwards |
| INSIDE | read painted floor in `inside.floor_roi_px` | **two consecutive agreeing** readings, else BLOCKED |
| ENGAGE | **approach + guardian sweep** (live-verified 2026-11-07): click `tower.approach_px` (walkable floor), wait `tower.approach_wait_s` (~1.6 s), then click the sweep `[tower.guardian_px] + tower.guardian_candidates_px` one point at a time | stop at the first point that produces battle markers; a sweep of N points gets **2 rounds** (single point: 3), hard cap **12 guardian clicks**, then **BLOCKED** (the guardian changes position every floor; a fixed point stops working) |
| BATTLE | `seerauto.skills.act.RunBattle` (verified, unmodified) or injected runner | its own PASS/FAIL + payout |
| RESULT | **click-and-read chain loop** (`max_confirm_clicks`): sample → capture `result.xp_regex` XP text (`xp_source=result_ocr`, first wins) / defeat markers / level-up markers → if a calibrated confirm word is on screen, click it and sleep 0.6–0.8 s → else stop | XP can appear only *after* a 「确认」; no single frame contains the whole chain |
| NEXT | floor cross-check when readable; `xp_value > 0` is primary | advanced ⇒ PROGRESS; XP > 0 ⇒ PROGRESS (`floor_source=xp_text`); win with neither ⇒ UNVERIFIED_ACTIVITY (counted, **not terminal**) |
| VERIFY (every `--verify-every` cycles, default 3) | open the pet panel (`tower.pet_panel.open_px`), read `升级所需经验值` in `xp_roi_px` (+`等级`), close with ESC (`close_px` null → key 53) | `xp_to_next` shrinks **or** `level` grows ⇒ **PROGRESS**, `xp_source=pet_panel_delta`, `xp_value=delta`; two identical readings ⇒ that cycle is UNVERIFIED. This is the independent game-side progress proof: `RunBattle` consumes the reward dialogs and returns `rewards=[]`, `xp=0.0` |
| NEXT | re-read painted floor | advanced ⇒ PROGRESS; not advanced and no defeat marker ⇒ **UNVERIFIED_ACTIVITY**; 「很遗憾」 ⇒ re-enter picker (≤3) |

Bounded: per-cycle timeout (`budget.cycle_timeout_s`), ≤3 re-entries after defeats,
≤3 failures, **≤3 consecutive UNVERIFIED cycles** (`budget.max_unverified`, then stop
with `stop_reason="unverified_limit"`), absolute deadline, pause/stop at cycle
boundaries. PP/HP markers ⇒ `BLOCKED` reason `pp_exhausted` (never click through it).
Unknown screen ⇒ BLOCKED. Every run ends with a final JSON carrying `stop_reason`
(`cycle_budget | deadline | stop_flag | pause_flag | failure_limit | unverified_limit
| no_work_limit | blocked | no_cycle`) and `unverified` so a night can be explained
from the log alone.

## 4. Calibration fields the operator must fill

File `seerauto/config/tower_calibration.json`. All coordinates are **game_px**
(window-local device pixels of `reference.capture_px`; the runner refuses if the live
capture differs).

Pre-filled from the 2026-11-06 live run — **verify, don't re-guess**:
`guardian_px [1190,760]`, `dialog.pick_floor_px [1484,887]`,
`dialog.continue_px [1481,1007]`, `dialog.floor_presets 1/11/21 = [1895,
795/877/956]`.

**P0 — filled by the lead on 2026-11-07 from live frames**
(`floor_roi_px [1100,1200,1400,1400]`, `guardian_px [1220,800]`,
`status=CALIBRATED`). The painted floor is **art lettering that OCR misreads** (true
`03` read as `08` at conf 0.30), so the INSIDE/NEXT logic was hardened on
2026-11-07 with a visual signature and an XP-primary completion rule:

* `tower.inside.signature` (optional, now filled): `roi_mean_color` over the platform
  `[850,650,1500,1100]`, `expect_rgb [159,181,170]`, `tolerance 40`.
  Semantics: **"I am inside the tower"**. It is only consulted when the painted floor
  cannot be read twice consistently, and a hit lets INSIDE proceed to ENGAGE with
  `floor=null`, `floor_source="signature"`; a miss keeps the runner BLOCKED.
  The block is validated against real frames (REPLAY): 4 inside frames max channel
  delta **10**; room/base/aux/wild frames min delta **49** > tolerance.
  Re-measure any time with:
  `PYTHONPATH=.pydeps python3 tools/tower_grind.py --sample-signature --image <png> --roi x0,y0,x1,y1`
* NEXT now treats `xp_value > 0` (「XIN精灵获得经验：N」) as the primary completion
  evidence with `floor_source="xp_text"`; a readable painted floor is only a
  secondary cross-check (a misread `08` cannot undo a real 1710 XP win). A win with no
  XP text and no floor advance stays `UNVERIFIED_ACTIVITY`.
* Reward chain: up to 10 confirm clicks (`max_confirm_clicks`), including the new
  level-up panel (`result.levelup_markers`).

**Live-verified additions (2026-11-07, gameops + first unattended run):**
`tower.approach_px [1238,900]`, `tower.approach_wait_s 1.8`,
`tower.guardian_px [1200,745]` (two-step posture), `tower.budget.max_unverified 3`.
Live tactical constants: F3–F7 pay **3300–3630 XP/floor**, **30–37 s/floor**, PP −1
per floor, HP unchanged; F8 needs the same two-step posture; **no bottom bar inside
the tower ⇒ `travel` impossible**; PP-out and defeat paths are still **UNOBSERVED**.

**P1 — verify on screen before trusting (already present, unverified):**
`tower.dialog.markers`; `tower.battle.battle_markers`; `tower.result.roi_px`;
`tower.result.xp_regex` (current `XIN精灵获得经验[:：]\s*(?P<xp>\d+)`, must keep a
named `xp` group); `tower.result.confirm_words`; `tower.result.defeat_markers`;
`tower.result.pp_markers`; `tower.inside.floor_regex` (named group `floor`).

## 5. Commands

### 4.0 Self-healing RECOVER (`--auto-recover`, default OFF; live 2026-11-07)

A defeat in the tower sends the character back to the **space-station hall** (the
bottom bar returns); the tower itself has **no heal entry** (all 18 bag hover slots
miss 精灵恢复; `game.heal_pet` fails inside because it needs the bottom bar), while
the hall heal path works (bottom bar → 精灵背包 → 精灵恢复, 20 XIN豆). 「继续挑战」
returns to the **historical highest floor (23)**, so getting back to a low floor needs
「选择楼层」→ preset 1.

`--auto-recover` (max `--max-recovers`, default 3 per run) enters RECOVER when any of:
(a) **HP read at start ≤10%** (`tower.pet_panel.hp_roi_px` + `hp_regex`); (b) a battle
whose reason contains **`no engageable monster`** — the project's documented HP=0
disguise (`docs/state.md`) — with an HP re-read first: if HP >10% **and still inside
the tower**, do not heal, log a `posture_retry` and try another posture; only a
genuinely low HP heals; (c) a defeat marker + lost battle; (d) a BLOCKED cycle where
the tower signature is missing (lost / hall). Every recover row carries
`"trigger": "hp0_at_start|no_engageable_monster|defeat|blocked"` plus the HP reading. RECOVER steps, using **only verified repo skills** (no new hard-coded
click coordinates):
1. 5 s observation window: screenshot, OCR, refuse to act if any confirm/defeat
   marker is on screen (no blind clicks);
2. if not inside the tower: `game.heal_pet` → require skill PASS and lead HP > 0;
3. `game.travel("tutor_room")` → require the tower dialog (`选择楼层`/`继续挑战`
   markers, two readings) → `_open_and_pick` picks preset 1;
4. ledger row `{"kind":"recover","from":"defeat|blocked_unknown_screen","heal_ok",
   "floor_picked":1,"ok","reason"}` and the main loop continues (it does not exit).
A failed **heal is tolerated** and recorded as `heal_unavailable` (the hall heal path
is not always the one available); the chain continues to travel, which is the decisive
step. Any failed **travel/dialog/preset** step ends **BLOCKED** with the step name, its
reason and the first 5 OCR lines of the screen. The budget is 3 per run; pause/stop are
honoured at each step; RECOVER never buys, sells, releases pets or touches batteries.

**`--recover-only`** (one-shot mode): no cycles are run; the chain above executes once
and the process exits with `{"ok":bool,"mode":"recover-only","steps":[{step,ok,reason}],
"screen_text":[...],"heal_ok","heal_unavailable","floor_picked"}` and exit code 0/7.
This is the command an external supervisor can call whenever the client is outside the
tower (world/hall/room), followed by a normal `--minutes N` window:

```bash
PYTHONPATH=.pydeps python3 tools/tower_grind.py --recover-only --allow-input
# ok:false -> read steps[*].reason + screen_text; the ledger has kind "recover.only"
``` Status: **UNIT_ONLY** — the hall heal path is live-verified by the operator,
the full RECOVER chain is not yet run live.

### 4.1 Sweep semantics (2026-11-07 second field run)

The authoritative sweep is **`[tower.guardian_px] + tower.guardian_candidates_px`**
(that is what the runner clicks, and what the regression test pins). `tower.guardian_sweep_px`
is only a fallback when no candidate list exists. To use the wider 10-point grid the
operator measured, put those points **into `guardian_candidates_px`** (keep the
primary `guardian_px` first). Bounds: sweep of N points → 2 rounds (1 point → 3
rounds), hard cap **12** guardian clicks, then BLOCKED.

`--verify-every N` (default from `tower.pet_panel.verify_every` = 3) controls the
independent pet-panel check; 0 disables it. Opening the panel every cycle is slower
and riskier, so 3 is the intended cadence.

## 5.1 How to run it in the background (survives the shell)

`setsid` is not shipped on macOS; use `nohup` + `disown` (`nohup` ignores SIGHUP so
the process survives the shell exiting, `disown` removes it from the shell's job
table). Capture the PID so it can be inspected/stopped:

```bash
cd /Users/ceontou/Documents/code/seer-automation-night && \
nohup env PYTHONPATH=.pydeps python3 tools/tower_grind.py --preset 1 --minutes 380 \
  --deadline-at 2026-11-07T07:00:00+08:00 --allow-input \
  > data/overnight/20261106_dual_mode/tower_grind.log 2>&1 < /dev/null & \
echo $! > /tmp/tower_grind.pid && disown

# check it is alive / read the evidence
ps -p "$(cat /tmp/tower_grind.pid)" -o pid,etime,command
tail -f data/overnight/20261106_dual_mode/tower_grind.log
# graceful stop at the next cycle boundary
touch /tmp/seerauto.stop
```

If the shell is a DSH-managed job, prefer the managed background job instead (it
keeps the PID and lets the harness report the exit code).

```bash
# 0) refuse-to-run / missing fields (never opens a client)
PYTHONPATH=.pydeps python3 tools/tower_grind.py --dry-run

# 1) after filling the P0 field and status=CALIBRATED
PYTHONPATH=.pydeps python3 tools/tower_grind.py --dry-run     # expect calibrated:true

# 2) the unattended window (start while at the tower dialog or already inside)
PYTHONPATH=.pydeps python3 tools/tower_grind.py --preset 1 --minutes 400 \
    --deadline-at 2026-11-07T07:00:00+08:00 --allow-input --auto-recover

# stop/pause
touch /tmp/seerauto.stop        # ends at the next cycle boundary
touch /tmp/seerauto.master.pause
```

Evidence: `data/overnight/tower_grind.jsonl` — one `cycle` record with `ts`,
`cycle`, `floor_before`, `floor_after`, `xp_text` (verbatim), `xp_value`,
`xp_source` (`result_ocr` or `battle_runner_after` when the battle layer already
consumed the dialog), `dialogs`, `clicks`, `engage_clicks`, `reentries`, `elapsed_s`,
canonical `outcome`.

## 6. Live acceptance plan

1. **REPLAY_PASS** — done here: 16 unit tests with scripted OCR (no client).
2. **LIVE_ATTEMPTED (zero capsule/HP risk)** — operator fills `floor_roi_px`, runs
   `--dry-run` until `calibrated: true`, then one cycle with `--max-cycles 1`,
   starting at the tower dialog. Expected: floor read, guardian engaged, one battle
   fought, ledger row with floor 01→02, `outcome=PROGRESS`.
3. **LIVE_PASS** — a multi-cycle window (≥5 cycles) where every cycle's painted floor
   advances and XP text is captured; only then may a throughput number be quoted.
   Until then this document claims no XP/hour.

## 7. Honest limits / open questions

1. `RunBattle` is the verified battle layer but was tuned for wild battles; whether its
   scene detector/scene points work inside the tower is **unverified live**. If it
   reports WORLD and hunts, the cycle ends FAILED (bounded, no blind clicking) — the
   operator should report that before the long window.
2. The reward dialogs may be consumed by `RunBattle` before RESULT reads them; then
   `xp_text` is null and `xp_value`/`xp_source` come from the battle layer's payout
   parser (`battle_runner_after`), which is recorded explicitly, not silently.
3. A defeat currently ends the cycle FAILED after bounded re-entries; whether the tower
   lets a defeated player re-pick a floor freely is assumed from the dialog (not
   re-tested live).
4. No navigation: the runner must start at the tower dialog or inside. If the client
   is elsewhere, it reports `unknown screen` and stops (does not guess) — and the
   tower interior has **no bottom bar**, so `travel` cannot be used from inside.
5. HP/PP healing is not automated here: `pp_exhausted` is a stop condition.
6. Live tactical data (n=2 floors, 2026-11-07): floor 1 = **1710 XP**, floor 2 = **3003
   XP** (levelled to Lv42), PP −1 per floor, HP unchanged, 20–25 s per floor, 6-click
   reward chain, level-up panel is a new screen state that must be dismissed. This is
   a small sample, **not** an XP/hour figure; the first accepted number needs a real
   multi-cycle window (≥5 cycles).
