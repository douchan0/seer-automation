# Independent acceptance — dual-mode control plane (2026-11-06 night)

Reviewer: `usage-eng` (task-2 ownership extended by the Lead). This is an **independent,
adversarial acceptance** of `infra-eng`'s task-1 deliverable: `seerauto/dual/*`,
`tools/overnight_control.py`, `tests/test_dual_*.py`.

Rules I held myself to: **no edits to the reviewed code** (write scope is this doc only),
all destructive experiments in `/tmp/accept/*` scratch dirs, no game input, no network, and
nothing written outside `/tmp` + this file. Every claim below names the command that
reproduces it.

Artifacts were frozen for review at **2026-11-06T23:10:04+08:00**; sha256 prefixes are in §7.
`infra-eng` was still editing until 23:08, so the tests were re-run on the frozen state.

Environment caveat that matters for one finding: **`ps` is blocked in this sandbox**
(`PermissionError: [Errno 1] Operation not permitted: 'ps'`). The code's documented fallback
(supervisor log → exit report → UNKNOWN) works, but the process-probe path itself could not be
exercised live; finding **A17** was proven by simulating the `ps` output instead.

---

## 1. Verdict summary

| # | Assertion | Verdict | One-line |
|---|---|---|---|
| A1 | Append-only ledgers, ledger-stamped `ts`, single-write + fsync | PASS | verified by code read + test + live records |
| A2 | Crash-truncated tail tolerated, counted and marked as tail | PASS | half-line appended, readers survive |
| A3 | Corruption in the middle is **not** excused as a tail | PASS | reported `truncated_tail: False` |
| A4 | Checkpoint replaced atomically (no torn reads) | PASS | 7,184 writes vs 48,910 reads, 0 torn |
| A5 | SIGKILL mid-checkpoint-write leaves a parseable file | PASS | 12 kills, 12 parseable; stale `.tmp` swept |
| A6 | Wake fires once, then cooldown-suppresses; counts persisted | PASS | DEADLINE + NO_WORK_STREAK + STALL sequences |
| A7 | No wake/health flood at sub-second cadence | PASS | 40 cycles → 0 extra wake events, +1 health record |
| A8 | Suppression counters are fully on disk after a **clean** exit | **PARTIAL** | exit JSON said `suppressed_total=4`, checkpoint had `0` |
| A9 | Every switch audited, suppressed switches recorded with reason | PASS | applied / reason_cooldown / min_dwell / already_in_mode |
| A10 | Five facts stay separate, three states each, no "healthy" aggregate | PASS | 5 keys, `UNKNOWN`≠FALSE, no merged boolean |
| A11 | No proxy: alive process/goal must not imply game progress | PASS | GOAL TRUE + everything else FALSE/UNKNOWN coexists |
| A12 | `status` safe and read-only with no state at all | PASS | nonexistent dir: exit 0, JSON, directory not created |
| A13 | A bad stored deadline must not blind the control plane | **FAIL** | `status` and `health` both return only an error; watchdog stops evaluating |
| A13b | `status` is strictly read-only | **PARTIAL** | corrupt checkpoint triggers move-aside + event append |
| A14 | `status` survives concurrent writers | PASS | 0 failures in 100 calls against a live health loop |
| A15 | `usage_meter` ↔ `ledger` contract (single writer, tolerant reader) | PASS | no writer in dual layer; `init` never truncates; half-line flagged |
| A16 | Global `--checkpoint` flag works for every subcommand | **PARTIAL** | `health` ignores it and writes the default checkpoint |
| A17 | Process probe does not match its own command line | **PARTIAL** | simulated `ps`: `--supervisor-pattern X` matches the `status` argv (latent; `ps` EPERM here) |
| A18 | `status` shows why each fact is what it is | **PARTIAL** | emits states only, drops `evidence`; reads repo-global files under `--data-dir` |
| A19 | Checkpoint read-modify-write is serialized across processes | **PARTIAL** | simulated interleaving lost a mode switch (audit says applied, checkpoint says old mode) |
| A20 | Required test deliverables present and green | PASS | 129 passed (dual + outcome), full suite 781 passed / 2 skipped |

Counts: **12 PASS, 7 PARTIAL, 1 FAIL.** No PASS was granted on a docstring alone — every PASS
has a command below.

---

## 2. Test runs (real numbers, frozen state)

```bash
cd /Users/ceontou/Documents/code/seer-automation-night
PYTHONPATH=.pydeps python3 -m pytest -q tests/test_dual_*.py tests/test_outcome*.py
# -> 129 passed in 0.30s
PYTHONPATH=.pydeps python3 -m pytest -q
# -> 781 passed, 2 skipped in 39.99s
```

Coverage surface (test functions before parametrisation): `test_dual_ledger.py` 25,
`test_dual_modes.py` 26, `test_dual_switch.py` 14, `test_dual_watchdog.py` 18,
`test_outcome.py` 12. The switch/watchdog test files were **missing at 23:03** and appeared at
23:05–23:06 while this acceptance was running; the numbers above are from the frozen state.

---

## 3. Finding that must be fixed now — A13

**Assertion.** An unparseable deadline stored in the checkpoint must degrade, not disable.
`tools/overnight_control.py` itself states the principle: *"a broken status command during a
live night is worse than a useless one"*.

**How verified** (both operator-reachable paths):

```bash
D=/tmp/accept/d17; rm -rf $D
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D init --goal-status active
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D checkpoint --set 'deadline="23:59ish"'   # documented CLI, no validation on this key
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D status          # exit 3
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D health          # exit 3
wc -l < $D/runtime_health.jsonl                                                      # 0
```

Observed:

```
status -> {"ok":false,"error":"deadline '23:59ish' is not ISO-8601"}                          exit 3
health -> {"ok":false,"error":"ValueError: deadline '23:59ish' is not ISO-8601"}              exit 3
runtime_health.jsonl -> 0 lines     (the watchdog never evaluated anything)
```

**Why this is a FAIL, not a nitpick.** One unvalidated `checkpoint --set deadline=...` (or a
hand edit) removes, in a single step: the mode, policy, remaining time, all five facts, the
night state and the deadline — and stops the watchdog from evaluating at all. That is exactly
the "nobody was watching" failure the control plane exists to prevent, and it happens at 03:00
when nobody is there to repair it. Note the writers that validate (`init`, `deadline --set`)
are fine; only the generic `checkpoint --set` path and a corrupted file can create the state.

**Suggested fix (owner: infra-eng, ~15 lines).**
1. In `cmd_status` / `cmd_health`, catch `ValueError` from `resolve_deadline`/`parse_deadline`
   and continue with `deadline=None` plus an explicit `"deadline_error": <message>`,
   `"ok": true` (or `ok:false` *with the full payload*). A missing deadline is already a
   supported state (`remaining_s: null`).
2. In `Watchdog.step`, treat an unparseable configured deadline as a non-firing DEADLINE
   criterion with `evidence="deadline unparseable: …"`, so the loop keeps watching the other
   seven conditions.
3. Validate the `deadline` key in `cmd_checkpoint --set` (same `parse_deadline` call) so the
   bad state is refused at write time.
4. Add one regression test: malformed stored deadline → `status` exit 0 with facts, `health`
   exit 0 with a health record.

---

## 4. Remaining PARTIALs, severity-ranked

### A8 — clean exit loses pending suppression counters (severity: medium-low, evidence honesty)

**Assertion.** "Suppression … is counted, none is dropped" (watchdog docstring) and the exit
line's `suppressed_total` should be what is on disk.

**How verified.**

```bash
D=/tmp/accept/d18; rm -rf $D
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D init \
    --deadline-at "2026-11-06T22:00:00+08:00"          # deadline already passed -> 2 wake kinds/tick
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D health \
    --watch --cycles 3 --cadence 0.01
```

Observed: exit line `{"ok":true,"cycles":3,"wakes_total":2,"suppressed_total":4}` but the
persisted checkpoint had `watchdog.suppressed_total: 0`; the next invocation started counting
from 0. Longer run: 40 cycles reported `suppressed_total=82`, only `4` reached disk (this is
the same mechanical gap, not a different bug).

**Why it matters.** Wakes are always written (the important part), so the night does not lose
*incidents*; it loses the counter evidence for claims like "it flapped N times and we
suppressed it". The exit JSON overstates what is durable, which can leak into a report.

**Suggested fix (owner: infra-eng).** In `Watchdog.run`'s exit path (`finally`), if
`_pending_suppressed` is non-empty or `suppressed_total` changed since the last persist, write
one final `runtime_health` record and `_save_state()`. Alternatively state in the CLI output
`suppressed_total_persisted` separately from the in-memory count.

### A19 — checkpoint RMW is not serialized across processes (severity: medium-low, integrity)

**Assertion.** Two processes may both read-modify-write `checkpoint.json`; nothing serializes
them (`save_checkpoint` is atomic per write, but load→modify→save is not).

**How verified** (deterministic simulation of the load/save window; not observed in the wild):

```python
# /tmp/accept/d10: watchdog-style process loads the checkpoint, a switch is applied by
# another Ledger handle between that read and _save_state()'s write.
class InterleavingLedger(Ledger):
    def load_checkpoint(self, *, path=None, default=None):
        state = super().load_checkpoint(path=path, default=default)
        if self.hook and not self.fired:
            self.fired = True; self.hook()      # concurrent switch lands here
        return state
```

Observed: `mode_switches.jsonl` says `applied: True` (EFFICIENT→MAX_RESULTS) while
`checkpoint.json` ends with `"mode": "EFFICIENT"`. The window is only the few ms of
`_save_state`, so the odds per night are small — but the consequence is precisely the kind of
divergence a post-mortem would misread, and the dual-mode token attribution depends on the
checkpoint's mode being the truth.

**Suggested fix (owner: infra-eng).** Take an `fcntl.flock` on `checkpoint.json.lock` (or on
the checkpoint fd) around `load_checkpoint`→`save_checkpoint` in the mutating paths
(`Watchdog._save_state`, `ModeSwitchService.request`, `cmd_checkpoint --set`,
`cmd_progress`). A watchdog-only single writer would also do, but the CLI must then refuse to
mutate while the loop owns it.

### A16 — `health` ignores the global `--checkpoint` override (severity: low, contract)

```bash
D=/tmp/accept/d12; rm -rf $D
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D init
PYTHONPATH=.pydeps python3 tools/overnight_control.py --data-dir $D --checkpoint $D/alt.json health
ls $D/alt.json        # missing
python3 -c "import json;print(json.load(open('$D/checkpoint.json')).get('watchdog') is not None)"  # True
```

`Watchdog.__init__` has no `checkpoint_path` parameter; `watch_config` reads the override but
state saves go to `<data-dir>/checkpoint.json`. Every other subcommand honours it. Fix: pass
the path through (constructor arg) and use it in `_load_checkpoint`/`_save_state`; add a test.

### A17 — process probe can match its own command line (severity: low, latent here)

`probe_processes(pattern)` filters only lines containing `grep` / `ps -axo`. If `ps` works and
the operator passes `--supervisor-pattern X`, the `status` process's own argv contains
`X` (argparse puts the flag and value there), so the probe reports a supervisor that does not
exist. Reproduced by feeding a realistic `ps` output (sandbox blocks real `ps`):

```
matches for pattern 'sleep 60' when ONLY the status process contains it:
  MATCH: 4242 python3 tools/overnight_control.py --data-dir /tmp/accept/d7 status --supervisor-pattern 'sleep 60'
```

The default pattern (`tools/supervise.py`) does **not** appear in the status argv, so the
default path is safe. Fix: exclude `os.getpid()` and the calling process's argv from the scan
(or require the pattern to be matched against `argv[0..1]` only). Add a test with a fake
`ps` output that includes the caller.

### A18 — `status` drops per-fact evidence and leaks repo-global evidence sources (severity: low, operator visibility)

Two separate observations:

1. `status` emits `facts.compact()` (states only). The full `collect()` carries
   `evidence`/`source`/`age_s` per fact — which is exactly what distinguishes "the executor is
   dead" from "the probe could not run". In this sandbox `ps` is EPERM, yet `status` says
   `SUPERVISOR_RUNNING: FALSE` with no hint that the process probe was unavailable:
   ```
   collect(...).to_dict()["SUPERVISOR_RUNNING"]:
     state=FALSE source=supervisor.log
     evidence="process probe unavailable (PermissionError); supervisor.log records it finished: 11-06 01:21:28 supervisor done"
   ```
   `status` shows only `FALSE`. Fix: include `facts_evidence` (or at least `probe_error`) in
   the status payload; it costs no extra probe.
2. With `--data-dir /tmp/...`, `status` still reads the repo-global `data/supervisor.log` and
   `data/overnight/wild_cycle.jsonl` (facts defaults are absolute), so an isolated experiment's
   facts depend on the main repo's files — observed `EXECUTOR_ALIVE: FALSE` from a 17-hour-old
   `wild_cycle.jsonl`. `health` has override flags for all of these; `status` has none. Fix:
   add the same override flags to `status` (or resolve defaults through a documented
   `runtime_root`).

### A13b — `status` is not strictly read-only on a corrupt checkpoint (severity: low, doc vs behaviour)

With `checkpoint.json` truncated to 32 bytes, `status` exits 0 but **moves the file aside**
(`checkpoint.json.corrupt-1793977433`) and appends a `checkpoint.corrupt` event. The append is
good forensics and the file is not destroyed, but the docstring says status is read-only and
does not create data; strictly it rewrites state. Recommendation: either document the repair
path in the status docstring, or gate it behind an explicit `--repair` flag (status report
"checkpoint unreadable, run `init --force`" and change nothing). Owner: infra-eng, wording +
test.

---

## 5. PASS evidence (the things I tried to break and could not)

**A1/A2/A3 — ledger.** `Ledger.append` injects `ts`/`ts_epoch` at write time (a payload
cannot backdate), single `os.write` on `O_APPEND` + `fsync`, 64 KiB record guard (tested:
70 KB payload → `LedgerError`). Appending `{"ts":"…","kind":"pass.end","outcom` to
`events.jsonl`:
```
problems(events) -> [{'line': 5, 'truncated_tail': True}]
read(events)     -> 4 parseable records (session.init + 3 events), streak still counted
status           -> exit 0, ledger_problems: {"events":[{"line":5,"truncated_tail":true}]}
```
A `GARBAGE-NOT-JSON` line in the middle was reported `truncated_tail: False` while the tail
half-line stayed `True` — the distinction the contract promises.

**A4/A5 — checkpoint atomicity.** 5 s of racing writer (`save_checkpoint` in a loop) against a
reader doing `json.loads` on the same path: **7184 writes, 48910 reads, 0 torn reads.**
12 × `SIGKILL` at random 50–400 ms offsets: checkpoint parseable after every kill; stale
`checkpoint.json.tmp` sometimes left behind (by design), `init`'s `sweep_tmp` removed it.

**A6/A7 — watchdog debounce.** Deadline already in the past: first `health` applied
`DEADLINE` + `EXECUTOR_DEAD` and wrote two `wake` events; second `health` 0.3 s later applied
nothing, `suppressed_this_tick: ["DEADLINE","EXECUTOR_DEAD"]`,
`remaining_cooldown_s: 599.711`, `suppressed_total: 2`, exactly 2 `wake` events and 2
`runtime_health` records. `--watch --cycles 40 --cadence 0.02`: **0** additional wake events,
**+1** health record, 82 suppression counts — no flood. NO_WORK sequence: batches 1–2 →
`NO_WORK_STREAK` wake; batches 3–4 → suppressed; a `PROGRESS` batch resets
`consecutive_no_work` to 0. STALL sequence (aged milestone proof): STALL fires once, next tick
suppressed, new proof clears it (`GAME_PROGRESSING: TRUE`). Cooldown state survives process
restart via `checkpoint.watchdog.wake_last_epoch`.

**A9 — switch audit.** EFFICIENT→MAX_RESULTS/NEED_RESEARCH applied; immediately
MAX_RESULTS→EFFICIENT/NEED_RESEARCH suppressed `reason_cooldown`
(`reason_remaining_s: 899.696`); different reason within dwell suppressed `min_dwell`; no-op
suppressed `already_in_mode`; empty evidence rejected exit 3 with **no** record. All four
requests are in `mode_switches.jsonl` (applied flag + `suppressed_by` + `cooldown` block), and
the three suppressed ones also appear as `mode.switch.suppressed` events.

**A10/A11 — five facts.** `status` payload has exactly `GOAL_ALIVE, AGENT_DECIDING,
SUPERVISOR_RUNNING, EXECUTOR_ALIVE, GAME_PROGRESSING`, no aggregate healthy key. Exercised
each source independently: `GOAL` TRUE (checkpoint active) ↔ FALSE (paused); `AGENT_DECIDING`
TRUE (fresh deciding marker) ↔ FALSE (idle) ↔ **UNKNOWN** (deciding marker 10 min old — the
stale-marker path is the important one); `EXECUTOR_ALIVE` TRUE (fresh heartbeat) ↔ FALSE
(600 s heartbeat). `GAME_PROGRESSING` FALSE with a 1-hour-old proof (window 900 s) and TRUE
with a fresh one. Coexistence test: `GOAL_ALIVE: TRUE` while `GAME_PROGRESSING: FALSE` in the
same payload — a live goal is not progress, which is the anti-proxy property. Their own
`test_changing_one_evidence_source_changes_exactly_one_fact` covers the same invariant.

**A12/A14 — status safety.** Nonexistent data dir: exit 0, full JSON with nulls,
`facts` all UNKNOWN/FALSE, and the directory was **not created**. Corrupt checkpoint
(see A13b for the caveat): no traceback, JSON out, corrupt file preserved aside. 100 `status`
calls while a health loop appended: **0 failures**, every stdout parsed as JSON.

**A15 — meter/ledger contract.** No code in `seerauto/dual/` or `tools/overnight_control.py`
writes `model_usage.jsonl` (`grep` shows only docstring mentions + the `LEDGERS` tuple).
Running `init` over a directory that already holds the meter's file leaves its sha256
unchanged (the `ensure()` `touch` does not truncate) and `status` reports its byte size. A
half-written meter line is reported as
`ledger_problems.model_usage = [{"line": 2, "truncated_tail": true}]` and both the control plane
and `tools/usage_meter.py report` keep working. The meter's real record is parseable by
`Ledger.read("model_usage")` (`kind: usage_snapshot`, `ts` present). Minor note: meter records
carry `epoch_ms` but not `ts_epoch`; nothing reads model_usage records today, and any reader
using `parse_ts(record.get("ts"))` handles it.

**A20 — deliverables.** All four required test files exist (the two missing at 23:03 arrived
by 23:06) and the suite is green on the frozen fingerprints. No LLM/network/GUI imports in
`seerauto/dual/` (`grep` for `requests|urllib|socket|openai|anthropic|llm|Quartz|CGEvent|pyautogui`
finds only the `__init__` sentence saying it does not).

---

## 6. What I did NOT verify (honesty boundary)

- A real nine-hour soak: no multi-hour run was performed; all cooldowns/throttles were
  exercised with injected clocks or sub-second cadences.
- The `ps` success path on an unsandboxed host (blocked here with EPERM); A17 is therefore a
  simulated-output proof, and SUPERVISOR_RUNNING in this environment always uses the log/report
  fallback.
- Executor/game-layer integration: nothing in this repo writes `executor_heartbeat.json`,
  `world_snapshots.jsonl` or `failures.jsonl` yet (game lane is separate), so those writers'
  contracts are only unit-tested, not observed writing.
- The five facts with a *real* live executor: `EXECUTOR_ALIVE` was TRUE only from a synthetic
  heartbeat; the running game lane has not published one.

---

## 7. Frozen fingerprints (frozen 2026-11-06T23:10:04+08:00)

| file | sha256 (16) | mtime |
|---|---|---|
| `seerauto/dual/__init__.py` | `f835963a751334ff` | 22:56:44 |
| `seerauto/dual/facts.py` | `022735da715d3bf3` | 22:58:27 |
| `seerauto/dual/ledger.py` | `a49eed91b0d186b9` | 23:06:36 |
| `seerauto/dual/modes.py` | `da24b42497a07ba9` | 22:52:37 |
| `seerauto/dual/outcome.py` | `18488861922b2a6d` | 23:00:06 |
| `seerauto/dual/state.py` | `f3c9de90afc10ac5` | 23:05:31 |
| `seerauto/dual/switch.py` | `02e9e8ecdfffb653` | 23:04:39 |
| `seerauto/dual/watchdog.py` | `200797ad9f6a906e` | 23:01:45 |
| `tools/overnight_control.py` | `09143a184a8fa84f` | 23:08:13 |
| `tests/test_dual_ledger.py` | `9b96c5b757a982d3` | 23:06:57 |
| `tests/test_dual_modes.py` | `3c2ffb796ffc27ce` | 23:00:06 |
| `tests/test_dual_switch.py` | `12371b2c62edb758` | 23:05:09 |
| `tests/test_dual_watchdog.py` | `18b31f60bea2278a` | 23:06:36 |
| `tests/test_outcome.py` | `09c8c9eca8ef1c00` | 07:34:26 (pre-existing) |

Live reading of the real experiment dir at 23:10:56 (read-only `status`):
`initialized: True`, `deadline_source: checkpoint`, `remaining_s: 28143.5`,
`facts: GOAL_ALIVE TRUE / AGENT_DECIDING UNKNOWN / SUPERVISOR_RUNNING FALSE / EXECUTOR_ALIVE FALSE / GAME_PROGRESSING FALSE`,
`ledgers: events 1576, model_usage 1676, mode_switches 0, runtime_health 13925, failures 0, milestones 0`,
`ledger_problems: {}`.

If any file's sha256 differs from the table above, re-run §2 and the §3 command before quoting
these verdicts.
