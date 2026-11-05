"""Login-screen classification — regression for the 2026-11-05 false "in the world".

The failure being locked down here is real and cost a session: right after pressing
登录, the client paints the world HUD *behind* the still-rendering server panel, so a
frame contains 「公屏聊天 • 离线」 and 「综合待命中」 while the player is not in the
world at all.  The old predicate treated those as world markers and the tool exited 0
— a success reported for a screen that cannot fight, walk or cast.

Every string in `SERVER_LIST_FRAME` was copied from the OCR dump of
`data/overnight/frames/world_01.png` (2026-11-05 19:48, client 1.0.8.9).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_login():
    path = os.path.join(ROOT, "tools", "login.py")
    spec = importlib.util.spec_from_file_location("seer_login_tool", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


login = _load_login()

#: Verbatim OCR of the server-selection screen, including the world HUD behind it.
SERVER_LIST_FRAME = [
    "公屏聊天 • 离线", "综合待命中", "28", "26", "20", "6", "14", "00",
    "组队开启中（PVP服", "00", "123", "Miss冰封", "等你下课", "流星",
    "xin计划（1.0.8.9）", "选择服务器", "29", "CC", "1-！", "18", "：00",
    "（测试服关闭中）", "饼饼饼", "美味蟹黄堡", "9", "24", "：00",
    "NLNS", "25", "极光雷伊", "好友在线", "在线人数", "禁止公众聊天",
    "请输入服务器编号", "快速进几", "查看全部服务器",
]

#: Verbatim OCR of the world, same session, after picking server 1.
WORLD_FRAME = [
    "公屏聊天 • 在线", "系统：已连接公屏聊天", "小XIN要来挑战我", "吗！",
    "主播纽斯", "xin计划（1.0.8.9）", "BET", "卫兵", "00", "hello", "12:00",
]

LOGIN_FRAME = [
    "航行公告", "风过星野，与你再启航", "11月份版本更新内容已发布",
    "登录，开启秋日漫游", "忘记密码", "前往注册",
]

ANNOUNCEMENT_FRAME = ["游戏公告", "亲爱的", "我已知晓"]

#: Verbatim OCR of data/overnight/frames/server_entered.png (2026-11-05 19:50).
#: This frame is the regression the review caught: it contains 「XIN计划，启动！」
#: and 「加载」, and the old marker set called it the world — reporting success
#: while the client was still at 76% of 正在加载星球资源.
LOADING_FRAME = [
    "公屏聊天 •离线", "综合待命中", "seer.61.com", "xin计划（1.0.8.9）",
    "XIN计划，启动！", "76%", "PROJECT", "正在加载星球资源",
]


@pytest.mark.parametrize("texts,expected", [
    (SERVER_LIST_FRAME, "server"),
    (WORLD_FRAME, "world"),
    (LOGIN_FRAME, "login"),
    (ANNOUNCEMENT_FRAME, "announcement"),
    ([], "unknown"),
    (["正在加载星球资源", "76%"], "loading"),
    (LOADING_FRAME, "loading"),
])
def test_screen_kind(texts, expected):
    assert login.screen_kind(texts) == expected


def test_server_list_is_not_the_world():
    """The bug: 综合待命中 + 公屏聊天 离线 must never add up to the world."""

    assert "综合待命中" not in login.PRIMARY_WORLD_MARKERS
    assert login.screen_kind(SERVER_LIST_FRAME) != "world"


def test_multimodal_screen_prefers_the_modal():
    # A transition frame can hold both the HUD and the server panel; the panel wins.
    mixed = WORLD_FRAME + ["选择服务器"]
    assert login.screen_kind(mixed) == "server"


def test_world_frame_still_recognised():
    assert login.screen_kind(WORLD_FRAME) == "world"
    assert any(m in " ".join(WORLD_FRAME) for m in login.PRIMARY_WORLD_MARKERS)


def test_announcement_beats_login():
    # The announcement modal sits on top of the login page and must be dismissed first.
    assert login.screen_kind(ANNOUNCEMENT_FRAME + LOGIN_FRAME) == "announcement"


def test_confirm_frames_is_at_least_two():
    # One frame is not evidence: the login -> server transition looks world-ish once.
    assert login.WORLD_CONFIRM_FRAMES >= 2
    assert login.WORLD_CONFIRM_GAP_S > 0


def test_a_loading_screen_is_never_the_world():
    """The splash caption 「XIN计划，启动！」 is not evidence of arrival."""

    assert login.screen_kind(LOADING_FRAME) == "loading"
    assert "XIN计划，启动" not in login.PRIMARY_WORLD_MARKERS
    assert "当前回合" not in login.PRIMARY_WORLD_MARKERS
    # ... and the world frame still is.
    assert login.screen_kind(WORLD_FRAME) == "world"


def test_loading_outranks_every_other_class():
    # A loading screen can carry leftover login/announcement text; loading wins.
    assert login.screen_kind(LOADING_FRAME + LOGIN_FRAME) == "loading"


#: Verbatim OCR of artifacts/round6/tower_floor14.png — the tower interior.
#: It has NO bottom bar and NO NPC name: the only arrival evidence is that the chat
#: channel is online.  A world predicate of ("卫兵", "已连接公屏聊天") therefore fails
#: inside the tower, which is where the grind lives — the supervisor would then keep
#: calling the login tool on a perfectly healthy client.
TOWER_INTERIOR_FRAME = [
    "公屏聊天 • 在线", "系统：尊享入场|hello 登录了", "xin计划（1.0.8.9）",
    "v0", "hello", "500",
]


def test_the_tower_interior_counts_as_the_world():
    assert login.screen_kind(TOWER_INTERIOR_FRAME) == "world"


def test_the_server_screen_is_not_the_world_even_though_it_says_chat():
    """The server list says 「公屏聊天 • 离线」; online/offline is the discriminator."""

    assert login.screen_kind(SERVER_LIST_FRAME) == "server"
    assert not any("公屏聊天 • 在线" in t for t in SERVER_LIST_FRAME)


def test_a_stuck_version_check_must_not_block_the_login_branch():
    """「版本检测中」 can sit on the login page while the art-pack dialog is up.

    If that string classifies as `loading`, the login branch is unreachable and the
    tool burns its whole timeout without ever pressing 登录.
    """

    assert "版本检测中" not in login.LOADING_MARKERS
    stuck = ["版本检测中", "航行公告", "登录，开启秋日漫游"]
    assert login.screen_kind(stuck) == "login"


def test_the_dead_marker_set_is_gone():
    # A second, unused world-marker tuple is how the comment and the code drifted apart.
    assert not hasattr(login, "WORLD_MARKERS")


#: Verbatim OCR of the client's own failure dialogs (2026-11-05 23:5x and 00:05).
DISCONNECT_FRAME = ["公屏聊天 •离线", "综合待命中", "seer.61.com", "xin计划（1.0.8.9）",
                    "此次连接已经断开，请重新登陆", "确认"]
ERROR_CODE_FRAME = ["公屏聊天 • 离线", "综合待命中", "xin计划（1.0.8.9）", "500033", "确认"]


@pytest.mark.parametrize("texts", [DISCONNECT_FRAME, ERROR_CODE_FRAME])
def test_client_failure_dialogs_are_their_own_class(texts):
    """Neither the login page nor the world — retrying blind is what caused 500033."""

    assert login.screen_kind(texts) == "error"


def test_the_error_class_outranks_loading_and_login():
    assert login.screen_kind(DISCONNECT_FRAME + LOGIN_FRAME) == "error"
    assert login.screen_kind(ERROR_CODE_FRAME + ["正在加载星球资源"]) == "error"
