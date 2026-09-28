# -*- coding: utf-8 -*-
"""按键听写端到端冒烟(不碰真实热键/不真粘贴):
   UI 悬浮窗渲染 + Dictation 录音缓冲 + partial 递进 + 最终输出 -> 剪贴板
运行: python test_dictation_smoke.py
产出截图: shots/dictation_ui_*.png
"""
import os
import sys
import time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def ck(name, cond, info=""):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print("[%s] %-30s %s" % ("PASS" if cond else "FAIL", name, info))


import dictation as D
import dictation_ui as DU

cfg = {
    "dictation_output": "clipboard",      # 冒烟不真粘贴, 免得粘到当前窗口
    "dictation_partial": True,
    "dictation_partial_interval_s": 0.35,
    "dictation_partial_min_new_s": 0.2,
    "dictation_history": True,
    "dictation_ui": True,
    "dictation_char_ms": 25,
    "dictation_font_size": 20,
    "dictation_hotkey": "ctrl+alt+space",
}

print("=" * 64)
print("1) UI 悬浮窗")
ui = DU.DictationUI(cfg)
ck("ui.start()", ui.start(), "")
ck("ui root alive", ui.root is not None, "")
ui.show("recording", "CTRL+ALT+SPACE - testing")
time.sleep(0.4)

SHOTS = os.path.join(HERE, "shots")
os.makedirs(SHOTS, exist_ok=True)

# ---- partial 递进 + 逐字动画 ----
seq = ["你好", "你好，这是", "你好，这是一段测试文本",
       "你好，这是一段测试文本，用来验证逐字显示的观感。"]
import pyautogui
for s in seq:
    ui.push_text(s)
    ui.set_level(0.15 + 0.1 * np.random.rand())
    time.sleep(0.28)
time.sleep(1.2)
p1 = os.path.join(SHOTS, "dictation_ui_recording.png")
pyautogui.screenshot().save(p1)
ck("shot recording", os.path.exists(p1), p1)
ck("text fully shown", ui._shown == seq[-1], repr(ui._shown))

print("=" * 64)
print("2) Dictation 录音链路(stub ASR)")
calls = {"n": 0}


def stub_asr(audio, partial=False):
    calls["n"] += 1
    secs = len(audio) / 16000.0
    if partial:
        return "识别到的内容"[: int(min(6, 2 + secs))]
    return "这是最终识别结果，已经过校对。"


dt = D.Dictation(cfg, ui=ui, log=lambda m: print("   " + str(m)))
dt.bind_asr(stub_asr)
ck("hotkey vks", dt._vks == [0x11, 0x12, 0x20], str(dt._vks))

dt.start("smoke")
ck("recording on", dt.recording, "")
# 喂 1.2s 音频(30ms/块 @16k)
for _ in range(40):
    dt.feed((np.random.randn(480) * 3000).astype(np.int16).tobytes())
    time.sleep(0.012)
ck("buffer grew", dt._buf_seconds() > 0.4, "%.2fs" % dt._buf_seconds())
ck("partial fired", calls["n"] >= 1, "calls=%d" % calls["n"])
dt.stop(commit=True)
ck("stopped", not dt.recording, "")

time.sleep(0.6)
p2 = os.path.join(SHOTS, "dictation_ui_done.png")
pyautogui.screenshot().save(p2)
ck("shot done", os.path.exists(p2), p2)

# ---- 剪贴板是否拿到最终文本 ----
import win32clipboard
got = None
try:
    win32clipboard.OpenClipboard()
    got = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    win32clipboard.CloseClipboard()
except Exception as e:
    print("   clipboard read fail: %r" % e)
ck("clipboard has result", got == "这是最终识别结果，已经过校对。", repr(got))

# ---- 历史落盘 ----
ck("history file", os.path.exists(D.HISTORY_PATH), D.HISTORY_PATH)

# ---- 取消路径 ----
dt.start("cancel")
for _ in range(20):
    dt.feed((np.random.randn(480) * 3000).astype(np.int16).tobytes())
    time.sleep(0.01)
dt.stop(commit=False)
ck("cancel ok", not dt.recording, "")

print("=" * 64)
print("PASS %d / FAIL %d" % (PASS, FAIL))
ui.destroy()
sys.exit(1 if FAIL else 0)
