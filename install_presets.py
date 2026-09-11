#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_presets.py — voice-assistant 预置技能包安装器（2026-08-30）

借鉴 harvis 等 Windows 语音助手项目的「开箱即用 skills 包」思路，
用与宏录制/技能训练完全同构的 intent steps 格式（见 skills.py）预置常用脚本。
- 不覆盖用户已创建的同名技能（source=preset 的旧版会被刷新）
- 安装后自动做触发词匹配自测
用法: python install_presets.py            # 安装 + 自测
      python install_presets.py --list     # 只列出预置包内容
"""

import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import skills  # noqa: E402

# 与 voice_assistant.py 的 ALLOWED_ACTIONS 保持一致（安装前自检用）
ALLOWED = {"screenshot", "screenshot_send", "type", "open",
           "click", "click_here", "click_target", "click_visual", "press", "scroll", "none"}

# ---- 预置技能包（常用 + 低风险 + 触发词口语化）----
PRESETS = [
    ("音量加大", ["音量加大", "声音大点", "音量大点", "调大音量"],
     [{"action": "press", "params": {"key": "volumeup"}}],
     "按一次音量+；想连续调就说几遍或说「执行技能音量加大」"),
    ("音量减小", ["音量减小", "声音小点", "音量小点", "调小音量"],
     [{"action": "press", "params": {"key": "volumedown"}}],
     "按一次音量-"),
    ("静音", ["静音", "取消声音", "闭麦声音"],
     [{"action": "press", "params": {"key": "volumemute"}}],
     "切换静音/恢复（系统同一键）"),
    ("显示桌面", ["显示桌面", "回到桌面", "最小化全部"],
     [{"action": "press", "params": {"keys": ["winleft", "d"]}}],
     "Win+D 显示桌面"),
    ("锁屏", ["锁屏", "锁电脑", "锁定屏幕"],
     [{"action": "press", "params": {"keys": ["winleft", "l"]}}],
     "Win+L 锁屏（执行后需密码解锁，慎说）"),
    ("打开微信", ["打开微信", "我要微信", "启动微信"],
     [{"action": "open", "params": {"app": "微信"}}],
     "经系统 ShellExecute 解析启动"),
    ("打开B站", ["打开B站", "打开哔哩哔哩", "我要看B站", "打开b站"],
     [{"action": "open", "params": {"app": "哔哩哔哩"}}],
     "B站桌面客户端"),
    ("打开知识库", ["打开知识库", "打开obsidian", "打开 Obsidian", "打开笔记"],
     [{"action": "open", "params": {"app": "obsidian"}}],
     "Obsidian（知识库客户端）"),
    ("打开任务管理器", ["打开任务管理器", "任务管理器", "看看进程"],
     [{"action": "open", "params": {"app": "taskmgr"}}],
     "系统任务管理器"),
    ("截屏", ["截屏", "截图", "截个图", "屏幕截图"],
     [{"action": "screenshot", "params": {}}],
     "截当前活动窗口到剪贴板（粘贴即可用）"),
    ("速记", ["速记", "记一笔", "打开记事本"],
     [{"action": "open", "params": {"app": "notepad"}}],
     "开记事本，配合「输入 xxx」可直接打字"),
    # ---- 服务启动（2026-08-30 二批：参考 HA 语音助手/Sara 的 scripts-launcher 模式）----
    ("启动本地大模型", ["启动本地大模型", "启动推理", "启动llama", "开1234"],
     [{"action": "open", "params": {"app": "本地大模型"}}],
     "Qwen2.5-7B @1234；显存占用高，与视觉模型/ComfyUI 勿同时开（8G 铁律）"),
    ("启动视觉模型", ["启动视觉模型", "启动vl", "开1235"],
     [{"action": "open", "params": {"app": "视觉模型"}}],
     "Qwen3-VL @1235（已带 --mmproj-offload）；生图前先关闭它"),
    ("启动服务栈", ["启动服务栈", "启动服务站", "启动工作站", "服务站", "工作站", "启动n8n", "开n8n"],
     [{"action": "open", "params": {"app": "服务栈"}}],
     "n8n + local-ai 组合（start-stack.bat），n8n 界面 :5678；「服务站/工作站」为 ASR 同音误识触发词"),
    ("启动智能中枢", ["启动智能中枢", "启动中枢", "启动agenthub"],
     [{"action": "open", "params": {"app": "智能中枢"}}],
     "agent-hub 全栈自动拉起（含掉线自愈）"),
    ("启动DSH", ["启动dsh", "打开DSH", "启动桌宠"],
     [{"action": "open", "params": {"app": "DSH"}}],
     "DeepSeek Harness @:3080"),
    ("服务状态", ["服务状态", "服务都活着吗", "检查服务"],
     [{"action": "open", "params": {"app": "终端"}},
      {"action": "type", "params": {"text": "python E:\\AI\\agent-hub\\agent_ctl.py status"}},
      {"action": "press", "params": {"key": "enter"}}],
     "新开终端跑 agent_ctl status；若粘贴没进输入框，点一下窗口再说一遍"),
    ("环境体检", ["环境体检", "体检"],
     [{"action": "open", "params": {"app": "终端"}},
      {"action": "type", "params": {"text": "python E:\\AI\\agent-hub\\scripts\\self_check.py"}},
      {"action": "press", "params": {"key": "enter"}}],
     "端口/磁盘/显存/venv 一键体检"),
    ("启动桌面识别", ["启动桌面识别", "启动uitars", "桌面识别"],
     [{"action": "open", "params": {"app": "桌面识别"}}],
     "UI-TARS-1.5-7B @:1237 桌面 GUI 精准定位（占显存 ~5.3G，与 1234/1235/ComfyUI 勿同开）；起来后「学习技能/视觉点击」自动用它"),
    # ---- AI 绘画 ----
    ("启动ComfyUI", ["ai绘画", "ai画图", "ai画画", "启动comfyui", "打开comfyui", "绘画", "画画"],
     [{"action": "open", "params": {"app": "ComfyUI"}}],
     "Comfy Desktop（D 盘主实例）；**生图前先关 1234/1235**（8G 显存铁律）；主口令=AI绘画（ComfyUI 英文易误识，已配纠错词典）"),
    ("打开ComfyUI界面", ["打开comfyui界面", "comfyui界面", "绘画界面"],
     [{"action": "open", "params": {"app": "http://127.0.0.1:8188"}}],
     "浏览器打开 :8188（需 ComfyUI 已启动）"),
    ("打开图库", ["打开图库", "我的图库", "看图库"],
     [{"action": "open", "params": {"app": "http://127.0.0.1:8765"}}],
     "booru-tagger Web 图墙 :8765"),
    ("打开n8n界面", ["打开n8n界面", "n8n工作流", "工作流界面"],
     [{"action": "open", "params": {"app": "http://127.0.0.1:5678"}}],
     "n8n 编排界面 :5678（需服务栈已启动）"),
    ("打开DSH界面", ["打开dsh界面", "dsh聊天"],
     [{"action": "open", "params": {"app": "http://127.0.0.1:3080"}}],
     "DeepSeek Harness 网页 :3080"),
    ("打开提示词库", ["打开提示词库", "提示词库"],
     [{"action": "open", "params": {"app": r"E:\AI\prompt-kb"}}],
     "资源管理器打开 E:\\AI\\prompt-kb"),
    ("打开模型仓库", ["打开模型仓库", "模型仓库"],
     [{"action": "open", "params": {"app": r"E:\AI\comfyui"}}],
     "资源管理器打开 E:\\AI\\comfyui"),
]


def install(force=False):
    installed, skipped = [], []
    for name, triggers, steps, note in PRESETS:
        # 动作白名单自检
        for s in steps:
            assert s.get("action") in ALLOWED, f"非法动作: {s}"
        existed = skills.load_skill(name)
        if existed and existed.get("source") == "manual" and not force:
            skipped.append(name)   # 用户手建的同名技能不动
            continue
        p = skills.save_skill(name, steps, triggers=triggers, source="preset",
                              note="预置脚本 2026-08-30；" + note)
        installed.append((name, p))
    return installed, skipped


def selftest():
    ok, fail = 0, 0
    for name, triggers, steps, _ in PRESETS:
        hit = skills.match_skill(triggers[0])
        if hit and hit.get("name") == name:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {triggers[0]} 未命中 {name}")
        hit2 = skills.match_skill("执行技能" + name)
        if not (hit2 and hit2.get("name") == name):
            fail += 1
            print(f"  [FAIL] 执行技能{name} 未命中")
        else:
            ok += 1
    return ok, fail


def main():
    if "--list" in sys.argv:
        for name, triggers, steps, note in PRESETS:
            acts = " → ".join(s["action"] for s in steps)
            print(f"  {name}: {acts}  ({'、'.join(triggers[:3])})")
        return 0
    installed, skipped = install()
    print(f"[install] 新装/更新 {len(installed)} 个: {sorted(n for n, _ in installed)}")
    if skipped:
        print(f"[install] 保留用户手建同名技能 {len(skipped)} 个: {skipped}")
    ok, fail = selftest()
    print(f"[selftest] 触发词命中 {ok} / 失败 {fail}")
    print(f"[info] 当前技能总数: {len(skills.list_skills())}")
    print("[usage] 说「音量加大」「打开微信」等短口令直接触发（需确认），或「执行技能 名字」")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
