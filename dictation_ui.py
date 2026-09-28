# -*- coding: utf-8 -*-
"""按键语音输入的悬浮指示器 —— Typeless 风格: 大字号 + 逐字浮现 + 状态一目了然。

设计要点(借鉴 VoiceSnap / local-dictate / OpenLess 的指示器交互):

  · 逐字动画: partial 结果每次只把「新增的那几个字」按 25~40ms/字 推出来,
    而不是整段闪一下替换 —— 这才是「逐字清晰」的关键观感。
    若新文本与已显示文本不再同前缀(ASR 会重写前缀), 自动回退到最长公共
    前缀后继续动画, 不会闪。
  · 不抢焦点: WS_EX_NOACTIVATE + TOOLWINDOW, 粘贴时焦点仍留在目标输入框。
  · 实心深色卡片: 任何桌面背景下都可读。
    (尝试过 -transparentcolor + canvas 圆角的方案: 全窗透明 Frame 会把底下的
     多边形一起变透明 —— 分层窗口按最终像素判透明, 不看 widget 叠放。放弃。)
  · 状态一路可见: 录音中(红) / 识别中(黄) / 已输入(绿) / 已取消(灰)。
  · 音量条: 让「到底录进去没有」这件事立刻有反馈, 避免对着哑麦说话。
  · 可拖动 + 自动淡出, 空闲不挡视线。

用法:
    ui = DictationUI(cfg); ui.start()
    ui.show("recording"); ui.push_text("你好"); ui.set_level(0.2)
    ui.finish("你好，世界。", ok=True)
"""
import time
import threading

PANEL = "#17171f"      # 卡片底色
BORDER = "#2e2e3d"     # 卡片描边
TXT = "#f4f4f8"        # 正文
DIM = "#8b8b96"        # 次要文字

STATE = {
    "recording":   ("#ff5c5c", "录音中"),
    "recognizing": ("#ffd54f", "识别中"),
    "done":        ("#3ddc84", "已输入"),
    "cancel":      ("#8b8b96", "已取消"),
    "idle":        ("#4aa3ff", "就绪"),
}


class DictationUI:
    W = 780
    PAD = 26

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.cfg = cfg
        self.font_size = int(cfg.get("dictation_font_size", 20))
        self.char_ms = int(cfg.get("dictation_char_ms", 30))
        self.idle_hide_s = float(cfg.get("dictation_idle_hide_s", 3.0))
        self.bottom_margin = int(cfg.get("dictation_bottom_margin", 120))
        self.enabled = bool(cfg.get("dictation_ui", True))
        self.root = None
        self._shown = ""        # 当前已显示文本
        self._target = ""       # 目标文本
        self._pending = ""      # 待逐字推出的部分
        self._tick_job = None
        self._hide_job = None
        self._levels = [0.0] * 28
        self._state = "idle"
        self._t0 = 0.0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        # ★ Windows 的 Tk 不是线程化的: Tk() / mainloop() / 所有界面调用必须在
        # 同一个线程。外部线程只往 _q 丢命令, Tk 线程自己排空执行。
        # 否则会得到 "Calling Tcl from different apartment"，界面直接冻住。
        import queue as _q
        self._q = _q.Queue()

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if not self.enabled:
            return False
        threading.Thread(target=self._run, daemon=True).start()
        return self._ready.wait(timeout=5.0)

    def _run(self):
        import tkinter as tk
        self.tk = tk
        try:
            self.root = tk.Tk()
        except Exception:
            self.root = None
            self._ready.set()
            return
        self._build()
        self._ready.set()
        try:
            self.root.mainloop()
        except Exception:
            pass

    def _drain(self):
        """Tk 线程内排空命令队列。"""
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.root.after(25, self._drain)
        except Exception:
            pass

    def destroy(self):
        # 也必须走队列: 跨线程直接调 root.destroy 同样会 "different apartment"
        try:
            if self.root:
                self._q.put(self.root.destroy)
        except Exception:
            pass

    # ------------------------------------------------------------ 终端面板
    STAGES = ["MIC", "VAD", "ASR", "POLISH", "PASTE"]

    def _build_console(self, r, tk):
        """终端风格的管道可视化: 阶段条 + 滚动事件流(等宽/时间戳/分级着色)。"""
        wrap = tk.Frame(r, bg=PANEL)
        wrap.pack(fill="x", padx=self.PAD, pady=(0, 12))
        # 阶段条: 当前阶段高亮
        bar = tk.Frame(wrap, bg=PANEL)
        bar.pack(fill="x", pady=(0, 4))
        self._stage_ls = []
        for i, name in enumerate(self.STAGES):
            if i:
                tk.Label(bar, text="─▶", font=("Consolas", 10),
                         bg=PANEL, fg="#3a3a4a").pack(side="left")
            lb = tk.Label(bar, text=name, font=("Consolas", 10, "bold"),
                          bg=PANEL, fg="#4a4a5a", padx=4)
            lb.pack(side="left")
            self._stage_ls.append(lb)
        # 事件流
        self.console = tk.Text(wrap, height=9, font=("Consolas", 10),
                               bg="#0d0d12", fg="#9fe870", relief="flat",
                               bd=0, highlightthickness=1,
                               highlightbackground=BORDER, wrap="none",
                               state="disabled")
        self.console.pack(fill="x")
        for tag, col in (("mic", "#7fd4ff"), ("asr", "#9fe870"),
                         ("polish", "#ffd54f"), ("err", "#ff6b6b"),
                         ("ok", "#3ddc84"), ("dim", "#5a5a68"),
                         ("hot", "#c678dd")):
            self.console.tag_configure(tag, foreground=col)
        self.console.tag_configure("ts", foreground="#4a4a5a")
        self._clog("听写管道就绪", "ok")

    def _clog(self, msg, kind="dim"):
        """终端追加一行(线程安全, 走命令队列)。"""
        if self.console is None:
            return

        def _f():
            try:
                self.console.configure(state="normal")
                ts = time.strftime("%H:%M:%S")
                self.console.insert("end", "%s " % ts, ("ts",))
                self.console.insert("end", msg + "\n", (kind,))
                self.console.configure(state="disabled")
                self.console.see("end")
            except Exception:
                pass
        self._ui(_f)

    def set_stage(self, name):
        """高亮当前阶段; name=None 复位。"""
        def _f():
            for lb in self._stage_ls:
                lb.configure(bg=PANEL, fg="#4a4a5a")
            if name:
                for lb in self._stage_ls:
                    if lb.cget("text") == name:
                        lb.configure(bg="#1f3d2a", fg="#3ddc84")
        self._ui(_f)

    def log(self, msg, kind="dim"):
        self._clog(msg, kind)

    # ------------------------------------------------------------ 构建
    def _build(self):
        import tkinter as tk
        from tkinter import font as tkfont
        tk_ = self.tk
        r = self.root
        r.title("听写指示器")
        r.overrideredirect(True)
        r.configure(bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        try:
            r.attributes("-topmost", True)
            r.attributes("-alpha", 0.0)
        except Exception:
            pass

        # --- 顶部状态条(实心色块, 一眼可辨) ---
        self.bar = tk.Frame(r, bg=DIM, height=6)
        self.bar.pack(side="top", fill="x")
        self.bar.pack_propagate(False)
        top = tk.Frame(r, bg=PANEL)
        top.pack(fill="x", padx=self.PAD, pady=(12, 4))
        self.dot = tk.Label(top, text="●", font=("Microsoft YaHei UI", 13, "bold"),
                            bg=PANEL, fg=DIM)
        self.dot.pack(side="left")
        self.state_l = tk.Label(top, text="● 就绪",
                                font=("Microsoft YaHei UI", 12, "bold"),
                                bg=PANEL, fg="#c9c9d4")
        self.state_l.pack(side="left", padx=(6, 0))
        self.time_l = tk.Label(top, text="", font=("Consolas", 12),
                               bg=PANEL, fg=DIM)
        self.time_l.pack(side="right")

        # --- 正文(大字号, 逐字浮现) ---
        self.text_font = tkfont.Font(family="Microsoft YaHei UI",
                                     size=self.font_size)
        self.text_l = tk.Label(r, text="", font=self.text_font,
                               bg=PANEL, fg=TXT, justify="left", anchor="nw",
                               wraplength=self.W - self.PAD * 2)
        self.text_l.pack(fill="x", padx=self.PAD, pady=(4, 6))

        # --- 音量条 ---
        self.vc = tk.Canvas(r, height=14, bg=PANEL, highlightthickness=0, bd=0)
        self.vc.pack(fill="x", padx=self.PAD, pady=(0, 6))

        # --- 提示行 ---
        self.hint_l = tk.Label(r, text="", font=("Microsoft YaHei UI", 10),
                               bg=PANEL, fg="#6f6f7b")
        self.hint_l.pack(padx=self.PAD, anchor="w", pady=(0, 12))

        # --- 终端风格管道面板(可选): 让识别过程不再是黑箱 ---
        self.console = None
        if self.cfg.get("dictation_console", True):
            self._build_console(r, tk_)

        # 拖动
        for w in (r, top, self.text_l, self.vc, self.hint_l):
            try:
                w.bind("<ButtonPress-1>", self._drag_start)
                w.bind("<B1-Motion>", self._drag_move)
            except Exception:
                pass

        r.update_idletasks()
        self._place()
        self._draw_volume()
        self._noactivate()
        r.after(60, self._fade_in)
        self._drain()          # 启动命令泵(自身 25ms 一轮)

    def _noactivate(self):
        """置顶但不抢焦点 —— 否则粘贴会粘到错误窗口。"""
        try:
            import ctypes
            hwnd = self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongPtrW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _place(self):
        try:
            import pyautogui
            sw, sh = pyautogui.size()
        except Exception:
            sw, sh = 1920, 1080
        h = self.root.winfo_reqheight()
        x = max(8, (sw - self.W) // 2)
        y = max(8, sh - h - self.bottom_margin)
        self.root.geometry("%dx%d+%d+%d" % (self.W, h, x, y))

    # ------------------------------------------------------------ 绘制
    def _wrap_lines(self, text):
        out, cur = [], ""
        maxw = self.W - self.PAD * 2
        for ch in text:
            if ch == "\n":
                out.append(cur)
                cur = ""
                continue
            t = cur + ch
            if self.text_font.measure(t) > maxw and cur:
                out.append(cur)
                cur = ch
            else:
                cur = t
        out.append(cur)
        return out

    def _render(self):
        """文本变化后重排 + 让窗口高度跟着内容走。"""
        txt = self._shown
        lines = self._wrap_lines(txt) or [""]
        self.text_l.configure(text="\n".join(lines))
        try:
            self._place()
        except Exception:
            pass

    def _draw_volume(self):
        c = self.vc
        c.delete("all")
        c.update_idletasks()
        w = c.winfo_width() or (self.W - self.PAD * 2)
        h = 14
        n = len(self._levels)
        bw = max(2.0, w / n - 3.0)
        for i, v in enumerate(self._levels):
            mag = max(2.0, min(1.0, v * 8.0) * h)
            x0 = i * (w / n)
            col = "#3ddc84" if v > 0.02 else "#2c2c3a"
            c.create_rectangle(x0, h - mag, x0 + bw, h, fill=col, outline="")

    # ------------------------------------------------------------ 动画
    def _tick(self):
        self._tick_job = None
        if not self._pending:
            return
        n = len(self._pending)
        self._shown += self._pending[0]
        self._pending = self._pending[1:]
        self._render()
        step = self.char_ms
        if n > 24:
            step = max(6, self.char_ms // 3)
        elif n > 10:
            step = max(12, self.char_ms // 2)
        if self._pending:
            self._tick_job = self.root.after(step, self._tick)

    def _apply_text(self, text):
        """在 Tk 线程里执行: 计算差量并逐字推出。"""
        if text == self._target:
            return
        self._target = text
        if text.startswith(self._shown):
            self._pending = text[len(self._shown):]
        else:
            # ASR 重写了前缀 -> 退到最长公共前缀, 再从那里逐字接上
            i = 0
            m = min(len(self._shown), len(text))
            while i < m and self._shown[i] == text[i]:
                i += 1
            self._shown = text[:i]
            self._pending = text[i:]
            self._render()
        if self._pending and self._tick_job is None:
            self._tick()

    # ------------------------------------------------------------ 对外 API
    def _cancel_hide(self):
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    def _fade_in(self):
        try:
            self.root.attributes("-alpha", 0.97)
        except Exception:
            pass

    def show(self, state, hint=""):
        """切换状态(录音中/识别中/已输入/已取消)。"""
        self._state = state
        if state == "recording":
            self._t0 = time.time()

        def _f():
            if state == "recording":
                self._shown = ""
                self._target = ""
                self._pending = ""
                self._levels = [0.0] * len(self._levels)
            self._cancel_hide()
            self._show_inner(state, hint)
        self._ui(_f)

    def _show_inner(self, state, hint):
        col, txt = STATE.get(state, STATE["idle"])
        self.dot.configure(fg=col)
        self.state_l.configure(text=txt, fg=col)
        self.bar.configure(bg=col)
        self.hint_l.configure(text=hint or "")
        self._render()
        self._draw_volume()
        if state == "recording":
            self._timer()

    def _timer(self):
        if self._state != "recording":
            self.time_l.configure(text="")
            return
        s = int(time.time() - self._t0)
        self.time_l.configure(text="%d:%02d" % (s // 60, s % 60))
        try:
            self.root.after(250, self._timer)
        except Exception:
            pass

    def push_text(self, text):
        """推入最新文本(partial 或最终), 逐字动画补齐。"""
        self._ui(lambda: self._apply_text(text))

    def set_level(self, rms):
        """更新音量条。"""
        with self._lock:
            self._levels.append(float(rms))
            if len(self._levels) > 28:
                self._levels = self._levels[-28:]
        self._ui(self._draw_volume)

    def finish(self, text, ok=True, hint=""):
        """最终文本: 立刻完整显示, 短暂停留后淡出。"""
        def _f():
            self._pending = ""
            if self._tick_job is not None:
                try:
                    self.root.after_cancel(self._tick_job)
                except Exception:
                    pass
                self._tick_job = None
            self._shown = text
            self._target = text
            self._show_inner("done" if ok else "cancel", hint)
            self._cancel_hide()
            self._hide_job = self.root.after(
                int(self.idle_hide_s * 1000), self.hide)
        self._ui(_f)

    def hide(self):
        def _h():
            try:
                self.root.attributes("-alpha", 0.0)
            except Exception:
                pass
        self._ui(_h)

    def _ui(self, fn):
        """跨线程投递命令(不直接碰 Tk)。"""
        if not self.enabled:
            return
        try:
            self._q.put(fn)
        except Exception:
            pass

    # ------------------------------------------------------------ 拖动
    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        try:
            x = self.root.winfo_x() + e.x - self._dx
            y = self.root.winfo_y() + e.y - self._dy
            self.root.geometry("+%d+%d" % (x, y))
        except Exception:
            pass
