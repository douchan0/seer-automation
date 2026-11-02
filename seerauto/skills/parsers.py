"""OCR text -> typed account facts.

Every function here is **pure**: it takes the item list that
:meth:`seerauto.live.LiveSession.ocr` returns (``{"text", "conf", "px", "norm"}``
dicts), and returns a value or ``None``.  Nothing touches the screen, the store
or any global state, so the whole reading half of the semantic layer can be
unit-tested offline against frames recorded earlier.

Two rules decide every design choice in this module:

1. **Unknown is ``None``, never ``False``/``0``/``""``.**  A missing label, a
   label whose number the OCR dropped, a card that shows no state word: all of
   them are "we did not see it", not "it is not true".
2. **Never invent a game mechanic.**  A line only becomes a typed fact when
   ``docs/`` (or a real screenshot in ``artifacts/``) establishes what it means.
   Where nothing establishes it, the function returns ``None`` and the docstring
   says why.

Geometry
--------
The live client is captured at 2400x1600 device px (window 1200x800 pt, scale
2.0 -- ``docs/state.md``).  OCR boxes are therefore compared in that *canonical*
space: items carrying the Vision-normalised ``norm`` tuple are mapped onto it,
items carrying only ``px`` are assumed to already be in it.  Adjacency
thresholds are constants below; every one of them was measured on a real frame,
and the frame that justifies it is named next to the constant.

Provenance of the examples
--------------------------
Unless a docstring says otherwise, the sample lines quoted in this module were
read out of ``artifacts/live/*.png`` with a read-only file OCR
(``native/bin/seerdx {"op":"ocr","path":...}``), and the transcribed text is
kept next to the parser in ``tests/test_observe_skills.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

Item = Mapping[str, Any]

#: Canonical pixel space of the live capture (window 1200x800 pt @ 2x).
LIVE_FRAME_PX = (2400.0, 1600.0)

# --------------------------------------------------------------------------- #
# geometry thresholds (each one measured on a real frame, see comments)
# --------------------------------------------------------------------------- #
#: Two boxes belong to the same text line when their centres are this close.
#: Shop wallet header ``XIN豆``/``5,000`` are 0 px apart; the next UI row is
#: ~81 canonical px below (``artifacts/shop.png``, 1200x746 -> x2 for canonical).
SAME_LINE_PX = 40.0
#: A value label sits to the right of its caption, never on top of it.
SIDE_MIN_DX = 60.0
#: ``当前回合：`` ends at x~1273 and its number sits at x~1305 (battle frame).
SIDE_MAX_DX = 320.0
#: Wallet value must be adjacent, otherwise it is a price from another widget.
WALLET_MAX_DX = 260.0
#: A skill name sits directly above its ``使用次数`` line, inside the same cell:
#: measured dy 43..49 px, and the neighbouring row is 171 px away (victory frame).
PP_NAME_DX = 170.0
PP_NAME_DY = (10.0, 130.0)
#: Left half of the frame is the player's side, right half the opponent's.
ALLY_MAX_CX = LIVE_FRAME_PX[0] / 2.0
#: ``docs/state.md``: 我方血量 ``89/92`` is at (816, 257).  A ``a/b`` pair that
#: is not in this band is far more likely to be a PP counter or a ratio.
HP_X_BAND = (650.0, 1000.0)

#: Confirm / close labels.  Kept in sync with ``seerauto.live.CONFIRM_WORDS``
#: (which is the list Round 4 actually clicked); duplicated instead of imported
#: because importing ``live`` would drag cv2/numpy into pure text parsing.
DIALOG_CONFIRM_WORDS = ("确认", "確定", "确定", "我已知晓", "我知道啦")
DIALOG_CLOSE_WORDS = ("关闭",)

#: Currency labels that the wallet/header can show.  ``赛尔豆`` has no verified
#: sample in ``artifacts/`` (see :func:`parse_currency`).
CURRENCY_LABELS = ("赛尔豆", "XIN豆", "金豆")

#: Reward nouns the game has been seen to pay out.  Nothing outside this table
#: is turned into a reward: an unknown noun would be a guess.
REWARD_KINDS = ("经验", "赛尔豆", "XIN豆", "金豆")

#: SPT panel state words.  ``docs/state.md``/``docs/progress.md``: every boss
#: card is labelled 未击败 or 已击败, and carries 击败后解锁交易行资格.
SPT_DEFEAT_WORDS = {"已击败": True, "未击败": False}
SPT_UNLOCK_LINE = "击败后解锁交易行资格"

#: Words that are never a pet/skill name.
_NON_NAMES = frozenset({
    "当前战报", "当前回合", "战斗", "精灵", "道具", "逃跑", "胜利", "确认",
    "关闭", "取消", "获得成就", "使用次数", "状态", "正常", "威力", "技能",
    "名字", "序号", "个体", "体力", "防御", "特防", "攻击", "特攻", "速度",
})


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Box:
    """One OCR box in canonical live px (2400x1600, y grows downwards)."""

    cx: float
    cy: float
    w: float | None = None
    h: float | None = None

    @property
    def known_size(self) -> bool:
        return self.w is not None and self.h is not None


def text_of(item: Item) -> str:
    """The OCR string, stripped.  Never raises on a malformed item."""

    return str(item.get("text") or "").strip()


def conf_of(item: Item) -> float:
    try:
        return float(item.get("conf") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def box_of(item: Item) -> Box | None:
    """Box in canonical px, or ``None`` when the item carries no position.

    ``norm`` wins when present (that is what ``LiveSession.ocr`` emits: Vision
    normalised, origin bottom-left), so the same item transcribed from a
    1200x746 screenshot and from a 2400x1600 one lands in the same space.
    """

    norm = item.get("norm")
    if isinstance(norm, (tuple, list)) and len(norm) == 4:
        try:
            x, y, w, h = (float(v) for v in norm)
        except (TypeError, ValueError):
            pass
        else:
            fw, fh = LIVE_FRAME_PX
            return Box((x + w / 2.0) * fw, (1.0 - (y + h / 2.0)) * fh, w * fw, h * fh)
    px = item.get("px")
    if isinstance(px, (tuple, list)) and len(px) == 2:
        try:
            return Box(float(px[0]), float(px[1]))
        except (TypeError, ValueError):
            return None
    return None


def _side_of(item: Item) -> str | None:
    """``"ally"`` / ``"enemy"`` for a positioned item, ``None`` when unknown."""

    box = box_of(item)
    if box is None:
        return None
    return "ally" if box.cx < ALLY_MAX_CX else "enemy"


# --------------------------------------------------------------------------- #
# battle turn
# --------------------------------------------------------------------------- #
_TURN_MERGED = re.compile(r"当前回合\s*[：:]\s*(\d{1,3})")
_TURN_LABEL_WITH_COLON = re.compile(r"当前回合\s*[：:]?\s*$")
_TURN_ONLY_DIGITS = re.compile(r"^\d{1,3}$")


def parse_turn(items: Iterable[Item]) -> int | None:
    """``当前回合：N`` -> ``N``.

    The OCR usually splits the caption and the number into two boxes, so this
    accepts both shapes::

        >>> parse_turn([{"text": "当前回合：1", "conf": 1.0, "px": (1200, 436)}])
        1
        >>> parse_turn([{"text": "当前回合：", "conf": 1.0, "px": (1163, 436)},
        ...             {"text": "1", "conf": 0.5, "px": (1305, 438)}])
        1

    Both examples are the real victory/battle frames in ``artifacts/live/``.
    A bare number with no ``当前回合`` caption is *not* a turn (it could be a
    level, a currency or a log line), so the answer is ``None``.
    """

    items = list(items)
    found: list[int] = []
    labels: list[Item] = []
    for item in items:
        text = text_of(item)
        if "当前回合" not in text:
            continue
        match = _TURN_MERGED.search(text)
        if match:
            found.append(int(match.group(1)))
        elif _TURN_LABEL_WITH_COLON.search(text):
            labels.append(item)

    for label in labels:
        label_box = box_of(label)
        if label_box is None:
            continue
        best: tuple[float, int] | None = None
        for item in items:
            if item is label:
                continue
            text = text_of(item)
            if not _TURN_ONLY_DIGITS.match(text):
                continue
            item_box = box_of(item)
            if item_box is None:
                continue
            dx = item_box.cx - label_box.cx
            dy = abs(item_box.cy - label_box.cy)
            if dy > SAME_LINE_PX or not SIDE_MIN_DX <= dx <= SIDE_MAX_DX:
                continue
            candidate = (dx, int(text))
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is not None:
            found.append(best[1])

    found = [turn for turn in found if turn >= 1]
    values = set(found)
    if len(values) == 1:
        return found[0]
    # Zero candidates: not visible.  More than one disagreeing value: two
    # different turn counters on screen, which we refuse to guess between.
    return None


# --------------------------------------------------------------------------- #
# hit points
# --------------------------------------------------------------------------- #
_HP_SHAPE = re.compile(r"^(\d{1,4})\s*/\s*(\d{1,4})$")


def parse_hp(items: Iterable[Item]) -> tuple[int, int] | None:
    """Ally HP ``89/92`` -> ``(89, 92)``.

    Anchored twice, because the battle HUD is full of ``a/b`` shapes (PP
    counters, ratios, item stacks):

    * the text must be exactly the ``a/b`` shape (no label glued to it);
    * its centre must sit in :data:`HP_X_BAND`, the x range where the ally HP
      readout lives (``docs/state.md``: ``89/92`` at (816, 257)).

    ``a <= b`` is required; anything else is not an HP readout::

        >>> parse_hp([{"text": "89/92", "conf": 1.0, "px": (816.3, 256.2)}])
        (89, 92)

    A PP line such as ``使用次数：24/25`` (x~1203) fails both anchors.
    """

    best: tuple[float, int, int] | None = None
    for item in items:
        match = _HP_SHAPE.match(text_of(item))
        if not match:
            continue
        current, maximum = int(match.group(1)), int(match.group(2))
        if maximum <= 0 or current > maximum:
            continue
        box = box_of(item)
        if box is None or not HP_X_BAND[0] <= box.cx <= HP_X_BAND[1]:
            continue
        candidate = (conf_of(item), current, maximum)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    return (best[1], best[2])


# --------------------------------------------------------------------------- #
# skill PP
# --------------------------------------------------------------------------- #
#: ``使用次数：31/35``.  The OCR has been seen to garble the middle characters
#: (``使用饮数：24/25``, ``使用沉数：20/20`` in the victory frame), so the two
#: characters between 使用 and the colon are not pinned; what is pinned is
#: the literal 使用 prefix and the ``a/b`` tail.
_PP_SHAPE = re.compile(r"使用[^\d]{0,3}[：:]?\s*(\d{1,3})\s*/\s*(\d{1,3})")
_PP_ANYWHERE = re.compile(r"\d{1,3}\s*/\s*\d{1,3}")


def parse_pp(items: Iterable[Item]) -> dict[str, tuple[int, int]]:
    """``使用次数：31/35`` lines -> ``{skill name: (current, max)}``.

    Real sample (victory frame, x~1203)::

        >>> parse_pp([{"text": "使用次数：31/35", "conf": 0.8, "px": (1203, 1293)}])
        {'pos:1203,1293': (31, 35)}

    The key is the skill name **only when the name box was read** and sits
    directly above the counter (same cell, see :data:`PP_NAME_DY`).  When the
    name is unreadable the entry is still recorded, keyed ``pos:X,Y`` in
    canonical px, because the counter itself is a real observation -- but it is
    labelled so that no caller can mistake a position for a skill name.  With
    names present:

        >>> parse_pp([
        ...     {"text": "瞪眼", "conf": 1.0, "px": (1411, 1197)},
        ...     {"text": "使用次数：30/30", "conf": 0.8, "px": (1408, 1246)},
        ... ])["瞪眼"]
        (30, 30)

    Returns ``{}`` when no counter is visible.
    """

    items = list(items)
    out: dict[str, tuple[int, int]] = {}
    for item in items:
        text = text_of(item)
        match = _PP_SHAPE.search(text)
        if not match:
            continue
        current, maximum = int(match.group(1)), int(match.group(2))
        if maximum <= 0 or current > maximum:
            continue
        box = box_of(item)
        key = None
        if box is not None:
            best: tuple[float, str] | None = None
            for other in items:
                if other is item:
                    continue
                name = text_of(other)
                if not _is_name_like(name):
                    continue
                other_box = box_of(other)
                if other_box is None:
                    continue
                dx = abs(other_box.cx - box.cx)
                dy = box.cy - other_box.cy
                if dx > PP_NAME_DX or not PP_NAME_DY[0] <= dy <= PP_NAME_DY[1]:
                    continue
                candidate = (dy, name)
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best is not None:
                key = best[1]
            else:
                key = f"pos:{int(round(box.cx))},{int(round(box.cy))}"
        if key is None:
            key = f"pp{len(out) + 1}"
        out[key] = (current, maximum)
    return out


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _is_name_like(text: str) -> bool:
    """Looks like a pet/skill name: 1-6 Han characters, nothing else.

    Latin letters are rejected on purpose: the victory frame OCRs the PP caption
    of one cell as ``E用FRE``, and a mixed-script fragment is a garbled caption,
    never a skill name.
    """

    if not text or len(text) > 6:
        return False
    if not all(_is_cjk(ch) for ch in text):
        return False
    return text not in _NON_NAMES


# --------------------------------------------------------------------------- #
# pet names
# --------------------------------------------------------------------------- #
_ALLY_NAME = re.compile(r"我方[【［\[]\s*([^】］\]\s]{1,12})\s*[】］\]]")
_ENEMY_NAME = re.compile(r"对方[【［\[]\s*([^】］\]\s]{1,12})\s*[】］\]]")
#: A bracketed name with the side label split off into its own OCR box.
_BRACKETED = re.compile(r"^[【［\[]\s*([^】］\]\s]{1,12})\s*[】］\]]")


def _clean_name(raw: str) -> str | None:
    name = raw.strip()
    if not name or name.isdigit() or name in _NON_NAMES:
        return None
    return name


def _name_after_label(items: Sequence[Item], label: str) -> str | None:
    """Repair the OCR splitting ``对方`` and ``【蘑菇译】登场了！`` into two boxes.

    Both boxes are on the same line and adjacent; nothing else on the battle
    screen puts a bare ``我方``/``对方`` label next to a bracketed name, so this
    is a repair of a known OCR split rather than a guess about the game.
    """

    for item in items:
        if text_of(item) != label:
            continue
        label_box = box_of(item)
        if label_box is None:
            continue
        best: tuple[float, float, str] | None = None
        for other in items:
            if other is item:
                continue
            match = _BRACKETED.search(text_of(other))
            if not match:
                continue
            other_box = box_of(other)
            if other_box is None:
                continue
            dx = other_box.cx - label_box.cx
            dy = abs(other_box.cy - label_box.cy)
            if dy > SAME_LINE_PX or not 0.0 < dx <= SIDE_MAX_DX:
                continue
            name = _clean_name(match.group(1))
            if name is None:
                continue
            # closest to the label's own line first: the victory frame also has a
            # ``【烈火猴】使用了抓，`` box one line below the 对方 label.
            candidate = (dy, dx, name)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is not None:
            return best[2]
    return None


def parse_pet_names(items: Iterable[Item]) -> dict[str, str | None]:
    """Both sides at once: ``{"ally": ..., "enemy": ...}``.

    ::

        >>> parse_pet_names([
        ...     {"text": "我方【烈火猴】（3）登场了！", "conf": 0.3},
        ...     {"text": "对方【蘑菇怪】登场了！", "conf": 0.5},
        ... ])
        {'ally': '烈火猴', 'enemy': '蘑菇怪'}

    ``docs/state.md``/Round 4 read the battle log lines
    ``我方【烈火猴】…登场了！`` and ``对方【蘑菇怪】登场了！``; the ``我方``/
    ``对方`` prefix is what decides which side a name belongs to, so a bracketed
    name with no side label is *not* attributed to anybody.

    The victory frame OCRs the enemy line as two boxes (``对方`` at x~230 and
    ``【蘑菇译】登场了！`` at x~382, same line); those are re-joined by adjacency,
    because a bare side label next to a bracketed name is a known OCR split, not
    a game feature.  What the OCR read is returned verbatim -- that frame really
    yields ``蘑菇译``, a one-character misread of the true ``蘑菇怪``, and the
    parser does not "correct" names it cannot verify.
    """

    ally: str | None = None
    enemy: str | None = None
    items = list(items)
    for item in items:
        text = text_of(item)
        if ally is None:
            match = _ALLY_NAME.search(text)
            if match:
                ally = _clean_name(match.group(1))
        if enemy is None:
            match = _ENEMY_NAME.search(text)
            if match:
                enemy = _clean_name(match.group(1))
    if ally is None:
        ally = _name_after_label(items, "我方")
    if enemy is None:
        enemy = _name_after_label(items, "对方")
    return {"ally": ally, "enemy": enemy}


def parse_pet_name(items: Iterable[Item]) -> str | None:
    """The player's own pet, e.g. from ``我方【烈火猴】``.

    ::

        >>> parse_pet_name([{"text": "我方【烈火猴】（3）登场了！", "conf": 0.3}])
        '烈火猴'

    The ally wins when both sides are on screen (Round 4 read both on the same
    frame); ``对方【蘑菇怪】`` is *not* an answer to "which pet am I using".
    """

    return parse_pet_names(items)["ally"]


# --------------------------------------------------------------------------- #
# level
# --------------------------------------------------------------------------- #
#: ``LV:10`` / ``LV：15``.
_LV_COLON = re.compile(r"LV\s*[：:]\s*(\d{1,3})")
#: The HUD font draws the colon as a small square glyph which the OCR returns as
#: ``8``: the real battle frame reads ``LV833`` for ``LV:33`` (ally) and
#: ``LV810`` for ``LV:10`` (enemy).  ``docs/state.md`` independently records the
#: enemy badge as ``LV:10`` at exactly that box, which is what pins the mapping.
_LV_COLON_AS_EIGHT = re.compile(r"LV\s*8\s*(\d{1,3})")
#: Colon dropped entirely by the OCR.
_LV_BARE = re.compile(r"LV\s*(\d{1,3})")
#: ``等级 15`` / ``等级：15``.
_LEVEL_CN = re.compile(r"等级\s*[：:]?\s*(\d{1,3})")
#: ``级：5`` -- the pet detail panel has been OCR'd with the ``等`` character
#: clipped off by an overlapping dialog (``artifacts/petpanel.png``), so the
#: short form is accepted, but only with an explicit colon.
_LEVEL_SHORT = re.compile(r"(?<![等所])级\s*[：:]\s*(\d{1,3})")


def parse_level(items: Iterable[Item], *, side: str | None = None) -> int | None:
    """Level from ``LV:10`` / ``LV：15`` / ``等级 15``.

    ``side`` is ``"ally"``/``"enemy"`` to pick one half of the battle HUD, or
    ``None`` to accept the value only when the screen is unambiguous::

        >>> parse_level([{"text": "LV：15", "conf": 0.9, "px": (556, 261),
        ...               "norm": (0.23, 0.82, 0.05, 0.02)}])
        15
        >>> parse_level([{"text": "LV833", "conf": 0.3, "px": (556.4, 261.4)},
        ...              {"text": "LV810", "conf": 0.5, "px": (1819.2, 261.5)}]) is None
        True

    On the real battle frame both sides are labelled (ally ``LV833`` = ``LV:33``
    at cx~556, enemy ``LV810`` = ``LV:10`` at cx~1819), so the honest answer for
    the whole frame is ``None``; pass ``side="ally"`` to get the player's pet
    level.  A bare number (the ``15`` badge in the world HUD) has no label and
    is never treated as a level.
    """

    found: list[tuple[int, str | None]] = []
    for item in items:
        text = text_of(item)
        value: int | None = None
        for pattern in (_LV_COLON, _LV_COLON_AS_EIGHT, _LV_BARE, _LEVEL_CN, _LEVEL_SHORT):
            match = pattern.search(text)
            if match:
                value = int(match.group(1))
                break
        if value is None:
            continue
        found.append((value, _side_of(item)))

    if side is not None:
        found = [(value, item_side) for value, item_side in found if item_side == side]
    else:
        # an unpositioned reading belongs to nobody in particular; keep it only
        # when it is the sole reading on screen
        positioned = [(v, s) for v, s in found if s is not None]
        if positioned:
            found = positioned

    values = {value for value, _ in found}
    if len(values) == 1:
        return found[0][0]
    return None


# --------------------------------------------------------------------------- #
# currency
# --------------------------------------------------------------------------- #
#: ``5,000`` / ``1200``.  Chinese numerals (``10w``) are deliberately *not*
#: handled: the shop price rows use them (``⑥ 10w XIN豆``) and guessing a factor
#: would be inventing a unit.
_NUMBER = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d{1,9}$")


def parse_currency(items: Iterable[Item]) -> dict[str, float]:
    """Wallet amounts for 赛尔豆 / XIN豆 / 金豆.

    The shop header renders the caption and the value as two boxes on one line
    (``XIN豆`` at cx~290, ``5,000`` at cx~482 canonical; ``artifacts/shop.png``)::

        >>> parse_currency([
        ...     {"text": "XIN豆", "conf": 0.3, "norm": (0.107, 0.764, 0.028, 0.016)},
        ...     {"text": "5,000", "conf": 1.0, "norm": (0.187, 0.764, 0.028, 0.016)},
        ... ])
        {'XIN豆': 5000.0}

    Only an *exact* label counts.  Price rows such as ``⑤ 1XIN豆``, ``❻ 20金豆``
    or ``10w XIN豆`` are a different widget, on a different line, with the number
    glued to the caption; none of them can be mistaken for the wallet.

    The value is omitted -- never invented -- when the OCR read the caption but
    dropped the number: the real screenshot has ``金豆`` with its ``0`` missing,
    and ``金豆`` is then simply absent from the result.  ``赛尔豆`` is supported
    by name, but no screenshot in ``artifacts/`` shows that caption, so reading
    it is untested (see the module report).
    """

    items = list(items)
    out: dict[str, float] = {}
    for label in CURRENCY_LABELS:
        label_items = [item for item in items if text_of(item) == label]
        if not label_items:
            continue
        value: float | None = None
        for label_item in label_items:
            label_box = box_of(label_item)
            if label_box is None:
                continue
            best: tuple[float, float] | None = None
            for other in items:
                if other is label_item:
                    continue
                other_text = text_of(other)
                if not _NUMBER.match(other_text):
                    continue
                other_box = box_of(other)
                if other_box is None:
                    continue
                dx = other_box.cx - label_box.cx
                dy = abs(other_box.cy - label_box.cy)
                if dy > SAME_LINE_PX or not 0.0 < dx <= WALLET_MAX_DX:
                    continue
                candidate = (dx, float(other_text.replace(",", "")))
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best is not None:
                value = best[1]
                break
        if value is not None:
            out[label] = value
    return out


# --------------------------------------------------------------------------- #
# rewards
# --------------------------------------------------------------------------- #
#: ``恭喜你得到了 106 个积累经验`` -- number *before* the noun.
_REWARD_AMOUNT_FIRST = re.compile(
    r"(?:得到|获得|领取)\s*(?:了)?\s*(\d[\d,]*)\s*个\s*([^\d\s，。！？、]{1,8})")
#: ``XIN精灵获得经验：106`` -- noun *before* the number.
_REWARD_KIND_FIRST = re.compile(
    r"(?:得到|获得|领取)[^\d]{0,6}?(" + "|".join(REWARD_KINDS) + r")[^\d]{0,4}?(\d[\d,]*)")


def parse_reward(items: Iterable[Item]) -> list[dict[str, Any]]:
    """Reward lines -> ``[{"kind", "amount", "raw"}, ...]``.

    ::

        >>> parse_reward([{"text": "XIN精灵获得经验：106", "conf": 0.5}])
        [{'kind': '经验', 'amount': 106.0, 'raw': 'XIN精灵获得经验：106'}]

    Two shapes are established by the project's own records:

    * ``XIN精灵获得经验：106`` -- the XP dialog of the live client, read on
      ``artifacts/live/after_3_battles.png`` (Round 4 also confirmed the same
      payout live: "领取经验 106");
    * ``恭喜你得到了 N 个积累经验`` -- the third dialog style recorded in
      ``docs/state.md``.  No screenshot of that style is in ``artifacts/``, so it
      is parsed but untested against a frame.

    Lines that merely *talk* about a quantity are not rewards and are ignored:
    ``离升级还需经验：1152`` (that is what is still missing), ``获得成就`` and
    ``获得胜利，继续加油！`` (no amount).  ``kind`` is the noun as written on
    screen, so a caller can see exactly which payout it got.
    """

    out: list[dict[str, Any]] = []
    for item in items:
        text = text_of(item)
        if not text or not any(verb in text for verb in ("得到", "获得", "领取")):
            continue
        match = _REWARD_AMOUNT_FIRST.search(text)
        if match:
            noun = match.group(2).strip()
            if noun.endswith("经验"):
                out.append({"kind": noun, "amount": float(match.group(1).replace(",", "")),
                            "raw": text})
                continue
            if noun in REWARD_KINDS:
                out.append({"kind": noun, "amount": float(match.group(1).replace(",", "")),
                            "raw": text})
                continue
        match = _REWARD_KIND_FIRST.search(text)
        if match:
            out.append({"kind": match.group(1),
                        "amount": float(match.group(2).replace(",", "")),
                        "raw": text})
    return out


# --------------------------------------------------------------------------- #
# SPT panel
# --------------------------------------------------------------------------- #
_STATE_WORD = re.compile(r"(已击败|未击败)")
_UNLOCK_WORD = re.compile(r"击败后解锁交易行资格|交易行资格")


def _spt_state(text: str) -> bool | None:
    match = _STATE_WORD.search(text)
    if not match:
        return None
    return SPT_DEFEAT_WORDS[match.group(1)]


def _name_above(items: Sequence[Item], anchor: Item | None, *,
                 max_dy: float = 260.0, max_dx: float = 300.0) -> str | None:
    """Nearest name-like caption above ``anchor`` (fallback: first in reading order).

    The SPT panel is a grid of boss cards; each card is name + state word, so the
    name is the caption directly above the state word.  No screenshot of that
    panel exists in ``artifacts/``, so this heuristic is *untested* geometry:
    callers get ``None`` rather than a guess when nothing plausible is above.
    """

    candidates = [item for item in items
                  if _is_name_like(text_of(item)) and "击败" not in text_of(item)
                  and "交易行" not in text_of(item) and "前往" not in text_of(item)
                  and text_of(item) not in ("确认", "关闭", "返回")]
    anchor_box = box_of(anchor) if anchor is not None else None
    if anchor_box is not None:
        best: tuple[float, str] | None = None
        for item in candidates:
            item_box = box_of(item)
            if item_box is None:
                continue
            dx = abs(item_box.cx - anchor_box.cx)
            dy = anchor_box.cy - item_box.cy
            if dx > max_dx or not 0.0 < dy <= max_dy:
                continue
            if best is None or dy < best[0]:
                best = (dy, text_of(item))
        if best is not None:
            return best[1]
    if candidates:
        # no usable geometry: reading order is all we have, and the first caption
        # of a card is its name
        return text_of(candidates[0])
    return None


def parse_spt_card(items: Iterable[Item]) -> dict[str, Any]:
    """One SPT boss card -> ``{"name", "defeated", "reward_unlock"}``.

    Shape recorded in ``docs/state.md``/``docs/progress.md`` (read on the live
    client 2026-11-02, *no screenshot of the panel is stored in the repo*)::

        >>> parse_spt_card([
        ...     {"text": "蘑菇怪", "conf": 0.9, "px": (1000, 690)},
        ...     {"text": "未击败", "conf": 0.9, "px": (1000, 760)},
        ...     {"text": "击败后解锁交易行资格", "conf": 0.8, "px": (1000, 800)},
        ... ])
        {'name': '蘑菇怪', 'defeated': False, 'reward_unlock': '击败后解锁交易行资格'}

    ``defeated`` is ``None`` unless the card actually printed 已击败/未击败 -- a
    card that only lists a reward must not be read as "not defeated".
    ``reward_unlock`` is the raw unlock caption, or ``""`` when the card does not
    show one.  With several cards on screen this returns the first one; use
    :func:`parse_spt_cards` for the whole grid.
    """

    items = list(items)
    state_item: Item | None = None
    defeated: bool | None = None
    unlock = ""
    for item in items:
        text = text_of(item)
        if not unlock and _UNLOCK_WORD.search(text):
            unlock = text
            continue
        if state_item is None:
            state = _spt_state(text)
            if state is not None:
                state_item, defeated = item, state
    name = _name_above(items, state_item)
    return {"name": name or "", "defeated": defeated, "reward_unlock": unlock}


def parse_spt_cards(items: Iterable[Item]) -> list[dict[str, Any]]:
    """Every boss card on the panel, in OCR order.

    Same contract as :func:`parse_spt_card`, one dict per state word.  A card
    without a state word is dropped (position without state is not progress),
    and each card carries the unlock caption nearest to it rather than whichever
    one the OCR happened to print first::

        >>> parse_spt_cards([
        ...     {"text": "蘑菇怪", "conf": 0.9, "px": (700.0, 400.0)},
        ...     {"text": "未击败", "conf": 0.9, "px": (700.0, 470.0)},
        ...     {"text": "雷伊", "conf": 0.9, "px": (1500.0, 400.0)},
        ...     {"text": "已击败", "conf": 0.9, "px": (1500.0, 470.0)},
        ... ])
        [{'name': '蘑菇怪', 'defeated': False, 'reward_unlock': ''}, \
{'name': '雷伊', 'defeated': True, 'reward_unlock': ''}]

    The coordinates above are illustrative (no SPT panel screenshot exists in
    ``artifacts/``); the words are the ones recorded live in ``docs/state.md``.
    """

    items = list(items)
    default_unlock = ""
    for item in items:
        if _UNLOCK_WORD.search(text_of(item)):
            default_unlock = text_of(item)
            break

    def unlock_for(anchor: Item) -> str:
        anchor_box = box_of(anchor)
        if anchor_box is None:
            return default_unlock
        best: tuple[float, str] | None = None
        for other in items:
            text = text_of(other)
            if not _UNLOCK_WORD.search(text):
                continue
            other_box = box_of(other)
            if other_box is None:
                continue
            dx = abs(other_box.cx - anchor_box.cx)
            dy = other_box.cy - anchor_box.cy
            if dx > 300.0 or not -260.0 <= dy <= 260.0:
                continue
            candidate = (abs(dy), text)
            if best is None or candidate[0] < best[0]:
                best = candidate
        return best[1] if best is not None else default_unlock

    cards: list[dict[str, Any]] = []
    for item in items:
        text = text_of(item)
        if _UNLOCK_WORD.search(text):
            continue
        state = _spt_state(text)
        if state is None:
            continue
        name = _name_above([it for it in items if it is not item], item)
        cards.append({"name": name or "", "defeated": state,
                      "reward_unlock": unlock_for(item)})
    return cards


# --------------------------------------------------------------------------- #
# dialog buttons
# --------------------------------------------------------------------------- #
_BUTTON_PUNCT = "，。．.,、！!？?；;：: 　"


def _clean_button(text: str) -> str:
    return text.replace(" ", "").replace("　", "").strip(_BUTTON_PUNCT)


def parse_dialog_buttons(items: Iterable[Item]) -> list[dict[str, Any]]:
    """Confirm / close labels -> ``[{"text", "px", "conf", "kind"}]``.

    Round 4 clicked exactly these words (``live.CONFIRM_WORDS``); the box that
    really was clicked is ``确认`` at (1188, 999) in the XP dialog::

        >>> parse_dialog_buttons([{"text": "确认", "conf": 0.5, "px": (1187.8, 998.8)}])
        [{'text': '确认', 'px': (1187.8, 998.8), 'conf': 0.5, 'kind': 'confirm'}]

    ``关闭`` is reported with ``kind="close"`` because the two mean different
    things to a caller (a close button may be the only way out of a panel).
    Order is the OCR order, so the caller can pick "the first one".
    """

    out: list[dict[str, Any]] = []
    for item in items:
        raw = text_of(item)
        cleaned = _clean_button(raw)
        if not cleaned:
            continue
        kind = None
        for word in DIALOG_CONFIRM_WORDS:
            if cleaned == word or cleaned.endswith(word):
                kind = "confirm"
                break
        if kind is None:
            for word in DIALOG_CLOSE_WORDS:
                if cleaned == word or cleaned.endswith(word):
                    kind = "close"
                    break
        if kind is None:
            continue
        px = item.get("px")
        out.append({
            "text": raw,
            "px": tuple(px) if isinstance(px, (tuple, list)) and len(px) == 2 else None,
            "conf": conf_of(item),
            "kind": kind,
        })
    return out


__all__ = [
    "LIVE_FRAME_PX",
    "SAME_LINE_PX",
    "SIDE_MIN_DX",
    "SIDE_MAX_DX",
    "WALLET_MAX_DX",
    "PP_NAME_DX",
    "PP_NAME_DY",
    "ALLY_MAX_CX",
    "HP_X_BAND",
    "DIALOG_CONFIRM_WORDS",
    "DIALOG_CLOSE_WORDS",
    "CURRENCY_LABELS",
    "REWARD_KINDS",
    "SPT_DEFEAT_WORDS",
    "SPT_UNLOCK_LINE",
    "Box",
    "text_of",
    "conf_of",
    "box_of",
    "parse_turn",
    "parse_hp",
    "parse_pp",
    "parse_pet_name",
    "parse_pet_names",
    "parse_level",
    "parse_currency",
    "parse_reward",
    "parse_spt_card",
    "parse_spt_cards",
    "parse_dialog_buttons",
]
