# -*- coding: utf-8 -*-
"""som.py —— Set-of-Mark 结构化感知层（对标 Microsoft OmniParser v2）

## 为什么要这个东西（2026-09-11 与主流对比后的结论）

自建链路此前已经能给出「文字 + 图标框」，但输出**仍然是像素坐标**：
    locate() -> (x, y) -> pyautogui.click(x, y)
这条路的脆弱点是显式的：坐标回归一旦偏 30px 就点到隔壁，而视觉模型的
坐标能力恰恰是最不可靠的一环（自建实测 VL 偏 ~100px、UI-TARS 8~49px）。

OmniParser 的做法（github.com/microsoft/OmniParser，24k★）是把屏幕
**token 化成一个编号列表**，让模型做「选择题」而不是「回归题」：

    截图 ──> YOLO 检框 ──> Florence-2 读图标功能 ──> [1] "搜索框" @(712,40)
                                                       [2] "设置齿轮" @(1180,40)
          └──> Set-of-Mark 叠加图（每个可交互元素打上编号）

    模型只回一句 "click element 7" —— 不需要视觉能力，也不需要算坐标。

它的关键设计洞见有三条，本模块逐条对照实现：
  1. **感知与推理解耦**：感知层廉价、可替换、可单测；推理层不必看 JPEG。
  2. **图标要有功能语义**：「齿轮」= 设置、「软盘」= 保存。只给框不给语义，
     模型会把「更多选项（三点）」当「关闭」。这就是 icon captioning 存在的理由。
  3. **编号比坐标稳**：选错编号的代价是「选错元素」，选错坐标的代价是
     「点到空白/别的东西」，后者更难发现也更难恢复。

## 与 OmniParser 的差异（刻意为之，不是遗漏）

| 维度 | OmniParser v2 | 本模块 |
|---|---|---|
| 检框 | 微调 YOLOv8（AGPL） | 复用 nuphus 内置 YOLO + PaddleOCR（已在跑，零显存） |
| 图标语义 | 微调 Florence-2-base | **复用已有 Qwen3-VL 网关**，N 个图标拼一张 contact sheet **一次请求**描述完 |
| 依赖 | torch + transformers + 权重下载 | 零新依赖，零新权重 |
| 显存 | Florence-2 常驻 | 无（网关按需加载，ttl 自动卸载） |

也就是说：**不引入 OmniParser 的权重，而用「已有部件重排」拿到它的输出形态**。
8G 显存机器上这比再挂一个 Florence-2 务实得多。若日后要更准，
只需把 `caption_icons()` 换成真 OmniParser 的 caption 模型，接口不变。

## 用法

    import som
    s = som.build()                     # 截全屏 -> 编号元素表 + SoM 图
    print(s["list_text"])               # 给模型看的文本列表
    print(s["som_image"])               # 给模型/人看的编号叠加图
    pt = som.pick(s, 7)                 # 用编号换坐标（喂给 pyautogui）
    pt = som.find(s, "自动化")           # 按文字找坐标

    python som.py                       # 自检（只读，不点击）
    python som.py "自动化"               # 查一个目标在第几号
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SHOTS = os.path.join(HERE, "shots")
SOM_IMG = os.path.join(SHOTS, "_som.png")
PERCEIVE_IMG = os.path.join(SHOTS, "_perceive.png")

# 图标 caption 的规模上限 —— 拼图不能无限大，否则 VL 看不清小图标
MAX_CAPTION_ICONS = 24
# 每个图标在拼图里的格子边长（像素）。24px 的图标放大到 96px 才读得出图案
CELL = 96


# ------------------------------------------------------------------ 工具
def _font(size=14):
    """尽量拿一个能画中文的字体，拿不到就退回 PIL 默认（英文可用）。"""
    try:
        from PIL import ImageFont
    except Exception:
        return None
    for name in ("msyh.ttc", "simhei.ttf", "segoeui.ttf", "arial.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _vl_ask(img, question, timeout=180):
    """问一次本地 VL（网关优先）。失败返回 'err:...' 字符串，不抛。"""
    import base64
    import io
    try:
        import visual_click as vc
        base, model, _ = vc.pick_backend("vl")
    except Exception as e:
        return "err: 选后端失败 %r" % (e,)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
        "temperature": 0,
        "max_tokens": 400,
    }
    if model:
        payload["model"] = model
    try:
        s = vc._session()
        r = s.post(base.rstrip("/") + "/chat/completions", json=payload,
                   timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return "err:" + repr(e)


# ------------------------------------------------------------------ 感知
def grab(to_path=PERCEIVE_IMG):
    """截全屏并落盘（nuphus perceive 需要文件路径）。返回 (PIL.Image, path)。"""
    import pyautogui
    os.makedirs(SHOTS, exist_ok=True)
    img = pyautogui.screenshot()
    img.save(to_path)
    return img, to_path


def perceive(img_path=None, rect=None):
    """调 nuphus perceive 拿原始元素（复用 nuphus_bridge，零显存）。

    rect=(x,y,w,h) 时只保留中心落在其中的元素 —— 把感知收敛到目标窗口，
    避免别的窗口同名元素串味。
    返回 {"texts":[...], "icons":[...]}（结构与 nuphus_bridge 一致）。
    """
    import nuphus_bridge as nb
    p = img_path or PERCEIVE_IMG
    els = nb.perceive(p)
    if rect:
        x, y, w, h = rect

        def inside(e):
            return x <= e.get("cx", 0) < x + w and y <= e.get("cy", 0) < y + h
        els = {"texts": [e for e in els["texts"] if inside(e)],
               "icons": [e for e in els["icons"] if inside(e)],
               "raw": els.get("raw"),
               "ocr_count": els.get("ocr_count"),
               "yolo_count": els.get("yolo_count")}
    return els


def _merge(elements):
    """把 texts/icons 合成一个列表，按阅读顺序（先上后下、再左到右）排序。

    OmniParser 也是排序后编号的 —— 编号顺序稳定，模型跨帧引用同一个编号
    才有意义（"click element 7" 在两帧里必须是同一个东西）。
    """
    rows = []
    for e in elements.get("texts", []):
        rows.append({"kind": "text", "label": (e.get("text") or "").strip(),
                     "cx": e.get("cx", 0), "cy": e.get("cy", 0),
                     "x": e.get("x", 0), "y": e.get("y", 0),
                     "w": e.get("w", 0), "h": e.get("h", 0),
                     "conf": e.get("conf"), "source": e.get("source") or "ocr"})
    for e in elements.get("icons", []):
        rows.append({"kind": "icon", "label": "",  # 图标没有文字，等 caption 填
                     "cx": e.get("cx", 0), "cy": e.get("cy", 0),
                     "x": e.get("x", 0), "y": e.get("y", 0),
                     "w": e.get("w", 0), "h": e.get("h", 0),
                     "conf": e.get("conf"), "source": e.get("source") or "yolo"})
    # 行聚类：y 差小于半个行高视为同一行，行内按 x 排
    rows.sort(key=lambda r: (r["cy"], r["cx"]))
    ordered, line, last_y = [], [], None
    for r in rows:
        tol = max(10, int((r["h"] or 14) * 0.6))
        if last_y is None or abs(r["cy"] - last_y) <= tol:
            line.append(r)
        else:
            ordered += sorted(line, key=lambda z: z["cx"])
            line = [r]
        last_y = r["cy"]
    ordered += sorted(line, key=lambda z: z["cx"])
    return ordered


# ------------------------------------------------------------------ 图标功能标注
def _contact_sheet(img, icons, cell=CELL):
    """把若干图标 crop 拼成一张网格图。

    为什么拼图而不是逐个问：8G 卡上每次 VL 请求都有排队/加载开销，
    N 个图标逐个问 = N 次请求。拼一张图**一次请求**描述完，是这里的关键技巧。
    返回 (拼图, [对应元素下标])
    """
    from PIL import Image
    if not icons:
        return None, []
    icons = icons[:MAX_CAPTION_ICONS]
    cols = 6
    rows = (len(icons) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (255, 255, 255))
    from PIL import ImageDraw
    d = ImageDraw.Draw(sheet)
    f = _font(13)
    for i, e in enumerate(icons):
        x, y, w, h = int(e["x"]), int(e["y"]), int(e["w"]), int(e["h"])
        pad = 2
        box = (max(0, x - pad), max(0, y - pad),
               min(img.width, x + w + pad), min(img.height, y + h + pad))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            continue
        crop = img.crop(box).convert("RGB")
        # 等比放大到格子内，保持长宽比（拉伸会毁掉图标形状特征）
        scale = min((cell - 24) / max(1, crop.width), (cell - 24) / max(1, crop.height))
        nw, nh = max(8, int(crop.width * scale)), max(8, int(crop.height * scale))
        crop = crop.resize((nw, nh))
        cx, cy = (i % cols) * cell, (i // cols) * cell
        sheet.paste(crop, (cx + (cell - nw) // 2, cy + (cell - nh) // 2))
        d.rectangle([cx + 1, cy + 1, cx + cell - 2, cy + cell - 2],
                    outline=(200, 200, 200))
        d.text((cx + 4, cy + 2), str(i + 1), fill=(200, 0, 0), font=f)
    # 末尾补白格，避免最后一行不满时 VL 数错
    return sheet, list(range(len(icons)))


def caption_icons(img, icons, timeout=240):
    """给无文字图标补功能描述。返回 [desc 或 ""]，与 icons 等长。

    失败（VL 不可用）返回全空串 —— 不抛异常，SoM 退化成「只有编号」仍可用。
    """
    if not icons:
        return []
    sheet, idx = _contact_sheet(img, icons)
    if sheet is None:
        return [""] * len(icons)
    q = ("这是一张由若干 UI 图标拼成的网格图，每个格子左上角有红色编号。"
         "请按编号顺序，为每个图标给出它的**功能名称**（2-6 个汉字，"
         "如「关闭」「设置」「保存」「搜索」「返回」「更多选项」）。"
         "只输出每行一个，格式严格为「编号: 名称」，不要任何解释、不要编号范围之外的格子。")
    ans = _vl_ask(sheet, q, timeout=timeout)
    if ans.startswith("err:"):
        return [""] * len(icons)
    out = [""] * len(icons)
    for line in ans.splitlines():
        line = line.strip().lstrip("-* ").replace("：", ":")
        if ":" not in line:
            continue
        a, _, b = line.partition(":")
        a, b = a.strip(), b.strip()
        if not a.isdigit():
            continue
        n = int(a)
        if 1 <= n <= len(idx) and b:
            # 过滤模型偶尔偷懒的"不确定/未知"之类无信息回答
            if b not in ("未知", "不确定", "无", "N/A", "na"):
                out[n - 1] = b[:12]
    return out


# ------------------------------------------------------------------ Set-of-Mark 绘制
def draw(img, elements, out_path=SOM_IMG, max_marks=60):
    """在截图上给每个元素画编号框，存成 Set-of-Mark 图。返回路径。"""
    from PIL import ImageDraw
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    vis = img.convert("RGB").copy()
    d = ImageDraw.Draw(vis)
    f = _font(13)
    for e in elements[:max_marks]:
        x, y, w, h = int(e["x"]), int(e["y"]), int(e["w"]), int(e["h"])
        if w < 2 or h < 2:
            continue
        color = (0, 110, 220) if e["kind"] == "text" else (220, 90, 0)
        d.rectangle([x, y, x + w, y + h], outline=color, width=1)
        tag = str(e["id"])
        # 标签画在框左上角外侧，避免盖住元素本身
        tx, ty = x, max(0, y - 14)
        d.rectangle([tx, ty, tx + 8 * len(tag) + 4, ty + 13], fill=color)
        d.text((tx + 2, ty), tag, fill=(255, 255, 255), font=f)
    vis.save(out_path)
    return out_path


# ------------------------------------------------------------------ 高层
def build(img_path=None, rect=None, caption=True, som_image=True,
          timeout=240, img=None):
    """一次构建：截图 -> 元素 -> 编号 -> （可选）图标 caption -> （可选）SoM 图。

    返回
        {
          "elements":  [{"id":1,"kind":..,"label":..,"desc":..,"cx":..,"cy":..,
                         "x":..,"y":..,"w":..,"h":..}]  # id 从 1 开始
          "list_text": "[1] 文本「助理」@(50,141)\n[2] 图标「设置」@(1180,40)..."
          "som_image": "shots/_som.png"
          "by_id":     {1: element}
          "notes":     ["..."]  # caption 是否成功等
        }
    """
    notes = []
    if img is None:
        img, p = grab(PERCEIVE_IMG if not img_path else img_path)
    else:
        p = img_path or PERCEIVE_IMG
        os.makedirs(SHOTS, exist_ok=True)
        if not os.path.exists(p):
            img.save(p)
    els = perceive(p, rect=rect)
    rows = _merge(els)

    icons = [r for r in rows if r["kind"] == "icon"]
    if caption and icons:
        descs = caption_icons(img, icons, timeout=timeout)
        got = 0
        for r, t in zip(icons, descs):
            if t:
                r["label"] = t
                r["desc"] = t
                got += 1
        notes.append("图标标注 %d/%d（%s）"
                     % (got, len(icons), "网关 VL" if got else "VL 不可用，退化为纯编号"))
    for r in rows:
        r.setdefault("desc", r.get("label", ""))

    for i, r in enumerate(rows, 1):
        r["id"] = i

    som_path = ""
    if som_image:
        som_path = draw(img, rows, out_path=SOM_IMG)

    return {"elements": rows, "list_text": format_list(rows),
            "som_image": som_path, "by_id": {r["id"]: r for r in rows},
            "notes": notes,
            "counts": (len(els["texts"]), len(els["icons"]))}


def format_list(elements, limit=60):
    """编号元素表 —— 这就是喂给模型的"屏幕"。

    刻意做成「[编号] 类型「名称」@(x,y)」这种极简格式：
    模型只需要回一个编号，不需要理解坐标，也不需要视觉能力。
    """
    lines = []
    for e in elements[:limit]:
        kind = "文本" if e["kind"] == "text" else "图标"
        name = e.get("label") or e.get("desc") or "?"
        lines.append("[%d] %s「%s」@(%d,%d)"
                     % (e["id"], kind, name[:16], e["cx"], e["cy"]))
    return "\n".join(lines)


def pick(som, n):
    """用编号换坐标（喂给 pyautogui）。返回 (x, y) 或 None。"""
    e = som["by_id"].get(int(n))
    return (e["cx"], e["cy"]) if e else None


def find(som, text, exact_first=True):
    """按文字/描述找坐标。返回 ((x,y), element) 或 (None, None)。

    排序：完全相等 > 前缀 > 包含（短文本优先）—— 与 nuphus_bridge.find 一致。
    """
    t = (text or "").strip()
    if not t:
        return None, None
    cands = []
    for e in som["elements"]:
        s = e.get("label") or e.get("desc") or ""
        if t not in s:
            continue
        exact = 1 if s == t else 0
        pref = 1 if s.startswith(t) else 0
        cands.append((exact, pref, -len(s), e))
    if not cands:
        return None, None
    cands.sort(key=lambda c: (-c[0], -c[1], -c[2]))
    e = cands[0][3]
    return (e["cx"], e["cy"]), e


# ------------------------------------------------------------------ 自检
def _selftest():
    import time
    print("=" * 66)
    print("Set-of-Mark 结构化感知 · 自检（只读，不点击）")
    print("=" * 66)

    t0 = time.time()
    img, p = grab(PERCEIVE_IMG)
    print("[1] 截图        OK  %dx%d  %.2fs" % (img.width, img.height, time.time() - t0))

    t0 = time.time()
    els = perceive(p)
    print("[2] 原始元素    OK  文本 %d + 图标 %d  %.2fs"
          % (len(els["texts"]), len(els["icons"]), time.time() - t0))

    t0 = time.time()
    rows = _merge(els)
    print("[3] 阅读序编号  OK  %d 个元素（编号顺序稳定 = 跨帧可引用）%.2fs"
          % (len(rows), time.time() - t0))

    icons = [r for r in rows if r["kind"] == "icon"]
    t0 = time.time()
    descs = caption_icons(img, icons)
    got = sum(1 for d in descs if d)
    print("[4] 图标功能标注 %s  %d/%d  一次请求（contact sheet）%.2fs"
          % ("OK" if got else "--", got, len(icons), time.time() - t0))
    for i, d in enumerate(descs[:8]):
        if d:
            print("      #%-2d -> %s" % (i + 1, d))

    for r, d in zip(icons, descs):
        r["label"] = d or ""
        r["desc"] = r["label"]
    for i, r in enumerate(rows, 1):
        r["id"] = i

    t0 = time.time()
    path = draw(img, rows)
    print("[5] SoM 叠加图   OK  %s  %.2fs" % (os.path.basename(path), time.time() - t0))

    print("[6] 样本列表（这就是喂给模型的「屏幕」）")
    for line in format_list(rows, limit=12).splitlines():
        print("      " + line)
    print("=" * 66)
    return {"elements": rows, "som_image": path}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        s = build()
        print(s["list_text"][:2000])
        if s["notes"]:
            print("· " + " | ".join(s["notes"]))
        pt, e = find(s, args[0])
        print("查找 %r -> %s" % (args[0], ("#%d %s @%s" % (e["id"], e.get("label"), pt)) if e else "未命中"))
    else:
        _selftest()
