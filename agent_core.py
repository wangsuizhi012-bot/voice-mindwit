# -*- coding: utf-8 -*-
"""agent_core.py —— 对标主流 computer-use 框架的编排核心

## 2026-09-11 与主流逐项对比后，补齐自建链路缺的六件事
（每条都写明对标项目，避免「自己另发明一套」）

| # | 能力 | 对标项目 | 自建补之前的真实缺陷 |
|---|---|---|---|
| 1 | 统一动作空间 | ByteDance **UI-TARS** | 只有 `click` —— 连「输入文字」「按快捷键」都没有 |
| 2 | 确定性后置条件 | **OpenAdapt** "Completion Criteria" | 把「达成没」交给 VL 自由判是/否：慢、贵、不可复现 |
| 3 | fail-closed 验证 | **OpenAdapt** "verify or halt" | 「有变化就算成功」，会把「点错但界面动了」当成功 |
| 4 | 危险动作守门 | **cua** 沙箱 / OpenAdapt 人工审批 gate | 任何文字都能点，包括「卸载」「清空」「退出」 |
| 5 | 反思重试 | Simular **Agent-S3** reflection agent | 3 次重试是**同 target 同定位器**，必然同样失败 |
| 6 | 分层记忆 + DAG | **Agent-S3** 叙事/情景双层记忆 + **UFO³** 任务图 | 只有坐标记忆 + 线性 run_steps，失败无法重规划 |

## 设计取舍（有意为之）

- **反思用「规则 + VL 增强」而非纯 VL**：Agent-S3 的 reflection 是 VL 驱动的，
  但本地 8G 卡上每次反思都要等 VL（首次 5~20s）。规则先出一批**确定性强**的
  替代策略（换用词 / 换动作 / 先滚动 / 缩范围），只在规则全用完时才请 VL。
  失败模式是有限的、可枚举的 —— 能枚举就别问模型。
- **断言优先零显存确定性来源**（UIA 控件名 + 窗口标题 + 像素 diff），
  VL 只作最后的语义兜底，且**标注为「语义判定」**，不伪装成确定性证据。
- **fail-closed 是默认**：拿不到证据 = 不算成功 = 中止。这条直接抄 OpenAdapt
  的 "halts when it cannot prove the intended outcome"，是自动化敢无人值守的前提。

## 用法

    import agent_core as ac

    # 声明式任务（DAG）
    nodes = [
        {"id": "open", "do": "click", "target": "文件"},
        {"id": "save", "do": "click", "target": "保存", "depends_on": ["open"],
         "assert": [{"text_appeared": "保存"}]},
    ]
    ok, log, res = ac.run_dag(nodes, window="记事本")

    ac.run_dag(nodes, dry_run=True)      # 只定位不点击（安全演练）

    python agent_core.py selftest        # 自检（只读）
    python agent_core.py actions         # 打印动作空间与断言清单
    python agent_core.py plan '<json>'   # 跑一个 DAG
"""
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

STATE = os.path.join(HERE, "state")
STRAT_PATH = os.path.join(STATE, "agent_memory.json")

# ------------------------------------------------------------------ 动作空间
# 对标 UI-TARS 的 unified action space（见其 prompt 模板）:
#   click / left_double / right_single / drag / hotkey / type / scroll /
#   wait / finished / call_user
# 命名上保留 UI-TARS 的语义，但用更长的可读名（left_double -> double_click）。
ACTIONS = {
    "click":        "单击目标（需要 target）",
    "double_click": "双击目标（需要 target）",
    "right_click":  "右键目标（需要 target）",
    "type":         "输入文字（需要 text；非 ASCII 走剪贴板）",
    "hotkey":       "按组合键（需要 keys，如 ['ctrl','s']）",
    "scroll":       "滚动（需要 amount，正=上 负=下）",
    "drag":         "拖拽（需要 from / to）",
    "wait":         "等待（需要 seconds，默认 1）",
    "finished":     "声明任务完成（终止信号，UI-TARS 的 finished()）",
    "call_user":    "交回人类（终止信号，UI-TARS 的 call_user()）",
}

# 断言类型：**确定性证据**（零显存）在前，语义兜底在后
ASSERTS = {
    "changed":         "屏幕变化比例 >= 值（默认 0.002）",
    "unchanged":       "屏幕变化比例 <  值（用于幂等/「不该动」的校验）",
    "text_appeared":   "出现文字（UIA 控件名 + OCR，确定性）",
    "text_gone":       "消失文字（确定性）",
    "window_appeared": "出现窗口标题子串（确定性）",
    "window_gone":     "窗口标题子串消失（确定性）",
    "vl":              "语义判定（兜底，非确定性证据，会标注）",
}

# ------------------------------------------------------------------ 危险守门
# 分级理由：卸载/格式化/转账 这类**不可撤销**的操作必须两道授权；
# 删除/退出/发送 这类可撤销或日常的，一次显式授权即可放行。
RISKY_HARD = ("格式化", "卸载", "清空回收站", "恢复出厂", "转账", "付款",
              "支付", "删除账号", "注销账号", "发送给", "群发")
RISKY_SOFT = ("删除", "移除", "重置", "退出", "关闭", "覆盖", "发送", "确认",
              "清空", "禁用", "注销", "关机", "重启", "确认支付", "提交订单")


def guard(step, allow_risky=False, allow_destructive=False):
    """危险动作守门（对标 cua 沙箱 / OpenAdapt 审批 gate）。

    默认**拒绝**：命中危险词且未显式授权 -> 不执行。这就是 fail-closed ——
    自动化敢无人值守的前提是「危险动作默认不做」，而不是「默认做」。

    返回 (ok, reason)。ok=False 时调用方必须中止该节点。
    """
    target = str(step.get("target") or "")
    text = str(step.get("text") or "")
    keys = " ".join(step.get("keys") or [])
    hay = target + " " + text
    hit_hard = [w for w in RISKY_HARD if w in hay]
    hit_soft = [w for w in RISKY_SOFT if w in hay]
    # Ctrl+Shift+Delete / Alt+F4 这类快捷键也算
    if "delete" in keys.lower() or "f4" in keys.lower():
        hit_soft.append("快捷键 " + keys)
    if hit_hard and not allow_destructive:
        return False, "⛔ 不可撤销操作 %s —— 需 allow_destructive 显式授权" % hit_hard
    if hit_soft and not (allow_risky or allow_destructive):
        return False, ("⛔ 危险操作 %s —— 需 allow_risky 显式授权（默认拒绝，"
                       "对标 cua 沙箱 gate）" % hit_soft)
    if hit_hard or hit_soft:
        return True, "⚠️ 已授权危险操作 %s" % (hit_hard + hit_soft)
    return True, ""


# ------------------------------------------------------------------ 零显存证据源
def _uia_names(hwnd=None):
    """当前窗口的 UIA 控件名集合（~50ms，零显存，确定性）。"""
    try:
        from locate import _enum_uia
        items, err = _enum_uia(timeout=2.0, hwnd=hwnd)
        if err:
            return set(), err
        return {(i[0] or "").strip() for i in items if (i[0] or "").strip()}, None
    except Exception as e:
        return set(), repr(e)


_PERCEIVE_TXT = os.path.join(HERE, "shots", "_assert.png")


def _perceive_texts(rect=None):
    """OCR 文本集合（~1.8s，零显存）。UIA 拿不到时的确定性来源。"""
    try:
        import som
        img, p = som.grab(_PERCEIVE_TXT)
        els = som.perceive(p, rect=rect)
        return {(e.get("text") or "").strip() for e in els["texts"]
                if (e.get("text") or "").strip()}, None
    except Exception as e:
        return set(), repr(e)


def _window_titles(interactive_only=False):
    """当前所有窗口标题（零显存，确定性）。"""
    try:
        import win_ctx
        return [d["title"] for d in win_ctx.list_windows(interactive_only=interactive_only)
                if d.get("title")], None
    except Exception as e:
        return [], repr(e)


def text_present(s, rect=None, hwnd=None):
    """文字是否出现在屏幕上。UIA 优先（快且准），OCR 兜底。

    返回 (bool|None, 来源说明)；None 表示**拿不到证据**（调用方按 fail-closed 处理）。
    """
    s = (s or "").strip()
    if not s:
        return None, "空查询"
    names, e1 = _uia_names(hwnd)
    if names and any(s in n for n in names):
        return True, "UIA"
    texts, e2 = _perceive_texts(rect)
    if texts:
        if any(s in t for t in texts):
            return True, "perceive(OCR)"
        # 两个来源**都拿到了数据**才能断言「不存在」
        if names or texts:
            return False, "UIA+perceive 均未见"
    if not names and not texts:
        return None, "证据源全不可用: uia=%s perceive=%s" % (e1, e2)
    return False, "未见"


# ------------------------------------------------------------------ 单动作执行
def _set_clipboard(text):
    """写 Unicode 文本到剪贴板（零依赖 ctypes）。

    ⚠️ 句柄必须声明成 64 位：`GlobalAlloc` 默认 restype 是 c_int，
    在 64 位下句柄会被截断成 32 位，SetClipboardData 拿到野指针 ——
    与 win_ctx 里 HWND 必须用 c_void_p 是同一类 bug。
    """
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]

    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = k32.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, buf, size)
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            return False
    finally:
        u32.CloseClipboard()
    return True


def _ensure_fg(hwnd, tries=15, gap=0.2):
    """确保窗口**真的**到了前台。返回 bool。

    ⚠️ 这条是实测逼出来的（2026-09-11）：
      `SetForegroundWindow` 返回成功 ≠ 窗口真的在前台。新建对话框/刚恢复的
      窗口在初始化期间会抢回焦点，于是：
        · 按键（hotkey/type）发给旧的前台窗口 -> 目标窗口纹丝不动
          （实测：对对话框发 Enter，对话框不关）
        · 点击变成"只激活不点击" -> 第一次点击白白浪费
      必须以 GetForegroundWindow 实测确认，不达标就重试。
      附带的安全意义：按键绝不落到"当前碰巧在前台"的窗口上（可能是用户的）。
    """
    import ctypes
    import win_ctx
    u32 = ctypes.windll.user32
    for _ in range(max(1, tries)):
        if int(u32.GetForegroundWindow()) == int(hwnd):
            return True
        try:
            win_ctx.activate(hwnd)
        except Exception:
            pass
        time.sleep(gap)
    return False


def _resolve_and_focus(window):
    """解析窗口 + 激活 + **确认已在前台** + **重新解析矩形**。

    ⚠️ 必须重新解析：最小化窗口的 rect 是 -32000 无效值，
    恢复后位置才有效，沿用旧 rect 会把正确落点判成「窗口外」。
    """
    import win_ctx
    import desktop_loop as dl
    win, rect, msg = dl.resolve_window(window)
    if not win:
        return None, None, msg
    if not _ensure_fg(win["hwnd"]):
        msg += "（⚠️ 未能确认置前，按键/点击可能落空）"
    time.sleep(0.15)
    win2, rect2, msg2 = dl.resolve_window(window)
    return (win2, rect2, msg2) if win2 else (win, rect, msg)


def act(step, window=None, dry_run=False):
    """执行一个动作。返回 (ok, msg, {"pt":..,"target":..})。

    只做「执行」，不做「验证」—— 验证交给 assert_conds，
    这样动作与判据解耦，才能组合出「同一个动作 + 不同判据」的复用。
    """
    import desktop_loop as dl
    import win_ctx
    import pyautogui

    do = (step.get("do") or "click").strip()
    if do not in ACTIONS:
        return False, "未知动作 %r（可用: %s）" % (do, " / ".join(ACTIONS)), {}

    if do == "finished":
        return True, "finished（动作空间终止信号）", {}
    if do == "call_user":
        return False, "call_user: %s" % (step.get("reason") or "需要人工介入"), {}
    if do == "wait":
        sec = float(step.get("seconds", 1))
        if not dry_run:
            time.sleep(sec)
        return True, "等待 %.1fs" % sec, {}

    if do in ("type", "hotkey", "scroll"):
        # ⚠️ 键盘输入是**发给当前前台窗口**的，不是发给某个坐标 ——
        #    所以必须先确保目标窗口在前台，否则文字/快捷键会落进任何
        #    恰好在前台的窗口（实测可能落进用户正在用的应用）。
        #    dry-run 时不做任何聚焦，保持"演练不改变系统状态"的语义。
        win_scope = step.get("window", window)
        focus_note = ""
        if not dry_run:
            if win_scope:
                w, _, m = _resolve_and_focus(win_scope)
                if not w:
                    return False, "键盘动作需要窗口在前台，但解析失败: " + m, {}
                focus_note = "（已聚焦 %s）" % (w["title"][:20] or m[:20])
            elif step.get("require_focus", True):
                return False, ("%s 未指定 window，按键会发给任意前台窗口 —— "
                               "已拒绝。请加 window=<目标窗口>，"
                               "确需发给当前前台则设 require_focus=False" % do), {}
        if dry_run:
            return True, "dry-run：将执行 %s %s" % (do, step.get("text") or step.get("keys")
                                                or step.get("amount")), {}
        if do == "type":
            text = str(step.get("text") or "")
            if text.isascii():
                pyautogui.write(text, interval=0.02)
                how = "pyautogui.write"
            else:
                # pyautogui.write 打不出非 ASCII（中文/emoji），走剪贴板粘贴
                if not _set_clipboard(text):
                    return False, "剪贴板写入失败（无法输入非 ASCII 文本）", {}
                pyautogui.hotkey("ctrl", "v")
                how = "剪贴板 + Ctrl+V"
            if step.get("submit"):
                time.sleep(0.1)
                pyautogui.press("enter")
                how += " + Enter"
            return True, "输入 %r（%s）%s" % (text[:20], how, focus_note), {}
        if do == "hotkey":
            keys = [str(k) for k in (step.get("keys") or [])]
            if not keys:
                return False, "hotkey 缺 keys", {}
            pyautogui.hotkey(*keys)
            return True, "按键 %s%s" % ("+".join(keys), focus_note), {}
        amt = int(step.get("amount", -3))
        px = step.get("at") or 0
        if px:
            pyautogui.scroll(amt, x=px[0], y=px[1])
        else:
            pyautogui.scroll(amt)
        return True, "滚动 %d 格%s" % (amt, focus_note), {}

    # 以下动作都需要先在屏幕上定位 target
    target = str(step.get("target") or "").strip()
    if not target:
        return False, "%s 需要 target" % do, {}

    win, rect, wmsg = _resolve_and_focus(window if window is not None else step.get("window"))
    if not win:
        return False, "窗口解析失败: " + wmsg, {}

    if do == "drag":
        tgt2 = str(step.get("to") or "").strip()
        if not tgt2:
            return False, "drag 需要 to", {}
        p1, how1 = dl.locate(target, window=step.get("window", window), snap=False)
        p2, how2 = dl.locate(tgt2, window=step.get("window", window), snap=False)
        if not p1 or not p2:
            return False, "drag 定位失败: from=%s(%s) to=%s(%s)" % (target, how1, tgt2, how2), {}
        p1, bad1 = dl.sanify(p1)
        p2, bad2 = dl.sanify(p2)
        if not p1 or not p2:
            return False, "drag 坐标越界: %s %s" % (bad1, bad2), {}
        if dry_run:
            return True, "dry-run：%s -> %s" % (p1, p2), {"pt": p1, "to": p2}
        pyautogui.moveTo(*p1)
        pyautogui.dragTo(p2[0], p2[1], duration=float(step.get("duration", 0.5)),
                         button="left")
        return True, "拖拽 %s -> %s（%s）" % (p1, p2, how1[:40]), {"pt": p1, "to": p2}

    pt, how = dl.locate(target, window=step.get("window", window),
                        snap=step.get("snap", True),
                        use_uitars=step.get("use_uitars", True))
    if not pt:
        return False, "定位失败(%s): %s" % (how[:70], target), {}
    pt, bad = dl.sanify(pt)
    if not pt:
        return False, bad, {}
    if rect and not win_ctx.contains(win, pt[0], pt[1]):
        return False, "落点 %s 不在窗口「%s」内，拒绝点击" % (pt, win["title"][:24]), {}
    if dry_run:
        return True, "dry-run 命中 %s @ %s（%s）" % (target, pt, how[:50]), {"pt": pt, "target": target}

    # locate() 可能花 1~2s（perceive 一次 1.8s），期间焦点会被别的东西抢走；
    # 点之前再确认一次前台，否则这一下点击会变成「只激活、不点击」。
    _ensure_fg(win["hwnd"], tries=6, gap=0.12)

    if do == "click":
        pyautogui.click(pt[0], pt[1])
    elif do == "double_click":
        pyautogui.doubleClick(pt[0], pt[1])
    elif do == "right_click":
        pyautogui.rightClick(pt[0], pt[1])
    else:
        return False, "未实现的动作 %s" % do, {}
    return True, "%s %s @ %s（%s）" % (do, target, pt, how[:50]), {"pt": pt, "target": target}


# ------------------------------------------------------------------ 确定性断言
def assert_conds(conds, before, after, window=None, rect=None, before_titles=None):
    """校验一组后置条件（对标 OpenAdapt 的 Completion Criteria）。

    返回 (status, details)，status ∈ {"ok","fail","unverified"}：
      - ok         : 全部条件通过，证据充分
      - fail       : 有条件明确不成立
      - unverified : **拿不到证据**（如 UIA/perceive/VL 全不可用）

    fail-closed 的关键：unverified 必须被上层当成**不成功**处理。
    「没证据」和「证据说不对」在无人值守里都不能算成功。
    """
    import desktop_loop as dl
    details, unverified = [], []

    if not isinstance(conds, (list, tuple)):
        conds = [conds]

    for c in conds:
        if not isinstance(c, dict) or not c:
            details.append("非法条件 %r" % (c,))
            return "fail", details
        (kind, val), = list(c.items())[:1]

        if kind == "changed":
            # ⚠️ bool 是 int 的子类：{"changed": True} 若直接 float() 会变成阈值 1.0
            #    （=100% 变化），静置屏幕永远不达标 —— 这个坑实测踩到了。
            thr = dl.CHANGE_THRESHOLD if isinstance(val, bool) or val is None else float(val)
            r = dl.diff_ratio(before, after)
            details.append("changed: %.3f%% vs 阈值 %.3f%% → %s"
                           % (r * 100, thr * 100, "OK" if r >= thr else "FAIL"))
            if r < thr:
                return "fail", details

        elif kind == "unchanged":
            thr = dl.CHANGE_THRESHOLD if isinstance(val, bool) or val is None else float(val)
            r = dl.diff_ratio(before, after)
            details.append("unchanged: %.3f%% vs 阈值 %.3f%% → %s"
                           % (r * 100, thr * 100, "OK" if r < thr else "FAIL"))
            if r >= thr:
                return "fail", details

        elif kind in ("text_appeared", "text_gone"):
            present, src = text_present(str(val), rect=rect)
            want = (kind == "text_appeared")
            if present is None:
                unverified.append("%s(%r): 无证据 — %s" % (kind, val, src))
                details.append("%s(%r): UNVERIFIED (%s)" % (kind, val, src))
                continue
            ok = (present == want)
            details.append("%s(%r): %s [%s] → %s"
                           % (kind, val, "在" if present else "不在", src,
                              "OK" if ok else "FAIL"))
            if not ok:
                return "fail", details

        elif kind in ("window_appeared", "window_gone"):
            titles, err = _window_titles()
            if err or not titles:
                unverified.append("%s(%r): 无证据 — %s" % (kind, val, err))
                details.append("%s(%r): UNVERIFIED (%s)" % (kind, val, err))
                continue
            hit = any(str(val) in t for t in titles)
            want = (kind == "window_appeared")
            # 也拿「前后标题差集」作为补充证据（新窗口更可靠）
            added = []
            if before_titles:
                added = [t for t in titles if t not in set(before_titles)]
            if not hit and added:
                hit = any(str(val) in t for t in added)
            ok = (hit == want)
            details.append("%s(%r): %s → %s"
                           % (kind, val, "命中" if hit else "未命中", "OK" if ok else "FAIL"))
            if not ok:
                return "fail", details

        elif kind == "vl":
            # 语义兜底 —— 明确标注为非确定性证据
            img = _pil_now()
            if img is None:
                unverified.append("vl: 截图失败")
                details.append("vl: UNVERIFIED（截图失败）")
                continue
            ans = dl.check_vl(img, "界面刚刚发生了变化。请判断：%s\n"
                                   "如果已经达成只回答「是」，否则只回答「否」。"
                                   "只回复一个字，不要解释。" % val)
            if ans.startswith("err:"):
                unverified.append("vl: VL 不可用 — %s" % ans[4:60])
                details.append("vl: UNVERIFIED（VL 不可用）")
                continue
            ok = ans.strip().startswith("是")
            details.append("vl[语义判定]: %r → %s" % (ans.strip()[:16], "OK" if ok else "FAIL"))
            if not ok:
                return "fail", details

        else:
            details.append("未知条件 %r" % (kind,))
            return "fail", details

    if unverified:
        return "unverified", details
    return "ok", details


def _pil_now():
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception:
        return None


# ------------------------------------------------------------------ 反思
def _clean_target(t):
    """去掉标注性括号/符号，取最可能被 OCR/UIA 命中的核心词。"""
    s = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", t or "")
    s = re.sub(r"[｜|·—\-—:：]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or (t or "").strip()


def reflect(step, failure, history, use_vl=True):
    """反思：给出**一个不同的做法**，而不是重复同一个动作。

    对标 Agent-S3 的 reflection agent。差别在于本实现**规则优先**：
    失败模式有限且可枚举，能枚举就别问模型（省 5~20s/次）。

    返回 {"action": "retry"|"call_user", "why":.., "step": <改造后的 step 或 None>}
    """
    target = str(step.get("target") or "")
    tried = set(history or [])
    strategies = []

    if "定位失败" in failure or "定位" in failure:
        c = _clean_target(target)
        if c and c != target and ("clean" not in tried):
            strategies.append(("clean", dict(step, target=c),
                               "换成清洗后的核心词 %r（去掉括号/符号噪声）" % c))
        if "scroll" not in tried:
            strategies.append(("scroll", {"do": "scroll", "amount": -3},
                               "先向下滚动，目标可能在视野外"))
        if step.get("use_uitars") is not False and "uitars_off" not in tried:
            strategies.append(("uitars_off", dict(step, use_uitars=False),
                               "关掉 UI-TARS，只用零显存的 UIA/OCR 定位器"))
        if "window_scope" not in tried:
            strategies.append(("window_scope", dict(step, window=None),
                               "放开窗口限制，允许在前台窗口内定位"))

    if "断言" in failure or "未达成" in failure or "UNVERIFIED" in failure:
        if step.get("do") in ("click", "double_click") and "alt_click" not in tried:
            alt = "double_click" if step.get("do") == "click" else "click"
            strategies.append(("alt_click", dict(step, do=alt),
                               "换成 %s（有些控件单击只选中、双击才打开）" % alt))
        if step.get("snap") is not False and "no_snap" not in tried:
            strategies.append(("no_snap", dict(step, snap=False),
                               "关掉整行吸附，直接用元素中心（避免吸到大卡片）"))
        if "wait_longer" not in tried:
            strategies.append(("wait_longer", {"do": "wait", "seconds": 2},
                               "多等 2s，界面可能还没渲染完"))

    if strategies:
        tag, new_step, why = strategies[0]
        return {"action": "retry", "tag": tag, "step": new_step,
                "why": "规则反思: " + why}

    # 规则用尽 -> 请 VL 看一眼当前屏幕，给最后一条建议
    if use_vl:
        img = _pil_now()
        if img is not None:
            import desktop_loop as dl
            q = ("我是桌面自动化程序。我要执行「%s %s」但失败了，原因是：%s。"
                 "请看当前屏幕，给一条**不同的**做法建议。"
                 "只回一行，格式：动作|目标|理由。动作只能是 click/double_click/type/hotkey/scroll。"
                 "如果确实无法完成，回复：call_user|无|原因"
                 % (step.get("do"), target, failure[:80]))
            ans = dl.check_vl(img, q)
            if not ans.startswith("err:"):
                parts = [p.strip() for p in ans.strip().splitlines()[0].split("|")]
                if len(parts) >= 2:
                    a, tg = parts[0].lower(), parts[1]
                    if a == "call_user":
                        return {"action": "call_user", "why": "VL 反思: " + (parts[2] if len(parts) > 2 else "无解")}
                    if a in ACTIONS:
                        ns = {"do": a}
                        if a in ("click", "double_click", "right_click"):
                            ns["target"] = tg or target
                        elif a == "type":
                            ns["text"] = parts[1]
                        elif a == "hotkey":
                            ns["keys"] = [k.strip() for k in tg.split("+") if k.strip()]
                        return {"action": "retry", "tag": "vl", "step": ns,
                                "why": "VL 反思: " + (parts[2] if len(parts) > 2 else "")}
    return {"action": "call_user", "why": "规则与 VL 反思均无可行替代方案"}


# ------------------------------------------------------------------ 分层记忆
# Agent-S3 是双层：叙事记忆（抽象策略，答「为什么这样做」）
#                   情景记忆（具体操作，答「具体怎么做」）
# 自建此前只有 locate.py 的 click_memory.json —— 那是**情景层**（坐标）。
# 这里补**叙事层**：一个任务的成功步骤链，下次可直接复用甚至零模型回放。
def _task_key(nodes):
    raw = json.dumps([{k: v for k, v in n.items() if k != "assert"}
                      for n in nodes], ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _load_strategies():
    try:
        with open(STRAT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_strategies(d):
    os.makedirs(STATE, exist_ok=True)
    tmp = STRAT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STRAT_PATH)   # 原子替换，避免写一半崩了留下坏 JSON


def save_strategy(goal, nodes, ok):
    """把这次任务的步骤链存进叙事记忆（成功才存，失败不污染）。"""
    d = _load_strategies()
    key = _task_key(nodes)
    rec = d.setdefault("tasks", {}).setdefault(key, {
        "goal": goal or "", "nodes": [], "ok": 0, "fail": 0, "last": ""})
    if ok:
        rec["nodes"] = [{k: v for k, v in n.items()} for n in nodes]
        rec["ok"] += 1
    else:
        rec["fail"] += 1
    rec["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_strategies(d)
    return key


def recall_strategy(nodes):
    """按任务结构取回上次成功的步骤链（叙事记忆回放）。"""
    d = _load_strategies()
    rec = (d.get("tasks") or {}).get(_task_key(nodes))
    if rec and rec.get("nodes") and rec.get("ok"):
        return rec["nodes"], rec
    return None, rec


def list_strategies():
    d = _load_strategies()
    return d.get("tasks", {})


# ------------------------------------------------------------------ DAG 编排
def _topo(nodes):
    """拓扑排序（Kahn）。返回 (有序节点, 错误)。

    对标 UFO³ 的 task graph：把线性 steps 升级成带依赖的图，
    好处是**失败只需重规划受影响的分支**，不必从头再来。
    """
    by_id = {}
    for i, n in enumerate(nodes):
        nid = n.get("id") or ("n%d" % (i + 1))
        n["id"] = nid
        by_id[nid] = n
    indeg = {nid: 0 for nid in by_id}
    children = {nid: [] for nid in by_id}
    for nid, n in by_id.items():
        for dep in (n.get("depends_on") or []):
            if dep not in by_id:
                return None, "节点 %s 依赖不存在的 %s" % (nid, dep)
            indeg[nid] += 1
            children[dep].append(nid)
    q = [nid for nid, d in indeg.items() if d == 0]
    order = []
    while q:
        nid = q.pop(0)
        order.append(by_id[nid])
        for c in children[nid]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if len(order) != len(by_id):
        return None, "依赖成环，无法排序"
    return order, None


def run_dag(nodes, dry_run=False, window=None, allow_risky=False,
            allow_destructive=False, reflect_retries=1, use_vl_reflect=True,
            strict=True, remember=True):
    """按依赖图执行任务。返回 (ok, log, results)。

    fail-closed（strict=True，默认）：节点只有在**断言全部通过**时才算成功；
    断言拿不到证据（unverified）同样视为不成功并中止 —— 抄 OpenAdapt
    "halt when it cannot prove the intended outcome"。

    与旧 run_steps 的区别：
      · 动作空间从「只有 click」扩到 10 个动作
      · 支持 depends_on（失败只影响下游分支，其余可继续）
      · 每步有确定性断言，而不是「有变化就算成功」
      · 失败会**反思并换策略**，而不是同 target 重试 3 次
    """
    import desktop_loop as dl

    order, err = _topo(nodes)
    if err:
        return False, ["拓扑排序失败: " + err], {}

    results, log = {}, []
    blocked = set()
    all_ok = True

    print("=" * 70)
    print("DAG 执行：%d 个节点%s" % (len(order), "（dry-run）" if dry_run else ""))
    print("=" * 70)

    for idx, n in enumerate(order, 1):
        nid = n["id"]
        deps = n.get("depends_on") or []

        # 上游失败 -> 本节点标记 blocked，不执行（不做无意义的连锁动作）
        bad_deps = [d for d in deps if d in blocked or not (results.get(d) or {}).get("ok")]
        if bad_deps:
            blocked.add(nid)
            msg = "阻塞：上游 %s 未成功" % bad_deps
            results[nid] = {"ok": False, "status": "blocked", "msg": msg}
            log.append("[%d] %s | BLOCKED | %s" % (idx, nid, msg))
            print("[%d/%d] %-10s BLOCKED  %s" % (idx, len(order), nid, msg))
            all_ok = False
            continue

        label = "%s %s" % (n.get("do", "click"), n.get("target") or n.get("text") or "")
        print("[%d/%d] %-10s %s" % (idx, len(order), nid, label.strip()))

        # 危险守门（默认拒绝）
        gok, greason = guard(n, allow_risky=allow_risky,
                             allow_destructive=allow_destructive)
        if not gok:
            results[nid] = {"ok": False, "status": "blocked", "msg": greason}
            blocked.add(nid)
            log.append("[%d] %s | BLOCKED | %s" % (idx, nid, greason))
            print("        ⛔ %s" % greason)
            all_ok = False
            continue
        if greason:
            print("        " + greason)

        conds = n.get("assert") or []
        tries, history, done = 0, [], False
        cur = n
        last_fail = ""

        while tries <= max(0, reflect_retries) and not done:
            tries += 1
            before = dl.grab()[1]
            before_titles = _window_titles()[0]
            win, rect, _ = dl.resolve_window(n.get("window", window))
            rect_use = rect

            ok, msg, geom = act(cur, window=n.get("window", window), dry_run=dry_run)
            logline = "[%d] %s | try%d | %s | %s" % (
                idx, nid, tries, "OK" if ok else "FAIL", msg)

            if not ok:
                last_fail = "动作失败: " + msg
                log.append(logline)
                print("        ✗ %s" % msg[:110])
                history.append("t%d-fact" % tries)
                if tries > reflect_retries:
                    break
                r = reflect(cur, last_fail, history, use_vl=use_vl_reflect)
                print("        ↻ %s" % r.get("why", ""))
                history.append(r.get("tag") or "?" + "-")
                history.append(r.get("tag") or "?")
                if r["action"] != "retry" or not r.get("step"):
                    last_fail += " | 反思: " + r.get("why", "")
                    break
                cur = r["step"]
                continue

            # 动作执行了 -> 必须验证（fail-closed）
            if dry_run or not conds:
                done = True
                log.append(logline + " | 无断言" + ("" if conds else "（仅执行）"))
                print("        ✓ %s%s" % (msg[:100], "" if conds else "（未声明断言）"))
                break

            time.sleep(float(n.get("settle", 0.8)))
            after = dl.grab()[1]
            status, details = assert_conds(conds, before, after,
                                          window=n.get("window", window),
                                          rect=rect_use, before_titles=before_titles)
            for d in details:
                print("        · %s" % d)
            log.append(logline + " | 断言 " + status + " | " + " ; ".join(details))

            if status == "ok":
                done = True
                break

            if status == "unverified" and not strict:
                # 非严格模式：拿不到证据时放行，但明确标注（默认不这样跑）
                done = True
                print("        ⚠️ 未验证放行（strict=False）")
                log.append(logline + " | 断言 unverified，非严格模式放行")
                break

            last_fail = "断言未通过(%s)" % status
            print("        ✗ 断言 %s" % status)
            history.append("t%d-assert-%s" % (tries, status))
            if tries > reflect_retries:
                break
            r = reflect(cur, last_fail, history, use_vl=use_vl_reflect)
            print("        ↻ %s" % r.get("why", ""))
            history.append(r.get("tag") or "?")
            if r["action"] != "retry" or not r.get("step"):
                last_fail += " | 反思: " + r.get("why", "")
                break
            cur = r["step"]

        results[nid] = {"ok": done, "status": "ok" if done else "fail",
                        "msg": last_fail or "完成", "tries": tries}
        if not done:
            blocked.add(nid)
            all_ok = False
            log.append("[%d] %s | FAIL | %s" % (idx, nid, last_fail))
            print("        → FAIL: %s" % last_fail[:110])

    if remember and not dry_run:
        key = save_strategy(nodes[0].get("goal", "") if nodes else "", nodes, all_ok)
        print("叙事记忆：%s（%s）" % (key, "已记录成功步骤链" if all_ok else "仅记录失败"))

    print("=" * 70)
    print("结果：%s" % ("全部成功" if all_ok else "存在失败/阻塞/未验证节点"))
    print("=" * 70)
    return all_ok, log, results


def run_steps(steps, **kw):
    """线性 steps 的向后兼容封装（内部转成 DAG 链）。

    ⚠️ 语义与旧 desktop_loop.run_steps 有差异：这里的断言是**确定性**的，
    旧的把「达成没」交给 VL 判是/否。要旧行为请用 desktop_loop.run_steps。
    """
    nodes = []
    prev = None
    for i, st in enumerate(steps, 1):
        n = dict(st)
        n.setdefault("id", "s%d" % i)
        if prev:
            n["depends_on"] = [prev]
        nodes.append(n)
        prev = n["id"]
    return run_dag(nodes, **kw)


# ------------------------------------------------------------------ 自检
def selftest():
    print("=" * 70)
    print("agent_core 自检（只读，不点击）")
    print("=" * 70)

    print("[1] 动作空间    %d 个动作" % len(ACTIONS))
    print("    " + " / ".join(ACTIONS))
    print("[2] 断言类型    %d 种（前 %d 种为确定性证据，零显存）"
          % (len(ASSERTS), len(ASSERTS) - 1))

    print("[3] 危险守门    默认拒绝清单")
    print("    不可撤销: " + "、".join(RISKY_HARD))
    print("    需授权  : " + "、".join(RISKY_SOFT))
    g1, r1 = guard({"target": "删除文件"})
    g2, r2 = guard({"target": "删除文件"}, allow_risky=True)
    g3, r3 = guard({"target": "格式化磁盘"})
    g4, r4 = guard({"target": "保存"})
    print("    删除(默认) -> %s | %s" % (g1, r1[:40]))
    print("    删除(授权) -> %s | %s" % (g2, r2[:40]))
    print("    格式化      -> %s | %s" % (g3, r3[:40]))
    print("    保存        -> %s（放行）" % g4)

    print("[4] 零显存证据源")
    names, e1 = _uia_names()
    print("    UIA 控件名  %s  %d 个 %s" % ("OK" if names else "--", len(names),
                                            ("(" + e1 + ")") if e1 else ""))
    titles, e2 = _window_titles(interactive_only=True)
    print("    窗口标题    %s  %d 个" % ("OK" if titles else "--", len(titles)))
    if titles:
        print("      样本      %s" % " | ".join(t[:18] for t in titles[:4]))

    print("[5] 断言实测（对当前静置屏幕，应当 changed=FAIL / unchanged=OK）")
    import desktop_loop as dl
    b = dl.grab()[1]
    time.sleep(0.4)
    a = dl.grab()[1]
    st1, d1 = assert_conds([{"changed": True}], b, a)
    st2, d2 = assert_conds([{"unchanged": True}], b, a)
    print("    changed:True   -> %s  %s" % (st1, d1[0][:60]))
    print("    unchanged:True -> %s  %s" % (st2, d2[0][:60]))
    if titles:
        st3, d3 = assert_conds([{"window_appeared": titles[0][:6]}], b, a,
                               before_titles=titles)
        print("    window_appeared(%r) -> %s  %s" % (titles[0][:6], st3, d3[0][:40]))
    # fail-closed：查一个绝不存在的文字，应当 FAIL 而不是 unverified
    st4, d4 = assert_conds([{"text_appeared": "绝不可能存在的文字ZZZ9"}], b, a)
    print("    text_appeared(不存在) -> %s  %s" % (st4, d4[0][:60]))

    print("[6] 反思实测（构造两个典型失败，看是否给出**不同的**做法）")
    r1 = reflect({"do": "click", "target": "自动化（面板）"},
                 "定位失败(全部定位器失败)", [], use_vl=False)
    print("    定位失败 -> tag=%s why=%s" % (r1.get("tag"), r1.get("why", "")[:70]))
    r2 = reflect({"do": "click", "target": "保存"},
                 "断言未通过(fail)", [], use_vl=False)
    print("    断言失败 -> tag=%s why=%s" % (r2.get("tag"), r2.get("why", "")[:70]))

    print("[7] 分层记忆")
    st = list_strategies()
    print("    叙事层   %s  %d 条已记录策略" % (STRAT_PATH, len(st)))
    cm = os.path.join(HERE, "click_memory.json")
    n_cm = 0
    try:
        with open(cm, "r", encoding="utf-8") as f:
            n_cm = len(json.load(f))
    except Exception:
        pass
    print("    情景层   %s  %d 条坐标记忆" % (cm, n_cm))
    print("=" * 70)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        selftest()
    elif argv[0] == "actions":
        print("动作空间：")
        for k, v in ACTIONS.items():
            print("  %-13s %s" % (k, v))
        print("\n断言类型：")
        for k, v in ASSERTS.items():
            print("  %-17s %s" % (k, v))
    elif argv[0] == "plan":
        data = json.loads(argv[1]) if len(argv) > 1 else {"nodes": []}
        ok, log, _ = run_dag(data.get("nodes", []),
                             dry_run=("--dry" in argv),
                             window=data.get("window"),
                             allow_risky=("--allow-risky" in argv),
                             strict=("--loose" not in argv))
        sys.exit(0 if ok else 1)
    elif argv[0] == "memory":
        for k, v in list_strategies().items():
            print("%s  ok=%d fail=%d  %s  goal=%s"
                  % (k, v.get("ok", 0), v.get("fail", 0), v.get("last", ""),
                     v.get("goal", "")[:40]))
    else:
        print(__doc__)
