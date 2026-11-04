"""Observe skills: parser truth tables, real-screenshot fixtures, fake-session tests.

Fixture provenance
==================
``artifacts/observe/`` holds byte-identical COPIES of frames recorded earlier.  The
originals were not touched, no live capture was taken while these tests were
written, and nothing here starts the game or the capture daemon.

``battle_turn1_2400x1600.png``   <- ``artifacts/live/battle_turn1_2400x1600.png``
    2400x1600 live battle, turn 1.  Shows ``LV: 33`` (ally), ``92/92``, ``当前回合：1``,
    ``LV: 10`` (enemy), ``蘑菇怪``, the 战斗/道具/精灵/逃跑 buttons.
``victory_moguguai_2400x1600.png`` <- ``artifacts/live/victory_moguguai_2400x1600.png``
    2400x1600 live victory.  ``89/92``, ``当前回合：3``, 胜利 + 确认, and two
    ``使用次数：a/b`` cells that the OCR read as ``使用饮数：24/25`` /
    ``使用沉数：20/20`` (a third, 31/35, is on screen but covered by the 获得成就
    overlay, so it is not part of the transcription).
``after_3_battles.png``          <- ``artifacts/live/after_3_battles.png``
    2400x1600 XP dialog: ``烈火猴`` / ``XIN精灵获得经验：106`` / ``离升级还需经验：1152``.
``shop.png``                     <- ``artifacts/shop.png``  (1200x746, 2026-10-31)
    Shop wallet header ``XIN豆 5,000`` and ``金豆`` whose value the OCR dropped.
``petpanel.png``                 <- ``artifacts/petpanel.png``  (1030x833, 2026-10-19)
    Pet detail panel: ``名字：小火猴`` / ``级：5`` / ``PP:30/30``.  HISTORICAL: that pet
    has since evolved into 烈火猴, so this frame proves the panel's *shape* only.

Text fixtures
=============
Every ``_item(...)`` below is verbatim ``seerdx`` file-OCR output for the PNG above
(``{"op":"ocr","path":<file already on disk>,"min":0.3}`` -- a read-only file OCR that
never touches the game), reduced to the boxes the parsers need.  ``px`` is the
game-px centre computed exactly the way ``LiveSession.ocr`` computes it and ``norm``
is the raw Vision-normalised box.  Strings are left as the engine returned them:
misreads such as ``LV833`` for ``LV:33``, ``使用饮数`` for ``使用次数`` and ``蘑菇译``
for ``蘑菇怪`` are part of the evidence, so they are not prettified.

Three consequences are asserted below:
  * the ally badge really is ``LV: 33`` with the colon OCR'd as ``8``;
  * the victory frame's enemy name really comes back as ``蘑菇译`` (one-character
    misread) and the parser preserves it instead of "fixing" it;
  * ``金豆`` is visible in the shop header with no value, so the currency parser
    must not report a number for it.

The SPT panel has no screenshot in this repo (``NOT VERIFIED``), so those boxes are
transcribed from the *text* recorded live in ``docs/state.md``; their positions are
illustrative and no test claims they are measurements.

OCR needs the daemon, so the skill tests drive a ``FakeSession``.  The one opt-in
test (``test_live_ocr_of_fixture_*``) is skipped unless
``SEERAUTO_LIVE_OCR_FIXTURES=1``, and even then it only reads the PNG on disk.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from seerauto.skills import parsers as P
from seerauto.skills.base import (
    Budget,
    SkillContext,
    SkillError,
    SkillKind,
    SkillRegistry,
    Status,
)
from seerauto.skills.observe import (
    CONF_SHAPE_REPAIRED,
    CONF_SHAPE_VERIFIED,
    OCR_MIN_CONF,
    SCENE_TEMPLATE_ORIGIN,
    GetStatus,
    build_observe_skills,
)
from seerauto.world.model import Evidence, GameWorldState, Scene, Source, empty_world

ROOT = Path(__file__).resolve().parents[1]
OBSERVE_DIR = ROOT / "artifacts" / "observe"
SEERDX = ROOT / "native" / "bin" / "seerdx"

#: name -> (width, height) of every copied fixture, asserted below so a stray
#: resize or a placeholder image cannot silently pass as evidence.
FIXTURE_IMAGES = {
    "battle_turn1_2400x1600.png": (2400, 1600),
    "victory_moguguai_2400x1600.png": (2400, 1600),
    "after_3_battles.png": (2400, 1600),
    "shop.png": (1200, 746),
    "petpanel.png": (1030, 833),
}


def _item(text: str, conf: float, px: tuple[float, float],
          norm: tuple[float, float, float, float] | None) -> dict:
    """One OCR box in the shape ``LiveSession.ocr()`` returns."""

    item = {"text": text, "conf": conf, "px": px}
    if norm is not None:
        item["norm"] = norm
    return item


def _pick(items: list[dict], *texts: str) -> list[dict]:
    """Sub-select transcribed boxes by text, keeping their real positions."""

    wanted = list(texts)
    picked = []
    for item in items:
        if item["text"] in wanted:
            picked.append(item)
            wanted.remove(item["text"])
    assert not wanted, f"fixture text(s) not found: {wanted}"
    return picked


BATTLE_ITEMS = [
    _item('LV833', 0.30000001192092896, (556.4, 261.4), (0.20346, 0.82554, 0.05674, 0.02212)),
    _item('92/92', 1, (816.3, 256.3), (0.31541, 0.82563, 0.04942, 0.02841)),
    _item('hello', 1, (470.9, 369.5), (0.18314, 0.7625, 0.02616, 0.01312)),
    _item('当前5报', 0.30000001192092896, (505.8, 1110.2), (0.18455, 0.29388, 0.05242, 0.02444)),
    _item('我方【烈火猴】（3）登场了！', 0.30000001192092896, (371.5, 1159.0), (0.08285, 0.26562, 0.1439, 0.02)),
    _item('对方【蘑菇怪】登场了！', 0.5, (352.3, 1195.5), (0.08285, 0.24183, 0.12791, 0.02192)),
    _item('xin计划（1.0.8.9）', 0.5, (1196.5, 29.6), (0.45494, 0.97168, 0.08721, 0.01961)),
    _item('当前回合：', 1, (1163.4, 435.7), (0.43884, 0.71162, 0.0918, 0.0321)),
    _item('1', 0.5, (1304.7, 437.5), (0.53488, 0.71242, 0.01744, 0.02832)),
    _item('LV810', 0.5, (1819.2, 261.5), (0.73256, 0.82562, 0.05087, 0.02187)),
    _item('蘑菇怪', 1, (1932.6, 373.0), (0.78779, 0.75812, 0.03488, 0.0175)),
    _item('战斗', 0.30000001192092896, (1838.4, 1190.4), (0.74273, 0.23747, 0.04651, 0.03704)),
    _item('精灵', 1, (1838.4, 1324.9), (0.74273, 0.15437, 0.04651, 0.03517)),
    _item('道具', 0.5, (2014.5, 1190.4), (0.81686, 0.23747, 0.04506, 0.03704)),
    _item('逃跑', 1, (2012.8, 1323.0), (0.81541, 0.15438, 0.04651, 0.0375)),
]

VICTORY_ITEMS = [
    _item('xin计划（1.0.8.9）', 0.5, (1196.5, 29.6), (0.45494, 0.97168, 0.08721, 0.01961)),
    _item('|公屏聊天 •在线', 0.30000001192092896, (279.1, 261.2), (0.07558, 0.82789, 0.0814, 0.01774)),
    _item('系统：尊享入场| hello 登录了', 0.5, (336.6, 299.9), (0.07849, 0.80375, 0.12355, 0.0176)),
    _item('89/92', 1, (816.3, 256.2), (0.31532, 0.82536, 0.04959, 0.02901)),
    _item('LV810', 0.30000001192092896, (1819.2, 263.2), (0.73253, 0.82556, 0.05092, 0.01991)),
    _item('hello', 0.5, (469.2, 369.5), (0.18314, 0.7625, 0.02471, 0.01312)),
    _item('蘑菇怪', 0.30000001192092896, (1932.6, 373.0), (0.78779, 0.75812, 0.03488, 0.0175)),
    _item('当前回合：', 0.5, (1159.9, 435.6), (0.43895, 0.71242, 0.08866, 0.03071)),
    _item('3', 1, (1315.1, 435.7), (0.5407, 0.71242, 0.01453, 0.0305)),
    _item('胜利', 1, (1196.5, 723.2), (0.46657, 0.52288, 0.06395, 0.05025)),
    _item('恭喜你！在这场战斗中', 1, (1194.8, 831.4), (0.4157, 0.46623, 0.16424, 0.02832)),
    _item('获得胜利，继续加油！', 0.5, (1186.0, 880.2), (0.4157, 0.43355, 0.15698, 0.0327)),
    _item('确认', 0.30000001192092896, (1193.0, 997.0), (0.47965, 0.36375, 0.03488, 0.02625)),
    _item('当前战报', 0.5, (509.3, 1110.2), (0.18587, 0.29322, 0.05268, 0.02576)),
    _item('我方【烈火猴】（2）登场了！', 0.5, (373.3, 1160.6), (0.08285, 0.26362, 0.14535, 0.02201)),
    _item('对方', 0.5, (230.2, 1197.4), (0.08285, 0.24183, 0.02616, 0.01961)),
    _item('【蘑菇译】登场了！', 0.30000001192092896, (382.0, 1197.4), (0.1061, 0.24183, 0.1061, 0.01961)),
    _item('【烈火猴】使用了抓，', 0.5, (341.9, 1234.0), (0.08721, 0.21786, 0.11047, 0.02179)),
    _item('『状态】：正常', 0.30000001192092896, (584.3, 1234.0), (0.20785, 0.21786, 0.07122, 0.02179)),
    _item('蘑菇怪】-使用了针刺，【状态】：正常', 0.30000001192092896, (462.2, 1272.5), (0.09448, 0.19375, 0.19622, 0.02194)),
    _item('【烈火猴】使用了抓，【状态］：正常', 0.5, (443.0, 1308.9), (0.08866, 0.16993, 0.19186, 0.02397)),
    _item('［蘑菇慅】', 0.30000001192092896, (272.1, 1343.7), (0.08866, 0.15033, 0.04942, 0.01967)),
    _item('［状态】；正常', 0.30000001192092896, (437.8, 1345.5), (0.14672, 0.14756, 0.07138, 0.02296)),
    _item('［蘑菇译】被击败了！', 0.30000001192092896, (348.8, 1382.0), (0.09012, 0.12625, 0.11047, 0.02)),
    _item('火𤠣', 0.30000001192092896, (995.9, 1192.1), (0.40407, 0.24619, 0.0218, 0.01756)),
    _item('获得成就', 0.5, (1203.5, 1152.1), (0.47382, 0.26787, 0.05527, 0.02416)),
    _item('击败蘑菇怿', 0.5, (1198.3, 1225.3), (0.4665, 0.22387, 0.06555, 0.02067)),
    _item('眼', 1, (1411.0, 1197.4), (0.57994, 0.23965, 0.01599, 0.02397)),
    _item('E用FRE', 0.30000001192092896, (1411.0, 1246.2), (0.57267, 0.21351, 0.03052, 0.01525)),
    _item('道具', 0.5, (2014.5, 1190.4), (0.81686, 0.23747, 0.04506, 0.03704)),
    _item('精灵', 1, (1840.1, 1324.9), (0.74419, 0.15438, 0.04506, 0.03517)),
    _item('逃跑', 1, (2012.8, 1323.0), (0.81541, 0.15438, 0.04651, 0.0375)),
    _item('使用饮数：24/25', 0.30000001192092896, (1203.5, 1368.3), (0.4593, 0.135, 0.0843, 0.01968)),
    _item('使用沉数：20/20', 0.30000001192092896, (1458.1, 1368.3), (0.56541, 0.135, 0.0843, 0.01968)),
]

XP_ITEMS = [
    _item('公屏聊天 • 在线', 0.30000001192092896, (280.8, 261.2), (0.07558, 0.82789, 0.08285, 0.01774)),
    _item('15', 0.5, (444.8, 258.0), (0.17733, 0.82789, 0.01599, 0.02179)),
    _item('系统：尊享入场|hello 登录了', 0.5, (336.6, 299.9), (0.07849, 0.80375, 0.12355, 0.0176)),
    _item('目', 0.30000001192092896, (467.4, 1364.7), (0.17297, 0.122, 0.0436, 0.05011)),
    _item('xin计划（1.0.8.9）', 0.5, (1196.5, 29.6), (0.45494, 0.97168, 0.08721, 0.01961)),
    _item('烈火猴', 0.5, (1198.3, 667.5), (0.47965, 0.57298, 0.03924, 0.01961)),
    _item('XIN精灵获得经验：106', 0.5, (1180.8, 895.6), (0.42442, 0.42919, 0.13517, 0.02206)),
    _item('离升级还需经验：', 1, (1149.4, 934.4), (0.42733, 0.405, 0.1032, 0.02202)),
    _item('1152', 1, (1304.7, 934.1), (0.52907, 0.40741, 0.02907, 0.01759)),
    _item('确认', 0.5, (1187.8, 998.8), (0.47529, 0.35937, 0.03924, 0.03278)),
    _item('万圣活动', 0.5, (1613.4, 360.7), (0.6468, 0.76471, 0.05087, 0.01967)),
    _item('SET', 0.30000001192092896, (2150.6, 347.0), (0.88081, 0.77312, 0.03052, 0.02)),
    _item('「11:43', 0.30000001192092896, (2159.3, 1349.0), (0.88517, 0.14812, 0.02907, 0.0175)),
]

SHOP_ITEMS = [
    _item('XIN豆', 0.30000001192092896, (145.0, 170.0), (0.10667, 0.76408, 0.02833, 0.01609)),
    _item('5,000', 1, (241.0, 170.0), (0.18667, 0.76408, 0.02833, 0.01609)),
    _item('金豆', 1, (298.0, 170.0), (0.23833, 0.76408, 0.02, 0.01609)),
    _item('XIN豆商城', 0.5, (177.0, 282.0), (0.1233, 0.61371, 0.04839, 0.01656)),
    _item('库存 999+', 0.5, (170.0, 318.0), (0.11823, 0.56484, 0.04688, 0.01776)),
    _item('0 400 XIN豆', 0.30000001192092896, (128.0, 346.0), (0.07667, 0.52547, 0.06, 0.02145)),
    _item('金豆商城', 1, (171.0, 420.0), (0.12165, 0.42619, 0.0417, 0.02161)),
    _item('库存 999+', 0.5, (170.0, 453.0), (0.11831, 0.38323, 0.04671, 0.01905)),
    _item('6 20金豆', 0.30000001192092896, (120.0, 483.0), (0.075, 0.34048, 0.05, 0.02413)),
    _item('库存 999+', 0.5, (170.0, 591.0), (0.11832, 0.19832, 0.04669, 0.01891)),
    _item('排位勋章', 1, (465.0, 170.0), (0.36667, 0.76139, 0.04167, 0.02145)),
    _item('感谢信', 0.5, (616.0, 171.0), (0.49667, 0.76139, 0.03333, 0.01877)),
    _item('金币-感谢币', 0.30000001192092896, (788.0, 170.0), (0.62998, 0.76393, 0.05337, 0.01637)),
    _item('XIN豆商城', 1, (438.0, 208.0), (0.33993, 0.71256, 0.05014, 0.01725)),
    _item('金豆商城', 1, (572.0, 208.0), (0.45495, 0.71276, 0.04344, 0.01683)),
    _item('⑤ 1XIN豆', 0.30000001192092896, (382.0, 346.0), (0.29333, 0.52547, 0.05, 0.02145)),
    _item('XIN豆商城', 1, (700.0, 283.0), (0.5599, 0.61063, 0.04686, 0.02004)),
    _item('库存 999+', 0.5, (693.0, 318.0), (0.555, 0.56568, 0.045, 0.01609)),
    _item('库存 999+', 0.5, (431.0, 453.0), (0.33666, 0.38331, 0.04502, 0.0189)),
    _item('库存 999+', 0.5, (693.0, 453.0), (0.55497, 0.38317, 0.04507, 0.01918)),
    _item('⑥2感谢信', 0.30000001192092896, (644.0, 483.0), (0.50984, 0.33951, 0.05366, 0.02606)),
    _item('金豆商城', 1, (433.0, 555.0), (0.34, 0.24665, 0.04167, 0.01877)),
    _item('库存 999+', 0.5, (431.0, 591.0), (0.33665, 0.19826, 0.04504, 0.01904)),
    _item('⑤6金豆', 0.30000001192092896, (378.0, 618.0), (0.29333, 0.16086, 0.04333, 0.02145)),
    _item('CDK兑换', 1, (1029.0, 171.0), (0.83, 0.75871, 0.055, 0.02413)),
    _item('XIN豆商城', 1, (961.0, 283.0), (0.77664, 0.61111, 0.04838, 0.01907)),
    _item('库存 999+', 0.5, (954.0, 318.0), (0.77162, 0.56536, 0.04675, 0.01673)),
    _item('6 2W XIN豆', 0.30000001192092896, (910.0, 346.0), (0.72991, 0.52484, 0.05684, 0.0227)),
    _item('库存 999+', 0.5, (954.0, 454.0), (0.77164, 0.38317, 0.04672, 0.0165)),
]

PET_PANEL_ITEMS = [
    _item('您还没有加入战队', 1, (125.0, 371.6), (0.02524, 0.53717, 0.19223, 0.03357)),
    _item('确认', 0.5, (118.0, 513.4), (0.0835, 0.36211, 0.06214, 0.04317)),
    _item('序号：007', 0.5, (433.0, 132.8), (0.37087, 0.82494, 0.09903, 0.03118)),
    _item('名字：小火猴', 0.5, (448.5, 168.7), (0.3707, 0.77874, 0.12944, 0.0374)),
    _item('级：5', 0.5, (431.0, 207.8), (0.39806, 0.73621, 0.04078, 0.02878)),
    _item('个体：21', 1, (545.0, 206.8), (0.4835, 0.73621, 0.09126, 0.03118)),
    _item('级所需经验值：0', 0.5, (494.0, 244.7), (0.4, 0.69065, 0.15922, 0.03118)),
    _item('体力：20', 0.30000001192092896, (502.0, 451.5), (0.4466, 0.44125, 0.08155, 0.03357)),
    _item('瞪眼', 1, (458.0, 502.4), (0.41942, 0.38369, 0.05049, 0.02638)),
    _item('威力：0', 0.5, (463.0, 537.4), (0.41942, 0.34053, 0.06019, 0.02878)),
    _item('PP:30/30', 1, (578.0, 539.4), (0.52229, 0.3403, 0.07776, 0.02443)),
]

#: ``docs/state.md``/``docs/progress.md``: every SPT card prints a state word and
#: ``击败后解锁交易行资格``.  NO SCREENSHOT OF THAT PANEL EXISTS -- positions below are
#: illustrative layout, not measurements; the text is what was recorded live.
SPT_PANEL_ITEMS = [
    _item("SPT 首领", 0.9, (1200.0, 120.0), (0.4375, 0.9000, 0.1200, 0.0300)),
    _item("蘑菇怪", 0.9, (700.0, 400.0), (0.2500, 0.7400, 0.0700, 0.0250)),
    _item("未击败", 0.9, (700.0, 470.0), (0.2500, 0.6900, 0.0700, 0.0250)),
    _item("击败后解锁交易行资格", 0.8, (700.0, 505.0), (0.2400, 0.6600, 0.1600, 0.0250)),
    _item("雷伊", 0.9, (1500.0, 400.0), (0.5900, 0.7400, 0.0600, 0.0250)),
    _item("已击败", 0.9, (1500.0, 470.0), (0.5900, 0.6900, 0.0700, 0.0250)),
    _item("击败后解锁交易行资格", 0.8, (1500.0, 505.0), (0.5700, 0.6600, 0.1600, 0.0250)),
    _item("前往", 0.9, (700.0, 560.0), (0.2700, 0.6300, 0.0400, 0.0200)),
]


# --------------------------------------------------------------------------- #
# fake session (duck-typed LiveSession, no daemon)
# --------------------------------------------------------------------------- #
class _Probe:
    """Attribute-style probe, like the real ``seerauto.live.SceneProbe``."""

    def __init__(self, scene: Scene, scores: dict[str, float], points: dict[str, tuple]) -> None:
        self.scene = scene
        self.scores = scores
        self.points = points
        self.frame_seq = 0


class FakeSession:
    """The only ``LiveSession`` surface an observe skill is allowed to touch.

    ``scene()`` / ``ocr()`` / ``latest()`` / ``pixel_size`` / ``describe()`` -- all of
    them recorded, so a test can prove that e.g. ``game.get_status`` never OCRs.
    """

    def __init__(self, *, scene: str = "BATTLE", scores: dict[str, float] | None = None,
                 items: list[dict] | None = None, window: dict | None = None,
                 frontmost: bool | None = True,
                 probe: _Probe | None = None) -> None:
        self._scene = scene
        self._scores = dict(scores or {"fight": 1.0, "victory": 0.0, "confirm": 0.0})
        self._items = [dict(item) for item in (items or [])]
        self._window = dict(window if window is not None else
                            {"id": 78351, "x": 270.0, "y": 87.0, "w": 1200.0, "h": 800.0})
        self._frontmost = frontmost
        self._probe = probe
        self.scene_calls = 0
        self.ocr_calls = 0
        self.ocr_min_conf: list[float] = []
        self.previous: Scene | None = None
        self.pixel_size = (2400.0, 1600.0)

    # -- observation surface ------------------------------------------------ #
    def scene(self, frame: object = None, *, previous: Scene | None = None) -> object:
        self.scene_calls += 1
        self.previous = previous
        if self._probe is not None:
            return self._probe
        return {"scene": self._scene, "scores": dict(self._scores),
                "points": {"fight": (1870.0, 1198.0)}}

    def ocr(self, *, min_conf: float = 0.3, path: str | None = None,
            frame: object = None) -> list[dict]:
        self.ocr_calls += 1
        self.ocr_min_conf.append(min_conf)
        return [dict(item) for item in self._items]

    def latest(self) -> object:
        return self._items[0].get("norm") if self._items else None

    def describe(self) -> dict:
        return {"opened": True, "window": dict(self._window),
                "pixel_size": self.pixel_size, "dry_run": True, "allow_input": False}

    def ensure_frontmost(self) -> bool | None:
        return self._frontmost


def _ctx(session: object, world: GameWorldState | None = None) -> SkillContext:
    return SkillContext(
        session=session,
        world=empty_world("test") if world is None else world,
        budget=Budget(max_duration_s=60.0, max_actions=0),
        dry_run=True,
        allow_input=False,
    )


@pytest.fixture()
def registry() -> SkillRegistry:
    return SkillRegistry(build_observe_skills())


def _world(result) -> GameWorldState:
    return GameWorldState.from_dict(result.data["world"])


# --------------------------------------------------------------------------- #
# fixture provenance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(FIXTURE_IMAGES))
def test_fixture_image_is_a_real_screenshot_copy(name: str) -> None:
    path = OBSERVE_DIR / name
    assert path.exists(), f"{path} is missing: fixtures must be copied from artifacts/"
    cv2 = pytest.importorskip("cv2")
    image = cv2.imread(str(path))
    assert image is not None, f"{path} is not a readable image"
    height, width = image.shape[:2]
    assert (width, height) == FIXTURE_IMAGES[name]


@pytest.mark.parametrize("items", [BATTLE_ITEMS, VICTORY_ITEMS, XP_ITEMS])
def test_transcribed_px_matches_norm_for_the_live_frames(items: list[dict]) -> None:
    """Guard against a hand-edited coordinate: px must be norm * (2400, 1600).

    ``norm`` is transcribed to 5 decimals, so allow 0.2 px of rounding slip -- far
    tighter than any real edit to a coordinate would be.
    """

    for item in items:
        x, y, w, h = item["norm"]
        expected_x = (x + w / 2.0) * 2400.0
        expected_y = (1.0 - (y + h / 2.0)) * 1600.0
        assert item["px"][0] == pytest.approx(expected_x, abs=0.2)
        assert item["px"][1] == pytest.approx(expected_y, abs=0.2)


# --------------------------------------------------------------------------- #
# parser truth tables
# --------------------------------------------------------------------------- #
def test_turn_merged_and_split_caption() -> None:
    assert P.parse_turn([_item("当前回合：3", 1.0, (1200.0, 436.0), None)]) == 3
    assert P.parse_turn(_pick(VICTORY_ITEMS, "当前回合：", "3")) == 3
    assert P.parse_turn(_pick(BATTLE_ITEMS, "当前回合：", "1")) == 1


def test_turn_without_the_caption_is_unknown() -> None:
    # the same frames also print 92/92, LV810, 15 -- none of them is a turn
    assert P.parse_turn([_item("3", 1.0, (1315.1, 435.7), None)]) is None
    assert P.parse_turn([]) is None


def test_turn_caption_without_number_is_unknown() -> None:
    assert P.parse_turn([_item("当前回合：", 1.0, (1163.4, 435.7), None)]) is None


def test_turn_conflict_is_unknown() -> None:
    items = [
        _item("当前回合：", 1.0, (1163.4, 435.7), (0.43884, 0.71162, 0.0918, 0.0321)),
        _item("1", 0.5, (1304.7, 437.5), (0.53488, 0.71242, 0.01744, 0.02832)),
        _item("当前回合：2", 1.0, (1163.4, 900.0), (0.43884, 0.4, 0.0918, 0.0321)),
    ]
    assert P.parse_turn(items) is None


def test_hp_from_the_real_frames() -> None:
    assert P.parse_hp(BATTLE_ITEMS) == (92, 92)
    assert P.parse_hp(VICTORY_ITEMS) == (89, 92)


def test_hp_requires_a_less_or_equal_b() -> None:
    assert P.parse_hp([_item("92/89", 1.0, (816.3, 256.2), None)]) is None
    assert P.parse_hp([_item("92/0", 1.0, (816.3, 256.2), None)]) is None


def test_hp_anchor_rejects_numbers_from_other_widgets() -> None:
    # same shape, but where levels / counts / currencies live
    assert P.parse_hp([_item("2/3", 1.0, (1932.6, 373.0), None)]) is None
    assert P.parse_hp([_item("12/12", 1.0, (1203.5, 1368.3), None)]) is None


def test_hp_never_reads_a_pp_counter() -> None:
    pp_only = _pick(VICTORY_ITEMS, "使用饮数：24/25", "使用沉数：20/20")
    assert P.parse_hp(pp_only) is None


def test_pp_keeps_positional_keys_when_the_cell_name_was_not_read() -> None:
    pp = P.parse_pp(VICTORY_ITEMS)
    assert set(pp.values()) == {(24, 25), (20, 20)}
    assert all(key.startswith("pos:") for key in pp), pp


def test_pp_named_when_the_cell_name_was_read() -> None:
    items = [
        _item("瞪眼", 1.0, (1411.0, 1197.4), (0.57994, 0.23965, 0.01599, 0.02397)),
        _item("使用次数：30/30", 0.8, (1408.0, 1246.0), (0.5785, 0.2163, 0.0843, 0.01968)),
    ]
    assert P.parse_pp(items) == {"瞪眼": (30, 30)}


def test_pp_empty_when_no_counter_is_visible() -> None:
    assert P.parse_pp(BATTLE_ITEMS) == {}
    assert P.parse_pp([]) == {}


def test_pp_rejects_an_inverted_counter() -> None:
    assert P.parse_pp([_item("使用次数：35/31", 0.8, (1203.0, 1368.0), None)]) == {}


def test_pet_name_both_frames() -> None:
    assert P.parse_pet_name(BATTLE_ITEMS) == "烈火猴"
    assert P.parse_pet_name(VICTORY_ITEMS) == "烈火猴"


def test_pet_names_distinguish_sides_and_preserve_the_misread() -> None:
    assert P.parse_pet_names(BATTLE_ITEMS) == {"ally": "烈火猴", "enemy": "蘑菇怪"}
    # the OCR split 对方 / 【蘑菇译】登场了！ into two boxes and read 怪 as 译
    assert P.parse_pet_names(VICTORY_ITEMS) == {"ally": "烈火猴", "enemy": "蘑菇译"}


def test_enemy_only_is_not_an_ally_answer() -> None:
    enemy_only = [item for item in BATTLE_ITEMS if "我方" not in item["text"]]
    assert P.parse_pet_name(enemy_only) is None
    assert P.parse_pet_names(enemy_only)["enemy"] == "蘑菇怪"


def test_bare_name_without_a_side_label_is_never_attributed() -> None:
    # the XP dialog prints 烈火猴 on its own; with no 我方【…】 line it is not a pet name
    assert P.parse_pet_name(XP_ITEMS) is None
    assert P.parse_pet_names(XP_ITEMS) == {"ally": None, "enemy": None}


def test_level_plain_shapes() -> None:
    assert P.parse_level([_item("LV:10", 0.9, (556.0, 261.0), None)]) == 10
    assert P.parse_level([_item("LV：15", 0.9, (556.0, 261.0), None)]) == 15
    assert P.parse_level([_item("等级 15", 0.9, (556.0, 261.0), None)]) == 15
    assert P.parse_level([_item("等级：15", 0.9, (556.0, 261.0), None)]) == 15


def test_level_handles_the_colon_glyph_ocrd_as_eight() -> None:
    assert P.parse_level(BATTLE_ITEMS, side="ally") == 33
    assert P.parse_level(BATTLE_ITEMS, side="enemy") == 10


def test_level_whole_battle_frame_stays_unknown_because_both_sides_are_labelled() -> None:
    assert P.parse_level(BATTLE_ITEMS) is None
    assert P.parse_level(VICTORY_ITEMS, side="ally") is None  # only the enemy badge here
    assert P.parse_level(VICTORY_ITEMS, side="enemy") == 10


def test_level_bare_number_is_not_a_level() -> None:
    # the world HUD badge reads "15" with no label at all
    assert P.parse_level(XP_ITEMS) is None


def test_level_pet_panel_short_label() -> None:
    assert P.parse_level(PET_PANEL_ITEMS) == 5


def test_level_ignores_the_xp_needed_line() -> None:
    line = [item for item in PET_PANEL_ITEMS if item["text"] == "级所需经验值：0"]
    assert P.parse_level(line) is None


def test_currency_reads_the_wallet_pair() -> None:
    assert P.parse_currency(SHOP_ITEMS) == {"XIN豆": 5000.0}


def test_currency_does_not_invent_the_value_the_ocr_dropped() -> None:
    currency = P.parse_currency(SHOP_ITEMS)
    assert "金豆" in [item["text"] for item in SHOP_ITEMS]  # caption really is on screen
    assert "金豆" not in currency                          # ...but its value was not read
    assert "赛尔豆" not in currency


def test_currency_ignores_prices_and_shop_names() -> None:
    currency = P.parse_currency(SHOP_ITEMS)
    assert currency == {"XIN豆": 5000.0}  # not 400, 20, 1, 2w or 600
    assert P.parse_currency(BATTLE_ITEMS) == {}
    assert P.parse_currency([]) == {}


def test_currency_label_without_number_is_not_a_zero() -> None:
    items = [_item("金豆", 1.0, (298.0, 170.0), (0.23833, 0.76408, 0.02, 0.01609))]
    assert P.parse_currency(items) == {}


def test_reward_xp_dialog_line() -> None:
    assert P.parse_reward(XP_ITEMS) == [
        {"kind": "经验", "amount": 106.0, "raw": "XIN精灵获得经验：106"}
    ]


def test_reward_ignores_what_is_still_needed() -> None:
    items = [_item("离升级还需经验：1152", 1.0, (1200.0, 934.0), None)]
    assert P.parse_reward(items) == []


def test_reward_documented_dialog_style() -> None:
    items = [_item("恭喜你得到了 106 个积累经验", 0.5, (1200.0, 900.0), None)]
    assert P.parse_reward(items) == [
        {"kind": "积累经验", "amount": 106.0, "raw": "恭喜你得到了 106 个积累经验"}
    ]


def test_reward_lines_without_an_amount_are_not_rewards() -> None:
    assert P.parse_reward(VICTORY_ITEMS) == []  # 获得胜利 / 获得成就, no number
    assert P.parse_reward([]) == []


def test_spt_card_unbeaten_and_beaten() -> None:
    unbeaten = P.parse_spt_card(_pick(SPT_PANEL_ITEMS, "蘑菇怪", "未击败",
                                      "击败后解锁交易行资格"))
    assert unbeaten["name"] == "蘑菇怪"
    assert unbeaten["defeated"] is False
    assert unbeaten["reward_unlock"] == "击败后解锁交易行资格"

    beaten = P.parse_spt_card([_item("雷伊", 0.9, (1500.0, 400.0),
                                     (0.59, 0.74, 0.06, 0.025)),
                               _item("已击败", 0.9, (1500.0, 470.0),
                                     (0.59, 0.69, 0.07, 0.025))])
    assert beaten["name"] == "雷伊"
    assert beaten["defeated"] is True


def test_spt_card_without_a_state_word_stays_unknown() -> None:
    card = P.parse_spt_card(_pick(SPT_PANEL_ITEMS, "蘑菇怪", "击败后解锁交易行资格"))
    assert card["name"] == "蘑菇怪"
    assert card["defeated"] is None          # not False!
    assert card["reward_unlock"] == "击败后解锁交易行资格"


def test_spt_cards_groups_two_bosses() -> None:
    cards = P.parse_spt_cards(SPT_PANEL_ITEMS)
    assert [(card["name"], card["defeated"]) for card in cards] == [
        ("蘑菇怪", False), ("雷伊", True)
    ]
    assert all(card["reward_unlock"] == "击败后解锁交易行资格" for card in cards)


def test_dialog_buttons_from_the_real_xp_dialog() -> None:
    assert P.parse_dialog_buttons(XP_ITEMS) == [
        {"text": "确认", "px": (1187.8, 998.8), "conf": 0.5, "kind": "confirm"}
    ]


def test_dialog_buttons_close_and_punctuation() -> None:
    items = [
        _item("关闭", 0.6, (2100.0, 346.0), None),
        _item("确认，", 0.5, (1191.0, 997.0), None),
        _item("我已知晓", 0.4, (1191.0, 997.0), None),
        _item("取消", 0.5, (1000.0, 997.0), None),
        _item("购买", 1.0, (555.0, 348.0), None),
    ]
    buttons = P.parse_dialog_buttons(items)
    assert [b["text"] for b in buttons] == ["关闭", "确认，", "我已知晓"]
    assert [b["kind"] for b in buttons] == ["close", "confirm", "confirm"]


def test_dialog_buttons_empty_when_nothing_matches() -> None:
    assert P.parse_dialog_buttons(BATTLE_ITEMS) == []
    assert P.parse_dialog_buttons([]) == []


# --------------------------------------------------------------------------- #
# skill contract
# --------------------------------------------------------------------------- #
def test_build_observe_skills_contract(registry: SkillRegistry) -> None:
    skills = build_observe_skills()
    assert [skill.id for skill in skills] == [
        "game.get_status",
        "game.get_current_scene",
        "game.observe",
        "game.inspect_pets",
        "game.inspect_resources",
        "game.inspect_spt_progress",
        "game.observe_truebody_life",
    ]
    by_id = {skill.id: skill for skill in skills}
    for skill in skills:
        spec = skill.spec
        assert spec.kind is SkillKind.OBSERVE, skill.id
        assert spec.requires_input is False, skill.id   # observation never clicks
        assert spec.requires_approval is False, skill.id
        assert spec.max_actions == 0, skill.id
        assert spec.max_duration_s > 0, skill.id
        assert spec.evidence, f"{skill.id} must say where its evidence comes from"
    assert by_id["game.get_status"].spec.max_duration_s == 5.0
    assert by_id["game.get_current_scene"].spec.max_duration_s == 5.0
    assert by_id["game.observe"].spec.max_duration_s == 20.0
    for skill_id in ("game.inspect_pets", "game.inspect_resources",
                     "game.inspect_spt_progress"):
        assert by_id[skill_id].spec.max_duration_s == 25.0, skill_id
    # honesty table: "real" only where Round 4 actually ran the primitive live
    assert {skill_id: skill.spec.verified for skill_id, skill in by_id.items()} == {
        "game.get_status": "real",
        "game.get_current_scene": "real",
        "game.observe": "real",
        "game.inspect_pets": "replay",
        "game.inspect_resources": "replay",
        "game.inspect_spt_progress": "unverified",
        # replay only: verified on the 104 recorded truebody frames, never live
        "game.observe_truebody_life": "replay",
    }
    assert registry.ids(SkillKind.OBSERVE) == sorted(by_id)


def test_observe_skill_enforces_its_declared_time_budget() -> None:
    """A slow look still returns its pixels, but never pretends to be on budget."""

    import dataclasses

    skill = GetStatus()
    skill.spec = dataclasses.replace(skill.spec, max_duration_s=0.0)
    result = skill(_ctx(FakeSession()))
    assert result.status is Status.PASS
    assert "budget" in result.reason
    assert result.escalation == "replan"


def test_observe_inputs_are_validated(registry: SkillRegistry) -> None:
    session = FakeSession(items=BATTLE_ITEMS)
    ctx = _ctx(session)
    with pytest.raises(SkillError):
        registry.get("game.observe")(ctx, spt_panel="yes")
    with pytest.raises(SkillError):
        registry.get("game.observe")(ctx, nonsense=True)


def test_skills_without_a_session_fail_instead_of_faking(registry: SkillRegistry) -> None:
    for skill_id in registry.ids(SkillKind.OBSERVE):
        result = registry.get(skill_id)(SkillContext(session=None, world=empty_world()))
        assert result.status is Status.FAIL, skill_id
        assert "no session" in result.reason, skill_id


# --------------------------------------------------------------------------- #
# game.get_status
# --------------------------------------------------------------------------- #
def test_get_status_reads_window_and_scene_without_ocr(registry: SkillRegistry) -> None:
    session = FakeSession(scene="BATTLE", items=BATTLE_ITEMS,
                          window={"id": 78351, "x": 270.0, "y": 87.0,
                                  "w": 1200.0, "h": 800.0})
    result = registry.get("game.get_status")(_ctx(session))

    assert result.status is Status.PASS
    assert session.ocr_calls == 0          # cheap path: no OCR look
    assert session.scene_calls == 1

    world = _world(result)
    scene_evidence = world.evidence_for("session.scene")
    assert scene_evidence.source is Source.LIVE_VERIFIED
    assert scene_evidence.origin == SCENE_TEMPLATE_ORIGIN
    assert "fight=" in scene_evidence.detail
    assert world.fact("session.scene") == "BATTLE"
    assert world.fact("session.in_battle") is True
    assert world.window is not None
    assert (world.window.x, world.window.y, world.window.w, world.window.h) == (
        270.0, 87.0, 1200.0, 800.0)
    assert world.window.frontmost is True
    assert world.window.evidence.origin == "window:seerdx.win"
    assert result.data["scores"]["fight"] == 1.0


def test_get_status_survives_an_unknown_frontmost_flag(registry: SkillRegistry) -> None:
    session = FakeSession(frontmost=None)
    result = registry.get("game.get_status")(_ctx(session))
    assert result.status is Status.PASS
    assert _world(result).window.frontmost is None   # unknown stays unknown


def test_get_status_accepts_a_scene_probe_object(registry: SkillRegistry) -> None:
    probe = _Probe(Scene.RESULT, {"fight": 0.31, "victory": 0.99, "confirm": 0.12},
                   {"victory": (1197.0, 723.0)})
    session = FakeSession(probe=probe)
    result = registry.get("game.get_status")(_ctx(session))
    assert result.data["scene"] == "RESULT"
    assert result.data["points"]["victory"] == [1197, 723]


# --------------------------------------------------------------------------- #
# game.observe
# --------------------------------------------------------------------------- #
def test_observe_fills_the_real_battle_frame(registry: SkillRegistry) -> None:
    session = FakeSession(scene="BATTLE", items=BATTLE_ITEMS)
    ctx = _ctx(session)
    before = copy.deepcopy(ctx.world.to_dict())

    result = registry.get("game.observe")(ctx)

    assert result.status is Status.PASS, result.reason
    assert session.ocr_calls == 1
    assert session.ocr_min_conf == [OCR_MIN_CONF]
    world = _world(result)
    pet = world.pets["烈火猴"]
    assert world.active_pet == "烈火猴"
    assert (pet.level, pet.hp, pet.hp_max) == (33, 92, 92)
    assert world.fact("battle.turn") == 1
    assert world.fact("battle.opponent.name") == "蘑菇怪"
    assert world.fact("battle.opponent.level") == 10
    assert world.fact("session.scene") == "BATTLE"
    assert world.fact("session.in_battle") is True
    assert result.data["texts"] and "当前回合：" in result.data["texts"]

    # every fact names the probe that produced it
    assert world.evidence_for("battle.turn").origin == "ocr:当前回合：+1"
    assert world.evidence_for("battle.turn").source is Source.LIVE_VERIFIED
    assert pet.extra["level_evidence"]["origin"] == "ocr:LV833"
    assert pet.extra["level_evidence"]["confidence"] == CONF_SHAPE_REPAIRED
    assert pet.extra["hp_evidence"]["origin"] == "ocr:92/92"
    assert pet.extra["hp_evidence"]["confidence"] == CONF_SHAPE_VERIFIED
    assert world.evidence_for("battle.opponent.name").origin.startswith("ocr:")

    # ctx.world is never mutated
    assert ctx.world.to_dict() == before


def test_observe_keeps_unknown_unknown(registry: SkillRegistry) -> None:
    """A BATTLE scene with an empty OCR look must not invent a single value."""

    session = FakeSession(scene="BATTLE", items=[])
    result = registry.get("game.observe")(_ctx(session))

    assert result.status is Status.PASS        # observing worked, the game was empty
    world = _world(result)
    assert world.pets == {}
    assert world.active_pet is None
    assert world.fact("battle.turn") is None
    assert world.evidence_for("battle.turn").source is Source.UNKNOWN
    assert world.evidence_for("battle.turn").origin  # explains why it is unknown
    assert world.fact("pets.<active>.name") is None
    assert world.fact("session.scene") == "BATTLE"  # the template still worked
    assert result.data["texts"] == []


def test_observe_does_not_wipe_a_value_it_did_not_re_observe(registry: SkillRegistry) -> None:
    """Sticky knowledge: keep the old value *with its evidence* and flag the path.

    The Goal Runner merges observations forward, so an unread turn must not erase
    the turn a previous observation paid an OCR look for.  The honest signal is
    "not re-observed this frame", which is ``world.unknowns``.
    """

    previous = empty_world("test")
    previous.set_fact("battle.turn", 3,
                      Evidence(Source.LIVE_VERIFIED, origin="ocr:当前回合：+3"))
    result = registry.get("game.observe")(_ctx(FakeSession(scene="BATTLE", items=[]), world=previous))

    world = _world(result)
    assert world.fact("battle.turn") == 3
    assert world.evidence_for("battle.turn").origin == "ocr:当前回合：+3"
    assert world.evidence_for("battle.turn").source is Source.LIVE_VERIFIED
    assert "battle.turn" in world.unknowns


def test_observe_marks_rewards_unknown_in_a_result_scene_without_a_payout_line(
        registry: SkillRegistry) -> None:
    session = FakeSession(scene="RESULT", items=VICTORY_ITEMS)
    result = registry.get("game.observe")(_ctx(session))
    world = _world(result)
    assert world.fact("rewards") is None
    assert world.evidence_for("rewards").source is Source.UNKNOWN


def test_observe_reads_the_xp_payout(registry: SkillRegistry) -> None:
    session = FakeSession(scene="DIALOG", items=XP_ITEMS)
    result = registry.get("game.observe")(_ctx(session))
    world = _world(result)
    assert world.fact("rewards") == [
        {"kind": "经验", "amount": 106.0, "raw": "XIN精灵获得经验：106"}
    ]
    assert world.evidence_for("rewards").origin == "ocr:XIN精灵获得经验：106"


def test_observe_can_skip_ocr(registry: SkillRegistry) -> None:
    session = FakeSession(scene="BATTLE", items=BATTLE_ITEMS)
    result = registry.get("game.observe")(_ctx(session), ocr=False)
    assert session.ocr_calls == 0
    assert result.data["texts"] == []
    world = _world(result)
    assert world.pets == {}
    assert world.fact("battle.turn") is None


def test_observe_spt_cards_when_asked(registry: SkillRegistry) -> None:
    session = FakeSession(scene="PANEL", items=SPT_PANEL_ITEMS)
    result = registry.get("game.observe")(_ctx(session), spt_panel=True)

    assert result.status is Status.PASS
    world = _world(result)
    assert world.spt["蘑菇怪"].defeated is False
    assert world.spt["雷伊"].defeated is True
    assert world.fact("spt.蘑菇怪.defeated") is False
    assert world.fact("spt.蘑菇怪.reward_unlock") == "击败后解锁交易行资格"
    assert world.fact("spt.panel_visible") is True
    assert world.evidence_for("spt.蘑菇怪.defeated").source is Source.LIVE_VERIFIED
    assert world.evidence_for("spt.蘑菇怪.defeated").confidence == CONF_SHAPE_REPAIRED


def test_observe_without_spt_panel_request_does_not_touch_spt(registry: SkillRegistry) -> None:
    session = FakeSession(scene="PANEL", items=SPT_PANEL_ITEMS)
    result = registry.get("game.observe")(_ctx(session))
    world = _world(result)
    assert world.spt == {}
    assert "spt.panel_visible" not in world.facts


def test_every_recorded_fact_names_its_probe(registry: SkillRegistry) -> None:
    result = registry.get("game.observe")(_ctx(FakeSession(items=VICTORY_ITEMS)))
    world = _world(result)
    for path, fact in world.iter_facts():
        assert fact.evidence.origin, path
        assert fact.evidence.source is not None, path


# --------------------------------------------------------------------------- #
# game.get_current_scene
# --------------------------------------------------------------------------- #
def test_get_current_scene_reports_the_decision(registry: SkillRegistry) -> None:
    session = FakeSession(scene="RESULT",
                          scores={"fight": 0.31, "victory": 1.0, "confirm": 0.12})
    result = registry.get("game.get_current_scene")(_ctx(session))
    assert result.status is Status.PASS
    assert result.data["scene"] == "RESULT"
    assert result.data["scores"] == {"fight": 0.31, "victory": 1.0, "confirm": 0.12}
    assert _world(result).fact("session.in_battle") is True   # victory panel = still in battle


def test_get_current_scene_world_is_not_in_battle(registry: SkillRegistry) -> None:
    result = registry.get("game.get_current_scene")(_ctx(FakeSession(scene="WORLD")))
    assert result.data["scene"] == "WORLD"
    assert _world(result).fact("session.in_battle") is False


# --------------------------------------------------------------------------- #
# game.inspect_pets
# --------------------------------------------------------------------------- #
def test_inspect_pets_blocks_when_nothing_is_visible(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_pets")(_ctx(FakeSession(scene="WORLD", items=[])))
    assert result.status is Status.BLOCKED
    assert result.reason == "pet panel not visible"
    assert result.data["mode"] is None
    assert result.data.get("pets") is None or result.data.get("pets") == []


def test_inspect_pets_reads_the_battle_hud(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_pets")(_ctx(FakeSession(items=BATTLE_ITEMS)))
    assert result.status is Status.PASS, result.reason
    assert result.data["mode"] == "battle_hud"
    pet = result.data["pets"][0]
    assert (pet["name"], pet["level"], pet["hp"], pet["hp_max"]) == ("烈火猴", 33, 92, 92)
    assert pet["active"] is True
    assert pet["evidence"]["source"] == Source.LIVE_VERIFIED.value
    assert pet["evidence"]["origin"] == "ocr:我方【烈火猴】（3）登场了！"


def test_inspect_pets_reads_the_pet_panel(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_pets")(
        _ctx(FakeSession(scene="PANEL", items=PET_PANEL_ITEMS)))
    assert result.status is Status.PASS, result.reason
    assert result.data["mode"] == "panel"
    pet = result.data["pets"][0]
    assert (pet["name"], pet["level"]) == ("小火猴", 5)
    assert pet["hp"] is None        # the panel shows 体力 (a stat), not current HP
    assert pet["active"] is None    # the panel does not say which pet is out
    assert pet["evidence"]["origin"] == "ocr:名字：小火猴"


def test_inspect_pets_does_not_mutate_the_caller_world(registry: SkillRegistry) -> None:
    ctx = _ctx(FakeSession(items=BATTLE_ITEMS))
    before = copy.deepcopy(ctx.world.to_dict())
    registry.get("game.inspect_pets")(ctx)
    assert ctx.world.to_dict() == before


# --------------------------------------------------------------------------- #
# game.inspect_resources
# --------------------------------------------------------------------------- #
def test_inspect_resources_reads_the_wallet(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_resources")(
        _ctx(FakeSession(scene="PANEL", items=SHOP_ITEMS)))
    assert result.status is Status.PASS, result.reason
    assert result.data["resources"] == {"XIN豆": 5000.0}
    assert result.data["consumables"] == []
    assert result.data["consumables_parsed"] is False
    assert "consumables_note" in result.data

    world = _world(result)
    assert world.fact("resources.XIN豆") == 5000.0
    assert "金豆" not in world.resources            # caption seen, value not -> not invented
    assert "resources.金豆" in world.unknowns
    assert world.evidence_for("resources.XIN豆").source is Source.REPLAY_VERIFIED


def test_inspect_resources_blocks_when_the_hud_is_not_visible(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_resources")(_ctx(FakeSession(items=BATTLE_ITEMS)))
    assert result.status is Status.BLOCKED
    assert result.reason == "resource HUD not visible"


def test_inspect_resources_blocks_on_a_caption_without_a_value(registry: SkillRegistry) -> None:
    items = [_item("金豆", 1.0, (298.0, 170.0), (0.23833, 0.76408, 0.02, 0.01609))]
    result = registry.get("game.inspect_resources")(_ctx(FakeSession(items=items)))
    assert result.status is Status.BLOCKED
    assert "no amount" in result.reason
    assert result.data["labels_seen"] == ["金豆"]


# --------------------------------------------------------------------------- #
# game.inspect_spt_progress
# --------------------------------------------------------------------------- #
def test_inspect_spt_progress_blocks_without_the_panel(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_spt_progress")(_ctx(FakeSession(items=BATTLE_ITEMS)))
    assert result.status is Status.BLOCKED
    assert result.reason == "SPT panel not visible"


def test_inspect_spt_progress_parses_the_cards(registry: SkillRegistry) -> None:
    result = registry.get("game.inspect_spt_progress")(
        _ctx(FakeSession(scene="PANEL", items=SPT_PANEL_ITEMS)))
    assert result.status is Status.PASS, result.reason
    assert [(boss["name"], boss["defeated"]) for boss in result.data["bosses"]] == [
        ("蘑菇怪", False), ("雷伊", True)
    ]
    world = _world(result)
    assert world.fact("spt.蘑菇怪.defeated") is False
    assert world.fact("spt.雷伊.defeated") is True
    assert world.fact("spt.蘑菇怪.reward_unlock") == "击败后解锁交易行资格"
    assert world.fact("spt.panel_visible") is True
    assert world.spt["蘑菇怪"].evidence.source is Source.LIVE_VERIFIED


def test_inspect_spt_progress_blocks_when_the_panel_shows_no_state_word(
        registry: SkillRegistry) -> None:
    items = [item for item in SPT_PANEL_ITEMS if item["text"] not in ("未击败", "已击败")]
    result = registry.get("game.inspect_spt_progress")(
        _ctx(FakeSession(scene="PANEL", items=items)))
    assert result.status is Status.BLOCKED
    assert "no boss card" in result.reason


# --------------------------------------------------------------------------- #
# opt-in: OCR the fixture PNG itself (read-only file OCR, never the game)
# --------------------------------------------------------------------------- #
def _first_button(items: list[dict]) -> str | None:
    buttons = P.parse_dialog_buttons(items)
    return buttons[0]["text"] if buttons else None


def _file_ocr(path: Path, width: int, height: int, min_conf: float = 0.3) -> list[dict]:
    payload = (json.dumps({"op": "ocr", "path": str(path), "min": min_conf})
               + "\n" + '{"op": "quit"}\n')
    proc = subprocess.run([str(SEERDX)], input=payload, capture_output=True,
                          text=True, timeout=180)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith('{"items"'):
            continue
        items = []
        for raw in json.loads(line)["items"]:
            items.append({
                "text": raw["text"],
                "conf": float(raw.get("conf", 0.0)),
                "px": ((raw["x"] + raw["w"] / 2.0) * width,
                       (1.0 - (raw["y"] + raw["h"] / 2.0)) * height),
                "norm": (raw["x"], raw["y"], raw["w"], raw["h"]),
            })
        return items
    raise AssertionError(f"no OCR answer for {path}")


@pytest.mark.skipif(os.environ.get("SEERAUTO_LIVE_OCR_FIXTURES") != "1",
                    reason="set SEERAUTO_LIVE_OCR_FIXTURES=1 to OCR the fixture PNGs "
                           "(read-only file OCR; the live lane belongs to the Lead)")
@pytest.mark.skipif(not SEERDX.exists(), reason="native/bin/seerdx is not built")
def test_live_ocr_of_fixture_battle_frame_reproduces_the_transcription() -> None:
    items = _file_ocr(OBSERVE_DIR / "battle_turn1_2400x1600.png", 2400, 1600)
    assert P.parse_turn(items) == 1
    assert P.parse_hp(items) == (92, 92)
    assert P.parse_pet_name(items) == "烈火猴"
    assert P.parse_level(items, side="ally") == 33
    assert P.parse_level(items, side="enemy") == 10


@pytest.mark.skipif(os.environ.get("SEERAUTO_LIVE_OCR_FIXTURES") != "1",
                    reason="set SEERAUTO_LIVE_OCR_FIXTURES=1 to OCR the fixture PNGs")
@pytest.mark.skipif(not SEERDX.exists(), reason="native/bin/seerdx is not built")
def test_live_ocr_of_fixture_victory_frame_reproduces_the_transcription() -> None:
    items = _file_ocr(OBSERVE_DIR / "victory_moguguai_2400x1600.png", 2400, 1600)
    assert P.parse_turn(items) == 3
    assert P.parse_hp(items) == (89, 92)
    assert P.parse_pet_name(items) == "烈火猴"
    assert _first_button(items) == "确认"
    pp = P.parse_pp(items)
    assert (24, 25) in pp.values()


