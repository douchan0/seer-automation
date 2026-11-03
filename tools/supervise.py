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

Each batch is a normal `goal start` with a bounded budget, so every batch is auditable
and the supervisor never accumulates an unbounded action count.

    PYTHONPATH=.pydeps python3 tools/supervise.py --minutes 240 --batch-minutes 8
    PYTHONPATH=.pydeps python3 tools/supervise.py --until 06:30 --batch-battles 12
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "data", "supervisor.log")
REPORT = os.path.join(ROOT, "data", "supervisor.json")

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


def heal_if_needed() -> bool:
    """Ask the heal skill to top the pet up; harmless when HP is already fine."""

    code, out = run([sys.executable, "-c", HEAL_SNIPPET], timeout=300)
    ok = "healed" in out.lower() or "hp alive" in out.lower() or code == 0
    log(f"heal pass exited {code}: {' | '.join(out.strip().splitlines()[-2:])}")
    return ok


HEAL_SNIPPET = r"""
import sys; sys.path.insert(0, '.')
from seerauto.live import LiveSession
from seerauto.skills.base import SkillContext, Budget
from seerauto.skills.act import build_act_skills, ensure_world, _dismiss_dialogs
from seerauto.world.model import GameWorldState
s = LiveSession(dry_run=False, enable_input=True).open()
s.client.send('activate'); 
ctx = SkillContext(session=s, world=GameWorldState(), dry_run=False, allow_input=True,
                   budget=Budget(max_duration_s=180, max_actions=12))
ensure_world(ctx); _dismiss_dialogs(ctx, limit=3)
reg = {k.id: k for k in build_act_skills()}
bag = reg['game.read_pet_bag'](ctx)
print('hp alive check:', bag.proof)
if 'hp alive=0' in (bag.proof or ''):
    r = reg['game.heal_pet'](ctx)
    print('healed:', r.status.value, r.proof[:120])
else:
    print('hp fine, no heal needed')
s.close()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=240.0)
    ap.add_argument("--until", default=None, help="HH:MM local deadline, e.g. 06:30")
    ap.add_argument("--batch-minutes", type=float, default=8.0)
    ap.add_argument("--batch-battles", type=int, default=8)
    ap.add_argument("--planner", default="deterministic")
    ap.add_argument("--max-failures", type=int, default=4)
    args = ap.parse_args()

    if args.until:
        now = datetime.now()
        hh, mm = (int(x) for x in args.until.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        deadline = target
    else:
        deadline = datetime.now() + timedelta(minutes=args.minutes)

    log("=" * 68)
    log(f"supervisor start; deadline {deadline:%m-%d %H:%M}")

    batches = 0
    failures = 0
    history: list[dict] = []
    while datetime.now() < deadline:
        started = time.time()
        if not ensure_in_game():
            failures += 1
            log(f"cannot reach the world (failure {failures}/{args.max_failures})")
            if failures >= args.max_failures:
                log("giving up: the client will not come back on its own")
                break
            time.sleep(30)
            continue
        heal_if_needed()
        # PP exhaustion looks like "everything is 0/NN": the same 精灵恢复 that refills
        # HP refills uses, so treat a PP-stop as a reason to heal before the next batch.

        batches += 1
        log(f"batch {batches}: goal run, {args.batch_minutes:.0f} min / "
            f"{args.batch_battles} battles")
        code, out = run([sys.executable, "-m", "seerauto", "goal", "start", "puni",
                         "--planner", args.planner, "--allow-input",
                         "--max-minutes", str(args.batch_minutes),
                         "--max-battles", str(args.batch_battles)],
                        # a batch's own clock stops between skills, so give the
                        # subprocess room for the last skill to finish
                        timeout=args.batch_minutes * 60 + 480)
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-4:]
        log(f"batch {batches} exited {code}: {' | '.join(tail)}")
        if any("out of uses" in t or "PP exhausted" in t for t in tail):
            log("PP exhausted in that batch -> healing before the next one")
            heal_if_needed()
        entry = {"batch": batches, "code": code, "elapsed_s": round(time.time() - started, 1),
                 "tail": tail}
        code2, status = run([sys.executable, "-m", "seerauto", "goal", "status"], timeout=90)
        entry["status"] = [ln for ln in status.splitlines() if ln.strip()][:6]
        history.append(entry)
        if code != 0:
            failures += 1
        else:
            failures = 0
        with open(REPORT, "w", encoding="utf-8") as fh:
            json.dump({"deadline": deadline.isoformat(timespec="seconds"),
                       "batches": batches, "failures": failures,
                       "history": history}, fh, ensure_ascii=False, indent=1)
        if failures >= args.max_failures:
            log("stopping after repeated failures")
            break
        time.sleep(3)

    log(f"supervisor done: {batches} batches, {failures} consecutive failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
