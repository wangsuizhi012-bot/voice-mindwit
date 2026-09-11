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
import re
import time

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# 控件类型权重：UIA 树里大量容器控件的名字也含目标文字，但其矩形是整块面板，
# 点它中心往往落在空白处。可点击的控件类型必须优先。
_CLICKABLE_TYPES = ("Button", "MenuItem", "TreeItem", "ListItem", "TabItem",
                    "Hyperlink", "CheckBox", "RadioButton", "SplitButton",
                    "ComboBox", "Edit")
_CONTAINER_TYPES = ("Window", "Pane", "Document", "Group", "ScrollBar",
                    "Tab", "ToolBar", "StatusBar", "TitleBar", "Custom")
# 工具提示式名字，如 `刷新"此电脑"(F5)`、`上移到"桌面"(Alt + 向上键)` ——
# 名字里含目标文字但语义是「快捷键提示」，不是目标本身。实测踩过：
#   找「桌面」-> 命中 (438,103) 的「上移到"桌面"(Alt + 向上键)」而不是树节点
_TOOLTIP_RE = re.compile(r"[（(][^)）]*[A-Za-z0-9+][^)）]*[)）]")


def _uia_score(name, ctype, target, depth):
    """控件匹配打分。0 表示不匹配。

    ⚠️ 打分是实测调出来的（2026-09-11）：
      - 完全相等 > 前缀 > 包含：找「桌面」时精确的树节点必须赢过
        `上移到"桌面"(Alt + 向上键)` 这种只是包含的按钮。
      - 窗口自身（depth==0 的 WindowControl）**直接排除**：它的名字就是窗口
        标题，必然包含目标子串 —— 找「文件」时曾命中
        「此电脑 - 文件资源管理器」整个窗口 (841,287)，点下去落在内容区空白。
      - 工具提示名降权；容器类降权。
    """
    if depth == 0 and "Window" in ctype:
        return 0
    if not name or not target:
        return 0
    if name == target:
        score = 100
    elif name.startswith(target):
        score = 80
    elif target in name:
        score = 60
    else:
        return 0
    if any(k in ctype for k in _CLICKABLE_TYPES):
        score += 15
    elif any(k in ctype for k in _CONTAINER_TYPES):
        score -= 20
    if _TOOLTIP_RE.search(name):
        score -= 25
    # 名字越长越可能是「包含目标的长文本」而非目标本身
    if len(name) > len(target) * 3:
        score -= 10
    return score


def _uia_rect(ctrl):
    """取控件屏幕矩形 (x, y, w, h)；取不到返回 None。

    ⚠️ 不同 uiautomation 版本 API 不同，必须兼容：
      2.0.29 : BoundingRectangle -> Rect(left,top,right,bottom) 对象
       老版本 : BoundingRect      -> (left,top,right,bottom) 四元组
    本机装的是 2.0.29，只有 BoundingRectangle；写死 BoundingRect 会 AttributeError。
    """
    r = getattr(ctrl, "BoundingRectangle", None)
    if r is None:
        r = getattr(ctrl, "BoundingRect", None)
    if r is None:
        return None
    if isinstance(r, (tuple, list)) and len(r) >= 4:
        l, t, rr, b = int(r[0]), int(r[1]), int(r[2]), int(r[3])
    else:
        try:
            l, t = int(r.left), int(r.top)
            rr, b = int(r.right), int(r.bottom)
        except Exception:
            return None
    if rr < l or b < t:
        return None
    return l, t, rr - l, b - t


def _uia_visible(ctrl):
    """控件是否可见。2.0.29 只有 IsOffscreen（语义相反），老版本有 IsVisible。"""
    off = getattr(ctrl, "IsOffscreen", None)
    if off is not None:
        try:
            return not bool(off)
        except Exception:
            pass
    vis = getattr(ctrl, "IsVisible", None)
    if vis is not None:
        try:
            return bool(vis)
        except Exception:
            pass
    return True          # 拿不到就假定可见，交给尺寸过滤兜底


def _uia_valid(ctrl):
    """控件是否有效。2.0.29 用 Exists（可能是属性或方法），老版本用 IsValidControl。"""
    v = getattr(ctrl, "Exists", None)
    if v is not None:
        try:
            return bool(v() if callable(v) else v)
        except Exception:
            pass
    v = getattr(ctrl, "IsValidControl", None)
    if v is not None:
        try:
            return bool(v() if callable(v) else v)
        except Exception:
            pass
    return True


def _enum_uia(timeout=3.0, hwnd=None):
    """枚举窗口所有可见控件, 返回 ([(name, ctype, x, y, w, h, depth), ...], err)。

    hwnd=None 时用前台窗口；传 hwnd 则枚举**指定窗口** —— 这是把定位
    收敛到目标窗口的关键（同名控件在别的窗口时不会被误选）。

    ⚠️ 血泪教训（2026-09-11）：旧实现里 walk() 内部用 `except: pass` 吞掉
    所有异常。而本机 uiautomation 2.0.29 没有 BoundingRect / IsValidControl /
    IsVisible（正确的是 BoundingRectangle / IsOffscreen），于是**每个控件都被
    静默跳过、永远返回 0 个控件**，并被误判成「前台是 Electron 所以没有 UIA」——
    这个环其实一直没工作过，误判持续了很久。现在改成：统计失败次数，
    若失败占比过高或 0 控件但有异常，把真实异常报出来。
    """
    try:
        import uiautomation as auto
    except Exception as e:
        return [], "uiautomation 未安装: " + repr(e)
    window = None
    if hwnd:
        try:
            window = auto.ControlFromHandle(int(hwnd))
        except Exception as e:
            return [], "取不到指定窗口控件(handle 无效?): " + repr(e)
    else:
        try:
            import win32gui
            window = auto.ControlFromHandle(win32gui.GetForegroundWindow())
        except Exception:
            try:
                window = auto.GetForegroundControl()
            except Exception:
                window = None
    if window is None:
        return [], "取不到前台窗口(可能无焦点窗口)"
    items = []
    errs = []
    deadline = time.time() + timeout

    def walk(ctrl, depth=0):
        if time.time() > deadline or depth > 14:
            return
        if _uia_valid(ctrl) and _uia_visible(ctrl):
            rect = _uia_rect(ctrl)
            if rect and rect[2] > 4 and rect[3] > 4:   # 宽高都大于 4px 才收
                try:
                    items.append(((ctrl.Name or ""), ctrl.ControlTypeName,
                                  rect[0], rect[1], rect[2], rect[3], depth))
                except Exception as e:
                    if len(errs) < 3:
                        errs.append("取 name/type 失败: %r" % (e,))
            elif rect is None and len(errs) < 3:
                errs.append("取矩形失败(版本 API 不符?)")
        try:
            for c in ctrl.GetChildren():
                walk(c, depth + 1)
        except Exception as e:
            if len(errs) < 3:
                errs.append("GetChildren 失败: %r" % (e,))

    walk(window)
    if not items and errs:
        return [], "UIA 枚举异常（0 控件）: " + " ; ".join(errs)
    return items, None


def find_control(target, hwnd=None, rect=None):
    """在控件树里找名字最匹配 target 的控件中心 (cx, cy)。

    hwnd: 限定枚举哪个窗口（None=前台窗口）
    rect: (x,y,w,h) 屏幕矩形，只接受中心落在其中的控件 —— 双保险，
          防止跨窗口控件串味。
    返回 (cx, cy) 或 (None, 原因字符串)。
    """
    items, err = _enum_uia(hwnd=hwnd)
    if err:
        return None, err
    if not items:
        return None, "窗口无可见控件(可能是 WinUI3/游戏/自绘 UI, 需走 OCR 兜底)"
    t = (target or "").strip()
    cands = []
    for it in items:
        # 兼容 6 元组（旧格式）与 7 元组（带 depth）
        name, ctype, x, y, w, h = it[0], it[1], it[2], it[3], it[4], it[5]
        depth = it[6] if len(it) > 6 else 1
        n = (name or "").strip()
        if not n:
            continue
        cx, cy = x + w // 2, y + h // 2
        if rect and not (rect[0] <= cx < rect[0] + rect[2]
                         and rect[1] <= cy < rect[1] + rect[3]):
            continue
        score = _uia_score(n, ctype, t, depth)
        if score > 0:
            cands.append((score, cx, cy, n, ctype))
    if cands:
        # 同分时取面积小的（更贴近具体控件，容器通常面积大）
        cands.sort(key=lambda c: -c[0])
        _, cx, cy, n, ct = cands[0]
        extra = "" if len(cands) == 1 else "（另有 %d 个候选）" % (len(cands) - 1)
        return (cx, cy), "控件树命中: %s (%s)%s" % (n[:24], ct, extra)
    return None, "控件树无匹配 '%s'（共 %d 个控件）" % (t, len(items))


_ocr_reader = None   # easyocr Reader 单例, 避免每次 OCR 都重新初始化(极慢)

def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        _ocr_reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
    return _ocr_reader


def _ocr_best(target, res, ox=0, oy=0):
    """从 OCR 结果里挑最佳命中。

    返回 ((x, y), 说明) 或 (None, 原因)。

    ⚠️ 修掉的 bug（2026-09-11）：原实现 `for bbox, text, conf in res: if target in text: return`
    取的是**第一个**匹配 —— easyocr 返回顺序不确定，页面上有两个含目标字样的
    文本时可能选错。现改为：完全相等 > 包含（越短越紧）> 置信度高。
    """
    cands = []
    for bbox, text, conf in res:
        s = (text or "").strip()
        if not s or target not in s:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2) + ox
        cy = int((min(ys) + max(ys)) / 2) + oy
        exact = 1 if s == target else 0
        cands.append((exact, -len(s), float(conf or 0), cx, cy, s))
    if not cands:
        return None, "OCR 未找到 '%s'" % target
    cands.sort(key=lambda c: (-c[0], -c[1], -c[2]))
    _, _, _, cx, cy, s = cands[0]
    extra = "" if len(cands) == 1 else "（另有 %d 个候选）" % (len(cands) - 1)
    return (cx, cy), "OCR 命中: %s%s" % (s[:24], extra)


def find_by_ocr_rect(target, rect, exclude=None):
    """在屏幕矩形 rect=(x,y,w,h) 内做 OCR，返回**屏幕坐标**。

    这是「窗口内定位」的 OCR 实现：裁剪全屏截图（屏幕坐标 1:1，零映射）
    -> OCR -> 结果加回裁剪原点。全程不涉及窗口截图，因此不需要标定。

    比全屏 OCR 的两个好处：① 别的窗口的同名文字不会串进来
    ② 图像更小，OCR 更快更准。
    """
    try:
        import pyautogui
    except Exception as e:
        return None, "pyautogui 未安装: " + repr(e)
    try:
        import easyocr  # noqa: F401
        reader = _get_ocr_reader()
    except Exception as e:
        return None, "OCR 不可用(需 pip install easyocr): " + repr(e)
    try:
        x, y, w, h = [int(v) for v in rect]
        # 夹到屏幕范围内，避免 screenshot 越界报错
        sw, sh = pyautogui.size()
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(sw, x + w), min(sh, y + h)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None, "区域过小或完全在屏幕外: %s" % (rect,)
        img = pyautogui.screenshot(region=(x0, y0, x1 - x0, y1 - y0))
        if exclude:
            try:
                from PIL import ImageDraw
                ex, ey, ew, eh = exclude
                ImageDraw.Draw(img).rectangle(
                    [ex - x0, ey - y0, ex - x0 + ew, ey - y0 + eh], fill="white")
            except Exception:
                pass
        import numpy as np
        res = reader.readtext(np.asarray(img.convert("RGB")), detail=1)
        return _ocr_best(target, res, ox=x0, oy=y0)
    except Exception as e:
        return None, "OCR 执行出错: " + repr(e)


def find_by_ocr(target, exclude=None):
    """兜底: 全屏截图 + OCR 找文字。exclude=(x,y,w,h) 先把该屏幕区域涂白，
    用于排除置顶小窗自身文字。"""
    try:
        import pyautogui
    except Exception as e:
        return None, "pyautogui 未安装: " + repr(e)
    try:
        import easyocr  # noqa: F401
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
        import numpy as np
        res = reader.readtext(np.asarray(img.convert("RGB")), detail=1)
        return _ocr_best(target, res)
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
    轮询 retry_s 秒, 等目标出现再命中。返回 (pt, info) 或 (None, 原因)。

    region=(x,y,w,h) 时只在该屏幕区域内匹配（收敛到目标窗口，避免误命中）。"""
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
                kw = {"region": tuple(region)} if region else {}
                if has_cv2:
                    loc = pyautogui.locateCenterOnScreen(img_path, confidence=confidence, **kw)
                else:
                    loc = pyautogui.locateCenterOnScreen(img_path, **kw)
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
