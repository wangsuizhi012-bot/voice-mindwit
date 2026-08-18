# -*- coding: utf-8 -*-
"""技能训练: 截图 -> 视觉大模型(本地 VL :1235 或云端 LLM) -> 解析成步骤 -> 存为技能。

对应需求「截图上传给大模型, 然后训练成 skill」。
- 本地优先: 调用本机已运行的 Qwen3-VL(:1235) 看截图, 推断在该界面上完成任务的点击/输入步骤。
- 云端可选: config.trainer 配了 base/key 则用云端多模态模型(更强, 但走网络)。
- 兜底: 若视觉模型不可用, train_skill 返回 None, 由调用方降级为「语音录制技能」(手动演示)。

步骤格式与 macro 同构(intent JSON), 便于 _execute_flow 直接重放。
"""
import os
import json
import base64
import threading
import requests

HERE = os.path.dirname(os.path.abspath(__file__))

_REGION_MAP = {
    "tl": lambda w, h: (0, 0, w // 2, h // 2),
    "tr": lambda w, h: (w // 2, 0, w // 2, h // 2),
    "bl": lambda w, h: (0, h // 2, w // 2, h // 2),
    "br": lambda w, h: (w // 2, h // 2, w // 2, h // 2),
    "left": lambda w, h: (0, 0, w // 2, h),
    "right": lambda w, h: (w // 2, 0, w // 2, h),
    "top": lambda w, h: (0, 0, w, h // 2),
    "bottom": lambda w, h: (0, h // 2, w, h // 2),
}


def _resolve_region(region):
    """region: None(活动窗口) / 'full' / tl/tr/bl/br/left/right/top/bottom / [x,y,w,h]。"""
    import pyautogui
    if region is None:
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            l, t, rr, b = win32gui.GetWindowRect(hwnd)
            return (l, t, rr - l, b - t)
        except Exception:
            return None
    if isinstance(region, (list, tuple)) and len(region) == 4:
        try:
            return tuple(int(v) for v in region)
        except Exception:
            return None
    if isinstance(region, str):
        key = region.strip().lower()
        if key in ("full", "全屏"):
            return None
        if key in _REGION_MAP:
            w, h = pyautogui.size()
            return _REGION_MAP[key](w, h)
    return None


def _b64_screenshot(region):
    import pyautogui
    from io import BytesIO
    from PIL import Image
    img = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    buf = BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _detect_vl_model(base):
    try:
        j = requests.get(base.rstrip("/") + "/models", timeout=5).json()
        data = j.get("data") or j.get("models") or []
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return "vision-model"


_PROMPT = (
    "你是一个桌面自动化专家。下面是一张软件界面的截图。用户想让电脑自动完成这个任务：{task}\n"
    "请只依据截图里实际可见的按钮/输入框文字，输出完成该任务需要的操作步骤，每行一个动作，"
    "严格用以下前缀之一，不要编号、不要解释：\n"
    "click_target: <控件上的文字>      （点界面上带该文字的按钮/输入框）\n"
    "click_visual: <目标描述>          （界面上看得到但文字识别不到的目标，用视觉点）\n"
    "type: <要输入的文字>              （在光标处输入）\n"
    "press: <热键>                    （如 enter / ctrl+s / ctrl+v / f5）\n"
    "open: <程序名>                   （打开程序）\n"
    "scroll: <+3 或 -3>               （向上/向下滚）\n"
    "如果某步需要先打开程序再操作，请先写 open。只输出动作行。"
)


def _parse_steps(text):
    steps = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        head, _, body = line.partition(":")
        head = head.strip().lower()
        body = body.strip()
        if head in ("click_target", "点", "点击"):
            if body:
                steps.append({"action": "click_target", "params": {"target": body}})
        elif head in ("click_visual", "视觉点", "看图点"):
            if body:
                steps.append({"action": "click_visual", "params": {"target": body}})
        elif head in ("type", "打字", "输入"):
            if body:
                steps.append({"action": "type", "params": {"text": body}})
        elif head in ("press", "按键", "热键"):
            if body:
                ks = [k.strip().lower() for k in body.replace("+", " ").split() if k.strip()]
                if len(ks) >= 2:
                    steps.append({"action": "press", "params": {"keys": ks}})
                elif ks:
                    steps.append({"action": "press", "params": {"key": ks[0]}})
        elif head in ("open", "打开"):
            if body:
                steps.append({"action": "open", "params": {"app": body}})
        elif head in ("scroll", "滚动", "滚"):
            try:
                amt = int(body)
                steps.append({"action": "scroll", "params": {"amount": amt}})
            except Exception:
                pass
    return steps


def _call_vision(base, api_key, model, b64, task):
    """调用多模态接口, 返回模型文本。api_key 为空表示本地无鉴权。"""
    img_url = "data:image/png;base64," + b64
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": _PROMPT.format(task=task)},
                {"type": "image_url", "image_url": {"url": img_url}},
            ]}
        ],
        "temperature": 0.2,
        "max_tokens": 600,
    }
    headers = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    r = requests.post(base.rstrip("/") + "/chat/completions",
                      json=payload, headers=headers, timeout=60)
    return r.json()["choices"][0]["message"]["content"]


def train_skill(name, task=None, region=None, cfg=None):
    """截图 + 视觉模型 -> 步骤。成功返回步骤列表, 失败返回 None。

    name : 技能名(也作为默认任务描述)
    task : 任务描述(可选, 默认用 name)
    region: 截图区域(默认活动窗口)
    cfg  : 已加载的 config 字典
    """
    cfg = cfg or {}
    task = (task or name or "").strip() or name
    try:
        b64 = _b64_screenshot(_resolve_region(region))
    except Exception as e:
        print("  截图失败: " + repr(e))
        return None
    # 云端优先(若配置), 否则本地 VL
    trainer = cfg.get("trainer") or {}
    base = trainer.get("base") or cfg.get("vl_base") or "http://localhost:1235/v1"
    key = trainer.get("key") or ""
    model = trainer.get("model") or _detect_vl_model(base)
    try:
        text = _call_vision(base, key, model, b64, task)
    except Exception as e:
        print("  视觉模型调用失败(将降级为手动录制): " + repr(e))
        return None
    steps = _parse_steps(text)
    return steps if steps else None
