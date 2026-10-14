# -*- coding: utf-8 -*-
"""voice-assistant 浏览器控制台 —— :8802 可交互面板（标准库实现，零新依赖）。

能力:
    GET  /                控制台单页（console/index.html）
    GET  /api/state       档位+显存+服务+网关+记忆统计（前端 3s 轮询）
    GET  /api/events      SSE 事件流（终端式实时日志）
    POST /api/profile     {"name":"chat|rpa|gpu_free"} 切档并应用
    GET  /api/selftest    读最近一次自检报告
    POST /api/selftest    后台重跑 selfcheck.py（完成后 SSE 通知）
    GET  /api/memory      ?mode=recent|recall|stats
    POST /api/memory      {"text","kind","source"} 记住
    POST /api/memory/forget  {"target":"<id>|关键词"}
    POST /api/command     {"text":"打开记事本"} 走 llm_intent→execute 真实执行
    GET  /api/config      可改配置项清单（白名单 + 当前值 + 是否需重启）
    POST /api/config      {"decision_engine":"auto", ...} 校验+备份+原子落盘

设计:
    - 指令执行与语音同一条链路（voice_assistant.llm_intent/execute），
      动作白名单/二次确认逻辑天然生效 —— 不另开后门
    - SSE 而非 WebSocket：单向推送足够，标准库即可
    - 所有状态端点只读探测，不改现场

用法:
    python console_server.py            # 前台跑（推荐放你的终端）
    浏览器打开 http://127.0.0.1:8802
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
INDEX_PATH = os.path.join(HERE, "console", "index.html")
PORT = 8802

import model_sched as ms        # noqa: E402
import vram_profile as vp       # noqa: E402
import memory_store as mem      # noqa: E402
import config_schema as cs      # noqa: E402


# ------------------------------------------------------- 配置读写（白名单）
def _cfg_backup():
    """写入前备份 config.json -> state/config.json.bak-YYYYMMDD-HHMMSS。

    返回备份绝对路径；失败返回 None（调用方据此决定要不要继续）。
    """
    src = cs.CONFIG_PATH
    if not os.path.exists(src):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(HERE, "state", "config.json.bak-" + stamp)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    except Exception:
        return None


def _cfg_write(full):
    """原子写 config.json：先写临时文件再 os.replace，避免半截文件。

    主程序启动时读这个文件，写坏了会让它起不来 —— 所以必须原子。
    """
    tmp = cs.CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, cs.CONFIG_PATH)


def apply_config(patch):
    """校验 + 备份 + 原子落盘。返回给前端的完整结果 dict。

    注意: 这里读的是**磁盘现值**而非主程序内存快照 —— 主程序只在启动时
    读一次 config.json，用它的快照合并会丢掉别人后写的改动。
    """
    ok, cleaned, errors = cs.validate(patch)
    if not ok:
        emit("config", "配置未写入（校验失败）: %s" % errors)
        return {"ok": False, "errors": errors, "applied": {},
                "note": "有项目校验失败，本次全部未写入"}
    if not cleaned:
        return {"ok": True, "applied": {}, "errors": {},
                "note": "没有需要修改的项"}

    full = cs.load_current()
    if not isinstance(full, dict):
        full = {}
    backup = _cfg_backup()
    if backup is None:
        return {"ok": False, "errors": {"_": "备份失败，已中止写入（安全优先）"},
                "applied": {}}
    for k, v in cleaned.items():
        full[k] = v
    try:
        _cfg_write(full)
    except Exception as e:
        emit("error", "配置写入失败: %r" % (e,))
        return {"ok": False, "errors": {"_": "写入失败: %r" % (e,)},
                "applied": {}, "backup": backup}

    restart = sorted(k for k in cleaned if cs.SCHEMA[k]["restart"])
    hot = sorted(k for k in cleaned if not cs.SCHEMA[k]["restart"])
    for k in cleaned:
        emit("config", "已改 %s = %r%s" % (
            k, cleaned[k], "（需重启主程序）" if k in restart else "（即生效）"))
    return {
        "ok": True,
        "applied": cleaned,
        "errors": {},
        "backup": backup,
        "restart_required": restart,
        "live": hot,
        "note": ("已写入并备份。需重启主程序才生效: %s" % "、".join(restart))
                if restart else "已写入，立即生效（对该进程后续行为）",
    }

# ---------------------------------------------------------------- 事件总线
_EV_LOCK = threading.Condition()
_EVENTS = []                    # (ts, kind, text)  上限 500
_CLIENTS = set()                # SSE 连接的 wfile 引用


def emit(kind, text):
    item = (time.time(), kind, text)
    with _EV_LOCK:
        _EVENTS.append(item)
        del _EVENTS[:-500]
        dead = []
        for c in list(_CLIENTS):
            try:
                payload = "data: %s\n\n" % json.dumps(
                    {"ts": item[0], "kind": kind, "text": text},
                    ensure_ascii=False)
                c.write(payload.encode("utf-8"))
                c.flush()
            except Exception:
                dead.append(c)
        for c in dead:
            _CLIENTS.discard(c)


# ---------------------------------------------------------------- 指令链路
_VA = None


def _va():
    """惰性 import voice_assistant（首次 import 可能较慢）。"""
    global _VA
    if _VA is None:
        emit("info", "加载 voice_assistant 模块（意图链路）...")
        import voice_assistant as va
        _VA = va
    return _VA


def run_command(text):
    """文本指令走与语音完全相同的 链路。"""
    va = _va()
    emit("asr", "指令: %s" % text)
    try:
        intent = va.llm_intent(text)
        emit("intent", json.dumps(intent, ensure_ascii=False))
        # validate_intent 返回 (ok, reason) 元组 —— 必须解包，不能 if err 真值判断
        ok, reason = va.validate_intent(intent)
        if not ok:
            emit("warn", "意图校验未过: %s" % reason)
            return {"ok": False, "stage": "validate", "error": reason,
                    "intent": intent}
        result = va.execute(intent, text)
        emit("exec", "执行完成: %s" % (result or "(无返回)"))
        return {"ok": True, "intent": intent, "result": result}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        emit("error", "指令异常: %r | %s" % (e, tb.replace("\n", " ⏎ ")[:400]))
        return {"ok": False, "stage": "exception", "error": repr(e),
                "traceback": tb}


# ---------------------------------------------------------------- 状态
# 实测 snapshot 各组件串行 ~8s（status 5.3s + gateway 2s + vram 1s），
# 不能让 /api/state 每次都现算 —— 后台线程 3s 刷新缓存，API 直接读（0ms）。
_STATE_CACHE = {"data": None, "at": 0.0}
_STATE_LOCK = threading.Lock()


def snapshot():
    v = ms.vram()
    prof = vp.current()
    svc = {k: {"up": d["up"], "external": d["external"], "managed": d["managed"],
               "port": d["spec"]["port"]}
           for k, d in ms.status().items()}
    gw = ms.gateway_running(timeout=3)
    vio = vp.violations()
    return {
        "profile": {"name": prof, "label": vp.PROFILES[prof]["label"],
                    "asr_device": vp.PROFILES[prof]["asr_device"],
                    "note": vp.PROFILES[prof]["note"]},
        "vram": {"used": v["used_mb"], "total": v["total_mb"],
                 "free": v["free_mb"], "source": v["source"]},
        "services": svc,
        "gateway": {"online": gw["online"],
                    "models": [m.get("model") for m in gw["models"]]},
        "violations": [{"name": n, "why": w} for n, w in vio],
        "memory": mem.stats(),
    }


def _state_refresher():
    while True:
        t0 = time.time()
        try:
            data = snapshot()
            with _STATE_LOCK:
                _STATE_CACHE["data"] = data
                _STATE_CACHE["at"] = time.time()
        except Exception as e:
            emit("error", "状态刷新异常: %r" % (e,))
        # 刷新耗时本身就 ~8s，sleep 到下一个 3s 边界
        time.sleep(max(1.0, 3.0 - (time.time() - t0)))


def cached_state():
    with _STATE_LOCK:
        if _STATE_CACHE["data"] is None:
            return {"note": "状态首次刷新中（~10s）", "refreshing": True}
        return _STATE_CACHE["data"]


# ---------------------------------------------------------------- 自检
def selftest_async():
    def work():
        emit("info", "重跑自检中（selfcheck.py）...")
        try:
            p = subprocess.run(
                [sys.executable, "-X", "utf8",
                 os.path.join(HERE, "selfcheck.py")],
                capture_output=True, text=True, timeout=180, cwd=HERE)
            ok = p.returncode == 0
            emit("selftest", ("自检完成（%s）。刷新自检卡片查看报告"
                              % ("0 FAIL" if ok else "有 FAIL")))
        except Exception as e:
            emit("error", "自检执行失败: %r" % (e,))
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默 access log，事件流只发有意义的

    # ---------- 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---------- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(),
                               "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, {"error": "index.html 缺失: %r" % e})
        elif path == "/api/state":
            self._send(200, cached_state())
        elif path == "/api/selftest":
            try:
                with open(os.path.join(HERE, "state", "selftest_report.json"),
                          "r", encoding="utf-8") as f:
                    self._send(200, json.load(f))
            except Exception:
                self._send(200, {"items": [], "note": "尚无报告，点「重跑自检」"})
        elif path == "/api/events":
            self._sse()
        elif path == "/api/memory":
            self._memory_get()
        elif path == "/api/config":
            self._send(200, {
                "items": cs.describe(),
                "groups": cs.grouped(),
                "restart_keys": list(cs.RESTART_KEYS),
                "hot_keys": list(cs.HOT_KEYS),
                "config_path": cs.CONFIG_PATH,
                "note": "只列出可改白名单项；correction_dict / asr_vocab 等"
                        "受保护项需手工编辑 config.json",
            })
        else:
            self._send(404, {"error": "not found"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        w = self.wfile
        w.write(b"retry: 3000\n\n")
        # 回放最近 30 条
        with _EV_LOCK:
            backlog = list(_EVENTS[-30:])
            _CLIENTS.add(w)
        for ts, kind, text in backlog:
            w.write(("data: %s\n\n" % json.dumps(
                {"ts": ts, "kind": kind, "text": text},
                ensure_ascii=False)).encode("utf-8"))
        w.flush()
        try:
            while True:
                time.sleep(15)
                w.write(b": keepalive\n\n")   # 注释心跳防代理断连
                w.flush()
        except Exception:
            pass
        finally:
            with _EV_LOCK:
                _CLIENTS.discard(w)

    def _memory_get(self):
        qs = dict(kv.split("=", 1) for kv in self.path.split("?")[1].split("&")
                  if "=" in kv) if "?" in self.path else {}
        import urllib.parse
        mode = qs.get("mode", "stats")
        if mode == "recent":
            rows = mem.recent(int(qs.get("n", 10)))
            out = [{"id": r[0], "kind": r[1], "text": r[2],
                    "created": r[3], "hits": r[4], "no_vec": r[5]}
                   for r in rows]
            self._send(200, {"mode": mode, "items": out})
        elif mode == "recall":
            hits = mem.recall(urllib.parse.unquote(qs.get("q", "")),
                              int(qs.get("k", 4)))
            self._send(200, {"mode": mode, "hits": [
                {"id": h[0], "text": h[1], "kind": h[2], "score": h[3]}
                for h in hits]})
        else:
            self._send(200, {"mode": "stats", "stats": mem.stats()})

    # ---------- POST
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._json_body()
            if path == "/api/profile":
                name = body.get("name", "")
                ok, actions = vp.apply(name, quiet=True)
                for a in actions:
                    emit("profile", a)
                # 状态由后台线程几秒内自动刷新，这里不现算（~8s 太慢）
                self._send(200, {"ok": ok, "actions": actions,
                                 "note": "状态几秒内自动刷新",
                                 "state": cached_state()})
            elif path == "/api/selftest":
                selftest_async()
                self._send(200, {"ok": True, "note": "后台执行中，事件流看进度"})
            elif path == "/api/memory":
                mid, msg = mem.remember(body.get("text", ""),
                                        body.get("kind", "fact"),
                                        body.get("source", "console"))
                emit("memory", msg)
                self._send(200, {"ok": mid is not None, "id": mid, "msg": msg})
            elif path == "/api/memory/forget":
                n, msg = mem.forget(body.get("target", ""))
                emit("memory", msg)
                self._send(200, {"ok": n > 0, "deleted": n, "msg": msg})
            elif path == "/api/command":
                res = run_command(body.get("text", ""))
                self._send(200, res)
            elif path == "/api/event":
                # voice_assistant 主进程上报对话事件（全双工 chat/打断/记忆）
                emit(body.get("kind", "info"), body.get("text", ""))
                self._send(200, {"ok": True})
            elif path == "/api/interrupt":
                # 手动打断: 写旗标文件, voice_assistant 的 _stop_flag 0.1s 内感知
                flag = os.path.join(HERE, "state", "interrupt.flag")
                with open(flag, "w") as f:
                    f.write("1")
                emit("interrupt", "⏹ 手动打断（按钮）")
                self._send(200, {"ok": True})
            elif path == "/api/config":
                # body 就是补丁本身，例如 {"decision_engine":"auto",
                # "hangover_s":0.8}；也接受 {"patch":{...}} 形式。
                patch = body.get("patch") if isinstance(body.get("patch"), dict) \
                    else body
                res = apply_config(patch)
                res["items"] = cs.describe()      # 回传新值，前端直接重渲染
                self._send(200 if res.get("ok") else 400, res)
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            emit("error", "API 异常 %s: %r" % (path, e))
            self._send(500, {"error": repr(e)})


def main():
    os.makedirs(os.path.join(HERE, "state"), exist_ok=True)
    threading.Thread(target=_state_refresher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    emit("info", "控制台启动 :%d" % PORT)
    print("voice-assistant 控制台  http://127.0.0.1:%d   (Ctrl+C 停止)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
