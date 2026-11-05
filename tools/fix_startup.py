#!/usr/bin/env python3
"""Unstick a client parked on 「版本检测中…」 by dismissing the art-pack dialog.

This is the exact fault that cost the 2026-11-05 evening: the client opened a second,
small window ("未检测本地有美术包，是否需要导入？").  While it was up the playfield
never left the version check, which reads exactly like a network hang, and
``seerdx win`` only reports the largest window — so every tool that trusted it waited
forever.

The skill that knows how to dismiss it already exists (`game.dismiss_startup_popup`):
it OCRs the dialog's *own* pixels and clicks 暂不导入 by text.  What was missing was a
one-command entry point that (a) decides whether the dialog is up, (b) runs the skill,
and (c) proves the window is gone afterwards instead of trusting a PASS.

    PYTHONPATH=.pydeps python3 tools/fix_startup.py            # act
    PYTHONPATH=.pydeps python3 tools/fix_startup.py --check    # only report

Exit codes: 0 = no dialog / dismissed, 3 = dialog still there, 4 = no game window.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto.live import LiveSession, list_game_windows  # noqa: E402
from seerauto.skills.act import DismissStartupPopup  # noqa: E402
from seerauto.skills.base import Budget, SkillContext  # noqa: E402
from seerauto.world.model import GameWorldState  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def describe(windows: list[dict]) -> str:
    return "; ".join(
        f"id={w.get('id')} {int(w.get('w') or 0)}x{int(w.get('h') or 0)}"
        f"{' (aux)' if w.get('aux') else ''}" for w in windows) or "none"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only, never click")
    args = ap.parse_args()

    windows = list_game_windows()
    if not windows:
        log("no SeerGame window at all; run `open -a /Applications/SeerGame.app` first")
        return 4
    aux = [w for w in windows if w.get("aux")]
    log(f"windows: {describe(windows)}")
    if not aux:
        log("OK: no auxiliary startup dialog is up")
        return 0
    if args.check:
        log(f"BLOCKED: {len(aux)} auxiliary dialog window(s) up — the playfield will "
            "stay on 版本检测中 until it is dismissed")
        return 3

    session = LiveSession(dry_run=False, enable_input=True).open()
    try:
        # The dialog is a separate native window: without the app raised, the click is
        # delivered but the dialog does not act on it (seen live 2026-11-05 23:21).
        session.client.send("activate")
        time.sleep(0.8)
        result = DismissStartupPopup()(SkillContext(
            session=session, world=GameWorldState(), dry_run=False, allow_input=True,
            budget=Budget(max_duration_s=45.0, max_actions=3)))
        log(f"skill {result.status.value}: {(result.proof or result.reason)[:200]}")
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    # Verify from the window list, not from the skill's own verdict.
    after = [w for w in list_game_windows() if w.get("aux")]
    log(f"after: {describe(after) or 'no windows'}")
    if after:
        log("FAILED: the auxiliary dialog is still up")
        return 3
    log("OK: auxiliary dialog is gone; the playfield can load now")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
