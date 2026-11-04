"""Battle-log event parsing, validated against the real recorded frames.

Why it matters: the enemy's HP and PP bars render with **no numbers** (`LV:??` plus a
plain bar), so "did it die" and "is it out of PP" can only come from the text log.
「圣光气」 is the documented marker that 谱尼 exhausted a move's PP.

The trap this file guards: the tutorial video's burned-in subtitles sit on the same
rows as the log, and one of them literally reads 「当他没有圣光气之后」.  A whole-frame
keyword search would read narration as combat.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from seerauto.skills.base import Budget, SkillContext, Status
from seerauto.skills.observe import ObserveBattleLog
from seerauto.skills.parsers import (
    enemy_defeated,
    holy_light_seen,
    parse_battle_log_events,
)
from seerauto.world.model import GameWorldState

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "battle_log_frames.json"


def _log_box(text: str) -> dict[str, Any]:
    """A box inside the bottom-left log panel (fy is measured from the TOP)."""

    return {"text": text, "conf": 0.9, "norm": (0.03, 0.02, 0.20, 0.03)}


def _subtitle_box(text: str) -> dict[str, Any]:
    return {"text": text, "conf": 0.9, "norm": (0.38, 0.01, 0.30, 0.04)}


def _items(*boxes):
    return [dict(b) for b in boxes]


# --------------------------------------------------------------------------- #
# unit
# --------------------------------------------------------------------------- #
def test_defeat_line_is_recognised():
    items = _items(_log_box("【谱尼】被击败了！"))
    assert parse_battle_log_events(items) == [{"kind": "defeated", "text": "【谱尼】被击败了！"}]
    assert enemy_defeated(items) is True


def test_holy_light_is_the_pp_empty_signal():
    items = _items(_log_box("【谱尼】使用了圣光气，自身充满了圣光气"))
    assert holy_light_seen(items) is True
    kinds = [e["kind"] for e in parse_battle_log_events(items)]
    # holy_light must win over the generic "使用了"
    assert kinds == ["holy_light"]


def test_subtitle_is_not_combat():
    """The video's narration says 圣光气 too; it must not be read as the battle."""

    items = _items(_subtitle_box("当他没有圣光气之后"))
    assert parse_battle_log_events(items) == []
    assert holy_light_seen(items) is False


def test_entered_and_used_are_classified():
    items = _items(_log_box("我方【萨帕克】(♀)登场了！"),
                   _log_box("【萨帕克】使用了精神强化"))
    kinds = [e["kind"] for e in parse_battle_log_events(items)]
    assert kinds == ["entered", "used"]


def test_critical_hit():
    items = _items(_log_box("【谱尼】使用了圣灵魔闪光，打出了致命一击。"))
    kinds = [e["kind"] for e in parse_battle_log_events(items)]
    # `crit` is checked before the generic `used`, so a critical-hit line that also
    # says 使用了 is classified as a crit rather than a plain move.
    assert kinds == ["crit"]


def test_box_without_position_is_ignored():
    assert parse_battle_log_events([{"text": "【谱尼】被击败了！"}]) == []


# --------------------------------------------------------------------------- #
# replay over the real frames
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def frames():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_the_real_corpus(frames):
    assert len(frames) >= 200


def test_every_event_kind_occurs_in_the_real_corpus(frames):
    seen = set()
    for frame in frames:
        items = [{"text": b["text"], "conf": 0.9,
                  "norm": (b["x"], b["y"], b["w"], b["h"])} for b in frame["boxes"]]
        seen.update(e["kind"] for e in parse_battle_log_events(items))
    assert {"defeated", "crit", "holy_light", "entered", "used"} <= seen


def test_real_subtitles_never_become_events(frames):
    """Four subtitle boxes in the corpus mention 圣光气; none may be parsed."""

    accepted = 0
    subtitles = 0
    for frame in frames:
        for b in frame["boxes"]:
            if b["x"] >= 0.34:
                subtitles += 1
        items = [{"text": b["text"], "conf": 0.9,
                  "norm": (b["x"], b["y"], b["w"], b["h"])} for b in frame["boxes"]]
        accepted += sum(1 for e in parse_battle_log_events(items)
                        if "之后" in e["text"])
    assert subtitles == 4
    assert accepted == 0


# --------------------------------------------------------------------------- #
# the skill
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, items):
        self.items = items

    def ocr(self, **kw):
        return [dict(i) for i in self.items]


def _ctx(session):
    return SkillContext(session=session, world=GameWorldState(), dry_run=True,
                        allow_input=False, budget=Budget(max_duration_s=20, max_actions=0))


def test_skill_records_the_pp_empty_fact():
    ctx = _ctx(_Session(_items(_log_box("【谱尼】使用了圣光气，自身充满了圣光气"))))
    result = ObserveBattleLog()(ctx)
    assert result.status is Status.PASS
    assert result.data["holy_light"] is True
    fact = result.data["world"]["facts"]["battle.enemy_pp_empty"]
    assert fact["value"] is True
    assert fact["evidence"]["source"] == "LIVE_VERIFIED"


def test_skill_does_not_claim_pp_empty_without_the_signal():
    ctx = _ctx(_Session(_items(_log_box("我方【萨帕克】登场了！"))))
    result = ObserveBattleLog()(ctx)
    assert result.status is Status.PASS
    assert result.data["holy_light"] is False
    assert "battle.enemy_pp_empty" not in result.data["world"]["facts"]


def test_skill_fails_when_the_log_is_unreadable():
    ctx = _ctx(_Session([]))
    result = ObserveBattleLog()(ctx)
    assert result.status is Status.FAIL
    assert "not readable" in result.reason


def test_defeat_does_not_name_a_side():
    """A defeat line alone must not be reported as a win."""

    ctx = _ctx(_Session(_items(_log_box("我方【萨帕克】被击败了！"))))
    result = ObserveBattleLog()(ctx)
    assert result.data["enemy_defeated"] is True   # the *line* exists...
    assert "未指明哪一方" in result.proof          # ...but the proof says so
