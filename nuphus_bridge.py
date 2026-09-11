# -*- coding: utf-8 -*-
"""nuphus-mcp stdio 桥 —— 让 Python 脚本直接调用 nuphus 的 MCP 工具。

为什么需要：
  nuphus-mcp 是 npm 分发的预编译 Rust 二进制，只有 MCP stdio 一种接口
  （实测 `nuphus-mcp.exe --help` 直接 stdin EOF 退出，无 CLI 模式）。
  它的 `desktop_perceive` 是本机**最快最准的零显存接地源**：
  一次返回 ~57 个文本 + ~37 个图标（带分类），远优于 easyocr。

  但它是 MCP 工具，之前只有大模型能调，Python 脚本调不到 —— 导致闭环里
  OCR 接地这一环形同虚设（desktop_loop.locate 里根本没接 OCR）。
  本模块补上这个缺口。

协议：JSON-RPC 2.0 over stdio，每行一条消息。
  1. initialize            -> 握手
  2. notifications/initialized（通知，无 id）
  3. tools/call            -> 实际调用

进程常驻复用（首次 spawn ~200ms，之后每次调用 ~50-200ms）。

用法:
    import nuphus_bridge as nb
    els = nb.perceive(r"E:\\AI\\shot.png")      # -> {"texts":[...], "icons":[...]}
    pt  = nb.find("自动化", els)                 # -> ((x, y), 说明)
    nb.close()                                   # 用完关闭
"""
import json
import os
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 已装 0.1.11 的二进制位置（npm 全局包内嵌平台包）
_CANDIDATES = [
    r"C:\Users\wsz945\AppData\Roaming\npm\node_modules\@nuphus\nuphus-mcp"
    r"\node_modules\@nuphus\nuphus-mcp-win32-x64\bin\nuphus-mcp.exe",
]


def find_exe():
    for p in _CANDIDATES:
        if os.path.exists(p):
            return p
    # 兜底：搜 npm 全局目录
    root = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "@nuphus")
    if os.path.isdir(root):
        for d in os.listdir(root):
            p = os.path.join(root, d, "node_modules", "@nuphus",
                             "nuphus-mcp-win32-x64", "bin", "nuphus-mcp.exe")
            if os.path.exists(p):
                return p
    return None


class _Proc:
    """常驻 MCP stdio 会话。"""

    def __init__(self, exe=None):
        self.exe = exe or find_exe()
        self.p = None
        self._id = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 生命周期
    def start(self):
        if self.p and self.p.poll() is None:
            return True
        if not self.exe:
            raise RuntimeError("找不到 nuphus-mcp.exe（npm 全局包里未安装）")
        self.p = subprocess.Popen(
            [self.exe],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=HERE,
            # 必须二进制管道 + 手动编解码：走文本模式会因 Windows 默认 GBK 解 UTF-8 报错
            bufsize=0,
        )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "voice-assistant-bridge", "version": "1.0"},
        })
        self._notify("notifications/initialized")
        return True

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.terminate()
            except Exception:
                pass
        self.p = None

    # ---------------------------------------------------------- 底层 RPC
    def _send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def _read_msg(self, timeout=60):
        """读一条 JSON-RPC 消息。跳过非 JSON 行（Rust 日志走的是 stderr，但保险）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue          # 非 JSON 行直接丢掉
        return None

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _rpc(self, method, params=None, timeout=60):
        """发请求 + 等到 id 匹配的响应。期间的通知先缓存丢弃。"""
        with self._lock:
            self._id += 1
            rid = self._id
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            self._send(msg)
            deadline = time.time() + timeout
            while time.time() < deadline:
                r = self._read_msg(timeout=max(1, deadline - time.time()))
                if r is None:
                    break
                if r.get("id") == rid:
                    if "error" in r:
                        raise RuntimeError("MCP error: %s" % r["error"])
                    return r.get("result")
            raise TimeoutError("MCP 调用超时: %s" % method)

    # ---------------------------------------------------------- 对外接口
    def call(self, tool, args=None, timeout=120):
        """调用任意 MCP 工具，返回其 result（原始结构）。"""
        self.start()
        res = self._rpc("tools/call",
                        {"name": tool, "arguments": args or {}}, timeout=timeout)
        return res

    def call_json(self, tool, args=None, timeout=120):
        """调用工具并把 text 内容解析成 JSON（nuphus 多数工具返回 JSON 文本）。"""
        res = self.call(tool, args, timeout=timeout)
        for c in (res or {}).get("content", []):
            if c.get("type") == "text":
                t = c.get("text", "")
                try:
                    return json.loads(t)
                except Exception:
                    return t
        return res

    def tools(self):
        self.start()
        return self._rpc("tools/list")


_SINGLETON = None


def session():
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = _Proc()
    return _SINGLETON


def close():
    global _SINGLETON
    if _SINGLETON:
        _SINGLETON.close()
        _SINGLETON = None


# ------------------------------------------------------------ 高层封装
def perceive(path):
    """调 desktop_perceive，返回归一化后的元素表。

    nuphus 返回结构随版本可能变，这里做兼容归一：
      {"texts": [{"text","x","y","w","h","conf"}...],
       "icons": [{"type","x","y","w","h"}...],
       "raw": 原始返回}
    """
    data = session().call_json("desktop_perceive", {"path": path})
    return normalize_perceive(data)


def normalize_perceive(data):
    """把 nuphus 的原始返回归一成 {"texts":[...], "icons":[...]}。

    ⚠️ 实测 0.1.11 的真实结构（不看这个会归一失败，图标全部漏掉）:
        {
          "count": 72, "ocr_count": 44, "yolo_count": 38, "yolo_available": true,
          "models_dir": "...",
          "elements": [
            {"id":0, "kind":"text"|"icon", "source":"ocr"|"yolo",
             "text":"助理", "confidence":0.85,
             "center":{"x":50,"y":141}, "rect":{"x":28,"y":136,"w":45,"h":10}},
            ...
          ]
        }
    所以：元素统一在 `elements` 里，**必须按 kind 分流**；
    center 是权威中心点（OCR 的 rect 中心有 ±2px 抖动，能直接用 center 就用）。
    """
    out = {"texts": [], "icons": [], "raw": data,
           "ocr_count": None, "yolo_count": None}
    if not isinstance(data, dict):
        return out

    out["ocr_count"] = data.get("ocr_count")
    out["yolo_count"] = data.get("yolo_count")

    def _pick(d, *keys):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return None

    def _box(d):
        b = _pick(d, "rect", "bbox", "box", "bounds", "region")
        if isinstance(b, dict):
            return (int(b.get("x", 0)), int(b.get("y", 0)),
                    int(b.get("w") or b.get("width") or 0),
                    int(b.get("h") or b.get("height") or 0))
        if isinstance(b, (list, tuple)) and len(b) >= 4:
            return int(b[0]), int(b[1]), int(b[2]), int(b[3])
        x, y = _pick(d, "x", "left"), _pick(d, "y", "top")
        if x is None or y is None:
            return None
        return int(x), int(y), int(_pick(d, "w", "width") or 0), int(_pick(d, "h", "height") or 0)

    def _center(d, box):
        c = d.get("center")
        if isinstance(c, dict) and c.get("x") is not None:
            return int(c["x"]), int(c["y"])
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            return int(c[0]), int(c[1])
        if box:
            return box[0] + box[2] // 2, box[1] + box[3] // 2
        return None

    # ── 主路径：elements + kind 分流
    elements = data.get("elements")
    rows = elements if isinstance(elements, list) else None
    # ── 兼容路径：老结构把文本/图标分在两个键里
    if rows is None:
        for key in ("texts", "elements", "labels", "words", "ocr"):
            v = data.get(key)
            if isinstance(v, list):
                rows = [{**d, "kind": "text"} for d in v if isinstance(d, dict)]
                break
        for key in ("icons", "ui_elements", "clickables"):
            v = data.get(key)
            if isinstance(v, list):
                for d in v:
                    if isinstance(d, dict):
                        rows = (rows or []) + [{**d, "kind": "icon"}]
                break
    if not rows:
        return out

    for d in rows:
        if not isinstance(d, dict):
            continue
        box = _box(d)
        ctr = _center(d, box)
        if ctr is None:
            continue
        kind = str(_pick(d, "kind", "type", "class", "category") or "text").lower()
        item = {
            "x": box[0] if box else ctr[0], "y": box[1] if box else ctr[1],
            "w": box[2] if box else 0, "h": box[3] if box else 0,
            "cx": ctr[0], "cy": ctr[1],
            "conf": _pick(d, "confidence", "conf", "score"),
            "source": str(_pick(d, "source", "detector") or ""),
            "id": _pick(d, "id"),
        }
        if kind == "icon":
            item["type"] = str(_pick(d, "text", "name", "label") or kind)
            out["icons"].append(item)
        else:
            item["text"] = str(_pick(d, "text", "label", "name", "value") or "")
            out["texts"].append(item)
    return out


def text_center(e):
    """元素中心点。优先用 nuphus 直接给的 center（比 rect 反算准）。"""
    if e.get("cx") is not None:
        return (int(e["cx"]), int(e["cy"]))
    return (int(e["x"] + e["w"] / 2.0), int(e["y"] + e["h"] / 2.0))


def find(target, elements, exact_first=True, rect=None):
    """在 perceive 结果里找目标文字。

    rect=(x,y,w,h) 时只接受该屏幕区域内的元素 —— 这是把定位收敛到
    目标窗口的关键：同名文字在别的窗口时不会被误选。

    排序：完全相等 > 包含（短文本优先）> 位置靠前。
    返回 ((x,y), 说明) 或 (None, 原因)
    """
    t = (target or "").strip()
    if not t:
        return None, "目标为空"
    if isinstance(elements, dict):
        texts = elements.get("texts", [])
    else:
        texts = elements or []

    def in_rect(e):
        if not rect:
            return True
        cx, cy = text_center(e)
        x, y, w, h = rect
        return x <= cx < x + w and y <= cy < y + h

    cands = []
    for e in texts:
        s = (e.get("text") or "").strip()
        if not s or t not in s:
            continue
        if not in_rect(e):
            continue
        exact = 1 if s == t else 0
        # 越短说明包含关系越紧（"自动化" 优于 "自动化任务面板"）
        cands.append((exact, -len(s), e))
    if not cands:
        why = "（已限定窗口区域）" if rect else ""
        return None, "perceive 文本中无 '%s'%s" % (t, why)
    cands.sort(key=lambda c: (-c[0], -c[1]))
    _, _, best = cands[0]
    cx, cy = text_center(best)
    extra = "" if len(cands) == 1 else "（另有 %d 个候选）" % (len(cands) - 1)
    return (cx, cy), "nuphus perceive 命中: %s%s" % (best["text"][:24], extra)


def snap_to_clickable(pt, elements, text_h=None, max_h_ratio=6.0,
                      max_texts=2, max_area=0.35):
    """把文字中心点吸附到「包含它的整行可点区域」中心。

    实测依据（2026-09-11）: 侧边栏菜单项
        文本「自动化」 rect=(28, 234, 60, 11)  -> 文字中心 (58, 240)
        YOLO 图标框    rect=( 2, 222, 261, 32) -> 行中心 (132, 238)
    文字中心落在行框内，而**行框才是真正的点击目标**（整行可点）。
    取行框中心作为落点，命中容错从 60x11 提升到 261x32，抗偏移能力大幅提高。
    这也解释了上一轮「资料库 49px 偏差仍能点中」的现象 —— 它在行内。

    ⚠️ 判据是实测调出来的，别凭直觉改（第一版 max_h_ratio=3.0 时
    43 个文本只吸附上 2 个 —— 真实行高是文字的 3.1~4.9 倍，全被误杀）：
      - 行高 <= 文字高 × max_h_ratio(6.0)     实测行 3.1~4.9 倍，合并卡片 9.1 倍
      - 行内文本数 <= max_texts(2)            实测行框 1 个，合并卡片 3 个 —— 这条最可靠
      - 面积 <= 屏幕面积 × max_area
    多条同时满足才吸附；命中多个取面积最小的（嵌套的按钮框会赢过整行框）。

    返回 ((x, y), 说明) 或 (None, 未找到合适的行框)。
    """
    if not pt or not elements:
        return None, "无行框可吸附"
    icons = elements.get("icons", []) if isinstance(elements, dict) else []
    texts = elements.get("texts", []) if isinstance(elements, dict) else []
    x, y = pt
    best = None
    for e in icons:
        ex, ey, ew, eh = e["x"], e["y"], e["w"], e["h"]
        if not (ex <= x < ex + ew and ey <= y < ey + eh):
            continue
        if text_h and eh > text_h * max_h_ratio:
            continue
        if ew * eh > 1280 * 720 * max_area:
            continue
        # 框内文本数：行框只装 1 个（偶尔 2 个），装 3 个以上的是合并卡片框
        inside = 0
        for t in texts:
            tx, ty = t.get("cx", t["x"]), t.get("cy", t["y"])
            if ex <= tx < ex + ew and ey <= ty < ey + eh:
                inside += 1
                if inside > max_texts:
                    break
        if inside > max_texts:
            continue
        # 取面积最小的那个（最贴合的容器）
        if best is None or ew * eh < best[2] * best[3]:
            best = (ex, ey, ew, eh)
    if best is None:
        return None, "文字中心不在任何行框内"
    ex, ey, ew, eh = best
    cx, cy = ex + ew // 2, ey + eh // 2
    return (cx, cy), "吸附到可点区域 (%d,%d,%dx%d) 中心" % (ex, ey, ew, eh)


def find_with_snap(target, elements, rect=None, snap=True):
    """find() + 可选的整行吸附。返回 ((x, y), 说明) 或 (None, 原因)。"""
    pt, how = find(target, elements, rect=rect)
    if not pt:
        return None, how
    if not snap:
        return pt, how
    matched = None
    if isinstance(elements, dict):
        for e in elements.get("texts", []):
            if e.get("cx") == pt[0] and e.get("cy") == pt[1]:
                matched = e
                break
    sp, show = snap_to_clickable(pt, elements,
                                 text_h=(matched or {}).get("h"))
    if sp:
        return sp, "%s -> %s" % (how, show)
    return pt, how


if __name__ == "__main__":
    import sys
    exe = find_exe()
    print("二进制:", exe or "未找到")
    if not exe:
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "tools":
        tl = session().tools()
        for t in (tl or {}).get("tools", []):
            print("  %-28s %s" % (t.get("name"), (t.get("description") or "")[:60]))
        close()
    elif len(sys.argv) > 1:
        p = sys.argv[1]
        t0 = time.time()
        els = perceive(p)
        print("perceive 耗时 %.2fs  文本 %d 个, 图标 %d 个"
              % (time.time() - t0, len(els["texts"]), len(els["icons"])))
        for e in els["texts"][:15]:
            print("   文本 %-30s (%4d,%4d,%3d,%3d) conf=%s"
                  % (e["text"][:28], e["x"], e["y"], e["w"], e["h"], e["conf"]))
        for e in els["icons"][:10]:
            print("   图标 %-30s (%4d,%4d,%3d,%3d)"
                  % (e["type"][:28], e["x"], e["y"], e["w"], e["h"]))
        close()
    else:
        print(__doc__)
