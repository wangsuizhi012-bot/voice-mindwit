# -*- coding: utf-8 -*-
"""窗口上下文 —— 前台窗口识别 / 幽灵窗口过滤 / 客户区坐标映射

补齐 nuphus-mcp 的 `desktop_windows_list` 缺的两件事（2026-09-11 实测）：
  1. 没有前台窗口标记 —— 11 个窗口里无法知道哪个是当前活动窗口
  2. 不过滤幽灵窗口 —— overlay / 最小化 / 鼠标穿透窗口都混在里面，
     且全部 visible=true，拿它当点击目标会点到空气

外加一件更隐蔽的事：
  3. 窗口截图得到的是**客户区**图像，而点击用的是**屏幕坐标**。
     两者之间差一个 (边框宽, 标题栏高)。窗口截图 -> OCR -> 直接点，
     坐标会整体偏移。

零依赖（纯 ctypes），零显存。

用法:
    python win_ctx.py              列出可交互窗口 + 前台标记
    python win_ctx.py --raw        原始 Win32 属性（排查用）
    python win_ctx.py --json       机器可读
    python win_ctx.py --verify     实测客户区偏移是否与假设一致
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# ---- 正确的 64 位类型声明（不声明 HWND 会被截断成 32 位，实测直接段错误）----
LRESULT = ctypes.c_ssize_t
HWND = wt.HWND

u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, HWND, wt.LPARAM), wt.LPARAM]
u32.IsWindowVisible.argtypes = [HWND]
u32.IsIconic.argtypes = [HWND]
u32.GetWindowTextLengthW.argtypes = [HWND]
u32.GetWindowTextW.argtypes = [HWND, wt.LPWSTR, ctypes.c_int]
u32.GetClassNameW.argtypes = [HWND, wt.LPWSTR, ctypes.c_int]
u32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wt.RECT)]
u32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wt.RECT)]
u32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wt.POINT)]
u32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
u32.GetWindowLongPtrW.restype = LRESULT
u32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wt.DWORD)]
u32.GetForegroundWindow.restype = HWND
u32.GetForegroundWindow.argtypes = []
u32.IsIconic.argtypes = [HWND]
u32.ShowWindow.argtypes = [HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [HWND]
u32.BringWindowToTop.argtypes = [HWND]
u32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
u32.GetWindowThreadProcessId.restype = wt.DWORD
k32.GetCurrentThreadId.restype = wt.DWORD
k32.GetCurrentThreadId.argtypes = []

SW_RESTORE = 9

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_EX_TRANSPARENT = 0x00000020   # 鼠标穿透 —— 绝对点不到
WS_EX_TOOLWINDOW = 0x00000080    # 工具窗，不在任务栏
WS_EX_LAYERED = 0x00080000       # 分层（overlay 常用）
WS_EX_NOACTIVATE = 0x08000000    # 不能激活 —— overlay 典型特征

# 已知的非交互 overlay 类 / 标题特征（实测得来，可继续补）
GHOST_CLASSES = {
    "Windows.UI.Core.CoreWindow",       # Windows 输入体验 / Shell Handwriting Canvas
    "ApplicationFrameWindow",           # 需结合其他条件，不能单靠它
}
GHOST_TITLE_HINTS = ("Overlay", "AgentCursorOverlay", "输入体验", "Handwriting Canvas")
# 这些即使全屏 0,0 也是真实窗口
REAL_TITLES = ("Program Manager",)


def _text(h, fn, buf=512):
    b = ctypes.create_unicode_buffer(buf)
    fn(h, b, buf)
    return b.value


def _process_name(pid):
    """用 QueryFullProcessImageNameW 取进程名，失败返回 ''。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(512)
        buf = ctypes.create_unicode_buffer(512)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        k32.CloseHandle(h)


def _rect(h, fn):
    r = wt.RECT()
    fn(h, ctypes.byref(r))
    return r


def raw_window(h):
    """单个窗口的全部原始属性（不做任何判断）。"""
    wr = _rect(h, u32.GetWindowRect)
    cr = _rect(h, u32.GetClientRect)
    pt = wt.POINT(0, 0)
    u32.ClientToScreen(h, ctypes.byref(pt))
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return {
        "hwnd": int(h),
        "title": _text(h, u32.GetWindowTextW),
        "class_name": _text(h, u32.GetClassNameW),
        "visible": bool(u32.IsWindowVisible(h)),
        "minimized": bool(u32.IsIconic(h)),
        "window": {"x": wr.left, "y": wr.top,
                   "w": wr.right - wr.left, "h": wr.bottom - wr.top},
        "client": {"x": pt.x, "y": pt.y, "w": cr.right, "h": cr.bottom},
        # 客户区(0,0) 与 窗口左上角 的差 = 边框宽 / 标题栏高
        "client_offset": {"dx": pt.x - wr.left, "dy": pt.y - wr.top},
        "style": u32.GetWindowLongPtrW(h, GWL_STYLE),
        "exstyle": u32.GetWindowLongPtrW(h, GWL_EXSTYLE),
        "process_id": pid.value,
        "process_name": _process_name(pid.value),
    }


def _classify(d, fg):
    """给定原始属性 + 前台句柄，返回 (可交互?, 原因)。"""
    ex = d["exstyle"]
    w = d["window"]
    title = d["title"]

    if not d["visible"]:
        return False, "不可见"
    if d["minimized"] or w["x"] <= -32000:
        return False, "已最小化"
    if w["w"] < 80 or w["h"] < 40:
        return False, "尺寸过小(%dx%d)" % (w["w"], w["h"])
    if ex & WS_EX_TRANSPARENT:
        return False, "鼠标穿透(WS_EX_TRANSPARENT)"
    if ex & WS_EX_NOACTIVATE:
        return False, "不可激活(WS_EX_NOACTIVATE)"
    if d["class_name"] in ("Windows.UI.Core.CoreWindow",):
        return False, "系统UI层(%s)" % d["class_name"]
    if any(k.lower() in title.lower() for k in GHOST_TITLE_HINTS):
        return False, "已知overlay(%s)" % title[:24]
    # 桌面本身：真实但不是「应用窗口」，标出来但不给前台优先级
    if title in REAL_TITLES:
        return True, "桌面"
    if title.strip() == "":
        return False, "无标题"
    return True, "前台" if d["hwnd"] == fg else "普通"


def list_windows(interactive_only=False):
    """枚举窗口，带前台标记与幽灵过滤。"""
    fg = int(u32.GetForegroundWindow() or 0)
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, HWND, wt.LPARAM)
    def _cb(h, _):
        d = raw_window(h)
        ok, why = _classify(d, fg)
        d["interactive"] = ok
        d["reason"] = why
        d["foreground"] = (d["hwnd"] == fg)
        if ok or not interactive_only:
            out.append(d)
        return True

    u32.EnumWindows(_cb, 0)
    # 优先级: 前台 > 可交互 > 面积大。
    # ⚠️ 必须把 interactive 排进 key —— 否则鼠标穿透的 overlay 会因为
    #    面积大而排在真实窗口前面，resolve() 就会把点击目标指到幽灵窗上。
    out.sort(key=lambda d: (not d["foreground"], not d["interactive"],
                            -d["window"]["w"] * d["window"]["h"]))
    return out


def foreground():
    """当前前台窗口（原始属性 + 分类）。"""
    fg = int(u32.GetForegroundWindow() or 0)
    if not fg:
        return None
    d = raw_window(fg)
    d["interactive"], d["reason"] = _classify(d, fg)
    d["foreground"] = True
    return d


def usable(d):
    """这个窗口能不能作为**操作目标**。返回 (bool, 说明)。

    与 d["interactive"] 的区别：
      interactive 表示「现在就能点」；最小化的窗口 interactive=False，
      但它是个真实的、恢复后完全可用的窗口 —— activate() 会先 SW_RESTORE。
      而不见 / 鼠标穿透 / 不可激活的窗口即使恢复也没用。
    """
    if d["interactive"]:
        return True, "可交互"
    if d["reason"] == "已最小化":
        return True, "已最小化（操作前会自动恢复）"
    return False, d["reason"]


def resolve(name):
    """按标题子串 / 进程名找窗口。

    ⚠️ 两个修掉的 bug（2026-09-11）：
      1. 旧实现调 `list_windows()` —— 该函数默认 interactive_only=False，
         等于**在幽灵窗口里搜索**。实测 resolve('Notepad') 命中了
         hwnd=133806（无标题、不可见的 IME 辅助窗口，UIA 只有 1 个控件），
         而不是真正的记事本窗口，导致后续定位全部失败。
      2. 前台优先排序遇上「不可见但 fg=True」的怪窗口会把它顶到第一。
         现在只在**可用窗口**里排（可用 = 交互 或 最小化可恢复）。

    返回 (window|None, 说明)
    """
    n = (name or "").lower()
    cands = [d for d in list_windows(interactive_only=False)
             if n in (d["title"] or "").lower() or n in (d["process_name"] or "").lower()]
    if not cands:
        return None, "未找到匹配 %r 的窗口" % name
    live = [d for d in cands if usable(d)[0]]
    if not live:
        why = "；".join(sorted({d["reason"] for d in cands}))
        return None, ("找到 %d 个同名窗口但均不可操作（%s）—— "
                      "请先恢复/显示该窗口" % (len(cands), why))
    # live 里 最小化的排后面（优先用现在就能点的）
    live.sort(key=lambda d: (d["reason"] == "已最小化", not d["foreground"],
                             -d["window"]["w"] * d["window"]["h"]))
    best = live[0]
    ok, why = usable(best)
    extra = "" if len(live) == 1 else "（另有 %d 个同名，已选最前）" % (len(live) - 1)
    return best, "命中 %s | %s%s" % (best["title"][:40] or "(无标题)", why, extra)


# ------------------------------------------------------------ 激活与范围判定
def activate(hwnd, settle=0.15):
    """把窗口切到前台。返回 True/False（以 GetForegroundWindow 实测为准）。

    ⚠️ 跨进程调 SetForegroundWindow 会被 Windows 的「前台锁」静默拒绝
    （不报错，也不生效）。必须先用 AttachThreadInput 把本线程挂到当前
    前台线程的输入队列上，才能拿到前台权限。这是本函数存在的原因，
    直接调 SetForegroundWindow 是不行的。
    """
    import time as _t
    # ⚠️ 不要写 HWND(int(hwnd)) 后再 int() —— ctypes.c_void_p 不支持 int()，
    #    直接传 int 即可（argtypes=[HWND] 会自动转换）。
    #    注意：EnumWindows 回调里的 h 反而是纯 int（ctypes 对 c_void_p 回调参数
    #    会转成 int），所以只有「自己构造的 c_void_p」才有这个坑。
    h = int(hwnd)
    if u32.IsIconic(h):
        u32.ShowWindow(h, SW_RESTORE)
    if int(u32.GetForegroundWindow() or 0) == h:
        return True

    fg = u32.GetForegroundWindow()
    tid_fg = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = k32.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        try:
            attached = bool(u32.AttachThreadInput(tid_me, tid_fg, True))
        except Exception:
            attached = False
    try:
        u32.BringWindowToTop(h)
        u32.SetForegroundWindow(h)
    finally:
        if attached:
            try:
                u32.AttachThreadInput(tid_me, tid_fg, False)
            except Exception:
                pass
    _t.sleep(settle)
    return int(u32.GetForegroundWindow() or 0) == h


def contains(win, x, y, margin=0):
    """点 (x,y) 是否落在窗口矩形（屏幕坐标）内。win 为 raw_window() 的返回。"""
    if not win:
        return False
    w = win["window"]
    return (w["x"] - margin) <= x < (w["x"] + w["w"] + margin) and \
           (w["y"] - margin) <= y < (w["y"] + w["h"] + margin)


def window_rect(win):
    """窗口矩形 -> (x, y, w, h)，可直接当截图裁剪区域（屏幕坐标，1:1）。"""
    if not win:
        return None
    w = win["window"]
    return (w["x"], w["y"], w["w"], w["h"])


# ------------------------------------------------------------ 坐标映射
def to_screen(hwnd, x, y):
    """窗口**客户区**坐标 -> 屏幕坐标。窗口截图里定位到的点必须过这一层。"""
    pt = wt.POINT(int(x), int(y))
    if not u32.ClientToScreen(HWND(int(hwnd)), ctypes.byref(pt)):
        return None
    return (pt.x, pt.y)


def to_client(hwnd, x, y):
    """屏幕坐标 -> 窗口客户区坐标。"""
    pt = wt.POINT(int(x), int(y))
    if not u32.ScreenToClient(HWND(int(hwnd)), ctypes.byref(pt)):
        return None
    return (pt.x, pt.y)


def click_point(hwnd, x, y):
    """窗口截图内定位到的 (x,y) -> 可直接喂给鼠标的屏幕坐标。

    ⚠️ 只有在「截图就是客户区、且 1:1 无缩放」时它才等于 to_screen()。
    实测并非如此 —— 见 calibrate_shot()。窗口截图链路请先标定。
    """
    return to_screen(hwnd, x, y)


# ------------------------------------------------------------ 截图映射标定
# 实测结论（2026-09-11, WorkBuddy 窗口 1296x688 / client 1280x672 / 截图 1258x664）:
#   屏幕坐标 = 截图原点 + 截图坐标 × (sx, sy)
#   标定值 sx≈1.018  sy≈1.022  —— 两个方向比例**不相等**，
#   且 sx≈client_w/shot_w(1.0175) 但 sy≠client_h/shot_h(1.012)。
#   => 没有闭式公式，必须实测标定；窗口截图尺寸也既非窗口矩形也非客户区矩形。
#
# 因此：**做定位时优先用全屏截图**（桌面坐标 1:1，零映射）。
#       窗口截图只用于省 token，一旦要点击就必须过标定。

def _gray(path, ds):
    import numpy as np
    from PIL import Image
    im = Image.open(path).convert("L")
    if ds > 1:
        im = im.resize((max(1, im.width // ds), max(1, im.height // ds)),
                       Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)


def calibrate_shot(win_png, full_png, ds=2, step=0.0005, span=0.03,
                   roi=None, search=6):
    """图像相关法标定「窗口截图 -> 屏幕坐标」的仿射映射。

    把窗口截图(的 ROI)按候选缩放比缩放, 在整屏截图里穷举平移取最小 MSE。
    纯像素级测量, 不受 OCR 中心抖动影响。

    roi: 窗口截图原始坐标下的静态区域 (x, y, w, h)；默认全图。
         建议传静态区域（如侧边栏），避开会变化的内容。
    返回 dict(sx, sy, dx, dy, mse)；失败返回 None。
    """
    import numpy as np
    from PIL import Image

    full = _gray(full_png, ds)
    shot = Image.open(win_png).convert("L")
    # ⚠️ 必须先按 ds 降采样再裁 ROI：否则 base 是原始分辨率，
    #    与降采样后的 full 尺寸不匹配，搜索会全被 continue 掉。
    if ds > 1:
        shot = shot.resize((max(1, shot.width // ds), max(1, shot.height // ds)),
                           Image.BILINEAR)
    if roi:
        shot = shot.crop((roi[0] // ds, roi[1] // ds,
                          (roi[0] + roi[2]) // ds, (roi[1] + roi[3]) // ds))
        roi_x, roi_y = roi[0], roi[1]
    else:
        roi_x, roi_y = 0, 0
    base = np.asarray(shot, dtype=np.float32)
    Hf, Wf = full.shape
    Bh, Bw = base.shape

    def _mse_at(sx, sy, ox, oy):
        w2 = int(round(Bw * sx))
        h2 = int(round(Bh * sy))
        if w2 < 8 or h2 < 8 or w2 > Wf or h2 > Hf:
            return None
        if ox + w2 > Wf or oy + h2 > Hf:
            return None
        img = np.asarray(
            Image.fromarray(base.astype(np.uint8)).resize((w2, h2), Image.BILINEAR),
            dtype=np.float32)
        return float(((full[oy:oy + h2, ox:ox + w2] - img) ** 2).mean())

    n = max(1, int(span / step))
    scales = [1.0 - span + i * step for i in range(2 * n + 1)]

    # ── 阶段1: 同尺度假定下粗定位（快, 定偏移）
    best = None
    for s in scales:
        for oy in range(0, min(search, Hf) + 1):
            for ox in range(0, min(search, Wf) + 1):
                m = _mse_at(s, s, ox, oy)
                if m is not None and (best is None or m < best[0]):
                    best = (m, s, s, ox, oy)
    if best is None:
        return None

    # ── 阶段2: 以粗定位的偏移为中心, 分轴细化比例（允许微小偏移浮动）
    _, s0, _, ox0, oy0 = best
    for _ in range(2):
        for axis in ("x", "y"):
            for s in scales:
                sx, sy = (s, best[2]) if axis == "x" else (best[1], s)
                for doy in (-1, 0, 1):
                    for dox in (-1, 0, 1):
                        m = _mse_at(sx, sy, max(0, best[3] + dox), max(0, best[4] + doy))
                        if m is not None and m < best[0]:
                            best = (m, sx, sy, max(0, best[3] + dox), max(0, best[4] + doy))

    mse, sx, sy, ox, oy = best
    return {
        "sx": sx, "sy": sy,
        # 换算回屏幕像素: ROI 左上角对应的屏幕位置
        "dx": ox * ds - roi_x * sx,
        "dy": oy * ds - roi_y * sy,
        "mse": mse, "ds": ds,
    }


def shot_to_screen(calib, x, y):
    """用标定结果把窗口截图坐标换算成屏幕坐标。"""
    return (int(round(calib["dx"] + x * calib["sx"])),
            int(round(calib["dy"] + y * calib["sy"])))


def calibrate_report(win_png, full_png, roi=None):
    """标定并打印结论 + 与「客户区公式」的偏离量。"""
    c = calibrate_shot(win_png, full_png, roi=roi)
    if not c:
        print("标定失败：ROI 或 search 参数不合适")
        return None
    from PIL import Image
    w0, h0 = Image.open(win_png).size
    fw, fh = Image.open(full_png).size
    print("=" * 74)
    print("窗口截图 -> 屏幕坐标 标定")
    print("=" * 74)
    print("  窗口截图 : %dx%d" % (w0, h0))
    print("  整屏截图 : %dx%d" % (fw, fh))
    print("  ROI      : %s" % (str(tuple(roi)) if roi else "全图"))
    print("  标定结果 : 屏幕 = (%d, %d) + 截图 × (%.4f, %.4f)   MSE=%.1f"
          % (c["dx"], c["dy"], c["sx"], c["sy"], c["mse"]))
    print("  两轴比例 : sx=%.4f  sy=%.4f  %s"
          % (c["sx"], c["sy"],
             "一致" if abs(c["sx"] - c["sy"]) < 0.002 else "**不一致 —— 不能当等比缩放**"))
    print("  提示     : 定位点击请优先用全屏截图（1:1, 零映射），")
    print("             窗口截图仅供省 token，点击前必须过本标定。")
    print("=" * 74)
    return c


# ------------------------------------------------------------ 展示
def dump(interactive_only=False, as_json=False):
    ws = list_windows(interactive_only=interactive_only)
    if as_json:
        print(json.dumps(ws, ensure_ascii=False, indent=2))
        return ws
    fg = foreground()
    print("=" * 96)
    print("前台窗口: %s  [%s]  pid=%s  at %s"
          % (fg["title"][:40] if fg else "-",
             fg["process_name"] if fg else "-",
             fg["process_id"] if fg else "-",
             "%d,%d" % (fg["window"]["x"], fg["window"]["y"]) if fg else "-"))
    print("=" * 96)
    print("%-3s %-34s %-20s %-18s %-12s %s"
          % ("F", "标题", "进程", "位置/尺寸", "客户区偏移", "判定"))
    print("-" * 96)
    for d in ws:
        w = d["window"]
        print("%-3s %-34s %-20s %-18s %-12s %s"
              % ("*" if d["foreground"] else "",
                 (d["title"] or "(无标题)")[:32],
                 (d["process_name"] or "?")[:18],
                 "%d,%d %dx%d" % (w["x"], w["y"], w["w"], w["h"]),
                 "+%d,+%d" % (d["client_offset"]["dx"], d["client_offset"]["dy"]),
                 d["reason"]))
    print("-" * 96)
    print("共 %d 个窗口，其中可交互 %d 个" % (len(ws), sum(1 for d in ws if d["interactive"])))
    return ws


def verify():
    """实测客户区偏移：截图坐标 -> 屏幕坐标 是否自洽。"""
    d = foreground()
    if not d:
        print("无前台窗口")
        return
    print("前台: %s (%s)" % (d["title"][:40], d["process_name"]))
    print("  window rect : %d,%d %dx%d" % (d["window"]["x"], d["window"]["y"],
                                           d["window"]["w"], d["window"]["h"]))
    print("  client orig : %d,%d %dx%d（屏幕坐标）" % (d["client"]["x"], d["client"]["y"],
                                                       d["client"]["w"], d["client"]["h"]))
    print("  客户区偏移  : +%d,+%d  <- 窗口截图定位必须加这个量"
          % (d["client_offset"]["dx"], d["client_offset"]["dy"]))
    # 往返验证
    s = to_screen(d["hwnd"], 0, 0)
    b = to_client(d["hwnd"], s[0], s[1]) if s else None
    print("  往返验证    : client(0,0) -> screen%s -> client%s  %s"
          % (s, b, "一致" if b == (0, 0) else "不一致!"))
    # 客户区截图尺寸与 GetClientRect 是否一致（决定截图坐标能否直接相加）
    print("  提示: 窗口截图尺寸应等于 client %dx%d；若不等，截图前需先激活窗口"
          % (d["client"]["w"], d["client"]["h"]))


if __name__ == "__main__":
    if "--raw" in sys.argv:
        print(json.dumps([raw_window(d["hwnd"]) for d in list_windows()],
                         ensure_ascii=False, indent=2))
    elif "--activate" in sys.argv:
        # 用法: python win_ctx.py --activate <标题子串>
        if len(sys.argv) < 3:
            print("用法: python win_ctx.py --activate <窗口标题子串>")
        else:
            d, msg = resolve(sys.argv[2])
            print(msg)
            if d:
                ok = activate(d["hwnd"])
                print("激活%s  hwnd=%d  %s" % ("" if ok else "失败", d["hwnd"],
                                              d["title"][:40]))
    elif "--contains" in sys.argv:
        # 用法: python win_ctx.py --contains <标题子串> x y
        if len(sys.argv) < 5:
            print("用法: python win_ctx.py --contains <窗口标题子串> x y")
        else:
            d, msg = resolve(sys.argv[2])
            x, y = int(sys.argv[3]), int(sys.argv[4])
            print("%s\n  点 (%d,%d) 在窗口内: %s" % (msg, x, y, contains(d, x, y)))
    elif "--verify" in sys.argv:
        verify()
    elif "--calibrate" in sys.argv:
        # 用法: python win_ctx.py --calibrate <窗口截图> <整屏截图> [roi_x,roi_y,w,h]
        if len(sys.argv) < 4:
            print("用法: python win_ctx.py --calibrate 窗口.png 整屏.png [x,y,w,h]")
        else:
            roi = None
            if len(sys.argv) > 4:
                roi = tuple(int(v) for v in sys.argv[4].split(","))
            calibrate_report(sys.argv[2], sys.argv[3], roi=roi)
    elif "--json" in sys.argv:
        dump(as_json=True)
    elif "--all" in sys.argv:
        dump(interactive_only=False)
    else:
        dump(interactive_only=True)
