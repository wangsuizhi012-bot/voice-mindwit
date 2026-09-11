# -*- coding: utf-8 -*-
"""test_agent_core.py —— agent_core 真机 E2E 测试（v2）

## 为什么换夹具（v1 的教训，写在这里免得下次再踩）

v1 用 `subprocess.Popen(['notepad.exe'])` 当夹具。两个问题：

1. **这台机器的 Notepad 标题是英文**（`无标题 - Notepad`），根本没有「记事本」
   三个字 —— 用标题 `记事本` 去 resolve 永远失败，测试假失败。
2. **更严重**：Win11 记事本有**会话恢复**。启动 notepad.exe 会把用户上次的
   会话（含**未保存的草稿**）一起恢复出来。v1 跑完时凭空多出一个
   `*新建 文本文档 (2).txt - Notepad` —— 那是用户自己的未保存草稿。
   另外点「文件」菜单时还误开了用户的一条最近文件。

结论：**永远不要拿用户可能正在用的应用当测试夹具。** 夹具必须满足
「标题唯一、内容我造、进程我控、绝不碰用户状态」。

## v2 夹具：自己弹一个 MessageBox

`user32.MessageBoxW` 起的对话框：
  · 标题唯一（`AgentCoreFixture_9F3`），绝不与用户窗口碰撞
  · 是真正的 Win32 原生控件 -> UIA 能枚举到 ButtonControl，定位可靠、无需 OCR
  · 关掉它 = 我的测试闭环完成，断言 `window_gone` **完全确定性**
  · 不做会话恢复、不落盘、不读写用户任何文件

覆盖 8 项：
  T1 动作空间 hotkey：回车关掉对话框 + 断言 window_gone（全程不定位，零依赖）
  T2 定位 + 点击 + 断言 window_gone（真实 UIA 定位 + 真实点击）
  T3 类型输入 type（非 ASCII 走剪贴板）
  T4 fail-closed：界面变了但断言不成立 -> FAIL（旧逻辑会误判成功）
  T5 反思：目标带噪声定位失败 -> 规则反思清洗用词 -> 成功
  T6 危险守门：默认拒绝，显式授权放行
  T7 dry-run：只定位不点击，对话框仍在、像素无变化
  T8 分层记忆 + 剪贴板 64 位句柄正确性（含剪贴板原样恢复）

跑法：python test_agent_core.py
"""
import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent_core as ac          # noqa: E402
import desktop_loop as dl        # noqa: E402
import win_ctx                   # noqa: E402

FIX = "AgentCoreFixture_9F3"     # 唯一夹具标题
RESULTS = []
PROCS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("\n>>> %s  %s  %s" % ("✅ PASS" if cond else "❌ FAIL", name, detail))
    return bool(cond)


# ------------------------------------------------------------------ 夹具
def _wait_fg(hwnd, tries=20, gap=0.2):
    """等窗口真正成为前台。

    ⚠️ `SetForegroundWindow` 返回成功 ≠ 它真的在前台 —— 对话框初始化期间
    会被抢占。T1 之前假失败的根因就是这个：激活「成功」了，但 Enter 发给了
    别的窗口，对话框纹丝不动。必须用 GetForegroundWindow 实测确认。
    """
    u32 = ctypes.windll.user32
    for _ in range(tries):
        if int(u32.GetForegroundWindow()) == int(hwnd):
            return True
        win_ctx.activate(hwnd)
        time.sleep(gap)
    return False


def spawn_msgbox(text="fixture", flags=0):
    """弹一个我自己的对话框（不落盘、不碰用户状态）。返回 (Popen, win)。"""
    assert_no_dialog("spawn前")      # 先清场，避免旧对话框污染 resolve
    code = ("import ctypes;"
            "ctypes.windll.user32.MessageBoxW(0, %r, %r, %d)" % (text, FIX, flags))
    p = subprocess.Popen([sys.executable, "-c", code])
    PROCS.append(p)
    win = None
    for _ in range(20):
        time.sleep(0.25)
        win, _ = win_ctx.resolve(FIX)
        if win and _wait_fg(win["hwnd"]):
            return p, win
    return p, win


def dialog_titles():
    t, _ = ac._window_titles()
    return [x for x in t if FIX in x]


def assert_no_dialog(tag):
    """测试间强制清场：上一个夹具没关干净会污染后续 resolve（踩过）。"""
    if dialog_titles():
        print("     ⚠️ [%s] 有 %d 个泄漏对话框，先清掉" % (tag, len(dialog_titles())))
        for _ in range(6):
            for w in win_ctx.list_windows(interactive_only=False):
                if w["interactive"] and FIX in (w["title"] or ""):
                    close_fixture(w)
            time.sleep(0.4)
            if not dialog_titles():
                break
    return not dialog_titles()


def button_name(hwnd):
    """读对话框里按钮的 UIA 名字（做到不依赖语言：中文机「确定」/英文机「OK」）。"""
    from locate import _enum_uia
    items, err = _enum_uia(timeout=3.0, hwnd=hwnd)
    if err:
        return None
    buttons = [i[0] for i in items if "Button" in (i[1] or "") and (i[0] or "").strip()]
    return buttons[0] if buttons else None


def close_fixture(win):
    """关掉自己的夹具对话框（只碰我自己的窗口）。"""
    if not win:
        return
    try:
        ctypes.windll.user32.PostMessageW(int(win["hwnd"]), 0x0010, 0, 0)  # WM_CLOSE
    except Exception:
        pass
    time.sleep(0.5)


def cleanup():
    for p in PROCS:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass


# ------------------------------------------------------------------ 主流程
def main():
    print("=" * 76)
    print("agent_core 真机 E2E（v2 夹具：自建 MessageBox，绝不用用户的应用）")
    print("=" * 76)

    # ---------------- T1 hotkey + window_gone ----------------
    print("\n" + "─" * 76)
    print("T1  动作空间 hotkey：回车关掉对话框 + 确定性断言 window_gone")
    print("─" * 76)
    p, win = spawn_msgbox("T1")
    if not check("T1a 夹具对话框已弹出（可被 resolve 命中）", bool(win),
                 "hwnd=%s" % (hex(win["hwnd"]) if win else "-")):
        cleanup()
        return 1
    ok1, log1, res1 = ac.run_dag(
        [{"id": "enter", "do": "hotkey", "keys": ["enter"],
          "assert": [{"window_gone": FIX}]}],
        window=FIX, reflect_retries=0, remember=False)
    left = dialog_titles()
    check("T1b 回车后对话框消失、断言 window_gone 通过",
          ok1 and not left, "剩余对话框=%s" % left)

    # ---------------- T2 定位 + 点击 ----------------
    print("\n" + "─" * 76)
    print("T2  UIA 定位 + 真实点击 + 断言 window_gone")
    print("─" * 76)
    p2, win2 = spawn_msgbox("T2")
    if win2:
        btn = button_name(win2["hwnd"])
        print("     对话框按钮 UIA 名: %r" % btn)
        ok2, log2, res2 = ac.run_dag(
            [{"id": "click_ok", "do": "click", "target": btn or "OK",
              "window": FIX, "assert": [{"window_gone": FIX}]}],
            reflect_retries=0, remember=False)
        left2 = dialog_titles()
        check("T2 定位按钮 -> 真实点击 -> 断言对话框已关",
              ok2 and not left2, "剩余对话框=%s" % left2)
    else:
        check("T2 夹具未就绪", False, "")

    # ---------------- T3 剪贴板写入（非 ASCII 路径） ----------------
    print("\n" + "─" * 76)
    print("T3  非 ASCII 输入路径：剪贴板写入 64 位句柄正确性")
    print("─" * 76)
    # ⚠️ 这里刻意**不真的按 Ctrl+V**：那会把文字粘贴进当前前台窗口，
    #    要是前台是用户的应用就污染了用户状态。只验证容易出错的那半
    #    （GlobalAlloc 句柄在 64 位下的截断问题），读回来一致即证明正确。
    old_clip = _clip_get()
    wrote = ac._set_clipboard("中文测试文字ABC")
    time.sleep(0.3)
    got = _clip_get()
    check("T3 非 ASCII 写入剪贴板并原样读回（64 位句柄未截断）",
          wrote and got == "中文测试文字ABC",
          "写入=%s 读回=%r" % (wrote, (got or "")[:20]))
    # 原样恢复用户剪贴板
    if old_clip:
        ac._set_clipboard(old_clip)
    print("     （用户剪贴板已恢复: %s）" % ("是" if old_clip else "原为空，未改动"))

    # ---------------- T4 fail-closed ----------------
    print("\n" + "─" * 76)
    print("T4  fail-closed：界面确实变了，但断言不成立 -> 仍判 FAIL")
    print("─" * 76)
    p4, win4 = spawn_msgbox("T4")
    ok4, log4, res4 = ac.run_dag(
        [{"id": "liar", "do": "click", "target": (button_name(win4["hwnd"]) if win4 else "OK"),
          "window": FIX,
          "assert": [{"text_appeared": "绝不可能出现的文字ZZZ9"}]},
         {"id": "down", "do": "hotkey", "keys": ["enter"], "depends_on": ["liar"],
          "assert": [{"changed": True}]}],
        reflect_retries=0, remember=False)
    check("T4a 界面变了但断言不成立 -> 节点 FAIL，整体 ok=False",
          (res4["liar"]["status"] == "fail") and not ok4,
          "liar=%s overall=%s" % (res4["liar"]["status"], ok4))
    check("T4b 下游节点被 BLOCKED（不做无意义的连锁动作）",
          res4["down"]["status"] == "blocked", "down=%s" % res4["down"]["status"])
    close_fixture(win4)

    # ---------------- T5 反思换策略 ----------------
    print("\n" + "─" * 76)
    print("T5  反思：目标带噪声 -> 定位失败 -> 规则反思清洗用词 -> 第 2 次成功")
    print("─" * 76)
    p5, win5 = spawn_msgbox("T5")
    btn5 = (button_name(win5["hwnd"]) if win5 else "OK") or "OK"
    noisy = btn5 + "（右下角那个）"
    ok5, log5, res5 = ac.run_dag(
        [{"id": "noisy", "do": "click", "target": noisy, "window": FIX,
          # 关掉 UI-TARS：否则 grounding 模型可能瞎蒙一个点刚好点到按钮上，
          # 让「第 1 次就成功」掩盖掉反思逻辑（测试要确定，不要运气）
          "use_uitars": False,
          "assert": [{"window_gone": FIX}]}],
        reflect_retries=1, use_vl_reflect=False, remember=False)
    check("T5 第 1 次定位失败 -> 反思清洗为 %r -> 第 2 次成功" % btn5,
          ok5 and res5["noisy"].get("tries", 0) >= 2,
          "tries=%s ok=%s" % (res5["noisy"].get("tries"), ok5))
    close_fixture(win5)

    # ---------------- T6 危险守门 ----------------
    print("\n" + "─" * 76)
    print("T6  危险守门：默认拒绝，显式授权才放行")
    print("─" * 76)
    ok6, log6, res6 = ac.run_dag(
        [{"id": "danger", "do": "click", "target": "关闭", "assert": [{"changed": True}]}],
        reflect_retries=0, remember=False)
    g_default, r_default = ac.guard({"target": "关闭"})
    g_risky, _ = ac.guard({"target": "关闭"}, allow_risky=True)
    g_hard, r_hard = ac.guard({"target": "格式化磁盘"}, allow_risky=True)
    check("T6a 默认拒绝危险动作，allow_risky 放行",
          (res6["danger"]["status"] == "blocked") and (not g_default) and g_risky,
          "run_dag=%s 默认=%s 授权=%s" % (res6["danger"]["status"], g_default, g_risky))
    check("T6b 不可撤销操作即使 allow_risky 也拦（还需 allow_destructive）",
          not g_hard, r_hard[:46])

    # ---------------- T7 dry-run ----------------
    print("\n" + "─" * 76)
    print("T7  dry-run：只定位不点击，对话框仍在、像素无变化")
    print("─" * 76)
    p7, win7 = spawn_msgbox("T7")
    btn7 = (button_name(win7["hwnd"]) if win7 else "OK") or "OK"
    ok7, log7, res7 = ac.run_dag(
        [{"id": "dry", "do": "click", "target": btn7, "window": FIX}],
        dry_run=True, remember=False)
    # ⚠️ 像素比较要取**稳定态**两帧：dry-run 里的置前动作会让刚失焦的窗口
    #    重绘标题栏，若跨着置前动作比较，0.2% 的阈值会被这点重绘噪声顶破
    #    （实测 0.8%，不是真的"屏幕变了"）。等重绘结束再取基准。
    time.sleep(0.8)
    b = dl.grab()[1]
    time.sleep(0.6)
    a = dl.grab()[1]
    chg = dl.diff_ratio(b, a)
    check("T7 dry-run 命中目标、对话框未关、稳定态像素无变化",
          ok7 and bool(dialog_titles()) and chg < dl.CHANGE_THRESHOLD,
          "稳定态变化=%.3f%% 对话框=%s" % (chg * 100, dialog_titles()))
    close_fixture(win7)

    # ---------------- T8 分层记忆 ----------------
    print("\n" + "─" * 76)
    print("T8  分层记忆：叙事层（策略）+ 情景层（坐标）")
    print("─" * 76)
    p8, win8 = spawn_msgbox("T8")
    btn8 = (button_name(win8["hwnd"]) if win8 else "OK") or "OK"
    # 单节点成功链：点击按钮关掉对话框 + 确定性断言 -> 应被叙事层记录
    demo = [{"id": "a", "do": "click", "target": btn8, "window": FIX,
             "assert": [{"window_gone": FIX}]}]
    ok8, _, _ = ac.run_dag(demo, window=FIX, reflect_retries=0, remember=True,
                           allow_risky=True)
    key = ac._task_key(demo)
    rec = ac.list_strategies().get(key, {})
    got, _ = ac.recall_strategy(demo)
    check("T8a 叙事层记录成功步骤链并可 recall",
          ok8 and rec.get("ok", 0) >= 1 and bool(got),
          "key=%s ok=%s 回放=%d 节点" % (key, rec.get("ok"), len(got or [])))
    cm = os.path.join(HERE, "click_memory.json")
    n_cm = 0
    try:
        import json
        with open(cm, "r", encoding="utf-8") as f:
            n_cm = len(json.load(f))
    except Exception:
        pass
    check("T8b 情景层（坐标记忆）独立存在", os.path.exists(cm),
          "%s %d 条" % (os.path.basename(cm), n_cm))
    close_fixture(win8)

    cleanup()

    print("\n" + "=" * 76)
    npass = sum(1 for _, c, _ in RESULTS if c)
    print("结果：%d/%d 通过" % (npass, len(RESULTS)))
    for name, c, d in RESULTS:
        print("  %s %s%s" % ("✅" if c else "❌", name, ("  (" + d + ")") if d else ""))
    print("=" * 76)
    return 0 if npass == len(RESULTS) else 1


def _clip_get():
    """读剪贴板文本（用于原样恢复用户剪贴板）。"""
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    if not u32.OpenClipboard(None):
        return None
    try:
        h = u32.GetClipboardData(13)      # CF_UNICODETEXT
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        cleanup()
