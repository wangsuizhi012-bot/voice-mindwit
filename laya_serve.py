"""Laya 决策服务(本地 HTTP, 零第三方依赖, 仅用标准库)。

跑在托管 venv 里(那里装了 laya/torch):
    <装有 laya/torch 的 python> laya_serve.py
    （如 %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe）

语音助手(项目 venv, 只有 requests)通过 HTTP 调用它, 两边互不污染依赖。

接口:
    GET  /health                      -> {"ok":true,"ready":true,"ckpt":"...","load_ms":1234}
    POST /v1/intent {"text":"..."}     -> {"action":"open","params":{...},"confidence":0.93,
                                           "probabilities":{...},"is_command":0.97,"engine":"laya","ms":41}
    POST /v1/systemone                -> Laya 原语直通 {"state":..., "questions":{...}}
"""
import json
import os
import sys
import threading
import time

# 关键: 系统代理会劫持本机 HTTP 调用, 必须在 import 任何网络库之前清掉
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["HF_HUB_OFFLINE"] = "1"          # 权重已在本地缓存, 别再联网
os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LAYA_PORT", "8801"))
SUBFOLDER = os.environ.get("LAYA_SUBFOLDER", "multilingual")   # "" = 英文权重(仓库根)
REPO = os.environ.get("LAYA_REPO", "convaiinnovations/laya")

STATE = {"ready": False, "ckpt": (REPO + "/" + SUBFOLDER) if SUBFOLDER else REPO,
         "load_ms": None, "error": None, "agent": None}

# ---- 动作空间: 与 voice_assistant.py 的 SYS_PROMPT 一致 ----
# 实测: criteria 用英文比中文准得多(点发送按钮 中文->screenshot_send 错 / 英文->click_target 对),
# 因为 multilingual 权重的指令微调以英文为主。中文注释保留便于对照。
ACTIONS = {
    "screenshot":       "take a screenshot of the screen",                       # 截屏到剪贴板
    "screenshot_send":  "take a screenshot then paste it into the chat and send",  # 截图并发送
    "type":             "type or input a piece of text at the cursor",             # 打字输入
    "open":             "open or launch an application",                           # 打开应用
    "click_here":       "click once at the current mouse position",                # 当前位置点一下
    "click_target":     "click a named button or control on the screen",           # 点某个控件
    "click":            "click an exact screen coordinate",                        # 点精确坐标
    "click_visual":     "find and click an element that has no name",              # 视觉兜底点击
    "press":            "press a keyboard key or shortcut such as ctrl+c",         # 按键/快捷键
    "scroll":           "scroll the mouse wheel to page up or down",               # 滚轮翻页
    "none":             "just chatting or asking, no computer action needed",      # 闲聊
}

INTENT_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "What does the user want the computer to do? Choose the closest action.",
        "criteria": ACTIONS,
    },
}
# 注意: 不要再加 noul 问题做「是不是指令」的门 —— 与 11 选项的 choice 同批推理会互相干扰
# (实测单独问 打开记事本=0.66, 合并后掉到 0.06, 把正确动作全判成 none)。
# 改为从 choice 的 none 概率反推: is_command = 1 - P(none)。

# ---- 参数抽取: Laya 负责「分类」, 槽位用规则抽(它不做生成) ----
REGION_RULES = [
    ("full", ("全屏", "整个屏幕", "整个桌面", "全部屏幕")),
    ("tl", ("左上角", "左上")), ("tr", ("右上角", "右上")),
    ("bl", ("左下角", "左下")), ("br", ("右下角", "右下")),
    ("left", ("左半边", "左半", "左边一半")), ("right", ("右半边", "右半", "右边一半")),
    ("top", ("上半部分", "上半", "上面一半")), ("bottom", ("下半部分", "下半", "下面一半")),
]

KEY_RULES = [
    ("ctrl+s", ("保存",)), ("ctrl+c", ("复制",)), ("ctrl+v", ("粘贴",)),
    ("ctrl+x", ("剪切",)), ("ctrl+a", ("全选",)), ("ctrl+z", ("撤销",)),
    ("f5", ("刷新",)), ("enter", ("回车", "确认", "换行")),
    ("delete", ("删除键",)), ("space", ("空格",)), ("esc", ("取消键",)),
    ("tab", ("切到下一个",)),
]

COMPOSITE_KEYS = {"ctrl+s": ["ctrl", "s"], "ctrl+c": ["ctrl", "c"], "ctrl+v": ["ctrl", "v"],
                  "ctrl+x": ["ctrl", "x"], "ctrl+a": ["ctrl", "a"], "ctrl+z": ["ctrl", "z"]}


def _after(text, words):
    t = text.strip()
    for w in words:
        i = t.find(w)
        if i >= 0:
            return t[i + len(w):].strip()
    return ""


def extract_params(action, text):
    t = (text or "").strip()
    if action in ("screenshot", "screenshot_send"):
        for region, kws in REGION_RULES:
            if any(k in t for k in kws):
                return {"region": region}
        return {}
    if action == "open":
        app = _after(t, ("打开", "启动", "运行", "开启", "开一下"))
        app = app.rstrip("吧啊呢。,.，").strip()
        return {"app": app} if app else {}
    if action == "type":
        body = _after(t, ("输入", "打字", "打一段", "写下", "打上", "写"))
        return {"text": body} if body else {}
    if action in ("click_target", "click_visual"):
        tgt = _after(t, ("点击", "点一下", "点", "按一下", "按"))
        for suf in ("按钮", "一下", "图标", "菜单", "选项"):
            if tgt.endswith(suf) and len(tgt) > len(suf):
                tgt = tgt[: -len(suf)]
        tgt = tgt.strip()
        return {"target": tgt} if tgt else {}
    if action == "click_here":
        p = {"button": "right", "clicks": 1} if ("右键" in t or "右击" in t) else {"button": "left", "clicks": 1}
        if "双击" in t or "两下" in t:
            p["clicks"] = 2
        return p
    if action == "click":
        import re as _re
        m = _re.findall(r"\d+", t)
        if len(m) >= 2:
            return {"x": int(m[0]), "y": int(m[1])}
        return {}
    if action == "press":
        for key, kws in KEY_RULES:
            if any(k in t for k in kws):
                if key in COMPOSITE_KEYS:
                    return {"keys": COMPOSITE_KEYS[key]}
                return {"key": key}
        return {}
    if action == "scroll":
        up = any(k in t for k in ("往上", "向上", "上滚", "上翻", "往上翻"))
        down = any(k in t for k in ("往下", "向下", "下滚", "下翻", "往下翻"))
        return {"amount": 3 if up else (-3 if down else 3)}
    return {}


def load_agent():
    t0 = time.time()
    try:
        import laya
        agent = laya.load(REPO, subfolder=(SUBFOLDER or None))
        STATE["agent"] = agent
        STATE["ready"] = True
        STATE["load_ms"] = int((time.time() - t0) * 1000)
        print("[laya] ready ckpt=%s load_ms=%d" % (STATE["ckpt"], STATE["load_ms"]), flush=True)
    except Exception as e:
        STATE["error"] = repr(e)
        print("[laya] LOAD FAILED: %r" % (e,), flush=True)


def rule_action(text):
    """确定性规则快通道: 这类指令 Laya 也常判错(保存/复制 -> none 等), 直接规则命中更稳。
    返回 (action, params) 或 None。"""
    import re as _re
    t = (text or "").strip()
    for key, kws in KEY_RULES:
        if any(k in t for k in kws):
            p = {"keys": COMPOSITE_KEYS[key]} if key in COMPOSITE_KEYS else {"key": key}
            return "press", p
    for w in ("输入", "打字", "打一段", "写下"):
        if w in t:
            body = t.split(w, 1)[1].strip()
            if body:
                return "type", {"text": body}
    # 点击类: 坐标优先(必须带两个数字), 再右键/双击, 再「点 + 目标文字」
    if "点" in t and len(_re.findall(r"\d+", t)) >= 2:
        m = _re.findall(r"\d+", t)
        return "click", {"x": int(m[0]), "y": int(m[1])}
    if any(k in t for k in ("右键", "右击")):
        return "click_here", {"button": "right", "clicks": 2 if ("双击" in t or "两下" in t) else 1}
    if ("双击" in t or "两下" in t) and "点" in t:
        return "click_here", {"button": "left", "clicks": 2}
    m = _re.search(r"(?:点击|点一下|点|按一下|按)\s*(.+)", t)
    if m and "点" in t:
        tgt = m.group(1).strip()
        for suf in ("按钮", "图标", "菜单", "选项", "一下", "这里", "那儿", "那边"):
            while tgt.endswith(suf) and len(tgt) > len(suf):
                tgt = tgt[: -len(suf)]
        if tgt in ("一下", "这里", "那儿", "那边", "下", "击"):
            tgt = ""                      # 只有"点一下/点这里", 没有真目标 -> 交给 Laya 判 click_here
        if tgt:
            return "click_target", {"target": tgt}
    if ("截图" in t or "截屏" in t or "截个图" in t) and any(k in t for k in ("发送", "发给我", "发给", "发过去")):
        region = {}
        for r, kws in REGION_RULES:
            if any(k in t for k in kws):
                region = {"region": r}
                break
        return "screenshot_send", region
    return None


def do_intent(text):
    agent = STATE["agent"]
    if agent is None:
        return {"error": "model not ready", "engine": "laya"}
    t0 = time.time()
    hit = rule_action(text)
    out = agent.system_one(text, INTENT_QUESTIONS)
    a = out["answers"]["action"]
    action = a["choice"]
    if hit:                       # 规则优先(确定性, 置信度记 1.0)
        action, params = hit
        conf = 1.0
        via = "rule"
    else:
        params = extract_params(action, text)
        conf = a["confidence"]
        via = "laya"
    probs = a["probabilities"]
    return {
        "action": action,
        "params": params,
        "confidence": conf,
        "probabilities": probs,
        "is_command": round(1.0 - float(probs.get("none", 0.0)), 4),
        "engine": "laya",
        "via": via,
        "ckpt": STATE["ckpt"],
        "ms": int((time.time() - t0) * 1000),
        "raw_choice": a["choice"],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[laya %s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "ready": STATE["ready"], "ckpt": STATE["ckpt"],
                             "load_ms": STATE["load_ms"], "error": STATE["error"], "port": PORT})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        b = self._body()
        try:
            if self.path.startswith("/v1/intent"):
                if not STATE["ready"]:
                    return self._send(503, {"error": "loading", "engine": "laya"})
                return self._send(200, do_intent(b.get("text", "")))
            if self.path.startswith("/v1/systemone"):
                if not STATE["ready"]:
                    return self._send(503, {"error": "loading"})
                t0 = time.time()
                out = STATE["agent"].system_one(b.get("state", ""), b.get("questions", {}))
                out["ms"] = int((time.time() - t0) * 1000)
                return self._send(200, out)
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": repr(e)})


def main():
    print("[laya] repo=%s subfolder=%r  port=%d" % (REPO, SUBFOLDER, PORT), flush=True)
    print("[laya] loading in background ...", flush=True)
    threading.Thread(target=load_agent, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("[laya] serving http://127.0.0.1:%d" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
