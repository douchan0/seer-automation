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
  5. wait until the world is really there (公屏聊天/卫兵/当前回合 …)

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

WORLD_MARKERS = ("公屏聊天", "卫兵", "当前回合", "XIN计划，启动", "综合待命中")
LOGIN_MARKERS = ("航行公告", "账密登录", "登录，开启", "开启万圣夜冒险", "忘记密码")
SERVER_MARKERS = ("选择服务器", "快速进入", "查看全部服务器")
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


def clear_popup() -> bool:
    """The art-pack dialog lives in its own small window and blocks the playfield."""

    aux = [w for w in list_game_windows() if w["aux"]]
    if not aux:
        return True
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
        cy = win["y"] + (1.0 - (best["y"] + best["h"] / 2.0)) * win["h"]
        client.send("clickpid", x=cx, y=cy, n=1)
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


def find_server(session: LiveSession, name: str, tries: int = 6) -> tuple[float, float] | None:
    """Find the server row, dragging the list when it is sorted by population."""

    aliases = (name, name[-2:], "公星")
    for attempt in range(tries):
        items = session.ocr(min_conf=0.25)
        hit = session.find_text(items, aliases)
        if hit:
            log(f"server {name!r} found at attempt {attempt}: {hit['px']}")
            return tuple(hit["px"])
        if attempt % 2 == 0:
            session.client.send("drag", x1=SCROLLBAR_X, y1=SCROLL_TOP,
                                x2=SCROLLBAR_X, y2=SCROLL_BOTTOM, steps=18)
        else:
            session.client.send("drag", x1=SCROLLBAR_X, y1=SCROLL_BOTTOM,
                                x2=SCROLLBAR_X, y2=SCROLL_TOP, steps=18)
        time.sleep(1.4)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="啦公星")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    if not list_game_windows():
        log("no game window; relaunching the client")
        subprocess.run(["open", "-a", "/Applications/SeerGame.app"], check=False)
        time.sleep(18)
    clear_popup()

    session = LiveSession(dry_run=False, enable_input=True).open()
    # The user authorised focus stealing for this session; without it every click
    # fails with NotFrontmost while they are using another app.
    session.client.send("activate")
    time.sleep(0.8)
    deadline = time.time() + args.timeout
    try:
        while time.time() < deadline:
            texts = screen_text(session)
            if has(texts, WORLD_MARKERS) and not has(texts, LOGIN_MARKERS + SERVER_MARKERS):
                log("already in the world")
                return 0
            if has(texts, SERVER_MARKERS):
                log("at the server list")
                pos = find_server(session, args.server)
                if pos:
                    session.click_px(pos[0], pos[1], f"enter {args.server}")
                    time.sleep(14)
                    continue
                log("server not found in the list")
            elif has(texts, ("我已知晓", "我知道了")):
                click_text_or(session, ("我已知晓", "我知道了"), None, "dismiss announcement")
                time.sleep(1.2)
            elif has(texts, LOGIN_MARKERS):
                session.client.send("activate")
                time.sleep(0.5)
                click_text_or(session, ("登录，开启", "登录", "账密登录"), LOGIN_BTN, "log in",
                              region=LOGIN_REGION)
                time.sleep(14)
            else:
                time.sleep(3)
        log("timed out before reaching the world")
        return 4
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
