"""`game.give_exp` — the 经验分配器 pour, tested without a client.

The account has been growing an 积累经验 pool it has no automated way to spend, and
`docs/MACHINE_MAP.md` still lists the panel as NOT FOUND.  These tests pin the two
things that make the skill safe to point at a live panel:

  * it refuses to act when the panel is not the allocator (no blind clicking);
  * it never presses 确认 unless the input box demonstrably holds the amount it typed;
  * it only reports PASS when the panel's own numbers changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seerauto.skills.act import GiveExp
from seerauto.skills.base import Budget, SkillContext, Status
from seerauto.world.model import GameWorldState

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "exp_allocator_panel.json"


def _items(panel: dict) -> list[dict]:
    """The recorded panel JSON, with the numbers the caller wants to see.

    The recording itself has "500" already sitting in the amount box, so the box is
    dropped unless ``pending`` asks for it -- otherwise "nothing was typed yet" and
    "500 was typed" would be the same frame and the test would prove nothing.
    """

    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out = []
    for item in raw:
        text = item["text"]
        if text.strip() == "500g":
            if "pending" not in panel:
                continue
            text = f"{panel['pending']}g"
        if "level" in panel and text.startswith("等级："):
            text = f"等级：{panel['level']}"
        if "pool" in panel and text.startswith("积累经验："):
            text = f"积累经验：{panel['pool']}"
        norm = tuple(item["norm"])
        # Live OCR items carry both shapes; the fixture only stored the normalised
        # one, so the canonical px pair is derived the same way `parsers.box_of` does.
        fw, fh = 2400.0, 1600.0
        cx = (norm[0] + norm[2] / 2.0) * fw
        cy = (1.0 - (norm[1] + norm[3] / 2.0)) * fh
        out.append({"text": text, "conf": item["conf"], "norm": norm, "px": (cx, cy)})
    return out


class PanelSession:
    """Hands out a scripted sequence of OCR results, one per call."""

    def __init__(self, frames: list[list[dict]], *, dry_run: bool = False):
        self.frames = frames
        self.calls = 0
        self.clicks: list[tuple] = []
        self.sent: list[tuple] = []
        self.dry_run = dry_run
        self.client = self

    # -- what the skill uses ------------------------------------------------ #
    def ocr(self, *, min_conf: float = 0.3, path: str | None = None, **kw):
        idx = min(self.calls, len(self.frames) - 1)
        self.calls += 1
        return self.frames[idx]

    def click_px(self, x, y, reason="", **kw):
        self.clicks.append((x, y, reason))

    def send(self, op, **kw):
        self.sent.append((op, kw))
        return {"ok": True}

    def scene(self, *a, **kw):
        raise AssertionError("give_exp must not need the scene probe")

    def latest(self):
        raise AssertionError("give_exp must not export frames")


def _ctx(session, *, allow_input=True) -> SkillContext:
    return SkillContext(session=session, world=GameWorldState(),
                        budget=Budget(max_duration_s=30.0, max_actions=8),
                        dry_run=not allow_input, allow_input=allow_input)


def test_refuses_when_the_panel_is_not_on_screen():
    session = PanelSession([[{"text": "烈火猴", "conf": 0.9, "norm": (0.2, 0.5, 0.1, 0.03)}]])
    res = GiveExp()(_ctx(session))
    assert res.status is Status.SKIPPED
    assert "not on screen" in res.reason
    assert session.clicks == []


def test_refuses_when_the_pool_is_empty():
    session = PanelSession([_items({"pool": 0})])
    res = GiveExp()(_ctx(session))
    assert res.status is Status.SKIPPED
    assert "nothing to pour" in res.reason
    assert session.clicks == []


def test_happy_path_proves_the_level_rise_from_the_panel_itself():
    # OCR call order inside the skill: (1) read the panel, (2) verify the typed
    # amount, (3) find the 确认 button, (4) re-read the panel to prove the effect.
    frames = [
        _items({}),
        _items({"pending": 500}),
        _items({"pending": 500}),
        _items({"level": 18, "pool": 18699927}),
    ]
    session = PanelSession(frames)
    res = GiveExp()(_ctx(session), amount=500)
    assert res.status is Status.PASS, res.reason
    assert res.after["level_before"] == 17.0
    assert res.after["level_after"] == 18.0
    assert res.after["granted"] == 45000.0
    ops = [op for op, _ in session.sent]
    assert "type" in ops, "the amount must be typed, not just assumed"
    assert any(r.startswith("聚焦") for _, _, r in session.clicks)
    assert any("确认" in r for _, _, r in session.clicks)


def test_never_presses_confirm_when_the_typed_amount_did_not_land():
    """A rejected keystroke must not turn into a pour of whatever was in the box."""

    frames = [
        _items({}),
        _items({"pending": 500}),   # the box still shows the first candidate, not 777
    ]
    session = PanelSession(frames)
    res = GiveExp()(_ctx(session), amount=777)
    assert res.status is Status.FAIL
    assert "not pressing 确认" in res.reason
    assert not any("确认" in r for _, _, r in session.clicks)


def test_a_pour_that_changed_nothing_is_a_failure_not_a_win():
    frames = [_items({}), _items({"pending": 500}), _items({"pending": 500}), _items({})]
    session = PanelSession(frames)
    res = GiveExp()(_ctx(session), amount=500)
    assert res.status is Status.FAIL
    assert "no change" in res.reason


def test_the_amount_is_capped_by_the_pool():
    frames = [_items({"pool": 120}), _items({"pool": 120, "pending": 120}),
              _items({"pool": 120, "pending": 120}), _items({"level": 18, "pool": 0})]
    session = PanelSession(frames)
    res = GiveExp()(_ctx(session), amount=10_000_000)
    typed = [kw for op, kw in session.sent if op == "type"]
    assert typed and typed[0]["text"] == "120"
    assert res.status is Status.PASS


# -- the double-pour path the review found ---------------------------------- #
def test_a_lost_title_is_not_treated_as_a_closed_panel():
    """If the title OCR fails but the body is still there, never click 确认 again.

    The recovery path (`_dismiss_dialogs`) clicks the first 确认 on the whole screen,
    which can be this panel's own button.  So "title missing" must not read as "panel
    closed" while 「该精灵增加」/「积累经验」 are still visible.
    """

    from seerauto.skills.act import _allocator_body_still_there

    body_only = [{"text": "该精灵增加", "conf": 0.9, "px": (100, 100)},
                 {"text": "积累经验：1000", "conf": 0.9, "px": (200, 120)}]
    assert _allocator_body_still_there(body_only) is True
    assert _allocator_body_still_there([{"text": "公屏聊天", "conf": 0.9}]) is False


def test_a_verified_pour_writes_the_flag_the_goal_predicate_reads():
    """`feature_unlocked: exp_giver.poured` reads world.unlocked, nothing else."""

    from seerauto.world.model import GameWorldState

    frames = [_items({}), _items({"pending": 500}), _items({"pending": 500}),
              _items({"level": 18, "pool": 18699927})]
    world = GameWorldState()
    ctx = SkillContext(session=PanelSession(frames), world=world,
                       budget=Budget(max_duration_s=30.0, max_actions=8),
                       dry_run=False, allow_input=True)
    res = GiveExp()(ctx, amount=500)
    assert res.status is Status.PASS
    assert "exp_giver.poured" in world.unlocked, (
        "a real pour must satisfy the node's completion predicate")
    assert world.unlocked["exp_giver.poured"].value is True
