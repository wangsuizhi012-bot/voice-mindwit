#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""uitars_smoke.py — UI-TARS :1237 加载烟测 + grounding 定位精度实测

1) 先试 -ngl 99 全 GPU（需显存空闲）；OOM 则回退 -ngl 16（部分 CPU，慢但能验证）
2) 发合成的 1280x720 设置窗口图，问「Click the red 确定 button」
3) 红色按钮真实中心=(890,528)（归一化 0-1000 制为 (695,733)），对比模型输出判定坐标约定
用法: python uitars_smoke.py            # 需先下载完模型
"""

import base64
import json
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIR = r"E:\AI\LLM\GGUF\UI-TARS"   # 2026-09-08 从 ComfyUI\models\LLM 迁出，勿改回 D 盘
MODEL = DIR + r"\UI-TARS-1.5-7B-q4_k_m.gguf"
EXE = r"E:\AI\llama.cpp-cuda\llama-server.exe"
PORT = 1237
IMG = r"E:\AI\voice-assistant\shots\uitars_ground_test.png"

import os
MMP = DIR + r"\UI-TARS-1.5-7B-q8_0.mmproj"

# 本机回环必须直连: 系统装了代理 http_proxy=127.0.0.1:7971, 否则
# 目标没起时代理会回 HTTP 502 而不是抛连接异常, 健康检查会误判「已就绪」。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def launch(ngl, ctx):
    cmd = [EXE, "-m", MODEL, "--mmproj", MMP, "--port", str(PORT),
           "-ngl", str(ngl), "--mmproj-offload", "-c", str(ctx)]
    print(f"[smoke] launch llama-server :{PORT} -ngl {ngl} -c {ctx}")
    return subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wait_health(timeout_s):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with _opener.open(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(3)
    return False


SYSTEM_PROMPT = """You are a GUI agent. You are given a task and a screenshot. Output exactly one action.

## Action Space
click(start_box='<|box_start|>(x,y)<|box_end|>')
left_double(start_box='<|box_start|>(x,y)<|box_end|>')
right_single(start_box='<|box_start|>(x,y)<|box_end|>')
type(content='')
hotkey(key='')
scroll(start_box='<|box_start|>(x,y)<|box_end|>', direction='')
wait()
finished()

Coordinates are normalized 0-1000 relative to image width/height. Output only the action."""


def ask(image_path, prompt):
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ],
        "temperature": 0, "max_tokens": 200,
    }
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _opener.open(req, timeout=600) as r:
        out = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    return out.strip(), time.time() - t0


def main():
    if not (os.path.exists(MODEL) and os.path.exists(MMP)):
        print("模型未下载完成"); return 1
    proc = launch(99, 8192)
    if not wait_health(45):
        print("[smoke] -ngl 99 失败(显存不足符合预期)，回退 -ngl 16 -c 4096")
        proc.kill(); proc.wait()
        proc = launch(16, 4096)
        if not wait_health(180):
            print("[smoke] 仍然起不来"); return 1
    try:
        out, dt = ask(IMG, "Click the red 确定 button")
        print(f"[smoke] 原始输出({dt:.1f}s): {out}")
        print("[smoke] 期望: 红按钮真实中心 (890,528) 像素 = (695,733) 归一化(0-1000)")
    finally:
        proc.kill(); proc.wait()
        print("[smoke] server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
