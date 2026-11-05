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


# --------------------------------------------------------------------------- #
# 谱尼真身: which life are we on?
# --------------------------------------------------------------------------- #
# The client prints the life as a large centre overlay:
#
#     谱尼真身
#     第一命
#     （虚无封印）
#
# Measured on the 104 truebody frames in ``frames/_ocr.json`` (2026-08-23 video):
# the "第N命" line survives OCR as 第一命/第二命/第三命/第四命/第五命/第六命, and
# the seal line survives as （虚无封印）/（元素封印）/（能量封印）/（生命封印）/
# （轮回封印）.  Two frames lose the numeral entirely ("第￥命", "第 命") -- those
# must return None, because guessing a life number is exactly the kind of invented
# fact this module forbids.
#
# The seal names are only a *check*: the sixth life has no seal ("第六命无对应"),
# so a missing seal is normal and must not be read as "unknown life".
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: `第一命`, `第五条命`, `第 3 命` -- `条` is optional and the numeral may be a digit.
TRUEBODY_LIFE_RE = re.compile(r"第\s*([一二三四五六七八九十\d]{1,2})\s*条?\s*命")
#: `（虚无封印）`, `元素封印`, with tolerant brackets.
TRUEBODY_SEAL_RE = re.compile(r"[（(]?\s*([\u4e00-\u9fa5]{2})\s*封印\s*[）)]?")

#: The seven seals, in the order the client presents them.
TRUEBODY_SEALS = ("虚无", "元素", "能量", "生命", "轮回", "永恒", "圣洁")

#: A truebody has six lives (docs/research/PUNI_CURRENT_VERSION_GUIDE.md §6).
#: Anything outside 1..6 is a misread, not a seventh life.
TRUEBODY_MAX_LIFE = 6


def _numeral(token: str) -> int | None:
    """`五` -> 5, `5` -> 5, anything else -> None."""

    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        value = int(token)
    else:
        value = _CN_DIGITS.get(token)
    if value is None:
        return None
    return value if 1 <= value <= TRUEBODY_MAX_LIFE else None


def _in_truebody_band(item: Item) -> bool:
    """True when the box sits where the client draws the life overlay.

    The overlay is mid-screen.  The battle log is bottom-left and the command menu
    bottom-right; a whole-frame regex would happily read 「致命一击」 or
    「命中率」 from those, which is why the band exists.
    """

    box = box_of(item)
    if box is None:
        return False
    fw, fh = LIVE_FRAME_PX
    fx, fy = box.cx / fw, box.cy / fh
    return 0.30 <= fx <= 0.75 and 0.15 <= fy <= 0.60


def parse_truebody_life(items: Iterable[Item]) -> dict[str, Any] | None:
    """Read the truebody life overlay.

    Returns ``{"life": int, "seal": str | None, "text": str}`` or ``None`` when the
    overlay is not on screen.  ``None`` means "not observed", never "life 0".

    Only the *centre* overlay is trusted: the battle log on the left also mentions
    命 (「命中率」「致命一击」), so a naive whole-frame regex would match those.  The
    centre band is where the client draws this overlay on every truebody frame.
    """

    items = list(items)
    centre = [it for it in items if _in_truebody_band(it)]
    # The life line and the seal line are separate OCR boxes on every frame
    # measured (「第一命」 / 「（虚无封印）」), so the seal is looked up across the
    # whole centre band rather than only inside the life box.
    band_seal = parse_truebody_seal(centre)

    best: dict[str, Any] | None = None
    for item in centre:
        text = text_of(item)
        if not text:
            continue
        match = TRUEBODY_LIFE_RE.search(text)
        if not match:
            continue
        life = _numeral(match.group(1))
        if life is None:
            continue
        seal_match = TRUEBODY_SEAL_RE.search(text)
        seal = seal_match.group(1) if seal_match else None
        if seal is not None and seal not in TRUEBODY_SEALS:
            seal = None
        seal = seal or band_seal
        candidate = {"life": life, "seal": seal, "text": text[:60]}
        # Prefer a reading that also carries the seal name.
        if best is None or (candidate["seal"] and not best["seal"]):
            best = candidate
    return best


def parse_truebody_seal(items: Iterable[Item]) -> str | None:
    """The seal named next to the life overlay, if it survived OCR.

    The sixth life has no seal, so ``None`` here is normal -- callers must not
    treat it as an error.
    """

    for item in items:
        text = text_of(item)
        if not text:
            continue
        if not _in_truebody_band(item):
            continue
        match = TRUEBODY_SEAL_RE.search(text)
        if match and match.group(1) in TRUEBODY_SEALS:
            return match.group(1)
    return None



# --------------------------------------------------------------------------- #
# 当前战报: the only structured event stream a battle gives us
# --------------------------------------------------------------------------- #
# Measured on `frames/_ocr.json` (all 417 frames): the battle log occupies
# x < 0.25 / y < 0.21 (normalised, origin bottom-left).  The video's burned-in
# subtitles sit at x >= 0.34 on the same rows, so a whole-frame keyword search
# would read narration as combat.  The two regions do not overlap.
BATTLE_LOG_X_MAX = 0.30
#: Fraction from the TOP of the frame; the log panel starts around 0.75.
BATTLE_LOG_Y_MIN = 0.72

#: Ordered: the first matching kind wins (a line can mention 使用了 and 圣光气).
BATTLE_EVENT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defeated", ("被击败",)),
    ("crit", ("致命一击",)),
    ("holy_light", ("圣光气",)),
    ("entered", ("登场",)),
    ("used", ("使用了",)),
)


def _in_battle_log(item: Item) -> bool:
    """True when the box is inside the bottom-left battle-log panel.

    ``Box.cy`` is measured **from the top**, so the log (which is at the bottom of
    the screen) sits at a *high* fy, not a low one.  Getting this backwards makes
    the parser silently find nothing at all.
    """

    box = box_of(item)
    if box is None:
        return False
    fw, fh = LIVE_FRAME_PX
    fx, fy = box.cx / fw, box.cy / fh
    return fx < BATTLE_LOG_X_MAX and fy > BATTLE_LOG_Y_MIN


def parse_battle_log_events(items: Iterable[Item]) -> list[dict[str, Any]]:
    """Battle-log lines as ``{"kind", "text"}``, in the order they are drawn.

    Kinds: ``defeated`` (someone was defeated), ``crit`` (致命一击), ``holy_light``
    (the enemy spent 圣光气), ``entered`` (a pet came in), ``used`` (a move was used).

    Ordering is *as drawn, top to bottom* -- it is not asserted to be recency
    order, because nothing in the recordings establishes which end is newest.
    """

    out: list[dict[str, Any]] = []
    for item in items:
        if not _in_battle_log(item):
            continue
        text = text_of(item)
        if not text:
            continue
        for kind, words in BATTLE_EVENT_WORDS:
            if any(word in text for word in words):
                out.append({"kind": kind, "text": text[:80]})
                break
    return out


def battle_log_has(items: Iterable[Item], kind: str) -> bool:
    """True when the visible log contains at least one event of ``kind``."""

    return any(event["kind"] == kind for event in parse_battle_log_events(items))


def enemy_defeated(items: Iterable[Item]) -> bool:
    """A defeat line is on screen.  It does **not** say who was defeated.

    The client prints 「【X】被击败了」 for both sides, so the caller must combine
    this with its own knowledge of the scene; treating any defeat as a win is how a
    run reports a victory it did not have.
    """

    return battle_log_has(items, "defeated")


def holy_light_seen(items: Iterable[Item]) -> bool:
    """谱尼 spent 圣光气 -- the documented marker that its PP is exhausted.

    This is the *only* machine-readable "the enemy is out of PP" signal: the enemy
    HP/PP bar carries no numbers at all.
    """

    return battle_log_has(items, "holy_light")


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
    "TRUEBODY_LIFE_RE",
    "TRUEBODY_SEAL_RE",
    "TRUEBODY_SEALS",
    "TRUEBODY_MAX_LIFE",
    "parse_truebody_life",
    "parse_truebody_seal",
    "_in_truebody_band",
    "BATTLE_LOG_X_MAX",
    "BATTLE_LOG_Y_MIN",
    "BATTLE_EVENT_WORDS",
    "parse_battle_log_events",
    "battle_log_has",
    "enemy_defeated",
    "holy_light_seen",
]


# --------------------------------------------------------------------------- #
# 经验分配器 (exp allocator)
# --------------------------------------------------------------------------- #
#: The panel is the only route this account has to turn the 积累经验 pool into
#: levels without fighting, so its numbers are worth reading precisely.
#: Transcribed from `frames/02_spirit_day1/f_00019.png` (video 1280x720) with a
#: read-only `seerdx ocr`; every label below appears verbatim in that frame:
#:
#:     经验分配器 / 序号：008 / 名字：烈火猴 / 等级：17 / 性格：孤独
#:     升级所需经验值：316 / 该精灵增加 (500) 点经验值 / 积累经验：18744927
#:     确认
#:
#: The numeric values are separate OCR boxes from their captions, so each one is
#: paired by geometry, and a caption with no adjacent number is `None` rather
#: than 0 (rule 1 of this module).
_EXP_FIELD_LABELS = {
    "序号": "index",
    "名字": "name",
    "等级": "level",
    "性格": "nature",
    "升级所需经验值": "xp_to_next",
    "积累经验": "pool",
}
_EXP_PANEL_TITLE = "经验分配器"
#: A number that belongs to a caption sits on the same line or just below it.
#: Measured on f_00019: 升级所需经验值 and 316 are ~40 px apart at 2400x1600
#: scale, while the next unrelated number (the input) is ~180 px away.
EXP_FIELD_DY = 70.0
EXP_FIELD_DX = 400.0
_EXP_NUMBER = re.compile(r"^\d[\d,]*$")


def _nearest_number(items: Sequence[Item], label: Box, *, used: set[int],
                    max_dx: float = EXP_FIELD_DX,
                    max_dy: float = EXP_FIELD_DY) -> tuple[float | None, int | None]:
    best: tuple[float, float, int] | None = None
    for i, item in enumerate(items):
        if i in used:
            continue
        text = text_of(item)
        if not _EXP_NUMBER.match(text.replace(" ", "")):
            continue
        box = box_of(item)
        if box is None:
            continue
        dy = abs(box.cy - label.cy)
        dx = box.cx - label.cx
        if dy > max_dy or dx < 0 or dx > max_dx:
            continue
        score = dy * 1000.0 + dx
        if best is None or score < best[0]:
            best = (score, float(text.replace(",", "")), i)
    if best is None:
        return None, None
    return best[1], best[2]


def parse_exp_allocator(items: Iterable[Item]) -> dict[str, Any] | None:
    """Read the 经验分配器 panel, or ``None`` when it is not on screen.

    Returns ``{"index", "name", "level", "nature", "xp_to_next", "pool",
    "title_seen"}``.  Every numeric field is ``None`` unless a number was actually
    adjacent to its caption, because "the OCR dropped it" and "it is zero" are
    different statements about an account.

    The panel is identified by its title; a bag detail panel that merely contains
    「等级」 is not this panel and must not be parsed as one.
    """

    items = list(items)
    if not any(_EXP_PANEL_TITLE in text_of(i) for i in items):
        return None
    out: dict[str, Any] = {name: None for name in _EXP_FIELD_LABELS.values()}
    out["title_seen"] = True
    used: set[int] = set()
    for i, item in enumerate(items):
        text = text_of(item)
        for label, field in _EXP_FIELD_LABELS.items():
            # the caption is matched as a prefix so "等级：17" and a bare "等级" both work
            if not text.startswith(label):
                continue
            inline = text[len(label):]
            if inline.startswith(("：", ":")):
                inline = inline[1:]
            inline = inline.strip()
            if field in ("name", "nature"):
                # these are words, not numbers -- they sit on the caption line
                if inline:
                    out[field] = inline
                continue
            if inline and _EXP_NUMBER.match(inline.replace(" ", "")):
                out[field] = float(inline.replace(",", ""))
                used.add(i)
                continue
            label_box = box_of(item)
            if label_box is None:
                continue
            value, index = _nearest_number(items, label_box, used=used)
            if value is not None and index is not None:
                out[field] = value
                used.add(index)
            break
    # ``该精灵增加 ___ 点经验值`` is the amount that would be poured.  In the
    # recorded frame the value is its own box ("500g", an OCR misread of 500)
    # sitting between the caption and 「点经验值」 on the same line, so fall back
    # to geometry: nearest number to the right, on that line.
    for item in items:
        text = text_of(item)
        if "该精灵增加" not in text:
            continue
        leftover = text.split("该精灵增加", 1)[1]
        match = re.search(r"(\d[\d,]*)", leftover)
        if match:
            out["pending_amount"] = float(match.group(1).replace(",", ""))
            break
        label_box = box_of(item)
        if label_box is None:
            continue
        best: tuple[float, float] | None = None
        for other in items:
            digits = re.match(r"^(\d[\d,]*)\D{0,2}$", text_of(other))
            if not digits:
                continue
            box = box_of(other)
            if box is None:
                continue
            dy = abs(box.cy - label_box.cy)
            dx = box.cx - label_box.cx
            if dy > 30.0 or dx <= 0 or dx > EXP_FIELD_DX:
                continue
            if best is None or dx < best[0]:
                best = (dx, float(digits.group(1).replace(",", "")))
        if best is not None:
            out["pending_amount"] = best[1]
        break
    return out


__all__ += ["parse_exp_allocator"]
