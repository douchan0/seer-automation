# BUGFIX — `tools/fix_startup.py`: `NameError: name 'time' is not defined` (2026-11-06)

Short version: the one-command unstick tool for the 「未检测本地有美术包，是否需要导入？」
startup dialog crashed on its **act** path, so tonight's live bring-up could not use it
and had to hand-roll capture → OCR → clickpid. Two-line fix (`import time`), proven by
`--check` and by a regression test that drives the act path.

## Symptom (live, 2026-11-06 bring-up)

`PYTHONPATH=.pydeps python3 tools/fix_startup.py --check` worked (it returns before the
crash), but the act path — `fix_startup.py` **without** `--check`, the one that actually
dismisses the dialog — died with:

```
NameError: name 'time' is not defined
```

The failure is at `tools/fix_startup.py:68` (`time.sleep(0.8)`, after
`session.client.send("activate")`). The module imported `argparse`, `os`, `sys` — not
`time`. The operator worked around it by driving the pieces by hand; the tool stayed
broken for every subsequent client restart.

## Root cause

`time` was used but never imported. Nothing caught it because:

* `--check` returns at line 61, before line 68 — the only automated path people ran;
* the skill (`DismissStartupPopup`) itself is covered by other tests, so the wrapper's
  crash was invisible to them;
* the failure needs a live auxiliary dialog *and* the act path, i.e. it only happens
  exactly when the tool is needed.

## Fix

`tools/fix_startup.py`:

```diff
 import argparse
 import os
 import sys
+import time
```

No behaviour change, no other edit.

## Verification — what is UNIT-verified vs LIVE-verified

| Claim | Status | Evidence |
|---|---|---|
| `time` is imported and bound in the module | UNIT-verified | `tests/test_night_runner.py::test_fix_startup_binds_time` |
| The act path reaches `time.sleep(0.8)` without `NameError` | UNIT-verified | `test_fix_startup_act_path_reaches_the_sleep` — fakes `LiveSession`/`DismissStartupPopup`/`list_game_windows`, asserts `slept == [0.8]` and exit 0 |
| `--check` with no window exits 4, no traceback | UNIT-verified + LIVE-observed | unit: `test_fix_startup_check_with_no_window_exits_4`; live run of `--check` on 2026-11-06 23:2x exited 0 with a real window and no traceback |
| `--check` with an aux dialog reports BLOCKED and clicks nothing | UNIT-verified | `test_fix_startup_check_reports_aux_dialog_without_clicking` (asserts no `LiveSession` is constructed) |
| **A real live dismissal of the art-pack dialog through this wrapper** | **NOT verified tonight** | The client was already past startup when the fix landed; we did not force a fresh art-pack popup. Next time the dialog appears, run the act path and record the OCR line + window list before/after. |

Reproduce the crash evidence (pre-fix) by reverting the import in a scratch copy; the
act-path test fails with `NameError` on line 68. After the fix:

```
$ PYTHONPATH=.pydeps python3 tools/fix_startup.py --check
windows: id=3284 1200x800
OK: no auxiliary startup dialog is up
$ echo $?
0
```

and

```
$ PYTHONPATH=.pydeps python3 -m pytest -q tests/test_night_runner.py
4 passed
```

## How to use it

```
PYTHONPATH=.pydeps python3 tools/fix_startup.py --check   # read-only: is the dialog up?
PYTHONPATH=.pydeps python3 tools/fix_startup.py           # dismiss it (foreground click)
```

Exit codes: `0` no dialog / dismissed · `3` dialog still up (or BLOCKED in `--check`)
· `4` no SeerGame window. `--check` never clicks and never opens a session.
