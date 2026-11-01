#!/usr/bin/env python3
"""Xin计划 client driver: screenshot / OCR / click / type by text or coordinates."""
import json, os, subprocess, sys, time
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTL = os.path.join(ROOT, 'scripts', 'seerctl')
GAME = os.path.join(ROOT, 'artifacts', 'game.png')
FULL = os.path.join(ROOT, 'artifacts', 'full_screen.png')

def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


WIN_RECT = (300, 120, 1149, 714)

def pin_window():
    """force a known window geometry so coordinates are stable"""
    subprocess.run([CTL, 'winset', 'yahaha', *map(str, WIN_RECT)], capture_output=True)
    time.sleep(0.25)

def window():
    out = sh(f'{CTL} wl').stdout
    for w in json.loads(out):
        if w['owner'] == 'SeerGame' and w['layer'] == 0:
            return w
    raise SystemExit('SeerGame window not found')

def grab(path=GAME, activate=True):
    pin_window()
    w = window()
    if activate:
        subprocess.run([CTL, 'activate', 'SeerGame'], capture_output=True)
        time.sleep(0.55)
    subprocess.run(['screencapture', '-x', FULL], capture_output=True)
    im = Image.open(FULL).convert('RGB')
    sw, shh = im.size
    scale = 2.0 if sw >= 2880 else 1.0
    b = w['bounds']
    x, y, ww, hh = int(b['x']), int(b['y']), int(b['w']), int(b['h'])
    crop = im.crop((int(x*scale), int(y*scale), int((x+ww)*scale), int((y+hh)*scale)))
    crop.save(path)
    return crop, w, scale

def ocr(path=GAME, minconf=0.3):
    out = sh(f'{CTL} ocr "{path}" {minconf}').stdout
    try:
        return json.loads(out)
    except Exception:
        return []

def linemap(items, tol=0.012):
    """group OCR boxes into lines (top->bottom, left->right)."""
    items = sorted(items, key=lambda i: -i['y'])
    lines = []
    for it in items:
        cy = it['y'] + it['h']/2
        placed = False
        for ln in lines:
            if abs(ln['cy'] - cy) < tol:
                ln['items'].append(it); ln['cy'] = sum(i['y']+i['h']/2 for i in ln['items'])/len(ln['items']); placed = True; break
        if not placed:
            lines.append({'cy': cy, 'items': [it]})
    for ln in lines:
        ln['items'].sort(key=lambda i: i['x'])
        ln['text'] = ' '.join(i['text'] for i in ln['items'])
    lines.sort(key=lambda l: -l['cy'])
    return lines

def local2global(px, py, win, scale):
    b = win['bounds']
    return b['x'] + px/scale, b['y'] + py/scale

def click(px, py, win=None, scale=None, count=1):
    """px,py are pixel coords inside the game.png crop (2x retina)."""
    win = win or window()
    if scale is None:
        im = Image.open(GAME)
        scale = 2.0 if im.size[0] > 1800 else 1.0
    gx, gy = local2global(px, py, win, scale)
    subprocess.run([CTL, 'click', str(gx), str(gy), str(count)], capture_output=True)
    return gx, gy

def find(items, needle, exact=False):
    res = []
    for it in items:
        t = it['text']
        if (t == needle) if exact else (needle in t):
            res.append(it)
    return res

def center(item, path=GAME, scale=None):
    im = Image.open(path)
    w, h = im.size
    if scale is None:
        scale = 2.0 if w > 1800 else 1.0
    px = (item['x'] + item['w']/2) * w
    py = (1 - (item['y'] + item['h']/2)) * h
    return px, py

def click_text(needle, exact=False, index=0, wait=1.2, verbose=True):
    im, win, scale = grab()
    items = ocr(GAME)
    hits = find(items, needle, exact)
    if not hits:
        if verbose: print(f'NOT FOUND: {needle}')
        return None
    it = hits[min(index, len(hits)-1)]
    px, py = center(it)
    gx, gy = click(px, py, win, scale)
    if verbose: print(f'clicked "{it["text"]}" at ({gx:.0f},{gy:.0f})')
    time.sleep(wait)
    return (gx, gy)

def dump(force=False):
    im, win, scale = grab()
    items = ocr(GAME)
    lines = linemap(items)
    for ln in lines:
        print(f"y={ln['cy']:.3f} | {ln['text']}")
    return items

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'dump'
    if cmd == 'dump':
        dump()
    elif cmd == 'clicktext':
        click_text(sys.argv[2])
    elif cmd == 'grab':
        grab(); print('ok')
    elif cmd == 'coord':
        im, win, scale = grab(); print(win, scale)

# ---------- high level helpers ----------
import re as _re

def dismiss_dialogs(max_tries=3):
    """click any confirm-style buttons visible (确认/确定/我知道了/关闭)."""
    for _ in range(max_tries):
        items = ocr(GAME, 0.4)
        hits = [i for i in items if i['text'] in ('确认','确定','我知道了','确定 ','Yes')]
        if not hits:
            return False
        im, win, scale = grab(activate=False)
        it = hits[0]
        px, py = center(it)
        click(px, py, win, scale)
        time.sleep(0.9)
    return True

def wait_text(needle, timeout=8.0, minconf=0.5, poll=0.7):
    t0 = time.time()
    while time.time() - t0 < timeout:
        grab(activate=False)
        items = ocr(GAME, minconf)
        if any(needle in i['text'] for i in items):
            return True
        time.sleep(poll)
    return False
