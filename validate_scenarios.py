#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_scenarios.py — voice-assistant 三场景可自动化验证（2026-08-30）

能自动验证的自动验证；物理上必须人在场（麦克风/看屏幕）的项给出验证指引。
用法:
  python validate_scenarios.py            # 跑全部自动项
  python validate_scenarios.py --speak    # 额外用音箱真的播报 TTS（验证音量）
场景对应交接:
  1 多轮对话+TTS      → LLM(1234) 真调 + SAPI 合成
  2 技能学习→执行重放 → skills schema 校验 + 技能列表（执行重放需真人看屏，默认不点鼠标）
  3 server 模式秒开   → funasr-server 可用性 + 配置切换检查
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

RESULTS = []


def record(name, ok, detail):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL' if ok is False else 'SKIP'}] {name} — {detail}")


def section(title):
    print(f"\n=== {title} ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speak", action="store_true", help="真的用音箱播放 TTS")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    # ---------- 场景 1: 多轮对话 + TTS ----------
    section("场景1 多轮对话 + TTS")
    try:
        from dialogue import Dialogue  # noqa
        d = Dialogue(cfg.get("llm_base", "http://localhost:9292/v1"),
                     preferred=cfg.get("model") or None,
                     tts_enabled=False)  # 先不发声，只验证对话
        r1 = d.ask("用一句话介绍你自己")
        record("对话·第一轮", bool(r1), f"LLM 回复: {r1[:50]}...")
        r2 = d.ask("我上一句问你什么？")
        multi = any(k in r2 for k in ("介绍", "自己", "上"))  # 能否引用上文
        record("对话·第二轮(带记忆)", multi, f"回复: {r2[:50]}...")
    except Exception as exc:
        record("对话链路", False, f"失败: {exc}（检查 1234 是否在线）")

    try:
        import win32com.client  # noqa
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        # 合成到文件（可验证产物，不吵人）
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        out = ROOT / "tts_test.wav"
        import pythoncom
        stream.Format.Type = 39  # SAFT44kHz16BitStereo
        stream.Open(str(out), 3, False)  # SSFCreateForWrite
        old = speaker.AudioOutputStream
        speaker.AudioOutputStream = stream
        speaker.Speak("语音助手三场景验证，对话与朗读功能正常")
        speaker.AudioOutputStream = old
        stream.Close()
        ok = out.exists() and out.stat().st_size > 10000
        record("TTS·SAPI 合成", ok, f"{out.name} {out.stat().st_size if out.exists() else 0} 字节")
        if ok and args.speak:
            speaker.Speak("语音助手三场景验证，对话与朗读功能正常")
            record("TTS·音箱播放", None, "已播放，请确认听到")
    except Exception as exc:
        record("TTS·SAPI", False, f"失败: {exc}")

    # ---------- 场景 2: 技能系统 ----------
    section("场景2 技能学习→执行重放")
    try:
        import skills
        allsk = skills.list_skills()
        record("技能列表", True, f"现有 {len(allsk)} 个技能: {[s if isinstance(s, str) else s.get('name') for s in allsk][:5]}")
        if allsk:
            first = allsk[0] if isinstance(allsk[0], str) else allsk[0].get("name")
            sk = skills.load_skill(first)
            steps = sk.get("steps", []) if isinstance(sk, dict) else []
            record("技能加载+schema", bool(steps), f"{first}: {len(steps)} 步 (intent 格式)")
            record("技能执行重放", None,
                   "需真人看屏——运行 run_assistant.bat 说「执行技能 " + str(first) + "」验证（脚本不代点鼠标）")
        else:
            record("技能加载", None, "无技能，先跑「学习技能 X」")
    except Exception as exc:
        record("技能系统", False, f"失败: {exc}")

    # ---------- 场景 3: server 模式秒开 ----------
    section("场景3 server 模式秒开")
    mode = cfg.get("asr_mode")
    record("当前 ASR 模式", True, f"asr_mode={mode}（server 指向 {cfg.get('asr_server')}）")
    funasr = _which("funasr-server")
    record("funasr-server 可用性", bool(funasr),
           f"{'找到: ' + funasr if funasr else '未找到——秒开模式需先 pip install funasr 命令行或启动 funasr-server --device cuda'}")
    if mode == "server":
        try:
            req = urllib.request.Request(cfg["asr_server"].replace("/v1", "") + "/v1/models", method="GET")
            urllib.request.urlopen(req, timeout=3)
            record("ASR server 在线", True, cfg["asr_server"])
        except Exception as exc:
            record("ASR server 在线", False, f"{exc}")

    # ---------- 物理必检项 ----------
    section("需真人在场的验证（脚本无法替代）")
    print("  1. 麦克风说「今天天气怎么样」→ 验证识别率+多轮+朗读全链路")
    print("  2. 对软件界面说「学习技能 X」→「执行技能 X」→ 验证 VL(1235) 训练+重放")
    print("  3. server 模式下重启助手 → 验证秒开（需先启动 funasr-server --device cuda）")

    ok_n = sum(1 for _, v, _ in RESULTS if v is True)
    fail_n = sum(1 for _, v, _ in RESULTS if v is False)
    skip_n = sum(1 for _, v, _ in RESULTS if v is None)
    print(f"\n[汇总] PASS {ok_n} / FAIL {fail_n} / 待人工 {skip_n}")
    return 0 if fail_n == 0 else 1


def _which(cmd):
    import shutil
    return shutil.which(cmd)


if __name__ == "__main__":
    sys.exit(main())
