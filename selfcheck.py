# -*- coding: utf-8 -*-
"""voice-assistant 全链路自检 —— 启动前体检 + re-scan 复查。

定位：
    model_sched.selftest() 只查「调度器视角」（显存/服务/守卫公式）。
    本模块做**整机体检**：档位一致性 + 模型文件 + Python 依赖 + 麦克风 +
    LLM/网关/embedding 连通 + TTS，全部带判定原因，结果落盘
    state/selftest_report.json（浏览器控制台 P3 直接读）。

设计原则：
    - 只读不写、不拉起服务（纯探测，不破坏现场）
    - 每项独立 try，单点失败不影响其余项
    - 三档结论: OK / WARN(能跑但降级) / FAIL(功能性缺失)

用法:
    python selfcheck.py            # 全量体检（终端表格 + 落盘）
    python selfcheck.py --json     # 只打印 JSON（供控制台/脚本调用）
    python selfcheck.py --quick    # 跳过耗时的依赖导入检查
"""
import importlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import model_sched as ms          # noqa: E402
import vram_profile as vp         # noqa: E402

REPORT_PATH = os.path.join(HERE, "state", "selftest_report.json")
ASR_MODEL_PT = r"E:\AI\modelscope_cache\models\iic--SenseVoiceSmall\snapshots\master\model.pt"
REQUIRED_CONFIG_KEYS = ["llm_base", "asr_mode", "asr_model"]


# ---------------------------------------------------------------- 工具
class Report:
    def __init__(self):
        self.items = []

    def add(self, name, status, detail, ms_used=0.0):
        self.items.append({"name": name, "status": status,
                           "detail": detail, "ms": round(ms_used * 1000)})
        icon = {"OK": "OK ", "WARN": "WARN", "FAIL": "FAIL"}.get(status, "??  ")
        print("[%s] %-14s %s" % (icon, name, detail))

    def finish(self):
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        data = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "profile": vp.current(),
                "items": self.items,
                "fail": sum(1 for i in self.items if i["status"] == "FAIL"),
                "warn": sum(1 for i in self.items if i["status"] == "WARN")}
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("-" * 74)
        print("结论: %d OK / %d WARN / %d FAIL   报告: %s"
              % (len(self.items) - data["fail"] - data["warn"],
                 data["warn"], data["fail"], REPORT_PATH))
        return data


def probe(name, fn):
    """单探测项包装：计时 + 异常兜底。fn() -> (status, detail)"""
    t0 = time.time()
    try:
        status, detail = fn()
    except Exception as e:
        status, detail = "FAIL", "探测异常: %r" % (e,)
    return name, status, detail, time.time() - t0


# ---------------------------------------------------------------- 各项探测
def p_vram():
    v = ms.vram(by_process=True)
    if not v["total_mb"]:
        return "FAIL", "显存读数不可用（DXGI+PDH 都失败），守卫将失效"
    return "OK", "%.0f/%.0f MB 空闲 %.0f MB（%s）" % (
        v["used_mb"], v["total_mb"], v["free_mb"], v["source"])


def p_profile():
    cur = vp.current()
    prof = vp.PROFILES[cur]
    vio = vp.violations()
    if vio:
        return "WARN", "档位 %s 有 %d 处违反: %s" % (
            cur, len(vio), "; ".join("%s(%s)" % (n, w[:40]) for n, w in vio))
    return "OK", "档位 %s（%s）一致，ASR 目标设备 %s" % (
        cur, prof["label"], prof["asr_device"])


def p_gateway():
    gw = ms.gateway_running(timeout=5)
    if not gw["online"]:
        return "WARN", "llama-swap :9292 未启动（LLM 不可用，语音命令降级；启动: E:\\AI\\_scripts\\start-llama-swap.bat）"
    loaded = [m.get("model") for m in gw["models"]]
    return "OK", ":9292 在线，已加载 %s" % (loaded or "无（首请求触发加载）")


def p_llm_api():
    gw = ms.gateway_running(timeout=4)
    if not gw["online"]:
        return "WARN", ":9292 离线（未启动 llama-swap，双击 E:\\AI\\_scripts\\start-llama-swap.bat）"
    try:
        s = ms._requests_session()
        r = s.get(ms.GATEWAY_URL.rstrip("/") + "/v1/models", timeout=5)
        data = r.json().get("data") or []
        ids = [m.get("id") for m in data][:5]
        if not ids:
            return "WARN", ":9292 /v1/models 返回空 —— 检查 llama-swap config.yaml"
        return "OK", "可用模型 %d 个: %s%s" % (
            len(data), ", ".join(ids), " ..." if len(data) > 5 else "")
    except Exception as e:
        return "WARN", "网关在线但 /v1/models 异常: %r" % (e,)


def p_embedding():
    s = ms._requests_session()
    try:
        r = s.get("http://127.0.0.1:1236/v1/models", timeout=4)
        ids = [m.get("id") for m in (r.json().get("data") or [])]
        if not ids:
            return "WARN", ":1236 在线但模型列表空（长期记忆检索会降级）"
        return "OK", "embedding 在线: %s（CPU，长期记忆检索底座）" % ids[0]
    except Exception:
        return "WARN", (":1236 离线 —— 长期记忆检索暂不可用；"
                        "启动: E:\\AI\\_scripts\\start-embedding-server.bat")


def p_vl():
    if vp.current() != "rpa":
        return "OK", "非 rpa 档，VL 不要求在线（TTL 空闲自动释放属正常）"
    if ms.is_up(1235, timeout=2):
        return "OK", "VL :1235 在线（rpa 档视觉判读可用）"
    return "WARN", "rpa 档但 VL :1235 离线 —— 视觉判读不可用（ensure vl 可拉起）"


def p_asr_model():
    if os.path.isfile(ASR_MODEL_PT):
        return "OK", "SenseVoice 缓存在 (%.0f MB)" % (os.path.getsize(ASR_MODEL_PT) / 1048576)
    return "FAIL", "ASR 模型缺失: %s" % ASR_MODEL_PT


def p_deps():
    results = []
    for mod in ("torch", "funasr", "sounddevice", "numpy"):
        try:
            importlib.import_module(mod)
            results.append("%s OK" % mod)
        except Exception as e:
            return "FAIL", "依赖缺失 %s: %r" % (mod, e)
    try:
        import torch
        dev = "cuda:0" if torch.cuda.is_available() else "cpu"
        if dev == "cpu":
            return "WARN", ("依赖齐但 torch 报无 CUDA。本机 nvidia-smi/NVML 已损坏，"
                            "可能是检测层误报 —— 以实际运行时 voice_assistant 的 "
                            "ASR device 输出为准；若确为 CPU 会自动降级不阻塞")
        return "OK", "依赖齐，torch CUDA 可用（%s）" % torch.cuda.get_device_name(0)[:30]
    except Exception:
        return "OK", "依赖齐（torch 版本读取失败，不影响） " + " ".join(results)


def p_mic():
    import sounddevice as sd
    devs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    if not devs:
        return "FAIL", "无输入设备 —— 录音不可用"
    default = sd.default.device[0]
    name = sd.query_devices(default)["name"] if default >= 0 else "(未设默认)"
    return "OK", "%d 个输入设备，默认: %s" % (len(devs), name[:40])


def p_tts():
    import win32com.client
    v = win32com.client.Dispatch("SAPI.SpVoice")
    voices = [v.GetVoices().Item(i).GetDescription() for i in range(v.GetVoices().Count)]
    return "OK", "SAPI 可用，%d 个音色: %s" % (len(voices), voices[0][:30] if voices else "-")


def p_config():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
    if missing:
        return "FAIL", "config.json 缺键: %s" % missing
    n_dict = len(cfg.get("correction_dict") or {})
    return "OK", "配置完整（纠错词典 %d 条, asr=%s/%s）" % (
        n_dict, cfg["asr_mode"], cfg["asr_model"])


def p_state_writable():
    test = os.path.join(HERE, "state", ".selfcheck_probe")
    with open(test, "w") as f:
        f.write("1")
    os.remove(test)
    return "OK", "state/ 可写（调度记录/报告可落盘）"


# ---------------------------------------------------------------- 主流程
def run(quick=False, as_json=False):
    rep = Report()
    checks = [
        ("显存读数", p_vram),
        ("显存档位", p_profile),
        ("llama网关", p_gateway),
        ("LLM接口", p_llm_api),
        ("embedding", p_embedding),
        ("VL服务", p_vl),
        ("ASR模型", p_asr_model),
        ("麦克风", p_mic),
        ("TTS", p_tts),
        ("配置", p_config),
        ("state目录", p_state_writable),
    ]
    if not quick:
        checks.insert(7, ("Python依赖", p_deps))
    for name, fn in checks:
        n, st, d, used = probe(name, fn)
        rep.add(n, st, d, used)
    data = rep.finish()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 1 if data["fail"] else 0


if __name__ == "__main__":
    sys.exit(run(quick="--quick" in sys.argv, as_json="--json" in sys.argv))
