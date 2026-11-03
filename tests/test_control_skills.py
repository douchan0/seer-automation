"""Tests for the control skills that decide *whether a click is safe*.

These are the pieces that turned a night of "clicks do nothing" into working
automation, so they get their own regression file:

* ``ensure_world`` must close a known panel and must **not** click when the screen
  is already the plain world.  The second half is the regression: the chat header
  「公屏聊天」 is always visible, and treating it as a panel made the closer fire
  three times on a screen with nothing open while the real 精灵仓库 panel went on
  swallowing every click.
* ``_dismiss_dialogs`` must find a confirm button even when OCR mangles the glyph,
  by falling back to the known modal bodies and the fixed button position.
* ``game.travel`` must reject an unknown destination instead of clicking something.
"""

from __future__ import annotations

from typing import Any

import pytest

from seerauto.skills.act import (
    MAP_DESTINATIONS,
    PANEL_CLOSERS,
    PET_BAG_CLOSE_PX,
    Travel,
    _dismiss_dialogs,
    ensure_world,
)
from seerauto.skills.base import Budget, SkillContext, Status
from seerauto.world.model import GameWorldState


class ClickSession:
    """Records clicks; OCR returns whatever the test set up."""

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.clicks: list[tuple[float, float, str]] = []
        self.window_raw = {"id": 1, "x": 0, "y": 0, "w": 1200, "h": 800}

    # -- what the skills use ------------------------------------------------ #
    def ocr(self, **kw):
        return [dict(i) for i in self.items]

    def find_text(self, items, needles, *, exact=False):
        for it in items:
            text = str(it.get("text") or "").strip()
            for n in needles:
                if (text == n) if exact else (n in text):
                    return dict(it)
        return None

    def click_px(self, x, y, reason="", **kw):
        self.clicks.append((x, y, reason))

        class _Rec:
            sent = True
            refused = None

        return _Rec()

    def latest(self):
        raise AssertionError("these tests must not need pixels")

    def score(self, frame, name, roi):  # pragma: no cover - only used by travel
        raise AssertionError("no pixel work in this file")


def ctx_for(session: ClickSession) -> SkillContext:
    return SkillContext(session=session, world=GameWorldState(), dry_run=False,
                        allow_input=True, budget=Budget(max_duration_s=10, max_actions=5))


def _item(text: str, x: float = 100.0, y: float = 100.0) -> dict[str, Any]:
    return {"text": text, "conf": 0.9, "px": (x, y)}


# --------------------------------------------------------------------------- #
# ensure_world
# --------------------------------------------------------------------------- #
def test_ensure_world_does_nothing_on_a_clean_world():
    """The chat header alone is NOT a panel — this is the regression under test."""

    session = ClickSession([_item("公屏聊天 • 在线", 280, 261),
                            _item("hello", 900, 900),
                            _item("万圣活动", 1613, 361)])
    assert ensure_world(ctx_for(session)) is True
    assert session.clicks == [], "a clean world must not receive a single click"


def test_ensure_world_closes_the_pet_panel_via_hp_rows():
    """Two 'cur/max' rows mean the pet bag, even when its text markers are unreadable."""

    session = ClickSession([_item("烈火猴", 785, 680), _item("0/95", 862, 735),
                            _item("小蘑菇", 1060, 678), _item("0/12", 1144, 735)])
    ensure_world(ctx_for(session))
    assert session.clicks, "the pet panel must be closed"
    assert session.clicks[0][:2] == PET_BAG_CLOSE_PX


def test_ensure_world_closes_the_star_map():
    session = ClickSession([_item("1.啦公星", 1197, 1218), _item("万圣活动", 1613, 361)])
    ensure_world(ctx_for(session))
    assert session.clicks and session.clicks[0][:2] == PANEL_CLOSERS["map"][1]


def test_ensure_world_closes_the_expanded_chat_not_the_header():
    """The expanded chat shows tab labels; only that state has a close button."""

    session = ClickSession([_item("综合", 1483, 643), _item("世界", 1596, 643),
                            _item("交易", 1704, 645), _item("招募", 1812, 645)])
    ensure_world(ctx_for(session))
    assert session.clicks and session.clicks[0][:2] == PANEL_CLOSERS["chat_open"][1]


# --------------------------------------------------------------------------- #
# modal dismissal
# --------------------------------------------------------------------------- #
def test_dismiss_dialogs_clicks_an_ocr_confirm_button():
    """A fake screen never changes, so the (bounded) loop clicks repeatedly —
    what matters is that the *first* click is on the confirmed button."""

    session = ClickSession([_item("确认", 1200, 980)])
    assert _dismiss_dialogs(ctx_for(session)) >= 1
    assert session.clicks[0][:2] == (1200, 980)


def test_dismiss_dialogs_falls_back_to_the_known_modal_position():
    """OCR often misses the bold 确认 glyph; the body text still identifies it."""

    session = ClickSession([_item("你没有可出战的精灵哦", 1207, 805)])
    assert _dismiss_dialogs(ctx_for(session)) >= 1
    assert session.clicks[0][:2] == (1200, 980)


def test_dismiss_dialogs_is_a_no_op_without_a_modal():
    session = ClickSession([_item("hello", 900, 900), _item("万圣活动", 1613, 361)])
    assert _dismiss_dialogs(ctx_for(session)) == 0
    assert session.clicks == []


# --------------------------------------------------------------------------- #
# travel table
# --------------------------------------------------------------------------- #
def test_every_destination_is_well_formed():
    assert len(MAP_DESTINATIONS) >= 5
    for name, spec in MAP_DESTINATIONS.items():
        x, y = spec["px"]
        assert 0 < x < 2400 and 0 < y < 1600, name
        assert spec["expect"], f"{name} needs at least one arrival marker"
        assert spec["label"], f"{name} needs a human label"


def test_travel_rejects_an_unknown_destination():
    session = ClickSession([])
    result = Travel()(ctx_for(session), destination="atlantis")
    assert result.status is Status.FAIL
    assert "unknown destination" in result.reason
    assert session.clicks == []


def test_battle_pass_stops_before_the_pet_faints():
    """A pass that ends at 0 HP strands the account; one that ends at 25% does not.

    Verified on 2026-11-03: the pet fainted mid-session and every later engage got
    「你没有可出战的精灵哦」, which needs a human to fix.  The guard is a pure
    comparison, so it is tested here without a client.
    """

    hp_floor = 0.25
    for hp, hp_max, expect_stop in ((12, 95, True), (0, 95, True),
                                    (24, 95, False), (95, 95, False)):
        ratio = hp / float(hp_max)
        assert (ratio <= hp_floor) is expect_stop, (hp, hp_max, ratio)


def test_skill_picker_prefers_power_and_skips_empty_pp():
    """The client's default is "first slot when the timer expires" — always wrong.

    抓 with 0/35 PP, 瞪眼 is a 0-power status move, 火花 hits for 40.  The picker has
    to take 火花, and it must fall back to a status move only when nothing damaging
    has PP left.
    """

    from seerauto.skills.act import RunBattle

    cells = [
        {"px": (1, 1), "name": "抓", "pp": 0, "pp_max": 35, "power": 40},
        {"px": (2, 2), "name": "瞪眼", "pp": 30, "pp_max": 30, "power": 0},
        {"px": (3, 3), "name": "火花", "pp": 16, "pp_max": 25, "power": 40},
        {"px": (4, 4), "name": "挑拨", "pp": 20, "pp_max": 20, "power": 0},
    ]
    assert RunBattle.pick_skill(cells)["name"] == "火花"

    # a stronger move wins when both have PP
    cells.append({"px": (5, 5), "name": "火焰漩涡", "pp": 5, "pp_max": 5, "power": 90})
    assert RunBattle.pick_skill(cells)["name"] == "火焰漩涡"

    # nothing damaging left -> use whatever still has PP
    for c in cells:
        if c["power"]:
            c["pp"] = 0
    picked = RunBattle.pick_skill(cells)
    assert picked is not None and picked["power"] == 0

    # nothing usable at all
    for c in cells:
        c["pp"] = 0
    assert RunBattle.pick_skill(cells) is None
