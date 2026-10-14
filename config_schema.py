# -*- coding: utf-8 -*-
"""配置白名单 —— 浏览器仪表盘可改项的**唯一权威**。

为什么要这一层（而不是让仪表盘随便改 config.json）:
  1. config.json 里有 correction_dict / asr_vocab / trainer 这类
     **不该被随手改**的结构（改坏了会静默降低识别率，难排查）。
     白名单之外一律拒绝，仪表盘永远碰不到它们。
  2. 每个可改项都要知道 **类型/范围/默认值/是否需要重启**，
     否则用户改完不知道为什么不生效 —— 这正是「拒绝黑箱」的要求。
  3. 校验集中在这里，console_server 只负责传话，不重复实现规则。

"hot" 的含义（重要，别误解）:
  主程序在**调用时**读 CONFIG，所以 hot=True 的项改完仅对该进程**后续**的
  行为生效 —— 例如 decision_engine 在下一个指令就按新值走，不需要重启。
  但主程序内存里的 CONFIG 不会自动重读文件，**仪表盘若同时开着设置面板，
  显示的是文件里的新值，而主程序用的是启动时读入的旧值**（对 hot 项，
  两者行为一致，因为项本身没被启动快照影响）。restart=True 的项
  （模型/后端/热键/静音窗口）则必须重启主程序才生效。

用法:
    from config_schema import SCHEMA, editable, validate, hot_keys, restart_keys
    cur = load_current()                  # config.json 现值
    rows = describe(cur)                  # 给前端渲染的完整清单
    ok, errs = validate({"decision_engine": "auto"})
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# --------------------------------------------------------------- 白名单定义
# key: (类型, 中文标签, 默认值, 说明, 是否需重启, 额外约束)
#   类型: bool | int | float | str | choice
#   额外约束: choice -> tuple(可选值)
#             int/float -> (min, max)
SCHEMA = {
    # ---- 决策引擎（"轻量版不思考、判断快"这条主线）----
    "decision_engine": {
        "type": "choice", "label": "决策引擎", "default": "llm",
        "choices": ("llm", "laya", "auto"), "restart": False,
        "help": "llm=大模型判断(慢但最准)；laya=本地决策模型(30-100ms)；"
                "auto=laya 优先、置信度不足自动回退大模型（推荐）",
    },
    "laya_base": {
        "type": "str", "label": "Laya 服务地址", "default": "http://127.0.0.1:8801",
        "restart": False,
        "help": "laya_serve.py 的地址；服务没起时自动回退大模型，不会报错",
    },
    "laya_min_conf": {
        "type": "float", "label": "Laya 置信度门槛", "default": 0.70,
        "min": 0.0, "max": 1.0, "restart": False,
        "help": "低于该值回退大模型（实测『把音量调大一点』只有 0.57）",
    },
    "laya_fallback_llm": {
        "type": "bool", "label": "Laya 低置信度回退大模型", "default": True,
        "restart": False,
        "help": "关掉=纯 Laya，更快但低置信度指令会被判为闲聊(none)",
    },

    # ---- 语音端点 / 打断 ----
    "hangover_s": {
        "type": "float", "label": "静音判句尾时长(秒)", "default": 0.8,
        "min": 0.2, "max": 3.0, "restart": False,
        "help": "越大越不容易把没说完的话截断；Whispera 实测用 0.832s",
    },
    "cooldown_s": {
        "type": "float", "label": "两句话最短间隔(秒)", "default": 1.0,
        "min": 0.0, "max": 5.0, "restart": False,
        "help": "小于该间隔的语音段会被丢弃，用于防重复触发",
    },
    "bargein_rms": {
        "type": "float", "label": "打断灵敏度(RMS)", "default": 0.02,
        "min": 0.005, "max": 0.2, "restart": False,
        "help": "外放时误触发就调高(0.03)；插话没反应就调低(0.015)。戴耳机无此问题",
    },
    "vad_engine": {
        "type": "choice", "label": "VAD 引擎", "default": "silero",
        "choices": ("silero", "webrtcvad"), "restart": True,
        "help": "silero 更抗噪声（推荐）；webrtcvad 无需 torch",
    },
    "vad_aggressiveness": {
        "type": "int", "label": "webrtcvad 激进程度", "default": 3,
        "min": 0, "max": 3, "restart": True,
        "help": "仅 vad_engine=webrtcvad 时有效；越高越激进（越容易判定为人声）",
    },

    # ---- ASR / 模型 ----
    "asr_mode": {
        "type": "choice", "label": "ASR 后端", "default": "local",
        "choices": ("local", "server", "r2t2"), "restart": True,
        "help": "local=进程内 SenseVoice(默认)；server=外部 funasr-server；r2t2=专用后端",
    },
    "asr_model": {
        "type": "str", "label": "ASR 模型", "default": "iic/SenseVoiceSmall",
        "restart": True,
        "help": "改这里会换识别模型，需重启；换 Paraformer 更准但更慢",
    },
    "asr_polish": {
        "type": "bool", "label": "ASR 结果二次校对", "default": True,
        "restart": False, "help": "用大模型润色识别结果，会更准但增加延迟",
    },
    "strip_fillers": {
        "type": "bool", "label": "过滤语气词", "default": True,
        "restart": False, "help": "去掉『嗯/啊/呃』这类口头语",
    },
    "auto_space": {
        "type": "bool", "label": "中英之间自动加空格", "default": True,
        "restart": False, "help": "听写输出时在中文与英文之间补空格",
    },

    # ---- 听写热键（"流式听写"这条主线）----
    "dictation_enabled": {
        "type": "bool", "label": "启用按键听写", "default": True,
        "restart": True, "help": "关掉则不注册热键，也不会有听写小窗",
    },
    "dictation_hotkey": {
        "type": "str", "label": "听写热键", "default": "ctrl+alt+space",
        "restart": True, "help": "形如 ctrl+alt+space；改完需重启",
    },
    "dictation_hotkey_mode": {
        "type": "choice", "label": "热键模式", "default": "auto",
        "choices": ("auto", "hold", "tap"), "restart": True,
        "help": "auto=长按说话/短按结束；hold=必须按住；tap=按一下开始再按一下结束",
    },
    "dictation_output": {
        "type": "choice", "label": "听写输出方式", "default": "paste",
        "choices": ("paste", "type"), "restart": False,
        "help": "paste=粘贴(快，会临时占用剪贴板)；type=逐字敲(慢但不碰剪贴板)",
    },
    "dictation_restore_clipboard": {
        "type": "bool", "label": "粘贴后恢复剪贴板", "default": True,
        "restart": False, "help": "仅 output=paste 时有效",
    },
    "dictation_partial": {
        "type": "bool", "label": "实时逐字显示(流式观感)", "default": True,
        "restart": False,
        "help": "录音中每隔一会儿重识别一次并逐字显示新增内容，观感接近实时字幕",
    },
    "dictation_partial_interval_s": {
        "type": "float", "label": "逐字刷新间隔(秒)", "default": 0.7,
        "min": 0.2, "max": 3.0, "restart": False,
        "help": "越小越流畅，但 CPU 占用越高（每次都要跑一遍 ASR）",
    },
    "dictation_max_seconds": {
        "type": "int", "label": "单次听写最长(秒)", "default": 60,
        "min": 5, "max": 600, "restart": False,
        "help": "超时自动结束并提交",
    },
    "dictation_ui": {
        "type": "bool", "label": "显示听写小窗", "default": True,
        "restart": False, "help": "屏幕底部的听写指示条",
    },
    "dictation_console": {
        "type": "bool", "label": "听写同时打印到终端", "default": True,
        "restart": False, "help": "关掉则终端更干净",
    },

    # ---- 对话 / TTS ----
    "chat_enabled": {
        "type": "bool", "label": "启用闲聊", "default": True,
        "restart": False,
        "help": "关掉则非指令语音不再进入对话，纯当指令工具用",
    },
    "tts": {
        "type": "bool", "label": "语音播报", "default": True,
        "restart": False, "help": "关掉就只在小窗/终端显示文字，不发声",
    },
    "model": {
        "type": "str", "label": "对话模型别名", "default": "spark",
        "restart": False,
        "help": "llama-swap 里的别名：spark(4B,快) / ornith(9B,质量高)",
    },
    "llm_base": {
        "type": "str", "label": "LLM 网关地址", "default": "http://localhost:9292/v1",
        "restart": False, "help": "llama-swap 网关；必须先启动网关",
    },

    # ---- 小窗 ----
    "overlay": {
        "type": "bool", "label": "显示置顶小窗", "default": True,
        "restart": True, "help": "右上角常驻置顶状态窗",
    },
}

# 需要重启才生效 / 改完即生效
RESTART_KEYS = tuple(k for k, v in SCHEMA.items() if v["restart"])
HOT_KEYS = tuple(k for k, v in SCHEMA.items() if not v["restart"])


def editable():
    """白名单键（有序，按 SCHEMA 定义顺序）。"""
    return tuple(SCHEMA.keys())


def is_editable(key):
    return key in SCHEMA


def load_current():
    """读 config.json 现值。文件坏/缺失都返回 {}，绝不抛。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _coerce(spec, raw):
    """把前端传来的值转成目标类型。返回 (ok, value_or_error)。"""
    t = spec["type"]
    try:
        if t == "bool":
            if isinstance(raw, bool):
                return True, raw
            if isinstance(raw, str):
                return True, raw.strip().lower() in ("1", "true", "yes", "on", "是")
            return True, bool(raw)
        if t == "int":
            v = int(float(raw))
        elif t == "float":
            v = float(raw)
        elif t == "choice":
            v = str(raw).strip()
            if v not in spec["choices"]:
                return False, "取值必须是 %s 之一，收到 %r" % (
                    "/".join(spec["choices"]), raw)
            return True, v
        else:  # str
            v = str(raw).strip()
            if not v:
                return False, "不能为空"
            return True, v
    except (TypeError, ValueError):
        return False, "需要 %s 类型，收到 %r" % (t, raw)
    # 数值范围
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        return False, "不能小于 %s" % lo
    if hi is not None and v > hi:
        return False, "不能大于 %s" % hi
    return True, v


def validate(patch):
    """校验一份补丁（部分键更新）。

    返回 (ok, cleaned, errors)。
      cleaned: {key: 转换后的值}，只含通过校验的键
      errors : {key: 原因}，只含失败的键
    白名单外的键 -> 直接进 errors，不静默丢弃。
    """
    if not isinstance(patch, dict):
        return False, {}, {"_": "请求体必须是 JSON 对象"}
    cleaned, errors = {}, {}
    for k, raw in patch.items():
        spec = SCHEMA.get(k)
        if spec is None:
            errors[k] = "不在可改白名单内（受保护，请手工编辑 config.json）"
            continue
        ok, val = _coerce(spec, raw)
        if ok:
            cleaned[k] = val
        else:
            errors[k] = val
    return (not errors), cleaned, errors


def describe(current=None):
    """给前端渲染的完整清单（含当前值、默认值、是否需重启）。"""
    cur = load_current() if current is None else current
    out = []
    for k, spec in SCHEMA.items():
        has = k in cur
        out.append({
            "key": k,
            "label": spec["label"],
            "type": spec["type"],
            "choices": list(spec.get("choices", [])),
            "min": spec.get("min"),
            "max": spec.get("max"),
            "default": spec["default"],
            "value": cur.get(k, spec["default"]),
            "present": has,          # False = config.json 里没写，走内置默认
            "restart": bool(spec["restart"]),
            "help": spec.get("help", ""),
        })
    return out


def grouped():
    """按用途分组，供前端分区渲染。"""
    return {
        "决策引擎": ["decision_engine", "laya_base", "laya_min_conf",
                     "laya_fallback_llm"],
        "语音端点与打断": ["hangover_s", "cooldown_s", "bargein_rms",
                           "vad_engine", "vad_aggressiveness"],
        "识别与模型": ["asr_mode", "asr_model", "asr_polish", "strip_fillers",
                       "auto_space", "model", "llm_base"],
        "按键听写": ["dictation_enabled", "dictation_hotkey",
                     "dictation_hotkey_mode", "dictation_output",
                     "dictation_restore_clipboard", "dictation_partial",
                     "dictation_partial_interval_s", "dictation_max_seconds",
                     "dictation_ui", "dictation_console"],
        "对话与播报": ["chat_enabled", "tts", "overlay"],
    }


if __name__ == "__main__":
    # 自测: python config_schema.py
    print("editable keys =", len(SCHEMA))
    print("need restart  =", RESTART_KEYS)
    print("hot           =", HOT_KEYS)
    print("--- 校验用例 ---")
    cases = [
        ({"decision_engine": "auto"}, "合法选项"),
        ({"decision_engine": "bogus"}, "非法选项"),
        ({"hangover_s": 0.8}, "合法数值"),
        ({"hangover_s": 99}, "超范围"),
        ({"hangover_s": "abc"}, "类型错误"),
        ({"correction_dict": {"a": "b"}}, "白名单外(必须拒绝)"),
        ({"tts": "false"}, "字符串布尔"),
    ]
    for patch, note in cases:
        ok, cleaned, errs = validate(patch)
        print("  %-22s ok=%-5s cleaned=%s errors=%s"
              % (note, ok, cleaned, errs or ""))
    print("--- 当前值抽样 ---")
    for r in describe():
        if r["key"] in ("decision_engine", "hangover_s", "model",
                        "dictation_partial", "bargein_rms"):
            print("  %-24s value=%-8s default=%-8s restart=%s present=%s"
                  % (r["key"], r["value"], r["default"], r["restart"],
                     r["present"]))
