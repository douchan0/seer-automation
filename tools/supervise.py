#!/usr/bin/env python3
"""Long-horizon supervisor: keep the goal advancing unattended, and recover by itself.

This is the piece that turns "a script that runs until it breaks" into "a runner that
survives the night".  Everything it does on a failure is something a human did during
the 2026-11-03 session, which is why the session kept stopping:

    no window          -> `open -a` (never `activate`: it steals focus and used to hit
                          a Helper process)
    login / server list -> tools/login.py logic (popup, announcement, login button,
                          server 1 with scrollbar drags)
    pet at 0 HP        -> game.heal_pet (hover-confirmed 精灵恢复 button)
    HP below floor     -> let the pass end, heal, continue
    victory stuck      -> OCR-driven 确认 retry (already inside game.run_battle)
    nothing usable     -> stop and write down why, rather than clicking blindly

Why this file was rewritten (2026-11-04): the previous version decided health from
`goal start`'s exit code alone.  `goal start` used to return 0 whenever the process
returned, so a batch that the planner refused outright ("nothing schedulable") looked
exactly like a successful one.  The 2026-11-04 run recorded **26 batches, 25 of them
with battles=0 actions=0, and failures=0** — pure spinning, reported as health, until
the deadline.

The fix is that a batch is now judged by *what it did*, not by whether it exited:

    SUCCESS  a node completed, or the goal ran out of executable work
    NO_WORK  nothing was schedulable — this is a stop condition, not a success
    FAILED   crash / client gone / error budget / stuck node
    TIMEOUT  a time or battle budget ran out
    PAUSED   a human asked it to stop
    BLOCKED  a hard blocker retrying cannot fix

and the loop stops on a NO_WORK streak, a failure streak, a deadline, or a long
stretch with no effective batch.

    PYTHONPATH=.pydeps python3 tools/supervise.py --minutes 240 --batch-minutes 8
    PYTHONPATH=.pydeps python3 tools/supervise.py --until 06:30 --batch-battles 12
    PYTHONPATH=.pydeps python3 tools/supervise.py --dry-run --minutes 5
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.runner.outcome import (Outcome, RunVerdict, completed_with_proof,  # noqa: E402
                                     exit_code_for)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "data", "supervisor.log")
REPORT = os.path.join(ROOT, "data", "supervisor.json")
DB_PATH = os.path.join(ROOT, "data", "goalrunner.sqlite3")

WORLD_MARKERS = ("公屏聊天", "卫兵", "当前回合", "综合待命中")
LOGIN_MARKERS = ("航行公告", "账密登录", "登录，开启", "开启万圣夜冒险")
SERVER_MARKERS = ("选择服务器", "快速进入")


def log(msg: str) -> None:
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=".pydeps")
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


# --------------------------------------------------------------------------- #
# accounting — pure, so the guards can be tested without a game client
# --------------------------------------------------------------------------- #
@dataclass
class Guards:
    """When to stop.  Every value is a hard stop, never a silent reset."""

    max_no_work_streak: int = 2
    max_failures: int = 4
    max_idle_minutes: float = 90.0
    max_batches: int = 0          # 0 = no cap
    min_battles_for_work: int = 1


@dataclass
class Ledger:
    """Running totals.  Written verbatim to supervisor.json so a human can audit it."""

    batches: int = 0
    effective_batches: int = 0
    no_work_batches: int = 0
    failed_batches: int = 0
    timeout_batches: int = 0
    paused_batches: int = 0
    blocked_batches: int = 0
    verified_progress: int = 0
    total_battles: int = 0
    total_actions: int = 0
    consecutive_no_work: int = 0
    consecutive_failures: int = 0
    last_progress_at: str | None = None
    last_verified_at: str | None = None
    last_selected_node: str | None = None
    last_stop_reason: str | None = None
    last_outcome: str | None = None
    completed_nodes: list[str] = field(default_factory=list)

    def record(self, verdict: Any, *, new_nodes: list[str], now: datetime,
               min_battles: int = 1) -> None:
        """Fold one batch into the totals.  `verdict` is a RunVerdict."""

        self.batches += 1
        self.total_battles += int(getattr(verdict, "battles", 0) or 0)
        self.total_actions += int(getattr(verdict, "actions", 0) or 0)
        self.last_stop_reason = getattr(verdict, "reason", None)
        self.last_outcome = getattr(getattr(verdict, "outcome", None), "value", None)

        outcome = getattr(verdict, "outcome", None)
        if outcome is Outcome.NO_WORK:
            self.no_work_batches += 1
            self.consecutive_no_work += 1
        else:
            self.consecutive_no_work = 0
        if outcome is Outcome.FAILED:
            self.failed_batches += 1
            self.consecutive_failures += 1
        elif outcome is Outcome.TIMEOUT:
            self.timeout_batches += 1
            self.consecutive_failures = 0
        elif outcome is Outcome.PAUSED:
            self.paused_batches += 1
            self.consecutive_failures = 0
        elif outcome is Outcome.BLOCKED:
            self.blocked_batches += 1
            self.consecutive_failures = 0
        else:
            self.consecutive_failures = 0

        if new_nodes:
            self.verified_progress += len(new_nodes)
            self.last_verified_at = now.isoformat(timespec="seconds")
            for node in new_nodes:
                if node not in self.completed_nodes:
                    self.completed_nodes.append(node)

        effective = (int(getattr(verdict, "battles", 0) or 0) >= min_battles
                     or bool(new_nodes))
        if effective:
            self.effective_batches += 1
            self.last_progress_at = now.isoformat(timespec="seconds")

    def idle_minutes_at(self, now: datetime) -> float | None:
        """Minutes since the last effective batch, or None if there never was one.

        Takes `now` explicitly so the guard is deterministic under test.
        """
        if not self.last_progress_at:
            return None
        try:
            then = datetime.fromisoformat(self.last_progress_at)
        except ValueError:
            return None
        return (now - then).total_seconds() / 60.0

    @property
    def idle_minutes(self) -> float | None:
        return self.idle_minutes_at(datetime.now())


def next_action(ledger: Ledger, guards: Guards, *, now: datetime,
                deadline: datetime) -> tuple[str, str | None]:
    """Return ("continue" | "stop", reason).  Pure apart from `now`/`deadline`.

    Order matters: a hard deadline beats everything, then the cheap streak guards,
    then the slow-burn idle guard.
    """

    if now >= deadline:
        return "stop", f"deadline {deadline:%H:%M} reached"
    if guards.max_batches and ledger.batches >= guards.max_batches:
        return "stop", f"batch cap {guards.max_batches} reached"
    if ledger.consecutive_no_work >= guards.max_no_work_streak:
        return "stop", (f"{ledger.consecutive_no_work} consecutive batches had no "
                        f"schedulable work (last: {ledger.last_stop_reason})")
    if ledger.consecutive_failures >= guards.max_failures:
        return "stop", (f"{ledger.consecutive_failures} consecutive failures "
                        f"(last: {ledger.last_stop_reason})")
    if guards.max_idle_minutes:
        idle = ledger.idle_minutes_at(now)
        if idle is not None and idle >= guards.max_idle_minutes:
            return "stop", (f"{idle:.0f} min without an effective batch "
                            f"(last progress {ledger.last_progress_at})")
        # Never let a run idle forever just because no batch was ever effective:
        # once one batch has happened with no progress at all, the idle clock is the
        # batch clock.
        if idle is None and ledger.batches >= 3:
            return "stop", (f"{ledger.batches} batches and not one was effective "
                            f"(last: {ledger.last_stop_reason})")
    return "continue", None


def parse_outcome(out: str) -> tuple[Outcome | None, str]:
    """Read the `outcome X (exit N): reason` line the goal CLI now prints.

    Falls back to (None, "") when the line is absent, so the caller can classify
    from the summary instead of guessing.
    """

    for line in reversed((out or "").splitlines()):
        text = line.strip()
        if not text.startswith("outcome "):
            continue
        rest = text[len("outcome "):]
        name, _, tail = rest.partition(" ")
        reason = ""
        if "): " in rest:
            reason = rest.split("): ", 1)[1]
        try:
            return Outcome(name), reason
        except ValueError:
            return None, reason
    return None, ""


def verdict_from_batch(code: int, out: str, *, new_nodes: list[str],
                       stats: dict[str, Any]) -> Any:
    """Build a RunVerdict from the CLI's own report, not from its exit code alone.

    The exit code is authoritative for FAILED/TIMEOUT (the CLI derives it from the
    same classifier); the printed outcome line carries the reason.
    """

    outcome, reason = parse_outcome(out)
    if outcome is None:
        if code == 124:
            outcome, reason = Outcome.TIMEOUT, reason or "batch subprocess timed out"
        elif code == 3:
            outcome, reason = Outcome.NO_WORK, reason or "nothing schedulable"
        elif code == 4:
            outcome, reason = Outcome.FAILED, reason or "another run holds the lock"
        elif code == 5:
            outcome, reason = Outcome.FAILED, reason or "goal start could not open a session"
        else:
            # Exit 0 with no outcome line is exactly the ambiguity this rewrite
            # exists to remove.  "I cannot establish what happened" is FAILED.
            outcome = Outcome.FAILED
            reason = reason or (f"no outcome reported (exit {code}) — cannot "
                                f"establish what this batch did")

    return RunVerdict(
        outcome=outcome,
        reason=reason,
        battles=max(0, int(stats.get("battles") or 0)),
        actions=max(0, int(stats.get("actions") or 0)),
        errors=max(0, int(stats.get("errors") or 0)),
        loops=max(0, int(stats.get("loops") or 0)),
        elapsed_s=float(stats.get("elapsed_s") or 0.0),
        completed_nodes=tuple(new_nodes),
    )


# --------------------------------------------------------------------------- #
# game-side recovery (unchanged behaviour, but now it reports what it did)
# --------------------------------------------------------------------------- #
def game_windows() -> list[dict]:
    out = subprocess.run([os.path.join(ROOT, "scripts", "seerctl"), "wl"],
                         capture_output=True, text=True)
    try:
        data = json.loads(out.stdout or "[]")
    except Exception:  # noqa: BLE001
        return []
    return [w for w in data if w.get("owner") == "SeerGame"]


def screen_text() -> list[str]:
    """One OCR pass through the daemon, without opening a session or taking focus."""

    from seerauto import capture as C

    client = C.DaemonClient(os.path.join(ROOT, "native", "bin", "seerdx"), timeout_s=25)
    try:
        client.send("shot", path="/tmp/seerauto_sup.png")
        items = client.send("ocr", path="/tmp/seerauto_sup.png", min=0.3).get("items", [])
        return [str(i.get("text") or "") for i in items]
    except Exception:  # noqa: BLE001
        return []
    finally:
        client.close()


def has(texts: list[str], markers) -> bool:
    joined = " ".join(texts)
    return any(m in joined for m in markers)


def ensure_in_game() -> bool:
    """Window up and character in the world.  Returns True when ready."""

    if not game_windows():
        log("no game window -> relaunching the client")
        subprocess.run(["open", "-a", "/Applications/SeerGame.app"], check=False)
        time.sleep(20)
    texts = screen_text()
    if has(texts, WORLD_MARKERS) and not has(texts, LOGIN_MARKERS + SERVER_MARKERS):
        return True
    log(f"not in the world ({texts[:4]}) -> running the login tool")
    code, out = run([sys.executable, "tools/login.py", "--timeout", "180"], timeout=400)
    tail = " | ".join(out.strip().splitlines()[-3:])
    log(f"login tool exited {code}: {tail}")
    texts = screen_text()
    return has(texts, WORLD_MARKERS)


def heal_if_needed(*, activate: bool = True) -> bool:
    """Ask the heal skill to top the pet up; harmless when HP is already fine.

    `activate` puts the game window in front so CGEvent clicks land on it.  That
    steals focus from whatever the human is doing — the one place the supervisor
    does so on purpose, because 精灵恢复 is a click, not a keystroke.
    """

    code, out = run([sys.executable, "-c", heal_snippet(activate)], timeout=300)
    ok = "healed" in out.lower() or "hp alive" in out.lower() or code == 0
    log(f"heal pass exited {code}: {' | '.join(out.strip().splitlines()[-2:])}")
    return ok


def heal_snippet(activate: bool) -> str:
    activate_line = ("s.client.send('activate')" if activate
                     else "print('heal: not activating (clicks may miss an unfocused window)')")
    return HEAL_SNIPPET_TEMPLATE.replace("__ACTIVATE__", activate_line)


HEAL_SNIPPET_TEMPLATE = r"""
import sys; sys.path.insert(0, '.')
from seerauto.live import LiveSession
from seerauto.skills.base import SkillContext, Budget
from seerauto.skills.act import build_act_skills, ensure_world, _dismiss_dialogs
from seerauto.world.model import GameWorldState
s = LiveSession(dry_run=False, enable_input=True).open()
__ACTIVATE__
ctx = SkillContext(session=s, world=GameWorldState(), dry_run=False, allow_input=True,
                   budget=Budget(max_duration_s=180, max_actions=12))
ensure_world(ctx); _dismiss_dialogs(ctx, limit=3)
reg = {k.id: k for k in build_act_skills()}
bag = reg['game.read_pet_bag'](ctx)
proof = bag.proof or ''
print('hp alive check:', proof)
# An empty read is NOT "health is fine": it usually means the client was still in a
# battle when the batch ended, and the run then spends whole batches at 1 battle each
# because the HP floor fires immediately.  Unknown -> heal.
unknown = 'hp alive=' not in proof
if 'hp alive=0' in proof or unknown:
    print('reason:', 'all pets fainted' if not unknown else 'hp unreadable -> heal anyway')
    r = reg['game.heal_pet'](ctx)
    print('healed:', r.status.value, r.proof[:140])
else:
    hp = proof.split('hp alive=')[1].split(';')[0]
    print('hp fine (' + hp + ' alive), no heal needed')
s.close()
"""


# --------------------------------------------------------------------------- #
# state snapshots — what the run actually changed, read from the store
# --------------------------------------------------------------------------- #
def snapshot() -> dict[str, Any]:
    """Latest run's status/stats/nodes, or {} when the store has nothing yet."""

    try:
        from seerauto.world.store import WorldStore

        store = WorldStore(DB_PATH)
    except Exception as exc:  # noqa: BLE001 - a missing store is not fatal here
        log(f"snapshot unavailable: {type(exc).__name__}: {exc}")
        return {}
    try:
        latest = store.latest_run()
        if not latest:
            return {}
        run_id = latest["run_id"]
        nodes = store.node_states(run_id)
        return {
            "run_id": run_id,
            "status": latest.get("status"),
            "stop_reason": latest.get("stop_reason"),
            "current_node": latest.get("current_node"),
            "stats": latest.get("stats") or {},
            # Same rule as outcome.classify_run: a bare "completed" status is a
            # claim, not evidence.  These two used to disagree, which let the
            # supervisor count a proof-less completion as verified progress.
            "completed": list(completed_with_proof(
                {k: v.get("status") for k, v in nodes.items()},
                {k: v.get("proof") for k, v in nodes.items() if v.get("proof")})),
            "proofs": {k: v.get("proof") for k, v in nodes.items() if v.get("proof")},
        }
    except Exception as exc:  # noqa: BLE001
        log(f"snapshot failed: {type(exc).__name__}: {exc}")
        return {}
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The honest before/after of one batch."""

    b_nodes = set(before.get("completed") or [])
    a_nodes = set(after.get("completed") or [])
    b_stats = before.get("stats") or {}
    a_stats = after.get("stats") or {}

    def delta(key: str) -> int:
        try:
            return int(a_stats.get(key) or 0) - int(b_stats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "new_completed_nodes": sorted(a_nodes - b_nodes),
        "dropped_completed_nodes": sorted(b_nodes - a_nodes),
        "battles_delta": delta("battles"),
        "actions_delta": delta("actions"),
        "errors_delta": delta("errors"),
        "run_changed": before.get("run_id") != after.get("run_id"),
        "before_status": before.get("status"),
        "after_status": after.get("status"),
        "after_stop_reason": after.get("stop_reason"),
    }


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=240.0)
    ap.add_argument("--until", default=None, help="HH:MM local deadline, e.g. 06:30")
    ap.add_argument("--batch-minutes", type=float, default=8.0)
    ap.add_argument("--batch-battles", type=int, default=8)
    ap.add_argument("--planner", default="deterministic")
    ap.add_argument("--max-failures", type=int, default=4)
    ap.add_argument("--max-no-work", type=int, default=2,
                    help="stop after this many consecutive batches with nothing schedulable")
    ap.add_argument("--max-idle-minutes", type=float, default=90.0,
                    help="stop when no batch has been effective for this long")
    ap.add_argument("--max-batches", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only, never send input (safe default for testing)")
    ap.add_argument("--no-heal-activate", action="store_true",
                    help="do not raise the game window before healing (avoids focus theft)")
    return ap


def deadline_from(args: argparse.Namespace) -> datetime:
    if args.until:
        now = datetime.now()
        hh, mm = (int(x) for x in args.until.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    return datetime.now() + timedelta(minutes=args.minutes)


def write_report(*, deadline: datetime, guards: Guards, ledger: Ledger,
                 history: list[dict[str, Any]], stop_reason: str | None) -> None:
    payload = {
        "deadline": deadline.isoformat(timespec="seconds"),
        "stopped_at": datetime.now().isoformat(timespec="seconds"),
        "stop_reason": stop_reason,
        "guards": asdict(guards),
        "ledger": asdict(ledger),
        # kept for backwards compatibility with readers of the old schema
        "batches": ledger.batches,
        "failures": ledger.consecutive_failures,
        "history": history,
    }
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    guards = Guards(max_no_work_streak=args.max_no_work,
                    max_failures=args.max_failures,
                    max_idle_minutes=args.max_idle_minutes,
                    max_batches=args.max_batches)
    deadline = deadline_from(args)

    log("=" * 68)
    log(f"supervisor start; deadline {deadline:%m-%d %H:%M} "
        f"dry_run={args.dry_run} batch={args.batch_minutes:.0f}min/"
        f"{args.batch_battles}battles guards=no_work<={guards.max_no_work_streak} "
        f"fail<={guards.max_failures} idle<={guards.max_idle_minutes:.0f}min")

    ledger = Ledger()
    history: list[dict[str, Any]] = []
    stop_reason: str | None = None

    while True:
        action, why = next_action(ledger, guards, now=datetime.now(), deadline=deadline)
        if action == "stop":
            stop_reason = why
            log(f"STOP: {why}")
            break

        if not args.dry_run and not ensure_in_game():
            # Go through record() so the batch counter has one owner and the
            # no-work streak is reset on a different failure mode.
            unreachable = RunVerdict(outcome=Outcome.FAILED, reason="client unreachable")
            ledger.record(unreachable, new_nodes=[], now=datetime.now(),
                          min_battles=guards.min_battles_for_work)
            log(f"cannot reach the world (failure {ledger.consecutive_failures}"
                f"/{guards.max_failures})")
            history.append({"batch": ledger.batches, "outcome": "FAILED",
                            "reason": "client unreachable"})
            write_report(deadline=deadline, guards=guards, ledger=ledger,
                         history=history, stop_reason=stop_reason)
            # adaptive backoff: give the client a chance to finish launching
            time.sleep(min(30.0 * ledger.consecutive_failures, 120.0))
            continue

        if not args.dry_run:
            heal_if_needed(activate=not args.no_heal_activate)

        before = snapshot()
        # `record()` owns the batch counter; numbering here must not increment it
        # too, or every batch is counted twice (it was, on the first run of this
        # rewrite: two batches reported as four).
        batch_no = ledger.batches + 1
        started = time.time()
        cmd = [sys.executable, "-m", "seerauto", "goal", "start", "puni",
               "--planner", args.planner,
               "--max-minutes", str(args.batch_minutes),
               "--max-battles", str(args.batch_battles)]
        if args.dry_run:
            cmd.append("--dry-run")
        else:
            cmd.append("--allow-input")
        log(f"batch {batch_no}: goal run, {args.batch_minutes:.0f} min / "
            f"{args.batch_battles} battles")

        code, out = run(cmd, timeout=args.batch_minutes * 60 + 480)
        after = snapshot()
        diff = diff_snapshots(before, after)
        new_nodes = diff["new_completed_nodes"]

        # `after` is the store's latest run, which is only *this* batch's run when
        # the run id changed (or when this is the first batch).  Otherwise the
        # batch never started a run (exit 4/5) and those stats belong to someone
        # else — using them made a failed batch look productive.
        owns_run = (not before.get("run_id")) or diff["run_changed"]
        stats = dict((after.get("stats") or {})) if owns_run else {}
        if not stats:
            stats = {"battles": max(0, diff["battles_delta"]),
                     "actions": max(0, diff["actions_delta"]),
                     "errors": max(0, diff["errors_delta"])}
        verdict = verdict_from_batch(code, out, new_nodes=new_nodes, stats=stats)

        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
        log(f"batch {batch_no} -> {verdict.outcome.value} (exit {code}, "
            f"{time.time() - started:.0f}s): {verdict.reason}")
        if tail:
            log(f"  tail: {' | '.join(tail)}")

        ledger.record(verdict, new_nodes=new_nodes, now=datetime.now(),
                      min_battles=guards.min_battles_for_work)
        ledger.last_selected_node = (after.get("current_node")
                                     or before.get("current_node")
                                     or ledger.last_selected_node)

        history.append({
            "batch": batch_no,
            "outcome": verdict.outcome.value,
            "reason": verdict.reason,
            "exit_code": code,
            "elapsed_s": round(time.time() - started, 1),
            "stats": stats,
            "new_completed_nodes": new_nodes,
            "diff": diff,
            "tail": tail,
        })
        write_report(deadline=deadline, guards=guards, ledger=ledger,
                     history=history, stop_reason=stop_reason)

        # PP exhaustion looks like "everything is 0/NN": the same 精灵恢复 that
        # refills HP refills uses, so treat a PP-stop as a reason to heal now.
        if not args.dry_run and any("out of uses" in t or "PP exhausted" in t for t in tail):
            log("PP exhausted in that batch -> healing before the next one")
            heal_if_needed(activate=not args.no_heal_activate)

        # A hard blocker or a human stop is not something to retry.
        if verdict.outcome is Outcome.BLOCKED:
            stop_reason = f"blocked: {verdict.reason}"
            log(f"STOP: {stop_reason}")
            break
        if verdict.outcome is Outcome.PAUSED:
            stop_reason = f"paused: {verdict.reason}"
            log(f"STOP: {stop_reason}")
            break

    log(f"supervisor done: {ledger.batches} batches, "
        f"{ledger.effective_batches} effective, {ledger.no_work_batches} no-work, "
        f"{ledger.failed_batches} failed, {ledger.verified_progress} verified node(s)")
    write_report(deadline=deadline, guards=guards, ledger=ledger, history=history,
                 stop_reason=stop_reason)
    # Exit code of the supervisor itself: 0 only when something actually moved.
    if stop_reason and stop_reason.startswith("paused"):
        return exit_code_for(Outcome.PAUSED)
    if stop_reason and stop_reason.startswith("blocked"):
        return exit_code_for(Outcome.BLOCKED)
    if stop_reason and ("no schedulable work" in stop_reason
                        or "not one was effective" in stop_reason):
        return exit_code_for(Outcome.NO_WORK)
    return 0 if ledger.effective_batches else exit_code_for(Outcome.NO_WORK)


if __name__ == "__main__":
    raise SystemExit(main())
