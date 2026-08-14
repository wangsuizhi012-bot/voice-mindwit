# -*- coding: utf-8 -*-
"""定位层: 把"点发送"这类口语指令解析成屏幕坐标。
主路径: Windows UI 自动化树(uiautomation, 纯 Python, 零额外模型, 标准软件命中率最高)。
兜底:   模板图像匹配(pyautogui.locateCenterOnScreen, 借鉴 waterRPA, 需教学存 templates/) 与
        OCR(easyocr, 可选; 用于游戏/自绘 UI 等无障碍树取不到的界面)。

用法:
    from locate import find_control, find_by_ocr, find_by_template, save_template
    pt, info = find_control("发送")      # -> (cx, cy) 或 (None, 原因)
"""
import os
import time

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


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


_ocr_reader = None   # easyocr Reader 单例, 避免每次 OCR 都重新初始化(极慢)

def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        _ocr_reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
    return _ocr_reader


def find_by_ocr(target, exclude=None):
    """兜底: 截图 + OCR 找文字(需 pip install easyocr)。Reader 缓存复用, 首次初始化后快很多。
    exclude=(x,y,w,h) 时先把该屏幕区域涂白, 用于排除置顶小窗自身文字。"""
    try:
        import pyautogui
    except Exception as e:
        return None, "pyautogui 未安装: " + repr(e)
    try:
        import easyocr  # noqa: F401  仅探测是否可用
        reader = _get_ocr_reader()
    except Exception as e:
        return None, "OCR 不可用(需 pip install easyocr): " + repr(e)
    try:
        img = pyautogui.screenshot()
        if exclude:
            try:
                from PIL import ImageDraw
                x, y, w, h = exclude
                ImageDraw.Draw(img).rectangle([x, y, x + w, y + h], fill="white")
            except Exception:
                pass
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


def _template_candidates(target):
    """返回 templates/ 里可能对应 target 的图片路径(精确名优先, 再子串匹配)。"""
    t = (target or "").strip()
    if not t or not os.path.isdir(TEMPLATES_DIR):
        return []
    try:
        files = os.listdir(TEMPLATES_DIR)
    except Exception:
        return []
    exact, fuzzy = [], []
    for f in files:
        stem = os.path.splitext(f)[0]
        if not f.lower().endswith(".png"):
            continue
        if stem == t:
            exact.append(os.path.join(TEMPLATES_DIR, f))
        elif t in stem:
            fuzzy.append(os.path.join(TEMPLATES_DIR, f))
    seen, out = set(), []
    for p in exact + fuzzy:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def find_by_template(target, confidence=0.75, retry_s=3.0):
    """模板图像匹配(借鉴 waterRPA): 在 templates/ 找 target 对应小图, 全屏匹配后点中心。
    轮询 retry_s 秒, 等目标出现再命中。返回 (pt, info) 或 (None, 原因)。"""
    cands = _template_candidates(target)
    if not cands:
        return None, "无模板 '%s' (教学一次自动存 templates/)" % target
    try:
        import pyautogui
    except Exception as e:
        return None, "pyautogui 未安装: " + repr(e)
    try:
        import cv2  # noqa
        has_cv2 = True
    except Exception:
        has_cv2 = False
    deadline = time.time() + retry_s
    last_err = None
    while time.time() < deadline:
        for img_path in cands:
            try:
                if has_cv2:
                    loc = pyautogui.locateCenterOnScreen(img_path, confidence=confidence)
                else:
                    loc = pyautogui.locateCenterOnScreen(img_path)
            except Exception as e:
                last_err = e
                loc = None
            if loc is not None:
                return (int(loc.x), int(loc.y)), "模板命中: " + os.path.basename(img_path)
        time.sleep(0.1)
    if last_err is not None:
        return None, "模板匹配出错: " + repr(last_err)
    return None, "模板未找到 '%s'" % target


def save_template(target, abs_x, abs_y, size=120):
    """教学态: 截取点击点周围 size×size 区域存为 templates/<target>.png, 供 find_by_template 复用。
    返回模板路径, 失败返回 None。"""
    try:
        import pyautogui
    except Exception:
        return None
    if not target:
        return None
    try:
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
    except Exception:
        return None
    safe = "".join(ch for ch in (target or "").strip() if ch not in '\\/:*?"<>|').strip()
    if not safe:
        safe = "tmpl_%d" % int(time.time())
    half = size // 2
    try:
        img = pyautogui.screenshot(region=(int(abs_x - half), int(abs_y - half), size, size))
        path = os.path.join(TEMPLATES_DIR, safe + ".png")
        img.save(path)
        return path
    except Exception:
        return None


def locate(target):
    """统一入口: 控件树 -> 模板匹配 -> OCR 兜底。返回 (pt_or_None, info)。"""
    pt, info = find_control(target)
    if pt:
        return pt, info
    pt, info = find_by_template(target)
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
