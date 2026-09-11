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

# ---- 统一网关（llama-swap，E:\AI\llama-swap\config.yaml）----
# 2026-09-11：llama-swap 已成为本机模型调度的**唯一正解**——按请求的 model 字段
# 自动加载，空闲按 ttl 卸载，8G 卡上自动「停旧起新」。所以这里不再自己管端口和显存，
# 而是优先走网关：
#     定位模型  uitars（ttl 120s）
#     语义模型  vl8b  （ttl 300s，别名 vl / vision）
# 走网关时**必须**在 payload 里带 model 字段；直连 llama-server（:1235/:1237）则不需要。
GATEWAY_BASE = "http://127.0.0.1:9292/v1"
GATEWAY_MODELS = {"uitars": "uitars", "vl": "vl8b"}

# 直连端口（网关没起时的兜底；也是历史实测精度数据的来源）
DIRECT_BASE = {"uitars": "http://localhost:1237/v1", "vl": "http://localhost:1235/v1"}


def _session():
    """本机回环必须直连。

    ⚠️ 本机装了代理（http_proxy=127.0.0.1:7971）。开着它会:
      1) 把发往 127.0.0.1:1235/1237 的请求也走代理转发;
      2) 目标没起时代理返回 HTTP 502 —— requests 不抛异常,
         于是「存活探测」会把没起的服务误判成在线。
    trust_env=False 关闭环境代理读取。
    """
    s = requests.Session()
    s.trust_env = False
    return s


def _detect_vl_model(base):
    try:
        j = _session().get(base.rstrip("/") + "/models", timeout=5).json()
        data = j.get("data") or j.get("models") or []
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return "vision-model"


def gateway_alive(timeout=3):
    """llama-swap 网关是否在线。"""
    try:
        r = _session().get(GATEWAY_BASE.rstrip("/") + "/models", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def wire_model(base, want):
    """该 base 是否需要带 model 字段；需要则返回模型名，否则 None。

    只按端口判断（网关固定 9292）—— 最省事且不会误伤直连场景。
    """
    if ":9292" in (base or ""):
        return GATEWAY_MODELS.get(want, want)
    return None


def pick_backend(want):
    """选后端：网关优先，否则回退直连端口。

    返回 (base, model|None, 说明)。网关在线时由它负责加载/卸载与显存腾挪，
    调用方完全不用关心端口和显存 —— 这就是接入网关的全部收益。
    """
    if gateway_alive():
        return (GATEWAY_BASE, GATEWAY_MODELS[want],
                "网关 :9292（model=%s）" % GATEWAY_MODELS[want])
    return DIRECT_BASE[want], None, "直连 %s" % DIRECT_BASE[want]


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
    r = _session().post(base.rstrip("/") + "/chat/completions", json=payload, timeout=60)
    return r.json()["choices"][0]["message"]["content"]


def _parse_cell(text):
    import re
    nums = re.findall(r"\d+", text or "")
    nums = [int(n) for n in nums if 1 <= int(n) <= 3]
    if len(nums) >= 2:
        return nums[0], nums[1]   # row, col (1-based)
    return None


# ---- UI-TARS 精准定位层(2026-08-31): 专用桌面 grounding 模型 @:1237, 像素级坐标 ----
# 优先于网格法: 直接问"点哪里", 返回 click(start_box='(x,y)'); 起不来/解析失败自动回退网格法
_UITARS_BASE = "http://localhost:1237/v1"
_UITARS_SYSTEM = (
    "You are a GUI agent. You are given a task and a screenshot. Output exactly one action.\n"
    "## Action Space\n"
    "click(start_box='<|box_start|>(x,y)<|box_end|>')\n"
    "type(content='')\nhotkey(key='')\nwait()\nfinished()\n"
    "Output only the action."
)


def _uitars_point(base, b64, target, W, H, model=None, timeout=180):
    """问 UI-TARS 目标坐标。返回截图内像素坐标 (x,y) 或 None。

    坐标约定自动判别: 本 llama.cpp 转换件实测输出像素坐标; 若越界则按 0-1000 归一化回缩。
    base/model: 走网关时 base=:9292/v1 且必须带 model；直连 :1237 则 model=None。
                （model 留空时按 base 自动判断，调用方通常不用管。）
    timeout: 走网关首次请求要等模型加载（uitars 实测 ~10s，大模型可到 40s），
             所以默认放宽到 180s，别用直连时代的 120s。
    """
    import re
    try:
        payload = {
            "messages": [
                {"role": "system", "content": _UITARS_SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                    {"type": "text", "text": "Click the %s" % target},
                ]},
            ],
            "temperature": 0, "max_tokens": 200,
        }
        m = model or wire_model(base, "uitars")
        if m:
            payload["model"] = m
        r = _session().post(base.rstrip("/") + "/chat/completions",
                            json=payload, timeout=timeout)
        out = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
    m2 = re.search(r"\(\s*(\d+)\s*[,，]\s*(\d+)\s*\)", out or "")
    if not m2:
        return None
    x, y = int(m2.group(1)), int(m2.group(2))
    if x > W or y > H:   # 越界 -> 是 0-1000 归一化制, 回缩成像素
        x, y = int(x / 1000.0 * W), int(y / 1000.0 * H)
    if 0 <= x < W and 0 <= y < H:
        return x, y
    return None


def _uitars_alive(base):
    """UI-TARS 可用性探测。

    ⚠️ 必须校验状态码 —— 本机代理会回 502 且不抛异常，
    不校验就会把「没启动」误判成「在线」，白白浪费一次失败请求。
    ⚠️ 走网关时没有 /health（上游端口是动态的），改用 /v1/models 探网关本身；
    此时「可用」不等于「已加载」—— 模型由网关按需拉起，这是正常状态。
    """
    try:
        if ":9292" in (base or ""):
            r = _session().get(base.rstrip("/") + "/models", timeout=5)
            return r.status_code == 200
        r = _session().get(base.rstrip("/").rsplit("/", 1)[0] + "/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def click_visual(target, region=None, vl_base=None, refine=True, uitars_base=None):
    """视觉定位并点击目标。成功返回 True, 失败 False。

    后端自动选择（2026-09-11）：**llama-swap 网关在线则优先走网关**，
    由网关负责模型加载/卸载与显存腾挪；网关没起才回退直连 :1235/:1237。

    region: 截图区域(默认活动窗口)。
    refine: 二级网格细分, 先把鼠标挪到大格中心附近, 再问一次该格内 3x3 细分。
    """
    gateway = gateway_alive()
    if gateway:
        vl_base = vl_base or GATEWAY_BASE
        uitars_base = uitars_base if uitars_base is not None else GATEWAY_BASE
        # ⚠️ 不能对网关调 _detect_vl_model —— 它返回的是列表里第一个模型
        #    （可能是 coder/moe35b），会把视觉请求打到错的模型上。
        model = GATEWAY_MODELS["vl"]
    else:
        vl_base = vl_base or DIRECT_BASE["vl"]
        uitars_base = uitars_base if uitars_base is not None else DIRECT_BASE["uitars"]
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
    # 一级: UI-TARS 像素定位(在线才试, 挂了零开销直接走网格法)
    if uitars_base and _uitars_alive(uitars_base):
        pt = _uitars_point(uitars_base, b64, target, W, H)
        if pt:
            import pyautogui
            x, y = int(off_x + pt[0]), int(off_y + pt[1])
            pyautogui.click(x, y)
            print("  UI-TARS 点击 (%d, %d): %s" % (x, y, target))
            return True
        print("  UI-TARS 未解析出坐标, 回退网格法")
    # 二级: 网格动作空间(原逻辑不变)
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
