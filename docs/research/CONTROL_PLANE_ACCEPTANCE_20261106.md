# Independent acceptance — dual-mode control plane (2026-11-06 night)

Reviewer: `usage-eng` (task-2 ownership extended by the Lead). Independent, adversarial
acceptance of `infra-eng`'s deliverable: `seerauto/dual/*`, `tools/overnight_control.py`,
`tests/test_dual_*.py` (plus the shared ledger helper touched in `seerauto/skills/catch.py`).

Rules held throughout: **no edits to the reviewed code** (my only write is this doc), all
destructive experiments in `/tmp/accept/*`, no game input, no network, nothing written to
`~/.dsh`.

Three rounds, all recorded; only round 3 is the current state:

| round | when | state | outcome |
|---|---|---|---|
| 1 | 23:14:06 | first frozen snapshot | 14 PASS · 6 PARTIAL · 1 FAIL (21 assertions) → reported |
| 2 | 23:29:18 | after the 23:18–23:21 fixes | 18 PASS · 4 PARTIAL · 1 FAIL (23) → reported (A22/A21 found) |
| 3 | **23:48:35** | after the 23:34–23:43 fixes (A22, A21, ledger durability) | **22 PASS · 2 PARTIAL · 0 FAIL (24)** |

Round-1/2 findings are kept in each section as history. Environment caveat: **`ps` is blocked
in this sandbox** (`PermissionError: Operation not permitted: 'ps'`), so the process-probe path
cannot run live here; A17/A22 were verified by reading the code and injecting a realistic `ps`
output, and the fallback path (log → exit report → UNKNOWN) is what runs here.

If a file's sha256 no longer matches §7, re-run the commands in §2/§3 before quoting a verdict.

---

## 1. Verdict summary (round 3, current)

| # | Assertion | R1 | R2 | R3 | Evidence |
|---|---|---|---|---|---|
| A1 | Append-only ledgers, ledger-stamped `ts`, single-write + fsync | PASS | PASS | PASS | code + live records + 64 KiB guard |
| A2 | Crash-truncated tail tolerated, counted, marked as tail | PASS | PASS | PASS | half-line → `truncated_tail: true` |
| A3 | Middle corruption is **not** excused as a tail | PASS | PASS | PASS | `truncated_tail: false` |
| A4 | Checkpoint replaced atomically (no torn reads) | PASS | PASS | PASS | 13,082 writes / 118,565 reads / 0 torn |
| A5 | SIGKILL mid-checkpoint-write leaves a parseable file | PASS | PASS | PASS | 20 kills, 0 torn; stale `.tmp` swept |
| A6 | Wake fires once, then cooldown-suppresses; survives restart | PASS | PASS | PASS | DEADLINE / NO_WORK / STALL sequences |
| A7 | No wake/health flood at sub-second cadence | PASS | PASS | PASS | 40 cycles → 0 extra wake events |
| A8 | Suppression counters on disk after a **clean** exit | PARTIAL | PASS | PASS | exit == checkpoint == last `health.final` |
| A9 | Every switch audited; suppressed switches recorded | PASS | PASS | PASS | applied / reason_cooldown / min_dwell / already_in_mode |
| A10 | Five facts separate, three states, no aggregate "healthy" | PASS | PASS | PASS | 5 keys, `UNKNOWN`≠FALSE |
| A11 | No proxy: alive process/goal ≠ game progress | PASS | PASS | PASS | GOAL TRUE + rest FALSE/UNKNOWN coexists |
| A12 | `status` safe and read-only with no state | PASS | PASS | PASS | nonexistent dir: exit 0, JSON, dir not created |
| A13 | Bad stored deadline must not blind the control plane | **FAIL** | PASS | PASS | validated `--set`; degrades with `deadline_error`; watchdog evaluates |
| A13b | `status` strictly read-only | PARTIAL | PARTIAL | PARTIAL | corrupt checkpoint still moved aside + event appended |
| A14 | `status` survives concurrent writers | PASS | PASS | PASS | 0/100 failures vs a live health loop |
| A15 | `usage_meter` ↔ `ledger` contract | PASS | PASS | PASS | single writer; `init` never truncates; half-line flagged |
| A16 | Global `--checkpoint` works for every subcommand | PARTIAL | PASS | PASS | `health` lands state in the override; default untouched |
| A17 | Process probe does not match its own command line | PARTIAL | PARTIAL | **PASS** | own pid + `overnight_control.py` + grep/ps lines skipped; fake-ps proof |
| A18 | `status` shows why each fact is what it is | PARTIAL | PARTIAL | PARTIAL | still states-only; no `facts_evidence`/`probe_error` |
| A19 | Checkpoint RMW serialized across processes | PARTIAL | PASS | PASS | async interleaving preserved; 50/50 concurrent increments |
| A20 | Required test deliverables present and green | PASS | PASS | PASS | 145 passed; full suite 837 passed / 2 skipped |
| A21 | Lock contention still honours the JSON/exit contract | — | PARTIAL | **PASS** | `switch` → JSON exit 3; `health --watch` → `health.lock_timeout`, keeps running |
| A22 | The A17 fix actually works | — | FAIL | **PASS** | `probe_processes(..., exclude_pids=…)` exists and is honoured |
| A23 | Append after a crash-truncated tail cannot swallow the next record | — | — | **PASS** | starts on a fresh line; naive concatenation contrast shown |

**Round-3 counts: 22 PASS, 2 PARTIAL, 0 FAIL (24 assertions).** No PASS is granted on a
docstring; each has a command below.

---

## 2. Test runs (real, round-3 frozen state)

```bash
cd /Users/ceontou/Documents/code/seer-automation-night
PYTHONPATH=.pydeps python3 -m pytest -q tests/test_dual_*.py tests/test_outcome*.py
# -> 145 passed in 0.85s            (round 2: 138; round 1: 129)
PYTHONPATH=.pydeps python3 -m pytest -q
# -> 837 passed, 2 skipped in 42.60s  (round 2: 804 / 2 skipped; round 1: 785 / 2 skipped)
```

`tests/test_dual_switch.py` / `tests/test_dual_watchdog.py` were absent at 23:03 and appeared
23:05–23:06. Every claim below was re-derived independently; their tests were inspected but not
trusted as evidence.

---

## 3. Round-3 fixes — independently verified

### A22 (was FAIL) — the probe fix is now real

`facts.py` now has the parameter the CLI passes, and the filter is name-based as well as
pid-based:

```python
def probe_processes(pattern, *, timeout_s=5.0, exclude_pids: Iterable[int] = ()) -> list[str]:
    skip = {int(pid) for pid in exclude_pids} | {os.getpid()}
    ...
    if "grep" in text or "ps -axo" in text or "overnight_control.py" in text:
        continue
    pid_text, _, _command = text.partition(" ")
    if pid_text.isdigit() and int(pid_text) in skip:
        continue
```

Verified with a fake `ps` output that contains the caller's own argv, a real supervisor line and
a grep line:

```
status-style plain('sleep 60')  -> []                      # was: matched its own status argv
health-style excl('sleep 60')   -> []
real supervisor match           -> ['4242 python3 tools/supervise.py --minutes 40']
```

Both call sites now use it (`status` line 224, `health` line 403). No `TypeError`, no silent
`_probe_failed` fallback. Fix verified → **A17 now PASS as well**.

### A21 (was PARTIAL) — lock contention degrades instead of killing the CLI

With the checkpoint lock held by another process for 10 s:

```bash
switch --to MAX_RESULTS --reason NEED_RESEARCH --evidence x
# exit=3, stdout = {"ok":false,"error":"LedgerError: checkpoint lock ... was not acquired within 5.0s; ..."}
# stderr traceback count = 0                                   (round 2: exit 1, empty stdout, traceback)
health --watch --cycles 4 --cadence 1
# exit=0, stdout = 4/5 JSON lines, traceback count = 0; loop keeps evaluating
# runtime_health kinds: ["health", "health.lock_timeout", "health.final", "health.lock_timeout"]
```

`state_persisted` is now present in the health JSON (True on a persisted cycle), so a reader can
tell "evaluated but could not save" from "saved". Fix verified.

### A23 (new) — ledger durability: append after a crash-truncated tail

Both ledgers (`seerauto/dual/ledger.py:append` and
`seerauto/skills/catch.py:append_catch_ledger`) open with `O_RDWR|O_APPEND`, `pread` the last
byte and prefix `"\n"` when the tail is not a newline. Reproduced by writing half a JSON line
(no trailing newline) and then appending a normal record:

```
raw tail                        ... outcom\n{"kind":"after-truncation",...}\n
parseable kinds                 ['first', 'after-truncation']
appended record recovered       True
problems(events)                [{'line': 2, 'truncated_tail': False}]
```

Contrast — the old failure mode, concatenation without the newline:

```
kinds ['first']                       # the following record was swallowed
problems [{'line': 2, 'truncated_tail': True}]   # the concatenated line was excused as "the tail"
```

Normal appends are unaffected (3 records → 3 lines, no blank lines).

**Observation (not a defect, worth knowing):** after a crash-heal the remnant is no longer the
last line, so `problems()` reports it as `truncated_tail: False` — i.e. the *positional* "tail"
contract now classifies a healed crash remnant as a mid-file hole. `status.ledger_problems`
will therefore stay non-empty for the rest of the night after any crash-heal. That is honest
(the hole is really there) but an operator should know it is expected, not a second writer. A
one-line note in the ledger docstring would prevent a 03:00 false alarm.

### Round-2 fixes re-checked on the round-3 hashes (no reverts)

- **A13**: `checkpoint --set deadline="23:59ish"` → exit 3 `{"key":"deadline"}`, checkpoint
  sha256 unchanged; corrupt deadline on disk → `status` exit 0 with `deadline_error` + five
  facts, `health` exit 0 and `persisted: True`.
- **A8**: `health --watch --cycles 3 --stall-s 1 --cooldown 9999` → exit `suppressed_total=2` ==
  `checkpoint.watchdog.suppressed_total=2` == last record kind `health.final`.
- **A19**: async interleaving (watchdog save vs a `switch` subprocess) → final mode
  `MAX_RESULTS`, watchdog key preserved, switch exit 0.

---

## 4. Still-open PARTIALs (both low severity, no blocking defect)

### A18 — `status` drops per-fact evidence and leaks repo-global sources

Round-3 `status` keys: `data_dir, deadline, deadline_error, deadline_source,
executor_heartbeat_age_s, facts, initialized, last_event, last_verified_progress,
ledger_problems, ledgers, mode, night, ok, policy, remaining_s, resolved_mode, unknown_facts`.
`facts` is still states only (`{"GOAL_ALIVE":"TRUE","AGENT_DECIDING":"UNKNOWN",…}`); there is no
`facts_evidence` or `probe_error` key. The evidence that separates "no process" from "probe
broke" lives only in `collect().to_dict()` and in `health` records, e.g.:

```
SUPERVISOR_RUNNING state=FALSE source=supervisor.log
  evidence="process probe unavailable (PermissionError); supervisor.log records it finished: 11-06 01:21:28 supervisor done"
```

A `status` reader cannot see that `ps` was unavailable. Also `--data-dir` does not isolate the
default fact sources: `data/supervisor.log` and `data/overnight/wild_cycle.jsonl` are absolute
repo paths (an isolated experiment inherits the main repo's evidence — observed
`EXECUTOR_ALIVE: FALSE` from a 17-hour-old `wild_cycle.jsonl`). `health` has override flags for
all of these; `status` has none. Fix (owner: infra-eng): add at least `probe_error` (or full
`facts_evidence`) and the same path overrides to `status`.

### A13b — `status` is not strictly read-only on a corrupt checkpoint

Truncating `checkpoint.json` to `{"mode":` and running `status`: exit 0 (correct degradation),
but the file is moved aside (`checkpoint.json.corrupt-<epoch>`, 1 file) and an event is appended
(`events.jsonl` 1→2). Good forensics, data preserved — but the docstring says status is
read-only. Fix: document the repair path in the status docstring, or gate it behind `--repair`.

---

## 5. PASS evidence (tried to break, could not)

**A1/A2/A3/A23 — ledger.** `append` stamps `ts`/`ts_epoch` at write time (a payload cannot
backdate), one `os.write` on `O_APPEND` + `fsync`, 64 KiB guard (70 KB payload → `LedgerError`),
and the new `pread`/newline logic shown in §3. `problems()` distinguishes a tail
(`truncated_tail: true`) from a middle hole (`false`); readers skip both without raising, and the
NO_WORK streak is preserved across a crash remnant.

**A4/A5 — checkpoint atomicity.** Racing writer vs a `json.loads` reader: **13,082 writes /
118,565 reads / 0 torn** (7,184/48,910/0 on the 23:06 ledger). `SIGKILL` at random 50–400 ms
offsets: 12 + 8 kills across ledger versions → checkpoint parseable every time; stale
`checkpoint.json.tmp` left in 6/8 runs (by design) and removed by `init`'s `sweep_tmp`.

**A6/A7 — debounce.** Deadline already passed: tick 1 applies `DEADLINE`+`EXECUTOR_DEAD` and
writes 2 `wake` events; tick 2 applies nothing, `suppressed_this_tick` lists both,
`remaining_cooldown_s: 599.711`, counters persisted. 40 cycles at 0.02 s add **0** wake events
and **+1** health record. NO_WORK 1–2 → wake, 3–4 → suppressed; a `PROGRESS` batch resets
`consecutive_no_work` (and does **not** set `GAME_PROGRESSING`). Aged-proof STALL fires once,
next tick suppressed, fresh proof clears it.

**A9 — switch audit.** applied `EFFICIENT→MAX_RESULTS/NEED_RESEARCH`; immediate reverse with the
same reason → suppressed `reason_cooldown` (≈899.7 s left); different reason inside dwell →
suppressed `min_dwell`; no-op → `already_in_mode`; empty evidence → exit 3 with **no** record.
Suppressed requests also appear as `mode.switch.suppressed` events.

**A10/A11 — five facts.** Exactly `GOAL_ALIVE, AGENT_DECIDING, SUPERVISOR_RUNNING,
EXECUTOR_ALIVE, GAME_PROGRESSING`, no aggregate healthy key. Each source exercised
independently (goal active/paused; agent deciding/idle/**stale-deciding→UNKNOWN**; executor
fresh/stale heartbeat; game fresh/1-hour-old proof), and `GOAL_ALIVE: TRUE` coexists with
`GAME_PROGRESSING: FALSE` in one payload.

**A12/A14 — status safety.** Nonexistent data dir → exit 0, nulls, directory not created.
100 `status` calls while a health loop appended → 0 failures, every stdout parsed as JSON.

**A15 — meter/ledger contract.** Nothing in `seerauto/dual/` or the CLI writes
`model_usage.jsonl` (grep finds only docstring mentions + the `LEDGERS` tuple). `init` over a
directory holding the meter's file leaves its sha256 unchanged; a half-written meter line shows
up as `ledger_problems.model_usage [{line:2, truncated_tail:true}]` and both the control plane
and `tools/usage_meter.py report` keep working. Minor note: meter records carry `ts` (ISO) +
`epoch_ms`, not `ts_epoch`; no dual reader parses them and `parse_ts(ts)` handles the shape.

**A20 — tests.** All four required test files exist and the suite is green on the frozen hashes
(§2). No LLM/network/GUI imports in `seerauto/dual/` (grep for
`requests|urllib|socket|openai|anthropic|llm|Quartz|CGEvent|pyautogui` finds only the `__init__`
sentence saying it does not).

---

## 6. Operational state of the live night (read-only observation, 23:48)

- Last watchdog record: `kind: "health.final"` at **23:43:02**, after a `wake PAUSE` for
  `/tmp/seerauto.stop` and a `watchdog.stop` event (`cycles: 219`). The background watchdog is
  **not writing now** — it exited cleanly on the stop flag, which is the intended behaviour of
  that flag. If a watchdog is meant to be running, it needs to be started again; a fresh start
  picks up all round-2/3 fixes (A8 flush, A19 locking, A21 lock-timeout, A22 probe).
- `runtime_health.jsonl` had 69 records with `suppressed_total` 3,706–3,738 — expected for a
  permanently stalled/executor-dead night: wakes are coalesced by cooldown, suppressions counted,
  no event flood.
- The dual-mode experiment is live and the control plane is being used correctly:
  - `checkpoint.json` (1,415 B) exists; `mode_switches.jsonl` has one **applied** switch at
    **23:16:13** `EFFICIENT→MAX_RESULTS` / `NEW_COMPLEX_TASK` with an evidence pointer.
  - `model_usage.jsonl` now has two meter records: `PREP` at 22:57:50 (42 requests,
    2,537,528 tokens) and `EFFICIENT` at **23:16:13** (63 requests, 5,454,568 tokens) — i.e. the
    snapshot was taken exactly at the switch with the mode that had just ended, which is the
    protocol `tools/usage_meter.py` documents. The per-mode table will work as designed.
  - `world_snapshots.jsonl` and `failures.jsonl` are receiving live writes from the game lane /
    task-5 runner; `executor_heartbeat.json` and `agent_turn.json` do not exist yet, so
    `EXECUTOR_ALIVE` is running from the (stale) `wild_cycle` ledger and `AGENT_DECIDING` stays
    UNKNOWN — correct behaviour for absent evidence, not a defect.

---

## 7. Frozen fingerprints (round 3, frozen 2026-11-06T23:48:35+08:00; no drift at freeze)

| file | sha256 (16) | mtime |
|---|---|---|
| `seerauto/dual/__init__.py` | `f835963a751334ff` | 22:56:44 |
| `seerauto/dual/facts.py` | `aceb844a6b29effe` | 23:34:52 |
| `seerauto/dual/ledger.py` | `2d1ce47b7126e335` | 23:40:32 |
| `seerauto/dual/modes.py` | `da24b42497a07ba9` | 22:52:37 |
| `seerauto/dual/outcome.py` | `18488861922b2a6d` | 23:00:06 |
| `seerauto/dual/state.py` | `f3c9de90afc10ac5` | 23:05:31 |
| `seerauto/dual/switch.py` | `e57ec889a976316a` | 23:19:02 |
| `seerauto/dual/watchdog.py` | `e731535ff8e51522` | 23:35:09 |
| `tools/overnight_control.py` | `aa92b316a72dac87` | 23:35:46 |
| `tests/test_dual_ledger.py` | `ec06c2d475990590` | 23:42:25 |
| `tests/test_dual_modes.py` | `3c2ffb796ffc27ce` | 23:00:06 |
| `tests/test_dual_switch.py` | `c8819a6041e3ece8` | 23:21:04 |
| `tests/test_dual_watchdog.py` | `688b26e4687231ed` | 23:35:46 |
| `tests/test_outcome.py` | `09c8c9eca8ef1c00` | 07:34:26 (pre-existing) |
| `seerauto/skills/catch.py` (shared ledger helper) | `e437a387feeda8b6` | 23:43:36 |

Round-2 (23:29) hashes for comparison: `facts 022735da715d3bf3`, `ledger fe25dc178e2c0a0d`,
`watchdog fcaad2f540981801`, `overnight_control e5780a05af9f4db1`,
`test_dual_ledger 9c9e98096eb547f1`, `test_dual_watchdog f860223012e3cc91`.

---

## 8. What I did NOT verify (honesty boundary)

- A real nine-hour soak; all cooldowns/throttles were exercised with injected clocks or
  sub-second cadences.
- The `ps` success path on an unsandboxed host (EPERM here); A17/A22 were proven by reading the
  code and injecting a realistic `ps` output, plus the sandbox fallback path was exercised.
- Executor/game-layer integration: `world_snapshots.jsonl` and `failures.jsonl` began receiving
  live writes at 23:13–23:45 (game lane / task-5 `tools/night_runner.py`), but that integration
  is out of scope here and I did not validate it; `executor_heartbeat.json` and
  `agent_turn.json` still do not exist, so `EXECUTOR_ALIVE`/`AGENT_DECIDING` have never been
  exercised against live writers.
- A production-scale lock-contention scenario (A21 was forced by holding the lock 10–14 s).
- The new CAP-CATCH files (`seerauto/skills/catch.py` is shared here only for the ledger-durability
  fix; the catch feature itself is out of scope for this acceptance).
