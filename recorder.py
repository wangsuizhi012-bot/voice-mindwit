# -*- coding: utf-8 -*-
"""演示编译器：把人的真实操作录成 agent_core 可直接重放的 DAG。

对标 OpenAdapt 的 record → compile 范式，但**复用本机现有资产**（零显存、零云 API）：

    pynput 监听键鼠
        ↓ 每次点击：存「点击时截图」+ 坐标 + 当时的前台窗口
    nuphus perceive(OCR) 反解
        ↓ 把「点了 (411,576)」变成「点了『发消息』」
    编译成 agent_core DAG
        ↓ target 存**文字锚点**而非坐标 → 抗窗口移动/分辨率变化
    flows/<name>.json
        ↓
    agent_core.run_dag 重放 —— 零模型调用，毫秒级

为什么存文字不存坐标（★ 关键设计，抄 OpenAdapt 的 visual anchors）：
    坐标脚本换个窗口位置就废；文字锚点由 OCR/UIA 在运行时重新定位，天然自愈。

用法：
    # 录制（默认 60s 后自动停，或按 Ctrl+Alt+Q 提前结束）
    python recorder.py record --name 我的任务 --duration 60

    # 看一下录到了什么
    python recorder.py show --name 我的任务

    # 重放
    python recorder.py replay --name 我的任务

    # 安全演练（只定位不点击）
    python recorder.py replay --name 我的任务 --dry

已知限制（诚实列出）：
    · 中文输入：pynput 拿不到输入法组字结果，录制时中文可能丢字。
      变通：录完用 `show` 检查，手工补 `{"do":"type","text":"你好"}` 即可。
    · 拖拽：暂未录制（只在拖拽起止点各记一次点击）。需要时手工改成 drag 节点。
    · 屏幕标定：本项目坐标一律来自**全屏截图**，1:1 零映射（窗口截图有比例失真）。
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

FLOWS_DIR = os.path.join(HERE, "flows")
RAW_DIR = os.path.join(HERE, "shots", "rec")

STOP_HOTKEY = "<ctrl>+<alt>+q"
CHANGE_THRESHOLD = 0.002

# AI 编译用的本地模型端点。网关 :9292 是唯一入口，无独立兜底端口（原 :1234 已废）
LLM_BASE = os.getenv("RECORDER_LLM_BASE", "http://127.0.0.1:9292/v1")
LLM_BASE_FALLBACK = "http://127.0.0.1:9292/v1"
LLM_MODEL = os.getenv("RECORDER_LLM_MODEL", "spark")


# ------------------------------------------------------------------ 采集
class Recorder:
    """监听鼠标/键盘，产出原始事件流。"""

    def __init__(self, max_events=500):
        self.events = []            # 原始事件
        self.buf = ""               # 连续可打印字符缓冲
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.max_events = max_events
        self._n_shot = 0

    # ---- 内部：拍一张「动作发生前」的截图 ----
    def _snap(self):
        import pyautogui
        import win_ctx
        self._n_shot += 1
        path = os.path.join(RAW_DIR, "s%03d.png" % self._n_shot)
        try:
            pyautogui.screenshot().save(path)
        except Exception:
            path = None
        try:
            fg = win_ctx.foreground()
            win = {"title": fg["title"], "process": fg["process_name"]} if fg else None
        except Exception:
            win = None
        return path, win

    def _flush_type(self):
        """把字符缓冲落成一个 type 事件。"""
        if self.buf:
            self.events.append({"kind": "type", "text": self.buf})
            self.buf = ""

    # ---- 鼠标 ----
    def on_click(self, x, y, button, pressed):
        if not pressed or self._stop.is_set():
            return
        with self._lock:
            self._flush_type()
            shot, win = self._snap()
            self.events.append({
                "kind": "click", "x": int(x), "y": int(y),
                "button": str(button).split(".")[-1],
                "shot": shot, "win": win, "t": time.time(),
            })
            if len(self.events) >= self.max_events:
                self._stop.set()

    # ---- 键盘 ----
    def on_press(self, key):
        if self._stop.is_set():
            return
        name = getattr(key, "name", None)
        ch = getattr(key, "char", None)
        with self._lock:
            if ch and ch.isprintable():
                self.buf += ch
                return
            if name == "enter":
                self._flush_type()
                self.events.append({"kind": "key", "keys": ["enter"]})
            elif name in ("tab", "esc", "backspace", "delete", "up", "down",
                          "left", "right"):
                self._flush_type()
                self.events.append({"kind": "key", "keys": [name]})
            # 其余功能键忽略（避免噪声）

    def on_release(self, key):
        """热键停止。"""
        try:
            from pynput import keyboard
            hotkey = keyboard.HotKey(
                keyboard.HotKey.parse(STOP_HOTKEY), self._stop.set)
            hotkey.release(key)
        except Exception:
            pass

    def stop(self):
        """外部请求结束录制(语音/热键调用)。线程安全。"""
        self._stop.set()

    def is_stopped(self):
        return self._stop.is_set()

    def run(self, duration):
        from pynput import keyboard, mouse
        os.makedirs(RAW_DIR, exist_ok=True)
        print("=" * 62)
        print("● 录制中……  按 Ctrl+Alt+Q 结束（或 %ds 后自动停）" % duration)
        print("  正在录：鼠标点击 / 键盘输入")
        print("=" * 62)

        hk = keyboard.HotKey(keyboard.HotKey.parse(STOP_HOTKEY),
                             self._stop.set)
        ml = mouse.Listener(on_click=self.on_click)
        kl = keyboard.Listener(
            on_press=lambda k: (hk.press(kl.canonical(k)), self.on_press(k)),
            on_release=lambda k: (hk.release(kl.canonical(k)), self.on_release(k)),
        )
        ml.start()
        kl.start()
        t0 = time.time()
        try:
            while not self._stop.is_set() and time.time() - t0 < duration:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            ml.stop()
            kl.stop()
            with self._lock:
                self._flush_type()
        print("■ 录制结束：%d 个事件" % len(self.events))
        return self.events


# ------------------------------------------------------------------ 编译
def _texts_of(elements):
    return {str(e.get("text") or "").strip(): e for e in elements["texts"]
            if str(e.get("text") or "").strip()}


def _resolve_click_target(shot, x, y, elements):
    """把点击坐标反解成「点到了什么文字」—— 这就是 OpenAdapt 的 visual anchor。

    两级：① 中心点落在元素框内（最准）② 最近的文本中心（容忍几 px 偏差）
    返回 (target|None, how)
    """
    cands = elements["texts"] + elements["icons"]

    def _inside(e):
        ex, ey = e.get("x"), e.get("y")
        ew, eh = e.get("w") or 0, e.get("h") or 0
        return ex <= x < ex + ew and ey <= y < ey + eh

    hits = [e for e in cands if _inside(e)]
    if hits:
        # 多个命中时取面积最小的（最具体）
        hits.sort(key=lambda e: (e.get("w") or 0) * (e.get("h") or 0))
        e = hits[0]
        t = str(e.get("text") or "").strip()
        if t:
            return t, "命中框内 %dx%d" % (e.get("w") or 0, e.get("h") or 0)
        return None, "命中框内但无文字（图标 x=%s）" % e.get("type")

    # ② 最近文本（≤30px）
    best, bd = None, 1e9
    for e in elements["texts"]:
        t = str(e.get("text") or "").strip()
        if not t:
            continue
        cx, cy = e.get("cx"), e.get("cy")
        if cx is None or cy is None:
            continue
        d = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if d < bd:
            best, bd = t, d
    if best and bd <= 30:
        return best, "最近文本 %.0fpx" % bd
    return None, "无匹配元素（最近 %.0fpx）" % bd


def compile_events(events, name, window_hint=None):
    """原始事件 → agent_core DAG 节点。返回 (nodes, notes)。"""
    import numpy as np
    import nuphus_bridge as nb
    from PIL import Image

    notes = []
    # 1) 收集所有点击 + 为自动断言准备「后一张图」
    clicks = [e for e in events if e["kind"] == "click"]
    timeline = [e for e in events if e["kind"] in ("click", "type", "key")]

    if not clicks:
        return [], ["没有录到任何点击"]

    # 每个点击的「之后」截图 = 下一个带截图的点击的 before 图，或最后一张
    shots = [e.get("shot") for e in clicks]

    nodes = []
    ci = 0
    for ev in timeline:
        if ev["kind"] == "type":
            nodes.append({"do": "type", "text": ev["text"]})
            continue
        if ev["kind"] == "key":
            nodes.append({"do": "hotkey", "keys": ev["keys"]})
            continue

        # click
        ci += 1
        shot = ev.get("shot")
        target, how = None, "无截图"
        if shot and os.path.exists(shot):
            try:
                els = nb.perceive(shot)
                target, how = _resolve_click_target(shot, ev["x"], ev["y"], els)
            except Exception as ex:
                how = "perceive 失败 %r" % (ex,)
        else:
            notes.append("第%d步无截图，按坐标记录" % ci)

        node = {"do": "double_click" if ev.get("button") == "double" else "click"}
        node["id"] = "s%d" % ci
        if target:
            node["target"] = target
        else:
            # 反解不出文字 → 退化为坐标锚点（不可移植，但至少能跑）
            node["target"] = ""
            node["at"] = [ev["x"], ev["y"]]
            notes.append("第%d步反解失败(%s)，退化为坐标 %s" % (ci, how, (ev["x"], ev["y"])))

        # 窗口约束：用点击时的前台窗口
        w = ev.get("win") or {}
        if w.get("process"):
            node["window"] = w["process"]
        elif window_hint:
            node["window"] = window_hint

        # 自动断言：与下一张截图对比
        nxt = shots[ci] if ci < len(shots) else None
        if shot and nxt and os.path.exists(shot) and os.path.exists(nxt):
            try:
                a = np.asarray(Image.open(shot).convert("RGB"), dtype=np.int16)
                b = np.asarray(Image.open(nxt).convert("RGB"), dtype=np.int16)
                if a.shape == b.shape:
                    chg = float((np.abs(a - b).max(axis=2) > 12).mean())
                    if chg >= CHANGE_THRESHOLD:
                        node["assert"] = [{"changed": True}]
                    else:
                        notes.append("第%d步：点击后无明显变化（可能没录到效果）" % ci)
            except Exception:
                pass

        nodes.append(node)

    return nodes, notes


# ------------------------------------------------------------------ AI 编译
_AI_SYS = (
    "你是 GUI 自动化脚本编译器。输入的 target 来自截图 OCR 反解，可能有识别错误"
    "（例如「发」被误识成「友」、「按住」误识成「按任」、「说话」误识成「说适」）。"
    "请把每个 target 修正成界面上真实存在的短文字，用于运行时按文字重新定位元素。"
    "要求：尽量短（2-6 字）；保留原文中的省略号；不确定就原样保留；"
    "只输出一个 JSON 字符串数组，长度与输入完全相同，不要解释、不要 markdown 代码块。"
)


def _post_llm(base, model, payload, timeout):
    import requests
    s = requests.Session()
    s.trust_env = False          # 回环必须直连，否则被系统代理劫持（502 不抛异常）
    if model:
        payload["model"] = model
    r = s.post(base.rstrip("/") + "/chat/completions", json=payload, timeout=timeout)
    return r.json()["choices"][0]["message"]["content"]


def ai_clean(nodes, base=None, model=None, timeout=180):
    """用本地 LLM 修正 OCR 乱码锚点 —— 这就是「AI 辅助录制」的核心一环。

    为什么需要：录制时 OCR 反解出的锚点是**乱码**（实测「发消息或按住空格说话..」
    被读成「友消息或按任空格说适..」）。若原样存进脚本，重放时按这串乱码
    去定位，必然失败 —— 录制产物直接废掉。AI 在这里把「记录」变成「可用程序」。

    返回 (nodes, 说明)。失败时原样返回（不阻塞录制，降级而非报错）。
    """
    import re
    targets = [n.get("target") for n in nodes
               if n.get("do") in ("click", "double_click", "right_click")
               and n.get("target")]
    if not targets:
        return nodes, "无 target 需要修正"

    payload = {
        "messages": [{"role": "system", "content": _AI_SYS},
                     {"role": "user", "content": json.dumps(targets, ensure_ascii=False)}],
        "temperature": 0,
        "max_tokens": 600,
    }
    bases = [base] if base else [LLM_BASE, LLM_BASE_FALLBACK]
    txt, used = None, None
    for b in bases:
        try:
            txt = _post_llm(b, model if model else (LLM_MODEL if b == LLM_BASE else None),
                            payload, timeout)
            used = b
            break
        except Exception as e:
            last = repr(e)
    if txt is None:
        return nodes, "AI 编译不可用（%s），保留原始锚点" % last[:60]

    # 剥 markdown 代码块 + 抽第一个 JSON 数组
    clean = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", txt.strip())
    m = re.search(r"\[.*\]", clean, re.S)
    if not m:
        return nodes, "AI 返回无法解析，保留原始锚点：%s" % clean[:50]
    try:
        fixed = json.loads(m.group(0))
    except Exception:
        return nodes, "AI 返回 JSON 非法，保留原始锚点"
    if not isinstance(fixed, list) or len(fixed) != len(targets):
        return nodes, ("AI 返回长度不符（%d vs %d），保留原始锚点"
                       % (len(fixed) if isinstance(fixed, list) else -1, len(targets)))

    changed = 0
    it = iter(fixed)
    for n in nodes:
        if n.get("do") in ("click", "double_click", "right_click") and n.get("target"):
            new = str(next(it)).strip()
            if new and new != n["target"]:
                n["target_raw"] = n["target"]      # 留原始 OCR 文本备查
                n["target"] = new
                changed += 1
    return nodes, "AI 修正 %d/%d 个锚点（模型 %s）" % (changed, len(targets), used)


# ------------------------------------------------------------------ 存储
def save_flow(name, nodes, extra=None):
    os.makedirs(FLOWS_DIR, exist_ok=True)
    path = os.path.join(FLOWS_DIR, name + ".json")
    data = {"name": name, "source": "recorder.py", "created": time.strftime(
        "%Y-%m-%d %H:%M:%S"), "steps": nodes}
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_flow(name):
    path = os.path.join(FLOWS_DIR, name + ".json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ CLI
def cmd_record(a):
    r = Recorder()
    events = r.run(a.duration)
    if not events:
        print("没录到任何事件，退出")
        return 1
    nodes, notes = compile_events(events, a.name, window_hint=a.window)
    if not nodes:
        print("编译后无有效步骤：%s" % "; ".join(notes))
        return 1

    # AI 编译：修正 OCR 乱码锚点（可用 --no-ai 关闭）
    if not a.no_ai:
        nodes, aimsg = ai_clean(nodes, base=a.llm_base, model=a.llm_model)
        notes.append("AI 编译：" + aimsg)
    path = save_flow(a.name, nodes, extra={"raw_event_count": len(events),
                                            "compile_notes": notes})
    print("\n已保存: %s（%d 步）" % (path, len(nodes)))
    if notes:
        print("编译提示：")
        for n in notes:
            print("  · %s" % n)
    for i, n in enumerate(nodes, 1):
        desc = n.get("target") or n.get("text") or n.get("at") or "-"
        print("  %2d. %-12s %s%s" % (i, n["do"], str(desc)[:30],
                                     "  [有断言]" if n.get("assert") else ""))
    return 0


def cmd_show(a):
    f = load_flow(a.name)
    if not f:
        print("流程不存在: %s" % a.name)
        return 1
    print(json.dumps(f, ensure_ascii=False, indent=2))
    return 0


def cmd_replay(a):
    import agent_core as ac
    f = load_flow(a.name)
    if not f:
        print("流程不存在: %s" % a.name)
        return 1
    steps = f["steps"]
    print("重放 %s（%d 步）%s" % (a.name, len(steps), "· dry-run" if a.dry else ""))
    ok, log, res = ac.run_dag(steps, dry_run=a.dry, window=a.window,
                              reflect_retries=a.reflect)
    for line in log:
        print("  " + str(line))
    print("结果:", "OK" if ok else "FAIL")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="演示编译器：录制→编译成 agent_core DAG")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="录制并编译")
    r.add_argument("--name", required=True, help="流程名")
    r.add_argument("--duration", type=int, default=60, help="最长录制秒数")
    r.add_argument("--window", default=None, help="给所有步骤兜底的窗口名")
    r.add_argument("--no-ai", action="store_true", help="跳过 AI 编译（不修锚点）")
    r.add_argument("--llm-base", default=None, help="AI 编译端点，默认网关 :9292 后兜底 :1234")
    r.add_argument("--llm-model", default=None, help="AI 编译模型名，默认 spark")
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("show", help="查看已录流程")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_show)

    q = sub.add_parser("replay", help="重放流程")
    q.add_argument("--name", required=True)
    q.add_argument("--dry", action="store_true", help="只定位不点击")
    q.add_argument("--window", default=None)
    q.add_argument("--reflect", type=int, default=1, help="反思重试次数")
    q.set_defaults(func=cmd_replay)

    a = p.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
