# -*- coding: utf-8 -*-
"""按键触发的语音输入(Typeless 式听写) —— 按住说话, 松开即上屏。

为什么按键模式本身就能提升准确率:
    VAD 一直在猜「你说完了没」, 猜错就截断或吞字; 按键模式由**人**给出精确
    边界, 不吃截断、不吃句首吞字, 也不用噪声门去赌。这是本项目最大的一次
    准确率收益, 且零模型成本。

交互(照抄 local-dictate / VoiceSnap 已验证的手感):
    · 长按热键 = 按住说话, 松开即识别上屏
    · 短按热键(<250ms) = 切换模式, 再按一次结束   —— 两种自动判别, 无需切配置
    · 录音中按 Esc = 取消本次, 不输出
    · 切换模式下连续静音 N 秒自动结束(可选, 默认关)
    · 上屏走「剪贴板 + Ctrl+V」, 并把原剪贴板内容还回去(剪贴板保护)

实时性: partial 线程每 0.7s 对已录音频重识别一次, 只把新增字符推给指示器逐字
显示 —— 不引入流式模型(省 1GB 下载), 但观感接近实时字幕。

热键实现: 沿用本仓库已验证的 win32api.GetAsyncKeyState 轮询(与 F8/F9 停止键
同一套路), 不依赖 pynput 的按键归一化, 在任何窗口有焦点时都生效。

回调契约(由 voice_assistant 注入):
    asr_fn(int16_audio, partial: bool) -> str
"""
import os
import json
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
HISTORY_PATH = os.path.join(STATE_DIR, "dictation_history.jsonl")

DEFAULT_CFG = {
    "dictation_enabled": True,
    "dictation_hotkey": "ctrl+alt+space",
    "dictation_hotkey_mode": "auto",     # auto | hold | toggle
    "dictation_tap_ms": 250,             # 短于它算「短按」(切换模式)
    "dictation_cancel_key": "esc",
    "dictation_output": "paste",         # paste | clipboard | none
    "dictation_partial": True,
    "dictation_partial_interval_s": 0.7,
    "dictation_partial_min_new_s": 0.5,
    "dictation_max_seconds": 60,
    "dictation_silence_stop_s": 0,       # 0=关(切换模式下连续静音自动停止)
    "dictation_silence_rms": 0.006,
    "dictation_restore_clipboard": True,
    "dictation_paste_delay_s": 0.35,
    "dictation_history": True,
    "dictation_history_max": 200,
    "dictation_min_chars": 1,            # 最终文本短于此值不上屏(防噪声)
}

# 名字 -> 虚拟键码
_VK = {
    "ctrl": 0x11, "control": 0x11, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
    "alt": 0x12, "menu": 0x12, "alt_l": 0xA4, "alt_r": 0xA5,
    "shift": 0x10, "shift_l": 0xA0, "shift_r": 0xA1,
    "win": 0x5B, "cmd": 0x5B, "super": 0x5B,
    "space": 0x20, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "enter": 0x0D, "backspace": 0x08, "caps": 0x14,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pgdn": 0x22, "print": 0x2C, "pause": 0x13,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
for _i in range(1, 25):
    _VK["f%d" % _i] = 0x6F + _i
for _c in "0123456789":
    _VK[_c] = ord(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK[_c] = ord(_c.upper())


def vk_of(name):
    return _VK.get(str(name or "").strip().lower())


def parse_hotkey(spec):
    """'ctrl+alt+space' -> [17, 18, 32]；解析不出返回空表。"""
    out = []
    for p in str(spec or "").replace("<", "").replace(">", "").split("+"):
        v = vk_of(p)
        if v:
            out.append(v)
    return out


class Dictation:
    def __init__(self, cfg=None, ui=None, log=None):
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(cfg or {})
        self.ui = ui
        self.log = log or (lambda *a: None)
        self.asr_fn = None            # 由主程序注入
        self.recording = False
        self.buf = []                 # 累积的 16k int16 音频块(bytes)
        self._pre = []                # 预滚动缓冲(补按键到开录之间的那几帧)
        self._pre_max = 14            # ~0.42s @30ms/frame
        self._lock = threading.Lock()
        self._t_press = 0.0
        self._t_start = 0.0
        self._toggle_armed = False    # 切换模式中: 已开始, 等下一次按下结束
        self._combo_down = False
        self._vks = parse_hotkey(self.cfg["dictation_hotkey"])
        self._cancel_vk = vk_of(self.cfg.get("dictation_cancel_key", "esc")) or 0x1B
        self._partial_busy = False
        self._last_partial_len = 0.0
        self._last_partial_text = ""

    # ------------------------------------------------------------ 接线
    def bind_asr(self, fn):
        self.asr_fn = fn

    def _emit(self, msg, kind="dim", stage=None):
        """事件同时进日志 + 终端面板(可视化进程, 不做黑箱)。"""
        self.log(msg)
        if self.ui is not None:
            try:
                self.ui.log(msg, kind)
                if stage:
                    self.ui.set_stage(stage)
            except Exception:
                pass

    # ------------------------------------------------------------ 音频喂入
    def feed(self, data):
        """由主程序的麦克风回调喂入(每块 ~30ms 的 int16 bytes)。"""
        with self._lock:
            self._pre.append(data)
            if len(self._pre) > self._pre_max:
                self._pre.pop(0)
            if self.recording:
                self.buf.append(data)
        if self.recording and self.ui:
            try:
                self.ui.set_level(_rms(data))
            except Exception:
                pass

    def _snapshot(self):
        with self._lock:
            return b"".join(self.buf)

    def _buf_seconds(self):
        with self._lock:
            return len(b"".join(self.buf)) / 2 / 16000.0

    # ------------------------------------------------------------ 开始/结束
    def start(self, note=""):
        if self.recording:
            return
        with self._lock:
            self.recording = True
            self.buf = list(self._pre)      # 预滚动: 不吞第一个字
            self._pre = []
        self._t_start = time.time()
        self._last_partial_len = 0.0
        self._last_partial_text = ""
        self._emit("[MIC] 开始录音 · 预滚 %.2fs 已垫入" % (len(self._pre) * 0.032),
                   "mic", "MIC")
        if self.ui:
            self.ui.show("recording", self._hint())
        if self.cfg.get("dictation_partial"):
            threading.Thread(target=self._partial_loop, daemon=True).start()

    def stop(self, commit=True):
        if not self.recording:
            return
        self.recording = False
        self._toggle_armed = False
        raw = self._snapshot()
        secs = len(raw) / 2 / 16000.0
        with self._lock:
            self.buf = []
        if not commit:
            self._emit("[MIC] 已取消 (%.1fs, 丢弃)" % secs, "err", None)
            if self.ui:
                self.ui.finish("", ok=False, hint="按 Esc 取消")
            return
        if secs < 0.25:
            self._emit("[MIC] 太短(%.2fs), 忽略" % secs, "err", None)
            if self.ui:
                self.ui.finish("", ok=False, hint="说得太短")
            return
        rms, peak = _audio_stats(raw)
        self._emit("[MIC] 停止 · %.2fs · rms=%.4f peak=%.2f"
                   % (secs, rms, peak), "mic", "ASR")
        if self.ui:
            self.ui.show("recognizing", "识别中…")
        t0 = time.time()
        text = self._recognize(raw)
        ms = int((time.time() - t0) * 1000)
        self._emit("[ASR] 最终 %dms (%.2fx 实时) → %s"
                   % (ms, (ms / 1000.0) / max(secs, 0.01), text),
                   "asr" if text else "err")
        if not text or len(text.strip()) < int(self.cfg.get("dictation_min_chars", 1)):
            self._emit("[ASR] 无有效文本, 不上屏", "err", None)
            if self.ui:
                self.ui.finish("", ok=False, hint="没听清，再说一次")
            return
        self.log("  [听写] %dms · %.1fs → %s" % (ms, secs, text))
        self._output(text)
        self._save_history(text, secs, ms)
        if self.ui:
            self.ui.finish(text, ok=True, hint=self._hint_done())

    def _recognize(self, raw):
        import numpy as np
        if self.asr_fn is None:
            return ""
        audio = np.frombuffer(raw, dtype=np.int16)
        try:
            return (self.asr_fn(audio, partial=False) or "").strip()
        except Exception as e:
            self.log("  [听写] 识别失败: " + repr(e)[:80])
            return ""

    # ------------------------------------------------------------ 实时 partial
    def _partial_loop(self):
        import numpy as np
        iv = float(self.cfg.get("dictation_partial_interval_s", 0.7))
        min_new = float(self.cfg.get("dictation_partial_min_new_s", 0.5))
        while self.recording:
            time.sleep(iv)
            if not self.recording:
                return
            secs = self._buf_seconds()
            if self._partial_busy or secs < 0.45 or secs - self._last_partial_len < min_new:
                continue
            self._last_partial_len = secs
            self._partial_busy = True
            self._emit("[ASR] partial %.1fs …" % secs, "dim", "ASR")
            try:
                audio = np.frombuffer(self._snapshot(), dtype=np.int16)
                t0 = time.time()
                txt = (self.asr_fn(audio, partial=True) or "").strip()
                pm = int((time.time() - t0) * 1000)
                if txt and txt != self._last_partial_text:
                    self._last_partial_text = txt
                    self._emit("[ASR] partial %dms → %s" % (pm, txt), "asr")
                    if self.ui:
                        self.ui.push_text(txt)
            except Exception:
                pass
            finally:
                self._partial_busy = False
            # 静音自动停止(切换模式用)
            ss = float(self.cfg.get("dictation_silence_stop_s", 0) or 0)
            if ss > 0 and secs > 1.0:
                with self._lock:
                    tail = b"".join(self.buf[-int(ss * 33):]) if self.buf else b""
                if tail and _rms(tail) < float(self.cfg.get("dictation_silence_rms", 0.006)):
                    self.log("  [听写] 静音 %.1fs, 自动结束" % ss)
                    self.stop(commit=True)
                    return
            if time.time() - self._t_start > float(self.cfg.get("dictation_max_seconds", 60)):
                self.log("  [听写] 到最大时长, 自动结束")
                self.stop(commit=True)
                return

    # ------------------------------------------------------------ 输出
    def _output(self, text):
        mode = str(self.cfg.get("dictation_output", "paste")).lower()
        if mode == "none":
            return
        if mode == "clipboard":
            _copy(text)
            self.log("  [听写] 已复制到剪贴板")
            return
        old = _read_clipboard_text() if self.cfg.get("dictation_restore_clipboard") else None
        _copy(text)
        try:
            import pyautogui
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
        except Exception as e:
            self._emit("[PASTE] 粘贴失败: %r" % e, "err", None)
            self.log("  [听写] 粘贴失败: " + repr(e)[:80])
            return
        self._emit("[PASTE] 已上屏 %d 字%s" % (
            len(text), " · 原剪贴板已还原" if old else ""), "ok", "PASTE")
        self.log("  [听写] 已上屏")
        if old:
            time.sleep(float(self.cfg.get("dictation_paste_delay_s", 0.35)))
            try:
                _copy(old)
            except Exception:
                pass

    def _save_history(self, text, secs, ms):
        if not self.cfg.get("dictation_history", True):
            return
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "sec": round(secs, 2), "ms": ms,
                                    "text": text}, ensure_ascii=False) + "\n")
            mx = int(self.cfg.get("dictation_history_max", 200))
            with open(HISTORY_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > mx:
                with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[-mx:])
        except Exception:
            pass

    # ------------------------------------------------------------ 热键轮询
    def _hint(self):
        mode = str(self.cfg.get("dictation_hotkey_mode", "auto")).lower()
        hk = str(self.cfg.get("dictation_hotkey", "")).upper()
        if mode == "hold":
            return "%s · 松开即上屏 · Esc 取消" % hk
        return "%s · 长按=按住说 · 短按=说完再按一次结束 · Esc 取消" % hk

    def _hint_done(self):
        return ("已上屏（原剪贴板已还原）"
                if self.cfg.get("dictation_restore_clipboard") else "已上屏")

    def hotkey_loop(self):
        """在守护线程里跑: 轮询热键按下/松开 + Esc 取消。"""
        import win32api
        if not self._vks:
            self.log("  听写热键配置无效: %r" % self.cfg.get("dictation_hotkey"))
            return
        hk = str(self.cfg.get("dictation_hotkey", "")).upper()
        self.log("  听写热键: %s（长按=按住说 · 短按=再说一次结束 · Esc 取消）" % hk)
        mode = str(self.cfg.get("dictation_hotkey_mode", "auto")).lower()
        tap_ms = float(self.cfg.get("dictation_tap_ms", 250))
        while True:
            try:
                if win32api.GetAsyncKeyState(self._cancel_vk) & 0x8000:
                    if self.recording:
                        self.stop(commit=False)
                        time.sleep(0.4)      # 防抖
                        continue
                down = all(win32api.GetAsyncKeyState(v) & 0x8000 for v in self._vks)
                if down and not self._combo_down:
                    self._combo_down = True
                    self._t_press = time.time()
                    self._emit("[HOTKEY] 按下", "hot", "MIC")
                    if not self.recording:
                        self.start()
                    elif self._toggle_armed:
                        self.stop(commit=True)      # 切换模式: 第二次按下结束
                elif not down and self._combo_down:
                    self._combo_down = False
                    held = (time.time() - self._t_press) * 1000.0
                    self._emit("[HOTKEY] 松开 (%.0fms)" % held, "hot")
                    if not self.recording:
                        continue
                    if mode == "toggle":
                        self._toggle_armed = True
                    elif held < tap_ms:
                        self._toggle_armed = True
                        self.log("  [听写] 切换模式：说完后按一次热键结束")
                        if self.ui:
                            self.ui.show("recording", "说完再按一次热键结束 · Esc 取消")
                    else:
                        self.stop(commit=True)
            except Exception:
                pass
            time.sleep(0.02)

    def start_hotkeys(self):
        if not self.cfg.get("dictation_enabled", True):
            return False
        threading.Thread(target=self.hotkey_loop, daemon=True).start()
        return True


def _rms(data):
    import numpy as np
    a = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
    if a.size == 0:
        return 0.0
    return float((a * a).mean() ** 0.5)


def _audio_stats(raw):
    import numpy as np
    a = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    if a.size == 0:
        return 0.0, 0.0
    return float((a * a).mean() ** 0.5), float(abs(a).max())


# ---------------------------------------------------------------- 剪贴板
def _copy(text):
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _read_clipboard_text():
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None
    return None
