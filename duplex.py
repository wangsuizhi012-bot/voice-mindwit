# -*- coding: utf-8 -*-
"""全双工引擎 —— 播报中持续监听、可打断、LLM 流式逐句播报。

借鉴 Whispera 的 realtime 行为（其源码 Cython 加密，抄架构不抄码），自建:

    ┌─ MicThread(复用 voice_assistant.Recorder 的 subscribe 音频) ─┐
    │  每块 16k int16 音频 → BargeInMonitor 帧级 VAD               │
    │                                                              │
    │  状态机:  IDLE ─人声─▶ LISTENING ─静音─▶ THINKING ─首句─▶    │
    │           SPEAKING ─人声≥350ms─▶ ⚡INTERRUPT ─▶ LISTENING     │
    │                  SPEAKING ─播完─▶ IDLE                       │
    └──────────────────────────────────────────────────────────────┘

三个组件:
    AsyncSpeaker   SAPI 异步播报（SPF_ASYNC 逐句入队，Stop 可打断）
    BargeInMonitor 播报期间的持续人声监听（回声对策: RMS 阈值 + 持续 ≥350ms
                   才判定打断；外放会拾到 TTS 声，耳机体验最佳）
    stream_chat    llama-swap 流式 + 句级切分（首句延迟 = 首句生成时间）

自测:
    python duplex.py --tts          # 播一句长文本，2 秒后自动打断（验证 Stop）
    python duplex.py --stream       # 验证 LLM 流式逐句（需网关在线）
"""
import queue
import threading
import time


# ================================================================ TTS
class AsyncSpeaker:
    """SAPI 异步播报器。speak() 逐句入队立即返回；stop() 打断清空。"""

    SPF_ASYNC = 1
    SPF_PURGEBEFORESPEAK = 8

    def __init__(self):
        self._lock = threading.Lock()
        self._spk = None

    def _speaker(self):
        with self._lock:
            if self._spk is None:
                import win32com.client
                self._spk = win32com.client.Dispatch("SAPI.SpVoice")
            return self._spk

    def speak(self, text):
        """异步入队一句（不阻塞调用线程）。"""
        if not (text or "").strip():
            return
        try:
            self._speaker().Speak(text, self.SPF_ASYNC)
        except Exception:
            pass

    def stop(self):
        """打断：清空播报队列并停止当前句。"""
        try:
            spk = self._speaker()
            spk.Speak("", self.SPF_ASYNC | self.SPF_PURGEBEFORESPEAK)
        except Exception:
            pass

    def speaking(self):
        """是否还在播（SPSVSRS_DONE=1, ISPEAKING=2）。

        ⚠ 2026-10-14 修：原写法是 `GetStatus(0).RunningState`，但 pywin32 的
        SAPI.SpVoice **没有 GetStatus 方法**（late-binding 与 EnsureDispatch
        两种绑定都实测确认），属性名就叫 `Status`。原写法抛 AttributeError
        被 except 吞掉 -> speaking() 恒为 False -> wait_done() 误判「已播完」
        立即返回，本函数上方的「先等进入播放态」保护从未生效。
        """
        try:
            return self._speaker().Status.RunningState == 2
        except Exception:
            return False

    def wait_done(self, timeout=None, stop_flag=None):
        """阻塞至播完；每 0.1s 检查 stop_flag()（True=外部要求打断）。

        SAPI 异步 Speak 到 RunningState 变 ISPEAKING 有 ~0.2s 延迟，
        必须先等它进入播放态，否则会误判「已完成」提前返回（自测抓到）。
        """
        t0 = time.time()
        deadline = time.time() + 1.0
        while not self.speaking():
            if stop_flag is not None and stop_flag():
                self.stop()
                return False
            if time.time() > deadline:
                break     # 1s 还没进入播放态 = 没内容可播
            time.sleep(0.05)
        while True:
            if stop_flag is not None and stop_flag():
                self.stop()
                return False
            if not self.speaking():
                return True
            if timeout and time.time() - t0 > timeout:
                self.stop()
                return False
            time.sleep(0.1)


# ================================================================ 打断监听
class BargeInMonitor(threading.Thread):
    """订阅麦克风音频块，SPEAKING 期间持续 VAD，人声持续 >= hold_s 触发打断。

    回声对策（外放场景 TTS 声会被麦克风拾到）:
        1) RMS 硬阈值（默认 0.02，TTS 播放声在正常音量下 RMS 通常 < 0.015）
        2) 持续时长门槛 hold_s（默认 0.35s，TTS 语音是连续的但能量波动，
           连续 350ms 超 RMS 阈值的人声基本只可能是真人插话）
        3) 打断后有 cooldown_s（默认 1.0s）免疫期，防止打断瞬间误触发
        戴耳机 = 零回声，阈值可放宽。--calib 模式可实测自己环境的阈值。
    """

    def __init__(self, on_interrupt, rms_threshold=0.02, hold_s=0.35,
                 cooldown_s=1.0, frame_ms=30, sample_rate=16000):
        super().__init__(daemon=True)
        self.on_interrupt = on_interrupt
        self.rms_threshold = rms_threshold
        self.hold_s = hold_s
        self.cooldown_s = cooldown_s
        self.frame_bytes = sample_rate * 2 * frame_ms // 1000  # int16 单声道
        self.speaking = False          # 由引擎切换（TTS 是否在播）
        self.enabled = True
        self._buf = b""
        self._hot_since = 0.0
        self._last_fire = 0.0
        self._run = True

    def feed(self, data):
        """音频块入口（由 Recorder.subscribe 转发，采集线程上下文）。"""
        if not (self._run and self.enabled and self.speaking):
            self._hot_since = 0.0
            return
        self._buf += data
        import numpy as np
        while len(self._buf) >= self.frame_bytes:
            frame, self._buf = self._buf[:self.frame_bytes], self._buf[self.frame_bytes:]
            a = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(a * a)))
            now = time.time()
            if rms >= self.rms_threshold:
                if self._hot_since == 0.0:
                    self._hot_since = now
                elif now - self._hot_since >= self.hold_s:
                    if now - self._last_fire >= self.cooldown_s:
                        self._last_fire = now
                        self._hot_since = 0.0
                        try:
                            self.on_interrupt()
                        except Exception:
                            pass
                        return
            else:
                self._hot_since = 0.0

    def run(self):
        while self._run:
            time.sleep(0.2)

    def stop(self):
        self._run = False


# ================================================================ LLM 流式
def stream_chat(text, history, llm_base, model, temperature=0.2,
                max_tokens=500, timeout=90):
    """流式对话。返回 (full_text, sentence_iter) —— sentence_iter 逐句产出，
    边生成边说话（首句延迟 = 首句生成时间，不等整段）。"""
    import json as _json
    import requests
    msgs = list(history[-8:]) + [{"role": "user", "content": text}]
    s = requests.Session()
    s.trust_env = False
    r = s.post(llm_base.rstrip("/") + "/chat/completions",
               json={"model": model, "messages": msgs,
                     "temperature": temperature, "max_tokens": max_tokens,
                     "stream": True},
               stream=True, timeout=timeout)
    r.encoding = "utf-8"   # 否则 iter_lines(decode_unicode) 按 latin-1 解码出乱码
    full = []
    buf = ""

    def sentences():
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = (_json.loads(payload)["choices"][0]["delta"]
                         .get("content") or "")
            except Exception:
                continue
            if not delta:
                continue
            full.append(delta)
            nonlocal buf
            buf += delta
            # 句级切分：。！？!?；换行；引号闭合
            import re
            while True:
                m = re.search(r"[。！？!?…；;]|\n", buf)
                if not m:
                    break
                sent = buf[:m.end()].strip()
                buf = buf[m.end():]
                if sent:
                    yield sent
        if buf.strip():
            yield buf.strip()

    return full, sentences()


# ================================================================ 引擎
class DuplexEngine:
    """把三件套粘成「可打断的语音对话循环」。

    用法（集成进 voice_assistant chat 模式）:
        eng = DuplexEngine(asr=现有transcribe, llm_base=..., model=...)
        recorder.subscribe(eng.feed)          # 接入音频
        eng.start()
        # eng.state 可读: idle/listening/thinking/speaking
    """

    def __init__(self, asr, llm_base, model, history=None,
                 on_event=None, rms_threshold=0.02):
        self.asr = asr                    # fn(int16_bytes) -> str
        self.llm_base = llm_base
        self.model = model
        self.history = history if history is not None else []
        self.on_event = on_event or (lambda kind, text: None)
        self.spk = AsyncSpeaker()
        self.monitor = BargeInMonitor(self.interrupt, rms_threshold=rms_threshold)
        self.state = "idle"
        self._seg_q = queue.Queue()       # 语音段(bytes) 队列
        self._interrupted = False
        self._run = True

    # ---- 状态
    def _set_state(self, s):
        self.state = s
        self.on_event("state", s)

    # ---- 音频入口（Recorder.subscribe 转发）
    def feed(self, data):
        self.monitor.feed(data)

    # ---- 打断
    def interrupt(self):
        if self.state != "speaking":
            return
        self._interrupted = True
        self.spk.stop()
        self.on_event("interrupt", "用户插话，停止播报")
        self._set_state("listening")

    # ---- 主循环
    def start(self):
        self.monitor.start()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._run = False
        self.monitor.stop()
        self.spk.stop()

    def _loop(self):
        """简化版对话循环: 段队列 → ASR → 流式LLM → 逐句TTS（可打断）。

        语音段来源由外部喂（voice_assistant.Recorder 的 _emit 处把段 bytes
        put 进 seg_q；不重复造 VAD）。
        """
        while self._run:
            try:
                seg = self._seg_q.get(timeout=1.0)
            except queue.Empty:
                if self.state != "idle" and not self.spk.speaking():
                    self._set_state("idle")
                continue
            self._interrupted = False
            self._set_state("thinking")
            try:
                text = (self.asr(seg) or "").strip()
            except Exception as e:
                self.on_event("error", "ASR 异常: %r" % (e,))
                continue
            if not text or self._is_stop(text):
                continue
            self.on_event("user", text)
            self.history.append({"role": "user", "content": text})
            # 流式: 首句一到就开播
            self._set_state("speaking")
            reply_parts = []
            try:
                _, sent_iter = stream_chat(
                    text, self.history, self.llm_base, self.model)
                first = True
                for sent in sent_iter:
                    if self._interrupted:
                        break
                    reply_parts.append(sent)
                    self.spk.speak(sent)
                    if first:
                        first = False
                if not self._interrupted:
                    self.spk.wait_done(stop_flag=lambda: self._interrupted)
            except Exception as e:
                self.on_event("error", "LLM/TTS 异常: %r" % (e,))
            if reply_parts and not self._interrupted:
                reply = "".join(reply_parts)
                self.history.append({"role": "assistant", "content": reply})
                self.on_event("reply", reply)
            elif self._interrupted:
                # 打断的半句也留痕（截断点即用户插话点）
                if reply_parts:
                    self.history.append({"role": "assistant",
                                         "content": "".join(reply_parts) + "…[被打断]"})
            self._set_state("idle")

    def _is_stop(self, text):
        return text.strip().strip("。.！!？? ") in ("退出聊天", "退出对话", "停止聊天")


# ================================================================ 自测
def _selftest_tts():
    spk = AsyncSpeaker()
    spk.speak("这是一段用来测试打断功能的播报。" * 6)
    print("speaking?", spk.speaking())
    time.sleep(2)
    print("2s 后打断...")
    spk.stop()
    time.sleep(0.5)
    print("speaking after stop?", spk.speaking())
    print("TTS 打断测试", "OK" if not spk.speaking() else "FAIL")


def _selftest_stream():
    import json as _j
    cfg = _j.load(open("config.json", encoding="utf-8"))
    t0 = time.time()
    _, it = stream_chat("用三句话介绍你自己", [], cfg["llm_base"], cfg["model"])
    for i, s in enumerate(it):
        print("[%.1fs] 句%d: %s" % (time.time() - t0, i + 1, s))
        if i >= 5:
            break


if __name__ == "__main__":
    import sys
    if "--tts" in sys.argv:
        _selftest_tts()
    elif "--stream" in sys.argv:
        _selftest_stream()
    else:
        print("用法: python duplex.py --tts | --stream")
