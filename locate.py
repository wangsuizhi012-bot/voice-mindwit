# -*- coding: utf-8 -*-
"""定位层: 把"点发送"这类口语指令解析成屏幕坐标。
主路径: Windows UI 自动化树(uiautomation, 纯 Python, 零额外模型, 标准软件命中率最高)。
兜底:   截图 + OCR(easyocr, 可选; 用于游戏/自绘 UI 等无障碍树取不到的界面)。

用法:
    from locate import find_control, find_by_ocr
    pt, info = find_control("发送")      # -> (cx, cy) 或 (None, 原因)
"""
import os
import time


def _enum_uia(timeout=3.0):
    """枚举当前前台窗口所有可见控件, 返回 [(name, control_type, x, y, w, h), ...]。"""
    try:
        import uiautomation as auto
    except Exception as e:
        return [], "uiautomation 未安装: " + repr(e)
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        window = auto.ControlFromHandle(hwnd)
    except Exception:
        try:
            window = auto.GetForegroundControl()
        except Exception:
            window = None
    if window is None:
        return [], "取不到前台窗口(可能无焦点窗口)"
    items = []
    deadline = time.time() + timeout

    def walk(ctrl, depth=0):
        if time.time() > deadline or depth > 14:
            return
        try:
            if ctrl.IsValidControl() and ctrl.IsVisible:
                name = ctrl.Name or ""
                rect = ctrl.BoundingRect
                if rect[2] > 4 and rect[3] > 4:   # 宽高都大于 4px 才收
                    items.append((name, ctrl.ControlTypeName,
                                  int(rect[0]), int(rect[1]),
                                  int(rect[2]), int(rect[3])))
        except Exception:
            pass
        try:
            for c in ctrl.GetChildren():
                walk(c, depth + 1)
        except Exception:
            pass

    walk(window)
    return items, None


def find_control(target):
    """在前台窗口控件树里找名字最匹配 target 的控件中心 (cx, cy)。

    返回 (cx, cy) 或 (None, 原因字符串)。
    """
    items, err = _enum_uia()
    if err:
        return None, err
    if not items:
        return None, "前台窗口无可见控件(可能是游戏/自绘 UI, 需走 OCR 兜底)"
    t = (target or "").strip()
    cands = []
    for name, ctype, x, y, w, h in items:
        n = (name or "").strip()
        if not n:
            continue
        score = 0
        if t and (t in n or n in t):
            score = 100
            if "Button" in ctype:
                score += 20
            if "Edit" in ctype or "ComboBox" in ctype:
                score += 5
        if score:
            cands.append((score, x + w // 2, y + h // 2, n, ctype))
    if cands:
        cands.sort(key=lambda c: -c[0])
        _, cx, cy, n, ct = cands[0]
        return (cx, cy), "控件树命中: %s (%s)" % (n, ct)
    return None, "控件树无匹配 '%s'" % t


def find_by_ocr(target):
    """兜底: 截图 + OCR 找文字(需 pip install easyocr)。未装则返回 None。"""
    try:
        import pyautogui
    except Exception as e:
        return None, "pyautogui 未安装: " + repr(e)
    try:
        import easyocr
    except Exception as e:
        return None, "OCR 不可用(需 pip install easyocr): " + repr(e)
    try:
        reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
        img = pyautogui.screenshot()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_tmp.png")
        img.save(path)
        res = reader.readtext(path, detail=1)
        for bbox, text, conf in res:
            if target and target in text:
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                return ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2), "OCR 命中: " + text
        return None, "OCR 未找到 '%s'" % target
    except Exception as e:
        return None, "OCR 执行出错: " + repr(e)


def locate(target):
    """统一入口: 先控件树, 再 OCR 兜底。返回 (pt_or_None, info)。"""
    pt, info = find_control(target)
    if pt:
        return pt, info
    return find_by_ocr(target)


# ---- 自主记忆库: 从手动点击中学习 (app, target) -> 相对坐标 ----
import json as _json
import os as _os

CLICK_MEMORY_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "click_memory.json")


def _time_str():
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S")


def get_foreground_rect_title():
    """返回 (left, top, width, height, title) 或 (None*5)。"""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return (None, None, None, None, None)
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        title = win32gui.GetWindowText(hwnd)
        return (l, t, r - l, b - t, title)
    except Exception:
        return (None, None, None, None, None)


def _load_memory():
    try:
        if _os.path.exists(CLICK_MEMORY_PATH):
            with open(CLICK_MEMORY_PATH, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return {"entries": []}


def _fuzzy(a, b):
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    return a in b or b in a


def recall_click(target):
    """从记忆库找 (app, target) 最近一次相对坐标, 用当前窗口 rect 还原绝对坐标。
    返回 ((x, y), info) 或 (None, reason)。"""
    mem = _load_memory()
    l, t, w, h, title = get_foreground_rect_title()
    if l is None or not w:
        return None, "取不到前台窗口 rect"
    best = None
    for e in mem.get("entries", []):
        if not _fuzzy(target, e.get("target")):
            continue
        if e.get("app") and title and (e["app"] in title or title in e["app"]):
            best = e
            break
        best = best or e
    if best is None:
        return None, "记忆库无 '%s'" % target
    rx, ry = best.get("rx"), best.get("ry")
    if rx is None or ry is None:
        return None, "记忆坐标无效"
    return (int(l + rx * w), int(t + ry * h)), "记忆命中: %s (%s)" % (best.get("target"), best.get("app", ""))


def save_click(target, abs_x, abs_y, app_title=None):
    """记录一次手动点击到记忆库(相对当前前台窗口坐标)。"""
    mem = _load_memory()
    l, t, w, h, title = get_foreground_rect_title()
    if l is None or not w:
        raise RuntimeError("取不到前台窗口 rect")
    title = app_title or title or ""
    rx, ry = (abs_x - l) / w, (abs_y - t) / h
    for e in mem.get("entries", []):
        if _fuzzy(target, e.get("target")) and (e.get("app") == title or (e.get("app") and title and e["app"] in title)):
            e["rx"], e["ry"] = rx, ry
            e["hits"] = e.get("hits", 0) + 1
            e["last"] = _time_str()
            break
    else:
        mem.setdefault("entries", []).append({
            "target": target, "app": title, "rx": rx, "ry": ry,
            "hits": 1, "last": _time_str(),
        })
    with open(CLICK_MEMORY_PATH, "w", encoding="utf-8") as f:
        _json.dump(mem, f, ensure_ascii=False, indent=2)
    return True
