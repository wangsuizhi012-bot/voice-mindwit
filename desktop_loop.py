# -*- coding: utf-8 -*-
"""桌面操作闭环: observe -> locate -> act -> verify -> retry

补齐 voice-assistant 现有链路缺失的最后一环「验证」。

现状缺口（2026-09-11 实测）:
  - locate.py / visual_click.py 都是「一次定位 + 一次点击」就结束, 点完不看结果;
  - 点空了没有任何反馈, 上层无法知道成败, 也就无法形成自动化循环。

本模块提供三件事:
  1. act_and_verify(target, expect)  —— 点一下, 再看屏幕, 没变化/没达成则重试
  2. run_steps(steps)               —— 多步任务顺序执行, 任一步失败即中止
  3. check_vl(img, question)        —— 用本地 VL(:1235) 判读界面状态

定位优先级（坐标来源必须可靠, VL 直出坐标实测偏 ~100px, 绝不用）:
  1) UIA 控件树   locate.find_control   —— 零显存, 标准软件最准
  2) UI-TARS      :1237                 —— 专用 grounding, 需先起服务(占 ~5.3G 显存)
  3) 网格动作空间  :1235                 —— 兜底, 粗定位

依赖: pyautogui / numpy / requests / pillow（voice-assistant venv 已具备）
运行时: E:\\AI\\_experiments\\funasr-test\\venv\\Scripts\\python.exe
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VL_BASE = "http://localhost:1235/v1"      # 语义判读
UITARS_BASE = "http://localhost:1237/v1"  # 精准定位（可选）

# 判定「屏幕发生了变化」的阈值: 变化像素占比
CHANGE_THRESHOLD = 0.002
# 图像灰度差阈值（抗 JPEG/渲染抖动）
PIXEL_TOLERANCE = 12


def _session():
    """本机 HTTP 会话: trust_env=False 关闭系统代理。

    ⚠️ 本机装有代理（http_proxy=http://127.0.0.1:7971）。若不关，
    对 127.0.0.1:1235/1237 的本地请求会被代理转发, 连不上时返回
    HTTP 502 而不是抛连接异常 —— 健康检查会误判「在线」, 且请求被
    额外绕一圈。对回环地址必须直连。
    """
    import requests
    s = requests.Session()
    s.trust_env = False
    return s


# ---------------------------------------------------------------- 感知
def grab():
    """截全屏, 返回 (PIL.Image, ndarray)。"""
    import pyautogui
    img = pyautogui.screenshot()
    return img, np.asarray(img.convert("RGB"), dtype=np.int16)


def diff_ratio(a, b):
    """两张同尺寸图的变化像素占比, 0~1。尺寸不同视为全变。"""
    if a.shape != b.shape:
        return 1.0
    d = np.abs(a - b).max(axis=2)
    return float((d > PIXEL_TOLERANCE).mean())


# ---------------------------------------------------------------- 语义判读
def vl_backend():
    """语义模型后端：网关优先，否则直连 :1235。返回 (base, model|None, 说明)。

    2026-09-11：llama-swap 网关（:9292）已接管模型加载/卸载，
    走网关就不用自己起服务、也不用担心显存不够（网关会停旧起新）。
    """
    try:
        import visual_click as vc
        return vc.pick_backend("vl")
    except Exception as e:
        return VL_BASE, None, "直连（选后端失败: %r）" % (e,)


def check_vl(img, question, base=None, model=None, timeout=180):
    """用本地 VL 判读截图。失败返回带 err: 前缀的字符串, 不抛异常。

    base/model 留空则自动选后端（网关优先）。
    timeout 默认 180s：走网关时首次请求要等模型加载，别沿用直连时代的 120s。
    """
    import base64
    import io

    if base is None:
        base, model, _ = vl_backend()
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
        "temperature": 0,
        "max_tokens": 120,
    }
    if model:
        payload["model"] = model
    try:
        r = _session().post(base.rstrip("/") + "/chat/completions",
                            json=payload, timeout=timeout)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return "err:" + repr(e)


# ---------------------------------------------------------------- 定位
# 最近一次 locate() 的诊断信息（供自检 / 排查读取）
LAST_LOCATE = {}

# nuphus perceive 需要截图落盘，复用同一路径避免堆积文件
_PERCEIVE_IMG = os.path.join(HERE, "shots", "_perceive.png")


def screen_size():
    """屏幕尺寸。定位坐标必须落在这个范围内, 否则一律视为定位失败。"""
    import pyautogui
    return pyautogui.size()


def sanify(pt):
    """坐标合法性检查。

    ⚠️ 教训（2026-09-11）：窗口截图与屏幕**不是 1:1**。
    实测 WorkBuddy: 截图 1258x664 vs 客户区 1280x672，映射为
    屏幕 = (2,0) + 截图 × (1.0155, 1.0230) —— 两轴比例还不相等。
    所以任何来自「窗口截图」的坐标在点击前都必须过标定；
    而来自**全屏截图**的坐标是 1:1，可直接用。

    这里做最底线的一层：越界坐标直接判失败，绝不点出去。
    """
    w, h = screen_size()
    x, y = int(pt[0]), int(pt[1])
    if not (0 <= x < w and 0 <= y < h):
        return None, "坐标越界 (%d,%d) 不在 %dx%d 屏幕内" % (x, y, w, h)
    return (x, y), ""


def clamp_region(rect):
    """把屏幕矩形夹到屏幕内，返回 (region|None, ox, oy)。

    为什么必须夹（实测 2026-09-11）：
      WorkBuddy 最大化窗口 rect = (-8,-8,1296,688)，原点为负。
      `pyautogui.screenshot(region=(-8,-8,...))` **不报错也不夹边，而是静默补黑边**：
      实测 crop 前 8 列像素全为 0，而真实背景是 31；对照全屏图，
      补黑边语义 MSE=0.01（成立），夹边语义 MSE=256.69（不成立）。
      也就是 crop(x=8) 才对应 screen(x=0)，所以「加回原点 -8」在数学上仍自洽。

      但那条黑边会一起喂给视觉模型（UI-TARS），既虚增尺寸也可能干扰 grounding。
      统一夹取后所有路径都变成「真实裁剪 + 原点偏移」，干净且一致。

    返回 (None, 0, 0) 表示区域完全在屏幕外。
    """
    if not rect:
        return None, 0, 0
    sw, sh = screen_size()
    x, y, w, h = [int(v) for v in rect]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(sw, x + w), min(sh, y + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None, 0, 0
    return (x0, y0, x1 - x0, y1 - y0), x0, y0


def resolve_window(window):
    """解析目标窗口。返回 (win|None, rect|None, 说明)。

    window=None -> 前台窗口。指定标题/进程名子串 -> resolve 解析。
    非可交互窗口（幽灵）一律拒绝，避免后续点到空气。
    """
    import win_ctx
    if window:
        win, msg = win_ctx.resolve(window)
        if not win:
            return None, None, "窗口解析失败: " + msg
        ok, why = win_ctx.usable(win)
        if not ok:
            return None, None, "目标窗口不可操作（%s），拒绝执行" % why
        return win, win_ctx.window_rect(win), msg
    win = win_ctx.foreground()
    if not win:
        return None, None, "取不到前台窗口"
    return win, win_ctx.window_rect(win), "前台窗口: %s" % (win["title"][:40] or "(无标题)")


def locate(target, window=None, use_uitars=True, use_grid=False, snap=True,
           uitars_crop=False):
    """统一接地：解析目标窗口 -> **只在该窗口内**定位 -> 校验落点。

    定位优先级（按 零显存 / 快 / 准 排序）：
      1) UIA 控件树（限定 hwnd+rect）     标准软件最准，零显存，~50ms
      2) nuphus perceive（PaddleOCR+YOLO）Electron/自绘界面唯一可用，零显存，~1.3s
      3) 模板匹配（限定 region）          需先教学存模板
      4) easyocr（限定 region）           兜底 OCR —— 实测比 perceive 慢且更不准，仅兜底
      5) UI-TARS（全屏，:1237）           占 5.3G 显存，按需；实测平均 8~49px 有噪声
      6) 网格动作空间                     默认关闭 —— 实测偏 ~100px 不可用

    window: 目标窗口标题/进程名子串；None=前台窗口。
    snap  : 命中后吸附到「整行可点区域」中心（容错更大），默认开。
    uitars_crop: UI-TARS 是否裁剪到窗口。默认 False（实测裁剪更差，见下方注释）。

    返回 ((x, y), 方法说明) 或 (None, 失败原因)
    """
    global LAST_LOCATE
    # ⚠️ 必须直接别名 LAST_LOCATE，不能 `LAST_LOCATE.update(diag)` ——
    #    update 是浅拷贝，之后对 diag 的写入（window_msg/tried/...）不会同步，
    #    外部读 LAST_LOCATE 会看到一堆 None（踩过）。同时保留字典同一性，
    #    外部持有的引用也能看到最新内容。
    LAST_LOCATE.clear()
    LAST_LOCATE.update({"target": target, "window": None, "rect": None,
                        "tried": [], "method": None, "snapped": False})
    diag = LAST_LOCATE

    win, rect, wmsg = resolve_window(window)
    diag["window_msg"] = wmsg
    diag["window"] = win
    diag["rect"] = rect
    if not win:
        return None, wmsg
    hwnd = win["hwnd"]

    def _ok(pt, how):
        """落点校验：必须在目标窗口矩形内。"""
        if not pt:
            return None
        if rect:
            import win_ctx
            if not win_ctx.contains(win, pt[0], pt[1]):
                diag["tried"].append("%s -> %s 落在窗口外，弃用" % (how, pt))
                return None
        diag["method"] = how
        return pt, how

    # 1) UIA 控件树（限定窗口 + 矩形双保险）
    try:
        from locate import find_control
        pt, info = find_control(target, hwnd=hwnd, rect=rect)
        diag["tried"].append("uia: " + str(info)[:70])
        r = _ok(pt, "uia")
        if r:
            return r
    except Exception as e:
        diag["tried"].append("uia: " + repr(e))

    # 2) nuphus perceive —— 零显存的 PaddleOCR + YOLO，Electron 界面主力
    perceive_els = None
    try:
        import nuphus_bridge as nb
        img, _ = grab()
        os.makedirs(os.path.dirname(_PERCEIVE_IMG), exist_ok=True)
        img.save(_PERCEIVE_IMG)
        perceive_els = nb.perceive(_PERCEIVE_IMG)
        diag["perceive_counts"] = (len(perceive_els["texts"]), len(perceive_els["icons"]))
        pt, info = nb.find_with_snap(target, perceive_els,
                                     rect=rect, snap=snap)
        diag["tried"].append("perceive: " + str(info)[:80])
        if info.startswith("nuphus") and "->" in info:
            diag["snapped"] = True
        r = _ok(pt, "perceive")
        if r:
            return r
    except Exception as e:
        diag["tried"].append("perceive: " + repr(e))

    # 3) 模板匹配（限定窗口 region，夹到屏幕内避免黑边 padding）
    try:
        from locate import find_by_template
        treg, tox, toy = clamp_region(rect)
        pt, info = find_by_template(target, retry_s=1.0, region=treg) if treg else \
            (None, "无有效窗口区域")
        diag["tried"].append("template: " + str(info)[:70])
        r = _ok(pt, "template")
        if r:
            return r
    except Exception as e:
        diag["tried"].append("template: " + repr(e))

    # 4) easyocr（限定窗口 region）
    try:
        from locate import find_by_ocr_rect
        pt, info = find_by_ocr_rect(target, rect) if rect else (None, "无窗口区域")
        diag["tried"].append("easyocr: " + str(info)[:70])
        r = _ok(pt, "easyocr")
        if r:
            return r
    except Exception as e:
        diag["tried"].append("easyocr: " + repr(e))

    # 5) UI-TARS —— 默认用**全屏**截图（1:1，零映射）
    #    ⚠️ 实测反直觉（2026-09-11 同屏背靠背 A/B）：
    #      裁剪到窗口再问并不更准，反而更差 ——
    #        助理   全屏 8px  / 裁剪 61px
    #        自动化 全屏 10px / 裁剪 31px
    #        资料库 全屏 49px / 裁剪 13px（唯一裁剪更好的）
    #      误差无系统性方向（y 一向准、x 抖动），是模型本身噪声，不是裁剪引入的偏移；
    #      且全屏结果两次独立测试完全复现（7/10/49），裁剪每次不同。
    #      => 「裁剪省 token 又更聚焦」的假设不成立。默认全屏；窗口很小时才考虑裁剪。
    if use_uitars:
        try:
            from visual_click import _screenshot_b64, _uitars_alive, _uitars_point
            if _uitars_alive(UITARS_BASE):
                if uitars_crop:
                    rgn, ox, oy = clamp_region(rect)
                else:
                    rgn, ox, oy = None, 0, 0
                b64, (w, h) = _screenshot_b64(rgn)
                pt = _uitars_point(UITARS_BASE, b64, target, w, h)
                if pt:
                    pt = (pt[0] + ox, pt[1] + oy)
                diag["tried"].append("uitars: %s (%dx%d%s)"
                                     % (pt, w, h, " 裁剪" if rgn else " 全屏"))
                r = _ok(pt, "uitars")
                if r:
                    return r
            else:
                diag["tried"].append("uitars: :1237 未启动")
        except Exception as e:
            diag["tried"].append("uitars: " + repr(e))

    # 6) 网格动作空间 —— 实测偏 ~100px，仅显式开启时用（保留是为了留证据，不是推荐）
    if use_grid:
        try:
            from visual_click import _ask_cell, _detect_vl_model, _parse_cell, _screenshot_b64
            greg, gox, goy = clamp_region(rect)
            b64, (w, h) = _screenshot_b64(greg)
            cell = _parse_cell(_ask_cell(VL_BASE, _detect_vl_model(VL_BASE), b64, target))
            if cell:
                r, c = cell
                pt = (int((c - 0.5) * w / 3.0) + gox,
                      int((r - 0.5) * h / 3.0) + goy)
                diag["tried"].append("grid: %s（不可靠）" % (pt,))
                r2 = _ok(pt, "grid")
                if r2:
                    return r2
        except Exception as e:
            diag["tried"].append("grid: " + repr(e))

    return None, "全部定位器失败 | " + " ; ".join(diag["tried"][-3:])


# ---------------------------------------------------------------- 闭环
def act_and_verify(target, expect=None, attempts=3, settle=1.2, dry_run=False,
                   window=None, snap=True, do_activate=True):
    """点一下 target, 验证结果, 不达标则重试。

    target : 目标文字（如 "自动化"）
    expect : 期望达成什么（自然语言, 交给 VL 判读；None 则只要求「屏幕有响应」）
    window : 限定操作的目标窗口（标题/进程名子串）；None=前台窗口。
             指定后：定位只在该窗口内 + 点击前先把该窗口切到前台 +
             落点必须在该窗口矩形内（否则拒绝点击）。
    dry_run: True 时只定位不点击（用于安全演练 / 排查定位是否准）

    返回 (bool, str)
    """
    import pyautogui
    import win_ctx

    win, rect, wmsg = resolve_window(window)
    if not win:
        return False, wmsg

    # 点击前把目标窗口切到前台：否则点击可能落到别的窗口上（实测后台窗口收不到）
    # ⚠️ 激活后必须**重新解析一次窗口**：最小化窗口的 rect 是 -32000 这种无效值，
    #    恢复后位置才有效；沿用激活前的 rect 会让范围校验把正确落点判成「窗口外」。
    if do_activate and not dry_run:
        if not win_ctx.activate(win["hwnd"]):
            print("  ⚠️ 窗口激活失败: %s" % wmsg)
        time.sleep(0.3)
        win2, rect2, wmsg2 = resolve_window(window)
        if win2:
            win, rect, wmsg = win2, rect2, wmsg2

    for i in range(1, attempts + 1):
        before_img, before = grab()
        pt, how = locate(target, window=window, snap=snap)
        if not pt:
            return False, "定位失败(%s): %s" % (how[:80], target)
        # 越界坐标一律不点（窗口截图坐标没标定时最容易踩这个）
        pt, bad = sanify(pt)
        if not pt:
            return False, "%s: %s" % (bad, target)
        # 落点必须在目标窗口内 —— 定位器已校验，这里再兜一道
        if rect and not win_ctx.contains(win, pt[0], pt[1]):
            return False, "落点 %s 不在窗口「%s」内，拒绝点击" % (pt, win["title"][:24])

        if dry_run:
            return True, "dry-run 命中 %s @ %s（窗口: %s）" % (how[:70], pt,
                                                              win["title"][:24] or "?")
        pyautogui.click(pt[0], pt[1])
        time.sleep(settle)
        after_img, after = grab()
        chg = diff_ratio(before, after)

        if chg < CHANGE_THRESHOLD:
            print("  [%d/%d] %s @%s 点击后屏幕无变化 -> 重试"
                  % (i, attempts, target, pt))
            continue

        if expect:
            ans = check_vl(after_img,
                           "界面刚刚发生了变化。请判断：%s\n"
                           "如果已经达成只回答「是」，否则只回答「否」。"
                           "只回复一个字，不要解释。" % expect)
            if ans.startswith("err:"):
                return True, "%s @%s 已响应(变化%.1f%%), 但 VL 判读不可用: %s" % (
                    target, pt, chg * 100, ans[4:40])
            if ans.strip().startswith("是"):
                return True, "%s @%s 达成目标(变化%.1f%%)" % (target, pt, chg * 100)
            print("  [%d/%d] %s @%s 有变化但未达成(%s) -> 重试"
                  % (i, attempts, target, pt, ans.strip()[:20]))
            continue

        return True, "%s @%s 有响应(变化%.1f%%)" % (target, pt, chg * 100)

    return False, "重试 %d 次仍未达成: %s" % (attempts, target)


def run_steps(steps, dry_run=False, window=None):
    """顺序执行多步任务。steps = [{"target":..., "expect":...}, ...]

    任一步失败立即中止（不做危险动作的无脑继续）。
    window 可被单步的 "window" 覆盖。
    返回 (bool, [每步结果字符串])
    """
    log = []
    for idx, st in enumerate(steps, 1):
        target = st["target"]
        expect = st.get("expect")
        print("[步骤 %d/%d] %s" % (idx, len(steps), target))
        ok, msg = act_and_verify(target, expect, attempts=st.get("attempts", 3),
                                 dry_run=dry_run,
                                 window=st.get("window", window))
        log.append("%d. %s -> %s | %s" % (idx, target, "OK" if ok else "FAIL", msg))
        print("   " + msg)
        if not ok:
            return False, log
    return True, log


# ---------------------------------------------------------------- 精度实测
DEFAULT_ACC_IMG = os.path.join(HERE, "shots", "acc_test.png")


def accuracy(cases, image_path=None):
    """用同一张真实截图, 对比各定位器与本地 OCR 真值的误差（px）。

    cases = [(目标文字, (真值x, 真值y)), ...]，真值来自 nuphus
    desktop_perceive 的 OCR center（已实测准确）。
    合格线 25px；<80px 算偏差；>80px 会点错。
    """
    import base64
    import math

    from PIL import Image

    p = image_path or DEFAULT_ACC_IMG
    if not os.path.exists(p):
        print("测试图不存在: %s（先截一张全屏 PNG）" % p)
        return []
    b64 = base64.b64encode(open(p, "rb").read()).decode("ascii")
    W, H = Image.open(p).size

    try:
        from visual_click import _uitars_alive, _uitars_point
    except Exception as e:
        print("无法导入 visual_click: %r" % (e,))
        return []

    ua = _uitars_alive(UITARS_BASE)
    print("=" * 68)
    print("定位精度实测   图: %s (%dx%d)   真值: 本地 OCR" % (os.path.basename(p), W, H))
    print("=" * 68)
    print("%-10s %-13s %-13s %-9s %s" % ("目标", "真值", "UI-TARS", "误差", "判定"))
    print("-" * 68)

    errs = []
    if not ua:
        print("UI-TARS :1237 未启动 —— 执行 start-uitars-server.bat 后重测")
    for target, gt in cases:
        if not ua:
            continue
        pt = _uitars_point(UITARS_BASE, b64, target, W, H)
        if not pt:
            print("%-10s %-13s %-13s %-9s %s" % (target, "%d,%d" % gt, "-", "-", "未解析"))
            continue
        d = math.hypot(pt[0] - gt[0], pt[1] - gt[1])
        verdict = "准" if d < 25 else ("偏差" if d < 80 else "会点错")
        errs.append(d)
        print("%-10s %-13s %-13s %-9s %s"
              % (target, "%d,%d" % gt, "%d,%d" % pt, "%.0fpx" % d, verdict))

    if errs:
        print("-" * 68)
        print("平均 %.0fpx ｜ 最好 %.0fpx ｜ 最差 %.0fpx ｜ 合格线 25px"
              % (sum(errs) / len(errs), min(errs), max(errs)))
    print("=" * 68)
    return errs


# ---------------------------------------------------------------- 自检
def selfcheck():
    """不点击任何东西, 只验证各个环是否就绪。"""
    print("=" * 62)
    print("桌面操作闭环 · 分段自检（只读, 不点击）")
    print("=" * 62)

    # 1 感知
    t0 = time.time()
    img, arr = grab()
    print("[1] 截图       OK  %dx%d  耗时 %.2fs" % (img.width, img.height, time.time() - t0))

    # 2 变化检测
    _, arr2 = grab()
    print("[2] 变化检测   OK  帧间变化 %.3f%%（静置应接近 0）" % (diff_ratio(arr, arr2) * 100))

    # 3 接地
    try:
        from locate import _enum_uia
        import win_ctx
        fg = win_ctx.foreground()
        items, err = _enum_uia(timeout=4.0, hwnd=fg["hwnd"] if fg else None)
        if err:
            print("[3] UIA 控件树  --  %s" % err)
        else:
            named = [i for i in items if (i[0] or "").strip()]
            print("[3] UIA 控件树  OK  控件 %d 个（有名字 %d 个）"
                  % (len(items), len(named)))
            # ⚠️ 2026-09-11 纠正：之前这里把「0 控件」一律解释成
            #    「前台是 Electron/自绘界面」，是错的 —— 真因是 uiautomation
            #    2.0.29 的 API 名不匹配（BoundingRect vs BoundingRectangle），
            #    被 except:pass 静默吞掉。修复后 WorkBuddy(Electron) 也有 160 个控件。
            if named:
                sample = [i[0] for i in named[:4]]
                print("     示例        %s" % (" | ".join(s[:16] for s in sample)))
            else:
                print("     → 该窗口确实不暴露有名字的控件，改用 perceive(OCR) / UI-TARS")
    except Exception as e:
        print("[3] UIA 控件树  --  %r" % (e,))

    # 4 语义后端（2026-09-11：改为 llama-swap 网关优先，不再自己管端口/显存）
    gw = False
    try:
        import visual_click as vc
        gw = vc.gateway_alive()
        if gw:
            print("[4] 后端选择    OK  llama-swap 网关 :9292（模型按需加载，空闲自动卸载）")
            for want in ("vl", "uitars"):
                b, mdl, _ = vc.pick_backend(want)
                print("    %-8s    %s  model=%s" % (want, b, mdl))
        else:
            print("[4] 后端选择    --  网关 :9292 未启动 -> 回退直连端口")
    except Exception as e:
        print("[4] 后端选择    --  %r" % (e,))

    # 直连端口探测（走网关时它们本就不该监听——监听反而会和网关抢显存）
    sess = _session()
    for name, url in (("VL :1235", VL_BASE), ("UI-TARS :1237", UITARS_BASE)):
        hb = url.rsplit("/v1", 1)[0] + "/health"
        try:
            r = sess.get(hb, timeout=3)
            # 必须校验状态码: 代理会回 502 而不抛异常
            if r.status_code == 200:
                tag = "OK  在线"
                if gw:
                    tag += "（⚠️ 已走网关，这里再常驻会抢显存）"
                print("    %-13s %s" % (name, tag))
        except Exception:
            pass

    # 5 执行器
    import importlib.util
    ok = importlib.util.find_spec("pyautogui") is not None
    print("[5] 执行器      %s  pyautogui" % ("OK " if ok else "-- "))

    # 6 窗口上下文（前台窗口 + 幽灵窗口过滤）
    try:
        import win_ctx
        all_w = win_ctx.list_windows(interactive_only=False)
        live = [d for d in all_w if d["interactive"]]
        fg = win_ctx.foreground()
        print("[6] 窗口上下文  OK  共 %d 个窗口, 可交互 %d 个, 滤除幽灵 %d 个"
              % (len(all_w), len(live), len(all_w) - len(live)))
        if fg:
            print("    前台        %s [%s] @ %d,%d %dx%d  客户区偏移 +%d,+%d"
                  % (fg["title"][:30] or "(无标题)", fg["process_name"],
                     fg["window"]["x"], fg["window"]["y"],
                     fg["window"]["w"], fg["window"]["h"],
                     fg["client_offset"]["dx"], fg["client_offset"]["dy"]))
    except Exception as e:
        print("[6] 窗口上下文  --  %r" % (e,))

    # 7 窗口内接地（本轮新增：范围约束 + nuphus perceive）
    try:
        import nuphus_bridge as nb
        import win_ctx
        img, _ = grab()
        os.makedirs(os.path.dirname(_PERCEIVE_IMG), exist_ok=True)
        img.save(_PERCEIVE_IMG)
        t0 = time.time()
        els = nb.perceive(_PERCEIVE_IMG)
        dt = time.time() - t0
        fg = win_ctx.foreground()
        rect = win_ctx.window_rect(fg) if fg else None
        ins = [e for e in els["texts"]
               if rect and win_ctx.contains(fg, e.get("cx", e["x"]), e.get("cy", e["y"]))]
        print("[7] 窗口内接地  OK  perceive %d 文本 + %d 图标  %.2fs（零显存）"
              % (len(els["texts"]), len(els["icons"]), dt))
        print("    范围约束    %d/%d 个元素落在前台窗口内（用于排除其他窗口同名文字）"
              % (len(ins), len(els["texts"])))
        # 吸附能力实测：文本中心能否被「整行可点区域」包住
        snapped = sum(1 for e in els["texts"]
                      if nb.snap_to_clickable((e.get("cx", 0), e.get("cy", 0)), els,
                                              text_h=e.get("h"))[0])
        print("    整行吸附    %d/%d 个文本可吸附到更大可点区域（提升点击容错）"
              % (snapped, len(els["texts"])))
    except Exception as e:
        print("[7] 窗口内接地  --  %r" % (e,))

    print("=" * 62)


if __name__ == "__main__":
    # 真值来自 nuphus desktop_perceive 对 shots/acc_test.png 的 OCR 结果
    ACC_CASES = [
        ("助理", (50, 141)),
        ("自动化", (58, 240)),
        ("资料库", (56, 272)),
        ("默认权限", (476, 608)),
    ]

    def _opt(flag, default=None):
        """取 --flag value 形式的选项值。"""
        a = sys.argv
        if flag in a:
            i = a.index(flag)
            if i + 1 < len(a):
                return a[i + 1]
        return default

    # 位置参数 = 非选项、且不是 --window 的值
    _win = _opt("--window")
    _pos = []
    skip_next = False
    for v in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if v == "--window":
            skip_next = True
            continue
        if v.startswith("--"):
            continue
        _pos.append(v)

    snap = "--no-snap" not in sys.argv

    if _pos and _pos[0] == "selfcheck":
        selfcheck()
    elif _pos and _pos[0] == "accuracy":
        accuracy(ACC_CASES)
    elif _pos and _pos[0] == "windows":
        import win_ctx
        win_ctx.dump()
    elif _pos and _pos[0] == "locate":
        # 只定位不点击（最安全的排查入口）
        if len(_pos) < 2:
            print("用法: python desktop_loop.py locate <目标文字> [--window 窗口名] [--no-snap]")
        else:
            pt, how = locate(_pos[1], window=_win, snap=snap)
            print("命中: %s @ %s" % (pt, how) if pt else "失败: %s" % how)
            d = LAST_LOCATE
            print("窗口: %s" % (d.get("window_msg") or "-"))
            print("矩形: %s" % (d.get("rect"),))
            for t in d.get("tried", []):
                print("  · %s" % t)
    elif _pos:
        # 用法: python desktop_loop.py "目标文字" ["期望结果"] [--dry] [--window 名]
        tgt = _pos[0]
        exp = _pos[1] if len(_pos) > 1 else None
        ok, msg = act_and_verify(tgt, exp, dry_run="--dry" in sys.argv,
                                 window=_win, snap=snap)
        print(("OK  " if ok else "FAIL") + msg)
    else:
        print(__doc__)
        print("子命令: selfcheck | accuracy | windows | locate <目标> | <目标> [期望] [--dry]")
        print("通用选项: --window <标题子串>  --no-snap  --dry")
