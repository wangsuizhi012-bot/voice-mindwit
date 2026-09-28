"""决策引擎开关: 原模型(llama-swap 网关 LLM) 还是 Laya(本地决策模型)。

用法(voice_assistant.py 里):
    import decision
    decision.bind(CONFIG, llm_intent)
    intent = decision.intent(text)          # 按 config.decision_engine 自动分发
    decision.set_engine("laya")             # 切换 + 落盘 config.json

config.json 新增键:
    "decision_engine": "llm"      # llm | laya | auto
    "laya_base": "http://127.0.0.1:8801"
    "laya_min_conf": 0.70         # 低于该置信度则(auto/laya 模式)回退 LLM
    "laya_fallback_llm": true
"""
import json
import os
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

ENGINES = ("llm", "laya", "auto")

_state = {"cfg": {}, "llm_fn": None, "last_engine": None}


def bind(cfg, llm_fn):
    _state["cfg"] = cfg
    _state["llm_fn"] = llm_fn


def engine():
    return str(_state["cfg"].get("decision_engine", "llm") or "llm").lower()


def set_engine(name, persist=True):
    """切换引擎并写回 config.json。返回 (ok, msg)。"""
    name = str(name or "").lower()
    if name not in ENGINES:
        return False, "未知引擎: %s (可选 %s)" % (name, "/".join(ENGINES))
    _state["cfg"]["decision_engine"] = name
    if persist:
        try:
            data = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["decision_engine"] = name
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return True, "已切到 %s(写盘失败: %r)" % (name, e)
    return True, "决策引擎 = %s" % name


def health():
    """探测 Laya 服务是否在跑。返回 (ok, info)。"""
    base = _state["cfg"].get("laya_base", "http://127.0.0.1:8801")
    try:
        r = requests.get(base + "/health", timeout=2)
        j = r.json()
        return bool(j.get("ready")), j
    except Exception as e:
        return False, {"error": repr(e), "base": base}


def _laya_intent(text):
    base = _state["cfg"].get("laya_base", "http://127.0.0.1:8801")
    t0 = time.time()
    try:
        r = requests.post(base + "/v1/intent", json={"text": text}, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:
        return None
    d.setdefault("params", {})
    d.setdefault("reply", "")
    d["_ms"] = int((time.time() - t0) * 1000)
    return d


def _llm(text):
    fn = _state["llm_fn"]
    d = fn(text) if fn else {"action": "none", "params": {}, "reply": ""}
    d = dict(d or {})
    d["engine"] = "llm"
    return d


def intent(text):
    """按当前引擎产出意图; 返回 dict(action/params/reply/engine/confidence)。"""
    eng = engine()
    min_conf = float(_state["cfg"].get("laya_min_conf", 0.70) or 0.70)
    fallback = bool(_state["cfg"].get("laya_fallback_llm", True))

    if eng == "llm":
        _state["last_engine"] = "llm"
        return _llm(text)

    laya = _laya_intent(text) if eng in ("laya", "auto") else None
    if laya is not None and eng == "laya" and not fallback:
        _state["last_engine"] = "laya"
        return laya
    if laya is not None and float(laya.get("confidence", 0) or 0) >= min_conf:
        _state["last_engine"] = "laya"
        return laya
    # 服务没起 / 置信度不够 / auto 模式兜底
    d = _llm(text)
    d["_laya"] = laya            # 留证据: Laya 当时给了什么
    d["_fallback"] = True
    _state["last_engine"] = "llm"
    return d


# ---- 语音切换口令 ----
SWITCH_WORDS = {
    "laya": ("用laya", "用laya模型", "切换到laya", "换成laya", "用决策模型",
             "用拉雅", "启用laya", "用莱雅"),
    "llm": ("用原来的模型", "用原模型", "换回原模型", "用回原来的模型", "用大模型",
            "切回原模型", "用原来的", "用本地大模型"),
    "auto": ("自动选择模型", "自动模式", "用自动模式"),
}


def match_switch(text):
    """识别『用 laya / 用原模型 / 自动模式』这类口令, 返回引擎名或 None。"""
    t = (text or "").replace(" ", "").lower()
    for eng, words in SWITCH_WORDS.items():
        for w in words:
            if w in t:
                return eng
    return None
