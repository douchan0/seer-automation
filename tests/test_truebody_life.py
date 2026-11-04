"""The truebody life detector, validated against real recorded frames.

Why this exists: a 谱尼真身 fight has six lives, and the enemy HP bar carries **no
number** (it renders as ``LV:??`` plus a plain bar), so "which life am I on" cannot
be read from HP.  The only reliable signal is the large centre overlay the client
draws at the start of each life:

    谱尼真身
    第一命
    （虚无封印）

The fixture in ``tests/fixtures/truebody_life_frames.json`` is the OCR transcription
of all 104 truebody frames from the 2026-08-23 tutorial video (``frames/_ocr.json``),
reduced to the boxes that mention 命/封印 so it can live in the repo.  The overlay is
visible on 74 of the 104 frames; on those the parser must read the life, and the
sequence must be monotonic.

The parser also independently confirms the life -> seal mapping (1 虚无, 2 元素,
3 能量, 4 生命, 5 轮回; the 6th life has no seal) — the game's own UI, not a guide.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from seerauto.skills.base import Budget, SkillContext, Status
from seerauto.skills.observe import ObserveTruebodyLife
from seerauto.skills.parsers import (
    TRUEBODY_LIFE_RE,
    parse_truebody_life,
    parse_truebody_seal,
)
from seerauto.world.model import GameWorldState

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "truebody_life_frames.json"


def _items(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"text": b["text"], "conf": 0.9,
             "norm": (b["x"], b["y"], b["w"], b["h"])} for b in boxes]


def _frame_items(frame: dict[str, Any]) -> list[dict[str, Any]]:
    return _items(frame["boxes"])


def _centre(text: str) -> dict[str, Any]:
    """A box in the middle of the screen (where the overlay is drawn).

    Geometry copied from the real frames: ``y`` is measured from the BOTTOM edge
    (Vision convention), so y=0.60 + h=0.07 puts the box centre at ~36% from the
    top -- which is where the client draws 「第N命」.
    """

    return {"text": text, "conf": 0.9, "norm": (0.40, 0.60, 0.18, 0.07)}


def _bottom_left(text: str) -> dict[str, Any]:
    """A box in the battle log, which also contains 命 words."""

    return {"text": text, "conf": 0.9, "norm": (0.02, 0.80, 0.30, 0.05)}


# --------------------------------------------------------------------------- #
# unit: the numeral forms the OCR actually produced
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, expected", [
    ("第一命", 1), ("第二命", 2), ("第三命", 3),
    ("第四命", 4), ("第五命", 5), ("第六命", 6),
    ("第五条命", 5),          # `条` appears once in the real data
    ("第 3 命", 3),           # digits survive some frames
])
def test_life_numerals(text, expected):
    parsed = parse_truebody_life([_centre(text)])
    assert parsed is not None
    assert parsed["life"] == expected


@pytest.mark.parametrize("text", ["第￥命", "第 命", "第七命", "命中率：100%", "致命一击"])
def test_unreadable_or_out_of_range_is_none(text):
    """Guessing a life number is worse than admitting we do not know."""

    assert parse_truebody_life([_centre(text)]) is None


def test_battle_log_is_not_mistaken_for_the_overlay():
    assert parse_truebody_life([_bottom_left("第一命")]) is None


def test_seal_is_paired_from_a_separate_box():
    """On every real frame 「第一命」 and 「（虚无封印）」 are separate boxes."""

    items = [_centre("第一命"), _centre("（虚无封印）")]
    parsed = parse_truebody_life(items)
    assert parsed is not None
    assert parsed["life"] == 1
    assert parsed["seal"] == "虚无"


def test_unknown_seal_word_is_dropped():
    items = [_centre("第一命"), _centre("（七大封印）")]
    parsed = parse_truebody_life(items)
    assert parsed is not None and parsed["seal"] is None


def test_sixth_life_has_no_seal_and_that_is_not_an_error():
    parsed = parse_truebody_life([_centre("第六命")])
    assert parsed is not None
    assert parsed["life"] == 6
    assert parsed["seal"] is None


# --------------------------------------------------------------------------- #
# replay: the real 104-frame sequence
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def frames():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_covers_the_truebody_frames(frames):
    assert len(frames) == 104


def test_parser_reads_every_frame_where_the_overlay_is_visible(frames):
    visible = [f for f in frames
               if any(TRUEBODY_LIFE_RE.search(b["text"]) for b in f["boxes"])]
    parsed = [f for f in visible if parse_truebody_life(_frame_items(f)) is not None]
    assert len(visible) == 74
    assert len(parsed) == len(visible), "recall must be 100% on visible frames"


def test_life_sequence_is_monotonic_across_the_whole_fight(frames):
    lives = []
    for frame in frames:
        parsed = parse_truebody_life(_frame_items(frame))
        if parsed:
            lives.append(parsed["life"])
    assert lives == sorted(lives), f"life numbers went backwards: {lives}"
    assert set(lives) == {1, 2, 3, 4, 5, 6}


def test_life_to_seal_mapping_matches_the_guide(frames):
    """The client's own overlay confirms the mapping the source ledger called UNVERIFIED."""

    pairs: dict[int, set[str]] = {}
    for frame in frames:
        parsed = parse_truebody_life(_frame_items(frame))
        if parsed and parsed["seal"]:
            pairs.setdefault(parsed["life"], set()).add(parsed["seal"])
    assert pairs[1] == {"虚无"}
    assert pairs[2] == {"元素"}
    assert pairs[3] == {"能量"}
    assert pairs[4] == {"生命"}
    assert pairs[5] == {"轮回"}
    assert 6 not in pairs          # 第六命无对应


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


def test_skill_writes_the_life_fact():
    world = GameWorldState()
    ctx = SkillContext(session=_Session([_centre("第三命"), _centre("（能量封印）")]),
                       world=world, dry_run=True, allow_input=False,
                       budget=Budget(max_duration_s=20, max_actions=0))
    result = ObserveTruebodyLife()(ctx)
    assert result.status is Status.PASS
    assert result.after["life"] == 3
    assert result.after["seal"] == "能量"
    assert "第3命" in result.proof
    # Observe skills return a copied world in data["world"]; the runner applies it
    # (runner.py:280).  Assert on that contract, not on ctx.world being mutated.
    fact = result.data["world"]["facts"]["counters.truebody.life"]
    assert fact["value"] == 3
    assert fact["evidence"]["source"] == "LIVE_VERIFIED"


def test_skill_fails_when_the_overlay_is_absent():
    ctx = SkillContext(session=_Session([_bottom_left("命中率：100%")]),
                       world=GameWorldState(), dry_run=True, allow_input=False,
                       budget=Budget(max_duration_s=20, max_actions=0))
    result = ObserveTruebodyLife()(ctx)
    assert result.status is Status.FAIL
    assert "not on screen" in result.reason


def test_skill_without_a_session_is_blocked_not_crashing():
    ctx = SkillContext(session=None, world=GameWorldState())
    result = ObserveTruebodyLife()(ctx)
    assert result.status is Status.FAIL
