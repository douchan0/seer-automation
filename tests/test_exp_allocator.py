"""The 经验分配器 panel reader, tested against a frame recorded from the guide video.

Why this panel: the account's own notes say the XP pool grows but the only known way
to spend it is this panel, and it has never been located on the live client
(docs/MACHINE_MAP.md).  Reading it is the cheap half of the problem, so it gets a
parser and a real fixture instead of being re-derived from scratch next session.

The fixture is `seerdx ocr` output for `frames/02_spirit_day1/f_00019.png`, converted
to the Vision-normalised `norm` tuples `LiveSession.ocr` emits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seerauto.skills.parsers import parse_exp_allocator

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "exp_allocator_panel.json"


@pytest.fixture(scope="module")
def panel_items():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_reads_every_field_off_the_recorded_panel(panel_items):
    got = parse_exp_allocator(panel_items)
    assert got is not None
    assert got["name"] == "烈火猴"
    assert got["level"] == 17.0
    assert got["nature"] == "孤独"
    assert got["index"] == 8.0
    assert got["xp_to_next"] == 316.0
    assert got["pool"] == 18744927.0


def test_reads_the_amount_waiting_in_the_input_box(panel_items):
    """The typed value is its own OCR box in the real frame ("500g")."""

    assert parse_exp_allocator(panel_items)["pending_amount"] == 500.0


def test_a_bag_detail_panel_is_not_this_panel():
    """`等级` alone must not be mistaken for the allocator; the title decides."""

    bag = [{"text": "烈火猴", "norm": (0.2, 0.5, 0.1, 0.03)},
           {"text": "等级：35", "norm": (0.3, 0.5, 0.1, 0.03)},
           {"text": "该精灵增加", "norm": (0.3, 0.4, 0.1, 0.03)}]
    assert parse_exp_allocator(bag) is None


def test_a_dropped_number_is_none_and_never_zero(panel_items):
    """Rule 1 of parsers.py: what the OCR did not see is not zero."""

    without = [i for i in panel_items if i["text"].strip() != "316"]
    got = parse_exp_allocator(without)
    assert got["xp_to_next"] is None
    assert got["pool"] == 18744927.0  # the inline field still reads


def test_empty_input_is_none_not_an_error():
    assert parse_exp_allocator([]) is None


def test_a_merged_ocr_row_cannot_fabricate_a_level():
    """OCR sometimes merges two rows; the body parser must not read the wrong number.

    `parse_exp_allocator` needs the title, so a merged row usually arrives through the
    title-independent body reader the skill falls back to.
    """

    from seerauto.skills.act import _allocator_body_still_there, _parse_allocator_body

    merged = [{"text": "等级：18 积累经验：18744927", "conf": 0.9, "px": (100, 100)}]
    assert _allocator_body_still_there(merged) is True
    got = _parse_allocator_body(merged)
    assert got is not None
    assert got["level"] == 18.0, got
    assert got["pool"] == 18744927.0, got


def test_the_body_parser_returns_none_without_any_numbers():
    from seerauto.skills.act import _parse_allocator_body

    assert _parse_allocator_body([{"text": "公屏聊天", "conf": 0.9}]) is None
