# -*- coding: utf-8 -*-
"""真实链路验证: 真模型识别 + 真 LLM 校对。输出必须可读。"""
import sys, os, time, wave
sys.path.insert(0, r"E:\AI\voice-assistant")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import asr_better, asr_polish

print("=" * 64)
WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_test.wav")
with wave.open(WAV, "rb") as w:
    sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
    raw = w.readframes(n)
print("wav: sr=%d ch=%d sw=%d dur=%.1fs" % (sr, ch, sw, n / sr))
a = np.frombuffer(raw, dtype=np.int16)
if ch > 1:
    a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
if sr != 16000:
    import cv2  # noqa
# 简单线性重采样到 16k
if sr != 16000:
    x = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
    a = np.interp(x, np.arange(len(a)), a.astype("float32")).astype(np.int16)
print("16k 样本:", len(a))

print("=" * 64)
print("[1] 音频前端")
audio, info = asr_better.preprocess(a, None)
print("   rms=%.4f peak=%.3f gain=%.2f lang=%s noise=%s"
      % (info["rms"], info["peak"], info["gain"],
         asr_better.pick_language(audio, None), asr_better.is_noise(audio, None)))

print("=" * 64)
print("[2] SenseVoice 真识别(首次加载模型, 请稍候)")
t0 = time.time()
asr = asr_better.ASRLocal()
asr.load()
print("   加载 %.1fs device=%s" % (time.time() - t0, asr.device()))
for tag, seg in (("整段", audio), ("前3秒", audio[:48000])):
    t0 = time.time()
    txt = asr.transcribe(seg, language=asr_better.pick_language(seg, None))
    ms = (time.time() - t0) * 1000
    dur = len(seg) / 16000.0
    print("   [%s] %.2fs 音频 -> %dms (%.2fx) => %s" % (tag, dur, ms, ms / 1000 / max(dur, .01), txt))

print("=" * 64)
print("[3] LLM 校对(真实网关)")
base = "http://localhost:9292/v1"
cases = [
    ("我想用康福有爱跑一张图，然后用拉玛模型总结", ["ComfyUI", "llama.cpp"]),
    ("帮我把这个视频用闪光视觉放大四倍", ["FlashVSR"]),
]
for txt, vocab in cases:
    t0 = time.time()
    new, note = asr_polish.polish(txt, base=base, model="spark",
                                  vocab=vocab, timeout=8.0)
    print("   [%dms] %s" % ((time.time() - t0) * 1000, note))
    print("     原: %s" % txt)
    print("     新: %s" % new)
print("=" * 64)
print("DONE")
