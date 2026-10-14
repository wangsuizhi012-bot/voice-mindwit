# -*- coding: utf-8 -*-
"""显存档位 —— 8G 单卡上「谁有权用 GPU」的一键切换层。

定位（先读，别重复造轮子）：
    底层调度（显存查询/租约/网关预检/TTL）全部在 model_sched.py，本模块只做
    一层**意图声明**：把「我现在要干什么」翻译成「哪些服务该在卡上」。

    8G 卡的真实约束（实测值见 model_sched.py 头注释）：
        SenseVoice@cuda ~1.1G + 轻LLM(网关) ~5.6G + 桌面合成余量 → chat 档 ~6.6G
        vl 7936MB / uitars 6451MB → heavy 组内互斥，永远错峰
        ComfyUI 生图/视频要独占 → gpu_free 档一键清卡

档位：
    chat      日常语音对话：ASR@cuda + 网关轻LLM；VL/UI-TARS 驱逐
    rpa       桌面 RPA：允许 VL/UI-TARS 按需上卡（heavy 组内仍互斥）
    gpu_free  GPU 全让：ASR 转 CPU + 网关卸载全部 + heavy 全释放（生图/视频前）

ASR 设备切换说明：
    SenseVoice 在 voice_assistant 进程内加载，热切设备需要进程内配合。
    本模块把目标设备写进 state（asr_device 字段），voice_assistant 侧读它
    并在空闲时重载 ASR 后端；本模块只负责状态声明 + 显存侧动作。

用法:
    python vram_profile.py                # 当前档位 + 全景状态 + 一致性校验
    python vram_profile.py list           # 档位清单
    python vram_profile.py set gpu_free   # 切换并立即应用（清卡）
    python vram_profile.py check          # 只校验不改动
"""
import json
import os
import sys
import time

import model_sched as ms

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state", "vram_profile.json")
DEFAULT_PROFILE = "chat"

# ---------------------------------------------------------------- 档位注册表
# allow     = 允许在卡上的 model_sched 服务名（embedding 是 CPU 组，恒可）
# asr_device = SenseVoice 目标设备（写 state，voice_assistant 空闲时应用）
# gateway   = 是否允许 llama-swap 加载模型
PROFILES = {
    "chat": {
        "label": "语音对话（日常默认）",
        "asr_device": "cuda",
        "allow": {"embedding"},
        "gateway": True,
        "note": "ASR@cuda + 轻LLM ~6.6G；VL/UI-TARS 驱逐（要用视觉再切 rpa）",
    },
    "rpa": {
        "label": "桌面 RPA（视觉工作）",
        "asr_device": "cuda",
        "allow": {"embedding", "vl", "uitars"},
        "gateway": True,
        "note": "允许 VL/UI-TARS 按需上卡；heavy 组内互斥由 model_sched 保证",
    },
    "gpu_free": {
        "label": "GPU 全让（生图/视频前）",
        "asr_device": "cpu",
        "allow": {"embedding"},
        "gateway": False,
        "note": "ASR 转 CPU + 网关卸载全部 + heavy 全释放；ComfyUI 独占 ~7.6G",
    },
}


# ---------------------------------------------------------------- 状态读写
def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(d):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def current():
    """当前档位名（state 里的，默认 chat）。"""
    name = _load_state().get("profile", DEFAULT_PROFILE)
    return name if name in PROFILES else DEFAULT_PROFILE


def asr_device_target():
    """voice_assistant 应用的 ASR 设备（跟随档位）。"""
    return PROFILES[current()]["asr_device"]


def gateway_allowed():
    return PROFILES[current()]["gateway"]


# ---------------------------------------------------------------- 校验
def violations():
    """当前在卡但违反档位的服务列表。返回 [(name, reason)]"""
    prof = PROFILES[current()]
    st = ms.status()
    out = []
    for name, d in st.items():
        if not d["up"] or name in prof["allow"]:
            continue
        if ms.SERVERS[name]["group"] == "cpu":
            continue  # CPU 服务不占显存，不违反任何档位
        kind = "外部服务（调度器管不到）" if d["external"] else "调度器托管"
        out.append((name, "%s在卡，档位 %s 不允许（%s）" % (name, current(), kind)))
    gw = ms.gateway_running(timeout=4)
    if gw["online"] and gw["models"] and not prof["gateway"]:
        models = ", ".join(m.get("model", "?") for m in gw["models"])
        out.append(("gateway", "网关已加载 [%s]，档位 %s 禁止网关占卡" % (models, current())))
    return out


# ---------------------------------------------------------------- 应用
def apply(name, quiet=False):
    """切换到档位 name 并执行显存侧动作。返回 (ok, actions)。"""
    if name not in PROFILES:
        return False, ["未知档位: %s（可选: %s）" % (name, "/".join(PROFILES))]
    prof = PROFILES[name]
    actions = []

    # 1) 落盘意图（voice_assistant 空闲时读 asr_device 并重载）
    st = _load_state()
    st["profile"] = name
    st["asr_device"] = prof["asr_device"]
    st["set_at"] = time.time()
    _save_state(st)
    actions.append("档位声明 -> %s（ASR 目标设备 %s）" % (name, prof["asr_device"]))

    # 2) 驱逐档外托管服务（外部服务只警告 —— 绝不越权）
    svc = ms.status()
    for sname, d in svc.items():
        if not d["up"] or sname in prof["allow"]:
            continue
        if ms.SERVERS[sname]["group"] == "cpu":
            continue
        if d["external"]:
            actions.append("⚠ %s 是外部服务（PID %s），档位不允许但我不动它，"
                           "请自行关闭" % (sname, d["pid"]))
        else:
            ok, msg = ms.release(sname, quiet=True)
            actions.append("%s 驱逐 %s（%s）" % ("OK" if ok else "!!", sname, msg))

    # 3) gpu_free：网关卸载全部模型（llama-swap 自己的模型它自己收）
    if not prof["gateway"]:
        gw = ms.gateway_running(timeout=4)
        if gw["online"] and gw["models"]:
            try:
                s = ms._requests_session()
                r = s.post(ms.GATEWAY_URL + "/api/models/unload", timeout=60)
                actions.append("OK 网关卸载全部模型 -> HTTP %d" % r.status_code)
            except Exception as e:
                actions.append("!! 网关卸载失败: %r" % (e,))
        time.sleep(2)

    # 4) 复查显存
    v = ms.vram()
    if v["free_mb"] is not None:
        actions.append("显存空闲 %.0f / %.0f MB" % (v["free_mb"], v["total_mb"] or 0))
    if not quiet:
        for a in actions:
            print("  " + a)
    return True, actions


# ---------------------------------------------------------------- 展示
def status_table():
    cur = current()
    prof = PROFILES[cur]
    print("=" * 74)
    print("显存档位  当前: %s  %s" % (cur, prof["label"]))
    print("  %s" % prof["note"])
    print("=" * 74)
    v = ms.vram(by_process=True)
    if v["total_mb"]:
        print("显存: %.0f/%.0f MB  空闲 %.0f MB（源 %s）"
              % (v["used_mb"], v["total_mb"], v["free_mb"], v["source"]))
        for p in v["processes"][:6]:
            print("   pid %-7s %-26s %7.0f MB" % (p["pid"], p["name"][:24], p["mb"]))
    st = ms.status()
    print("服务:")
    for sname, d in st.items():
        flag = "在卡 " if d["up"] else "离线 "
        cpu_group = ms.SERVERS[sname]["group"] == "cpu"
        bad = d["up"] and not (sname in prof["allow"] or cpu_group)
        ok = "⚠ " if bad else "  "
        print("  %s%s %-9s :%-5d %s" % (ok, flag, sname,
                                        d["spec"]["port"],
                                        "外部" if d["external"] else
                                        ("托管" if d["managed"] else "-")))
    gw = ms.gateway_running(timeout=4)
    if gw["online"]:
        cur_m = [m.get("model") for m in gw["models"]]
        print("网关 :9292 在线  已加载: %s" % (", ".join(cur_m) if cur_m else "（无）"))
    else:
        print("网关 :9292 离线")
    vio = violations()
    if vio:
        print("违反档位:")
        for nm, why in vio:
            print("  ⚠ %-9s %s" % (nm, why))
    else:
        print("一致性: 当前状态符合档位 %s" % cur)


def list_profiles():
    print("可用档位:")
    for name, p in PROFILES.items():
        mark = " <- 当前" if name == current() else ""
        print("  %-9s %s%s" % (name, p["label"], mark))
        print("            %s" % p["note"])


# ---------------------------------------------------------------- CLI
def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "list":
        list_profiles()
    elif cmd == "status":
        status_table()
    elif cmd == "check":
        vio = violations()
        if vio:
            for nm, why in vio:
                print("⚠ %s: %s" % (nm, why))
            sys.exit(1)
        print("OK 状态符合档位 %s" % current())
    elif cmd == "set":
        if len(argv) < 2:
            print("用法: python vram_profile.py set chat|rpa|gpu_free")
            sys.exit(2)
        ok, _ = apply(argv[1])
        print()
        status_table()
        sys.exit(0 if ok else 1)
    else:
        print("未知命令 %r；可用: list / status / check / set <name>" % cmd)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
