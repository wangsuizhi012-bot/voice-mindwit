# -*- coding: utf-8 -*-
"""运行时视觉点击: 本地显卡 + Python 低本点击方案的核心兜底。

当控件树(locate.py)取不到目标(游戏/自绘 UI/无文字控件)时, 用本机 VL(:1235)看截图,
按「网格动作空间」(见 E:/AI/knowledge/nuphus-desktop-automation)只回答目标在哪格,
坐标由纯算术得出 -> 可复现、不依赖 OCR 精度、token 极省。

流程: 截活动窗口 -> VL 选 3x3 格 -> 点该格中心(可选二级细分)。
对应需求「本地大模型调用 skill 完成点击」「利用显卡 + Python 低本点击」。
"""
import os
import json
import base64
import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def _detect_vl_model(base):
    try:
        j = requests.get(base.rstrip("/") + "/models", timeout=5).json()
        data = j.get("data") or j.get("models") or []
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return "vision-model"


def _screenshot_b64(region=None):
    import pyautogui
    from io import BytesIO
    from PIL import Image
    img = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    buf = BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size


_CELL_PROMPT = (
    "这是一张软件界面截图。目标元素是：{target}\n"
    "请把这张图看成 3x3 网格(3 行 3 列, 左上角为第1行第1列)。"
    "只回答目标元素最可能在哪一格, 格式严格为: 第i行第j列 (i,j 均为 1-3 的整数)。"
    "不要任何解释, 只回这一句。"
)


def _ask_cell(base, model, b64, target):
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": _CELL_PROMPT.format(target=target)},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ]}
        ],
        "temperature": 0.0,
        "max_tokens": 40,
    }
    r = requests.post(base.rstrip("/") + "/chat/completions", json=payload, timeout=60)
    return r.json()["choices"][0]["message"]["content"]


def _parse_cell(text):
    import re
    nums = re.findall(r"\d+", text or "")
    nums = [int(n) for n in nums if 1 <= int(n) <= 3]
    if len(nums) >= 2:
        return nums[0], nums[1]   # row, col (1-based)
    return None


def click_visual(target, region=None, vl_base=None, refine=True):
    """视觉定位并点击目标。成功返回 True, 失败 False。

    region: 截图区域(默认活动窗口); vl_base: VL 接口(默认 :1235)。
    refine: 二级网格细分, 先把鼠标挪到大格中心附近, 再问一次该格内 3x3 细分。
    """
    vl_base = vl_base or "http://localhost:1235/v1"
    model = _detect_vl_model(vl_base)
    try:
        b64, (W, H) = _screenshot_b64(region)
    except Exception as e:
        print("  视觉截图失败: " + repr(e))
        return False
    # 全屏截图时 W/H 是整屏; 活动窗口 region 时 img.size 即窗口尺寸, 但点击坐标需加窗口偏移
    off_x, off_y = 0, 0
    if region and len(region) == 4:
        off_x, off_y = region[0], region[1]
    try:
        cell = _parse_cell(_ask_cell(vl_base, model, b64, target))
    except Exception as e:
        print("  视觉选格失败: " + repr(e))
        return False
    if not cell:
        return False
    r, c = cell
    # 大格中心(相对于截图左上角)
    cw, ch = W / 3.0, H / 3.0
    cx = (c - 0.5) * cw
    cy = (r - 0.5) * ch
    if refine:
        # 二级: 只截该大格区域再细分一次, 提升精度
        try:
            import pyautogui
            sub = (int(off_x + cx - cw / 2), int(off_y + cy - ch / 2),
                   int(cw), int(ch))
            sub = (max(0, sub[0]), max(0, sub[1]), sub[2], sub[3])
            sb64, (sW, sH) = _screenshot_b64(sub)
            scell = _parse_cell(_ask_cell(vl_base, model, sb64, target))
            if scell:
                sr, sc = scell
                cx = (c - 1) * cw + (sc - 0.5) * (cw / 3.0)
                cy = (r - 1) * ch + (sr - 0.5) * (ch / 3.0)
        except Exception:
            pass
    import pyautogui
    x = int(off_x + cx)
    y = int(off_y + cy)
    pyautogui.click(x, y)
    print("  视觉点击 (%d, %d): %s" % (x, y, target))
    return True
