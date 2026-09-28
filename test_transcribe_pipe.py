# -*- coding: utf-8 -*-
"""转写链路回归测试: 直接打 voice_assistant.transcribe 的真实调用路径。

为什么要有这个文件:
    曾经因为 _clog() 只收 2 个参数、调用处传了 3 个, 导致**任何一句语音都崩**,
    而当时的自检只覆盖 asr_better / asr_polish, 没打到 transcribe 这条主链路。
    本文件用桩件把 transcribe 完整跑一遍(含噪声门/前端/校对/终端面板回传)。

运行: python test_transcribe_pipe.py
"""
import os
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import voice_assistant as va

PASS = 0
FAIL = 0


def ck(name, cond, info=""):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print("[%s] %-34s %s" % ("PASS" if cond else "FAIL", name, info))


# ---------------------------------------------------------------- 桩件
class StubASR:
    def __init__(self, text="打开记事本"):
        self.text = text
        self.calls = []

    def ready(self):
        return True

    def device(self):
        return "stub"

    def transcribe(self, audio, language=None):
        self.calls.append({"n": int(audio.size), "lang": language,
                           "peak": int(abs(audio).max())})
        return self.text


class StubUI:
    """记录终端面板收到的 log / set_stage 调用。"""

    def __init__(self):
        self.logs = []
        self.stages = []

    def log(self, msg, kind="dim"):
        self.logs.append((kind, msg))

    def set_stage(self, s):
        self.stages.append(s)


def fresh(text="打开记事本"):
    asr = StubASR(text)
    ui = StubUI()
    va._asr = asr
    va._dictation_ui = ui
    va._asr_corr = {}
    va._MODEL_ID = "stub-model"
    return asr, ui


print("=" * 66)
print("1) _clog 参数形态(崩溃点回归)")
ui = StubUI()
va._dictation_ui = ui
try:
    va._clog("one")
    va._clog("two", "mic")
    va._clog("three", "mic", "ASR")
    ok = True
except TypeError as e:
    ok = False
    print("   TypeError:", e)
ck("_clog 1/2/3 参数均可用", ok)
ck("stage 被转发到 set_stage", ui.stages == ["ASR"], str(ui.stages))
ck("三条日志都进了面板", len(ui.logs) == 3, "n=%d" % len(ui.logs))

print("=" * 66)
print("2) 噪声门: 低能量段直接丢弃")
asr, ui = fresh()
low = (np.random.randn(16000) * 3).astype(np.int16)
va.CONFIG["asr_min_rms"] = 0.004
out = va.transcribe(low, partial=False, command=True)
ck("返回空", out == "", repr(out))
ck("未调 ASR", len(asr.calls) == 0, "calls=%d" % len(asr.calls))
ck("面板有噪声门提示", any("噪声门" in m for _, m in ui.logs))

print("=" * 66)
print("3) 正常转写(音量放大 + 语言策略 + 文本后处理)")
asr, ui = fresh("打开 Comefi ui")
# 纠错词典由 build_asr() 从 config.json 读入 _asr_corr, 故此处设 _asr_corr
va._asr_corr = {"Comefi ui": "ComfyUI"}
va.CONFIG["asr_polish"] = False
va.CONFIG["asr_polish_command"] = False
quiet = (np.random.randn(16000) * 600).astype(np.int16)   # 1s, 小声
out = va.transcribe(quiet, partial=False, command=True)
ck("非空返回", bool(out), repr(out))
ck("ASR 被调用一次", len(asr.calls) == 1, "calls=%d" % len(asr.calls))
ck("音频被归一化放大", asr.calls[0]["peak"] > 20000,
   "peak=%d" % asr.calls[0]["peak"])
ck("短音频强制 zh", asr.calls[0]["lang"] == "zh", str(asr.calls[0]["lang"]))
ck("纠错词典生效", out == "打开 ComfyUI", repr(out))
ck("前端指标回传面板", any("rms=" in m for _, m in ui.logs))

print("=" * 66)
print("4) 校对开关: 指令模式默认关, 听写模式开")
called = {"n": 0}


def fake_polish(text, **kw):
    called["n"] += 1
    return text + "。", "已校对 (stub)"


va.asr_polish.polish = fake_polish
asr, ui = fresh("今天天气不错")
va.CONFIG["asr_polish"] = True
va.CONFIG["asr_polish_command"] = False
va.CONFIG["asr_polish_min_chars"] = 4
va.transcribe((np.random.randn(32000) * 8000).astype(np.int16),
              partial=False, command=True)
ck("指令模式不校对", called["n"] == 0, "n=%d" % called["n"])

va.transcribe((np.random.randn(32000) * 8000).astype(np.int16),
              partial=False, command=False)
ck("听写模式会校对", called["n"] == 1, "n=%d" % called["n"])

n_before = called["n"]
va.transcribe((np.random.randn(32000) * 8000).astype(np.int16),
              partial=True, command=False)
ck("partial 不校对", called["n"] == n_before, "n=%d" % called["n"])

print("=" * 66)
print("5) 边界: 短文本跳过校对 / 面板缺失不崩")
called["n"] = 0
va.transcribe((np.random.randn(32000) * 8000).astype(np.int16),
              partial=False, command=False)   # "今天天气不错"=6字 >= 4 -> 校对
va.CONFIG["asr_polish_min_chars"] = 50
va.transcribe((np.random.randn(32000) * 8000).astype(np.int16),
              partial=False, command=False)
ck("低于 min_chars 跳过", called["n"] == 1, "n=%d" % called["n"])

va._dictation_ui = None
try:
    r = va.transcribe((np.random.randn(32000) * 8000).astype(np.int16),
                      partial=False, command=False)
    ck("面板缺失仍能转写", bool(r), repr(r))
except Exception as e:
    ck("面板缺失仍能转写", False, repr(e))

print("=" * 66)
print("PASS %d / FAIL %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
