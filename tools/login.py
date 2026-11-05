#!/usr/bin/env python3
"""Log the client back in — the step every unattended run eventually needs.

The client drops to the login page on its own (idle timeout at the server list, a
relogin prompt, a client update).  Every one of those used to end a run, because
"log in" was a human step.  This makes it a program:

  1. clear the auxiliary art-pack window (a native window, so it is OCRd on its own)
  2. dismiss the announcement (我已知晓)
  3. press the login button (text first, then the measured fallback point)
  4. wait for the server list and pick server 1 (啦公星), dragging the scrollbar when
     the list is sorted by population and server 1 is off-page
  5. wait until the world is really there (卫兵 / 公屏聊天 • 在线, twice, with no server/login marker on screen)

    PYTHONPATH=.pydeps python3 tools/login.py
    PYTHONPATH=.pydeps python3 tools/login.py --server 啦公星 --timeout 240
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seerauto import capture as C  # noqa: E402
from seerauto import geometry as G  # noqa: E402
from seerauto.live import LiveSession, list_game_windows  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Markers that only exist once the character is really standing in the world.
#: 「公屏聊天」/「综合待命中」 are deliberately **not** here: the server-list screen
#: renders the world HUD behind its own translucent panel, so both strings are
#: visible before a server has been chosen.  Trusting them made this tool report
#: "already in the world" while the client sat on 选择服务器 — a false success that
#: cost a whole session (2026-11-05, 19:47).
#: Only strings that cannot appear before the character is in the world.
#: 「XIN计划，启动！」 is the splash/loading caption and 「当前回合」 is battle HUD — both
#: appear *before* arrival, and trusting them made this tool report "confirmed in the
#: world" on a 76% 「正在加载星球资源」 screen (2026-11-05, repro: server_entered.png).
#: 「公屏聊天 • 在线」 is the one signal that survives *inside* special scenes: the
#: tower interior has no bottom bar and no NPC name, and its only OCR-able arrival
#: evidence is 「公屏聊天 • 在线」 + 「系统：尊享入场」.  The server screen instead says
#: 「公屏聊天 • 离线」, so the online/offline word separates the two exactly.
PRIMARY_WORLD_MARKERS = ("卫兵", "已连接公屏聊天", "公屏聊天 • 在线", "公屏聊天•在线")
#: Loading must be checked before anything else: it is the state that most looks like
#: success and most certainly is not arrival.
#: Deliberately NOT including 「版本检测中」: that string can sit on the *login page*
#: while the art-pack dialog is up, and classifying it as loading made the login branch
#: unreachable (the module would burn its whole timeout).  The popup is handled up front
#: by clear_popup(); here only real page loads count.
LOADING_MARKERS = ("正在加载", "加载中")
LOGIN_MARKERS = ("航行公告", "账密登录", "登录，开启", "开启万圣夜冒险", "忘记密码")
SERVER_MARKERS = ("选择服务器", "快速进入", "查看全部服务器", "请输入服务器编号")
ANNOUNCE_MARKERS = ("我已知晓", "我知道了")
#: Two consecutive observations, this far apart, must agree before we claim the
#: world.  A single frame during the login->server transition looks world-ish.
WORLD_CONFIRM_GAP_S = 1.5
WORLD_CONFIRM_FRAMES = 2


def screen_kind(texts) -> str:
    """Classify one observation.  Order matters: the modal wins over the HUD."""

    joined = " ".join(str(t) for t in texts)
    if any(m in joined for m in LOADING_MARKERS):
        return "loading"
    if any(m in joined for m in SERVER_MARKERS):
        return "server"
    if any(m in joined for m in ANNOUNCE_MARKERS):
        return "announcement"
    if any(m in joined for m in LOGIN_MARKERS):
        return "login"
    if any(m in joined for m in PRIMARY_WORLD_MARKERS):
        return "world"
    return "unknown"
#: Measured on the 1200x800 client (game_px).
LOGIN_BTN = (1624, 1157)
SCROLLBAR_X = 2023
SCROLL_TOP = 640
SCROLL_BOTTOM = 1000


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def screen_text(session: LiveSession) -> list[str]:
    try:
        return [str(i["text"]) for i in session.ocr(min_conf=0.25)]
    except Exception:  # noqa: BLE001
        return []


def has(texts: list[str], markers) -> bool:
    joined = " ".join(texts)
    return any(m in joined for m in markers)


def clear_popup(background: bool = False) -> bool:
    """The art-pack dialog lives in its own small window and blocks the playfield.

    While it is up the main window stays on 「版本检测中…」 forever, which looks like a
    network hang.  The dialog is a *native* window, so it is OCRd on its own; the click
    has to be a global HID click because the PID lane (`clickpid`) provably does not
    reach this client (tools/bg_probe.py: same coordinates, foreground works, PID
    does not).  In background mode there is nothing safe to do, so say so.
    """

    aux = [w for w in list_game_windows() if w["aux"]]
    if not aux:
        return True
    if background:
        log("art-pack popup is up and only a foreground click can dismiss it; "
            "refusing to steal focus")
        return False
    win = aux[0]
    path = "/tmp/seerauto_login_aux.png"
    subprocess.run(["screencapture", "-x",
                    f"-R{int(win['x'])},{int(win['y'])},{int(win['w'])},{int(win['h'])}", path],
                   check=False, capture_output=True, timeout=10)
    client = C.DaemonClient(os.path.join(ROOT, "native", "bin", "seerdx"), timeout_s=20)
    try:
        items = client.send("ocr", path=path, min=0.2).get("items", [])
        best = None
        for it in items:
            if "暂不导入" in str(it.get("text") or ""):
                best = it
                break
        if best is None:
            log(f"popup has no 暂不导入: {[i.get('text') for i in items][:6]}")
            return False
        cx = win["x"] + (best["x"] + best["w"] / 2.0) * win["w"]
        # Vision's y is bottom-up, so the top-left screen y is 1 - (y + h/2).
        cy = win["y"] + (1.0 - (best["y"] + best["h"] / 2.0)) * win["h"]
        client.send("click", x=cx, y=cy, n=1)
        time.sleep(1.6)
        log(f"popup dismissed at ({cx:.0f},{cy:.0f})")
        return True
    finally:
        client.close()


#: The login button lives in the right-hand panel, low on the page; the announcement
#: text on the left also contains the word 登录, so a bare text search clicks the
#: wrong thing (it did — four times in a row at (1294,744)).
LOGIN_REGION = (1150.0, 900.0, 2400.0, 1400.0)


def click_text_or(session: LiveSession, words, fallback_px, why: str,
                  region: tuple[float, float, float, float] | None = None) -> bool:
    items = session.ocr(min_conf=0.25)
    if region:
        x0, y0, x1, y1 = region
        inside = [i for i in items
                  if x0 <= i["px"][0] <= x1 and y0 <= i["px"][1] <= y1]
        hit = session.find_text(inside, tuple(words))
        if hit is None:
            # no candidate inside the region -> use the measured fallback point
            if fallback_px:
                session.click_px(float(fallback_px[0]), float(fallback_px[1]), why)
                log(f"{why}: no text in region, clicked fallback {fallback_px}")
                return True
            return False
    else:
        hit = session.find_text(items, tuple(words))
    if hit:
        session.click_px(hit["px"][0], hit["px"][1], why)
        log(f"{why}: clicked text at ({hit['px'][0]:.0f},{hit['px'][1]:.0f})")
        return True
    if fallback_px:
        session.click_px(float(fallback_px[0]), float(fallback_px[1]), why)
        log(f"{why}: clicked fallback {fallback_px}")
        return True
    return False


def drag_server_list(session: LiveSession, *, down: bool = True) -> None:
    """Drag the server list's scrollbar, converting game_px -> global points.

    ``seerdx``'s ``drag`` op posts a CGEvent, so it wants **global** screen points.
    The old code passed ``SCROLLBAR_X=2023`` (window-local game_px) straight in, which
    is ~740 pt to the right of the window: the drag landed on the desktop and moved the
    user's real mouse around another app.  ``LiveSession`` owns the conversion.
    """

    top = SCROLL_TOP if down else SCROLL_BOTTOM
    bottom = SCROLL_BOTTOM if down else SCROLL_TOP
    geo = getattr(session, "geo", None)
    if geo is None:
        log("no geometry; refusing to drag the server list")
        return
    # A drag is real mouse movement over whatever is frontmost; without the window
    # raised it drags some other app's content.
    if not session.ensure_frontmost():
        log("game window is not frontmost; refusing to drag the desktop")
        return
    # The geometry is captured once at open(); if the window moved since, the converted
    # points are stale and the drag lands somewhere else — the exact bug being fixed.
    try:
        session._revalidate_geometry()
    except Exception as exc:  # noqa: BLE001
        log(f"window geometry changed; refusing to drag ({exc})")
        return
    p1 = geo.global_pt_from_game_px(SCROLLBAR_X, top)
    p2 = geo.global_pt_from_game_px(SCROLLBAR_X, bottom)
    session.client.send("drag", x1=p1.x, y1=p1.y, x2=p2.x, y2=p2.y, steps=18)


def find_server(session: LiveSession, name: str, tries: int = 6) -> tuple[float, float] | None:
    """Find the server row, dragging the list when it is sorted by population."""

    aliases = (name, name[-2:], "公星")
    for attempt in range(tries):
        items = session.ocr(min_conf=0.25)
        hit = session.find_text(items, aliases)
        if hit:
            log(f"server {name!r} found at attempt {attempt}: {hit['px']}")
            return tuple(hit["px"])
        drag_server_list(session, down=(attempt % 2 == 0))
        time.sleep(1.4)
    return None


def enter_server_by_number(session: LiveSession, number: int) -> bool:
    """Type the server id into 请输入服务器编号 and press 快速进入.

    This is the path that actually worked on 2026-11-05: the list is sorted by
    population and 啦公星 (server 1) is not on the first page, so looking for the
    name there can never succeed.  The label sits inside the input, so the click is
    offset to the right of it to land past the placeholder text.
    """

    items = session.ocr(min_conf=0.2)
    box = session.find_text(items, ("服务器编号", "请输入服务器"))
    go = session.find_text(items, ("快速", "进入"))
    if box is None:
        log("server-number field not found")
        return False
    session.click_px(box["px"][0] + 120, box["px"][1], "focus server number field")
    time.sleep(0.7)
    # Clear first: this runs on every pass through the server screen, and without a
    # clear the field accumulates ("1" -> "11" -> "111") and 快速进入 goes elsewhere.
    # Backspace, NOT "cmd+A": seerdx's `key` op posts a bare virtual-key code with no
    # modifiers, so code 0 types the letter "a" into the field instead of selecting it.
    for _ in range(6):
        session.client.send("key", code=51)     # 51 = delete/backspace
        time.sleep(0.08)
    time.sleep(0.3)
    session.client.send("type", text=str(number))
    time.sleep(0.9)
    if not _field_shows(session, box, str(number)):
        log(f"server number field does not read {number!r}; refusing to enter")
        return False
    if go is None:
        items = session.ocr(min_conf=0.2)
        go = session.find_text(items, ("快速", "进入"))
    if go is None:
        log("快速进入 button not found after typing the server number")
        return False
    session.click_px(go["px"][0], go["px"][1], f"quick-enter server {number}")
    log(f"typed server {number} and pressed 快速进入")
    return True


def _field_shows(session: LiveSession, box: dict, want: str) -> bool:
    """Did the number actually land in the field?  Cheap, and it prevents a wrong server."""

    try:
        items = session.ocr(min_conf=0.2)
    except Exception:  # noqa: BLE001
        return False
    want_digits = "".join(ch for ch in want if ch.isdigit())
    for item in items:
        px = item.get("px") or (0, 0)
        if abs(px[0] - (box["px"][0] + 120)) > 220 or abs(px[1] - box["px"][1]) > 40:
            continue
        got = "".join(ch for ch in str(item.get("text") or "") if ch.isdigit())
        if got == want_digits:
            return True
    return False


def confirmed_world(session: LiveSession) -> bool:
    """Claim the world only after PRIMARY_WORLD_MARKERS on two spaced observations."""

    for i in range(WORLD_CONFIRM_FRAMES):
        if screen_kind(screen_text(session)) != "world":
            return False
        if i + 1 < WORLD_CONFIRM_FRAMES:
            time.sleep(WORLD_CONFIRM_GAP_S)
    return True


def _log_kind(kind: str) -> None:
    if kind == "loading":
        log("client is still loading (版本检测/加载星球资源) — not the world yet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="啦公星")
    ap.add_argument("--server-number", type=int, default=1,
                    help="server id for the 请输入服务器编号 quick-enter path")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    if not list_game_windows():
        log("no game window; relaunching the client")
        subprocess.run(["open", "-a", "/Applications/SeerGame.app"], check=False)
        time.sleep(18)
    if not clear_popup():
        log("cannot clear the art-pack popup; not logging in")
        return 5

    session = LiveSession(dry_run=False, enable_input=True).open()
    # The user authorised focus stealing for this session; without it every click
    # fails with NotFrontmost while they are using another app.
    session.client.send("activate")
    time.sleep(0.8)
    deadline = time.time() + args.timeout
    try:
        while time.time() < deadline:
            texts = screen_text(session)
            kind = screen_kind(texts)
            if kind == "world":
                if confirmed_world(session):
                    log("confirmed in the world")
                    return 0
                log("world markers looked transient; re-checking")
                continue
            if kind == "server":
                log("at the server list")
                pos = find_server(session, args.server, tries=2)
                if pos:
                    session.click_px(pos[0], pos[1], f"enter {args.server}")
                    time.sleep(14)
                    continue
                log(f"server {args.server!r} is not on the visible page; "
                    f"using the number field instead")
                if enter_server_by_number(session, args.server_number):
                    time.sleep(16)
                    continue
                time.sleep(3)
            elif kind == "announcement":
                click_text_or(session, ANNOUNCE_MARKERS, None, "dismiss announcement")
                time.sleep(1.2)
            elif kind == "login":
                session.client.send("activate")
                time.sleep(0.5)
                click_text_or(session, ("登录，开启", "登录", "账密登录"), LOGIN_BTN, "log in",
                              region=LOGIN_REGION)
                time.sleep(14)
            elif kind == "loading":
                _log_kind(kind)
                time.sleep(3.0)
            else:
                time.sleep(3)
        log("timed out before reaching the world")
        return 4
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
