# -*- coding: utf-8 -*-
"""准确率改造的自检: 只测纯逻辑, 不开麦克风。运行: python test_asr_upgrade.py"""
import os
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asr_better
import asr_polish
import dictation

PASS = 0
FAIL = 0


def ck(name, got, want):
    global PASS, FAIL
    ok = (got == want)
    PASS += ok
    FAIL += (not ok)
    print("[%s] %-28s got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))


def ck_true(name, cond, info=""):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print("[%s] %-28s %s" % ("PASS" if cond else "FAIL", name, info))


print("=" * 64)
print("1) audio front-end")
# 小声说话: peak 只有 2000 -> 应被放大
quiet = (np.random.randn(16000) * 700).astype(np.int16)
out, info = asr_better.preprocess(quiet)
ck_true("quiet amplified", info["gain"] > 2.0, "gain=%.2f" % info["gain"])
ck_true("peak near target", 0.85 <= info["peak"] * info["gain"] <= 1.0 or
        abs(abs(out).max() / 32767.0 - 0.90) < 0.02,
        "out_peak=%.3f" % (abs(out).max() / 32767.0))
# 纯噪声底噪: gain 不应失控
noise = (np.random.randn(16000) * 3).astype(np.int16)
_, ninfo = asr_better.preprocess(noise)
ck_true("noise gain capped", ninfo["gain"] <= 12.0 + 1e-6, "gain=%.2f" % ninfo["gain"])
ck_true("noise still noise", asr_better.is_noise(noise), "rms=%.5f" % ninfo["rms"])
# 正常音量不应判为噪声
normal = (np.random.randn(16000) * 8000).astype(np.int16)
ck_true("normal voice not noise", not asr_better.is_noise(normal), "")

print("=" * 64)
print("2) language policy")
cfg = {"asr_language": "auto", "asr_short_force_zh": True, "asr_short_sec": 2.5}
ck("short(1s) -> zh", asr_better.pick_language(np.zeros(16000, np.int16), cfg), "zh")
ck("long(5s) -> auto", asr_better.pick_language(np.zeros(80000, np.int16), cfg), "auto")
ck("forced zh", asr_better.pick_language(np.zeros(80000, np.int16),
                                        {"asr_language": "zh"}), "zh")

print("=" * 64)
print("3) text post-process")
corr = {"commfy ui": "comfyui", "comfy ui": "comfyui", "服务站": "服务栈"}
ck("longest-key-first", asr_better.apply_correction("commfy ui", corr), "comfyui")
ck("dict hit", asr_better.apply_correction("打开服务站", corr), "打开服务栈")
ck("filler head", asr_better.strip_fillers("嗯，打开记事本"), "，打开记事本")
ck("filler repeat", asr_better.strip_fillers("嗯嗯嗯 打开"), "打开")
ck_true("keep mid-sentence filler",
        asr_better.strip_fillers("好啊") == "好啊", "好啊 kept")
ck("cjk-latin space", asr_better.normalize_spacing("打开comfyui"), "打开 comfyui")
ck("latin-cjk space", asr_better.normalize_spacing("用LLaMA跑"), "用 LLaMA 跑")

print("=" * 64)
print("4) hotkey parse")
ck("vk ctrl", dictation.vk_of("ctrl"), 0x11)
ck("vk space", dictation.vk_of("space"), 0x20)
ck("vk f8", dictation.vk_of("f8"), 0x77)
ck("parse combo", dictation.parse_hotkey("ctrl+alt+space"), [0x11, 0x12, 0x20])
ck("parse bracket", dictation.parse_hotkey("<ctrl>+<alt>+<space>"), [0x11, 0x12, 0x20])
ck("parse single", dictation.parse_hotkey("ctrl_r"), [0xA3])
ck("parse bad", dictation.parse_hotkey("nonexistent"), [])

print("=" * 64)
print("5) polish guards")
ck("clean markdown", asr_polish._clean("```\n你好。\n```"), "你好。")
ck("clean prefix", asr_polish._clean("校对后：你好。"), "你好。")
ck("clean quotes", asr_polish._clean("\"你好。\""), "你好。")
ck_true("similarity high", asr_polish._similarity("打开命令符", "打开命令行") > 0.75, "")
ck_true("similarity low", asr_polish._similarity("打开", "我建议你使用系统工具") < 0.75, "")
# 相似度闸门: 自由发挥应被拒
t, note = asr_polish.polish.__wrapped__ if hasattr(asr_polish.polish, "__wrapped__") else (None, None)
print("   (polish() 网络调用在离线自检里跳过)")

print("=" * 64)
print("PASS %d / FAIL %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
