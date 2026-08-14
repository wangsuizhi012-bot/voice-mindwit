# -*- coding: utf-8 -*-
"""语音控制助手 / Voice Control Assistant
监听麦克风(webrtcvad 端点检测) -> SenseVoice 中文转写 -> 本地 Qwen(:1234) 意图理解 -> pyautogui 执行
复用 funasr-test 里已验证的 SenseVoice 模型与麦克风逻辑。

日志实时同时输出到: 控制台(GBK) + run_assistant.log(UTF-8) + 常驻置顶小窗(可选)。
"""
import sys, os, json, time, queue, threading
import requests

# ---- 控制台/文件统一 UTF-8 (Windows 控制台默认即 UTF-8, PEP528) ----
os.environ.setdefault("TQDM_DISABLE", "1")   # 关 FunASR 进度条噪声
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import logging
for _n in ("funasr", "modelscope", "modelscope_hub", "tqdm", "transformers"):
    try:
        logging.getLogger(_n).setLevel(logging.WARNING)
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS_DIR = os.path.join(HERE, "shots")
LOG_PATH = os.path.join(HERE, "run_assistant.log")
os.makedirs(SHOTS_DIR, exist_ok=True)

# ---- 默认配置(可被 config.json 覆盖) ----
CONFIG = {
    "mic_keyword": "",            # 麦克风关键字; 留空=跟随 Windows 默认输入设备
    "llm_base": "http://localhost:1234/v1",
    "screenshot_mode": "active_window",  # active_window | full | region
    "screenshot_region": None,             # [x,y,w,h] 当 mode=region
    "auto_send": False,           # 截图粘贴后是否自动按回车发送(默认关, 安全第一)
    "hangover_s": 0.6,            # 末尾静音多久算一句话说完
    "vad_aggressiveness": 3,
    "cooldown_s": 1.0,            # 两次指令最小间隔
    "confirm_high_risk": False,   # 高风险动作(发送/打字/打开/点击)执行前是否要语音二次确认
    "wake_word": "",              # 唤醒词模型名(openwakeword, 如 hey_jarvis); 留空=持续监听(不安全但省事)
    "vad_engine": "webrtcvad",    # webrtcvad | silero (silero 更鲁棒, 需装 silero-vad)
    "stop_hotkey": "f8",         # 全局热键停止整个程序(防自动化跑飞); 值: f8/f9/f10/f11/esc/f5
    "stop_recording_hotkey": "f9",  # 停止录制宏并保存(不退出程序); 值: f8/f9/f10/f11/esc/f5
    "ocr_fallback": False,       # OCR 兜底(默认关: 边聊天边用会误命中聊天窗口文字; 需要时设 true)
}

def load_config():
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                CONFIG.update(json.load(f))
        except Exception as e:
            print("读取 config.json 失败, 用默认: " + repr(e))

load_config()

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 480
PREROLL_FRAMES = 10

# ---- 日志: 同时打到控制台 + 文件 ----
class _Tee:
    def __init__(self, *streams):
        self.streams = list(streams)
    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass

def setup_logging():
    """日志同时输出到控制台(GBK)与文件(UTF-8)，并捕获未处理异常。"""
    try:
        f = open(LOG_PATH, "w", encoding="utf-8")
    except Exception:
        f = None
    streams = [sys.stdout]
    if f is not None:
        streams.append(f)
    sys.stdout = _Tee(*streams)
    sys.stderr = _Tee(sys.stderr, *( [f] if f else [] ))
    import traceback as _tb
    def _hook(et, ev, tb):
        try:
            sys.stderr.write("未捕获异常:\n" + "".join(_tb.format_exception(et, ev, tb)))
        except Exception:
            pass
    sys.excepthook = _hook
    log("==== 会话开始 " + time.strftime("%Y-%m-%d %H:%M:%S") + " ====")

def log(m):
    print(m, flush=True)

# ---- 常驻置顶小窗(实时显示识别/执行) ----
class Overlay:
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("语音助手")
        self.root.attributes("-topmost", True)
        try:
            self.root.geometry("580x150+12+12")
        except Exception:
            pass
        self.rect = (12, 12, 580, 150)   # 小窗屏幕位置, 供 OCR 排除该区域(不关窗)
        self.var = tk.StringVar(value="监听中…\n（说 退出 停止）")
        self.label = tk.Label(self.root, textvariable=self.var,
                              font=("Microsoft YaHei UI", 12), wraplength=555,
                              justify="left", anchor="nw",
                              bg="#1e1e2e", fg="#e6e6e6")
        self.label.pack(fill="both", expand=True, padx=12, pady=12)
        self.root.update()
    def _apply(self, text):
        try:
            self.var.set(text)
            self.root.update_idletasks()
        except Exception:
            pass
    def set(self, text):
        try:
            self.root.after(0, self._apply, text)
        except Exception:
            pass
    def run(self):
        try:
            self.root.mainloop()
        except Exception:
            pass

OVN = None

# ---- 麦克风选择(复用已验证逻辑) ----
def choose_mic():
    import sounddevice as sd
    devices = sd.query_devices()
    ins = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    chosen = None
    kw = (CONFIG.get("mic_keyword") or "").lower()
    if kw:
        for i, d in ins:
            if kw in (d.get("name") or "").lower():
                chosen = i
                break
    if chosen is None:
        chosen = sd.default.device[0]
    log("可用输入设备: " + str([(i, devices[i].get("name")) for i, _ in ins]))
    log("使用输入设备 [%d] %s" % (chosen, devices[chosen].get("name")))
    return chosen

# ---- ASR (SenseVoice) ----
_asr_model = None
def load_asr():
    global _asr_model
    import torch
    from funasr import AutoModel
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    log("加载 SenseVoice 模型中(首次会从缓存读取) 设备=%s ..." % device)
    _asr_model = AutoModel(model="iic/SenseVoiceSmall", vad_model="fsmn-vad",
                           device=device, disable_update=True)
    log("ASR 模型就绪")

def transcribe(int16_audio):
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    import numpy as np
    a = int16_audio.flatten().astype("float32") / 32768.0
    res = _asr_model.generate(input=a, language="auto", use_itn=True)
    return rich_transcription_postprocess(res[0]["text"])

# ---- LLM 意图 ----
_MODEL_ID = None
def detect_model():
    global _MODEL_ID
    if _MODEL_ID:
        return _MODEL_ID
    try:
        r = requests.get(CONFIG["llm_base"] + "/models", timeout=5)
        j = r.json()
        data = j.get("data") or j.get("models") or []
        if data:
            _MODEL_ID = data[0]["id"]
            log("本地 LLM: " + _MODEL_ID)
            return _MODEL_ID
    except Exception as e:
        log("探测 LLM 模型失败(用占位名): " + repr(e))
    return "local-model"

SYS_PROMPT = """你是一个本地语音助手的中文意图解析器。用户说一句话，判断他想做什么，严格只返回 JSON，不要多余文字。
可用动作:
- "screenshot": 截取当前活动窗口(或全屏)并复制到剪贴板, 不发送。params:{}
- "screenshot_send": 截图并粘贴到当前光标所在输入框然后发送(按回车)。仅当用户明确说"发送/发给我/发到..."时用。params:{}
- "type": 把文字打到当前光标处。params:{"text":"要打的内容"}
- "open": 打开程序。params:{"app":"应用名, 如 微信/记事本/资源管理器"}
- "click_here": 在鼠标当前所在位置点击。**仅当用户只说"点一下/点这里/按一下"这类话、后面没有任何目标文字时**才用。params:{"button":"left","clicks":1}  (button 可 left/right, clicks 可 1 或 2 表示双击)
- "click_target": 点击屏幕上指定的控件(按钮/输入框/菜单项等), 系统会自动在前台窗口里找到它并移动鼠标点击, 用户无需移动鼠标。**只要用户说"点/点击/按"后面跟了具体目标文字(如"点发送""点击一下默认权限""点确定按钮""点搜索"), 就必须用 click_target**, 让系统自己移动鼠标去找。params:{"target":"控件上的文字, 如 发送/确定/设置/搜索","hover":false}  当用户说"悬停/移到/放到 xxx 上"(只移动鼠标不点击)时 hover 填 true
- "click": 点击屏幕精确坐标(仅当用户明确说出坐标数字如"点 100 200"时才用, 否则绝不用)。params:{"x":0,"y":0}
- "press": 按键或热键。单键用 params:{"key":"enter"} (key 可为 enter/space/f5/esc/backspace/delete/home/end/up/down/left/right/tab 等); 组合键用 params:{"keys":["ctrl","s"]} (ctrl/alt/shift + 字母或功能键)。常见: "保存"->ctrl+s, "复制"->ctrl+c, "粘贴"->ctrl+v, "全选"->ctrl+a, "刷新"->f5, "回车/确认/发送"->enter
- "scroll": 滚动鼠标滚轮。params:{"amount":3} 正数向上滚、负数向下滚。当用户说"往下滚/向上滚/往下翻/往上翻/滚动页面/滚轮"时用
- "none": 闲聊或无需操作。params:{}
返回格式: {"action":"...","params":{...},"reply":"一句话回复(中文,可选)"}
如果是命令类(截图/打开/打字/点击/按键/发送)务必返回对应 action；普通聊天返回 none。口语映射: 只"点一下/点这里/按一下"(无目标文字)->click_here; "点/点击 + 具体目标文字"(如"点发送""点击一下默认权限")->click_target; "双击"->click_here{clicks:2}; "右键"->click_here{button:right}; "回车/确认"->press{key:enter}; "保存/复制/粘贴/全选/刷新/撤销/剪切"->对应热键 press。
重要: 用户说"复制/粘贴/剪切/全选/保存/刷新/撤销"指的是键盘快捷键操作, 必须返回 press 动作(对应 ctrl+c / ctrl+v / ctrl+x / ctrl+a / ctrl+s / f5 / ctrl+z), 绝不要返回 type 或 none。用户说"点xxx/点击xxx/点一下xxx/按一下xxx按钮"(带具体目标文字)指的是点击某个界面元素, 必须返回 click_target{target:"xxx"}让鼠标自己移动去找; 只有当用户只说"点一下/点这里/按一下"(完全没有目标文字)时才返回 click_here。"""

def parse_json(text):
    try:
        s = text.find("{")
        e = text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s:e + 1])
    except Exception:
        pass
    return {"action": "none", "params": {}, "reply": ""}

def llm_intent(text):
    model = detect_model()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
    }
    try:
        r = requests.post(CONFIG["llm_base"] + "/chat/completions", json=payload, timeout=30)
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log("LLM 调用失败: " + repr(e))
        return {"action": "none", "params": {}, "reply": ""}
    intent = parse_json(content)
    params = intent.get("params", {}) or {}
    log("意图：" + str(intent.get("action", "?")) + "  " + json.dumps(params, ensure_ascii=False))
    return intent

# ---- 动作执行 ----
def copy_image_to_clipboard(img):
    import win32clipboard
    from io import BytesIO
    buf = BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]   # 去掉 BMP 文件头, 只留 DIB
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()

def copy_text_to_clipboard(t):
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, t)
    finally:
        win32clipboard.CloseClipboard()

def get_shot_region():
    mode = CONFIG.get("screenshot_mode", "active_window")
    if mode == "full":
        return None
    if mode == "region":
        r = CONFIG.get("screenshot_region")
        return tuple(r) if r else None
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        l, t, rr, b = win32gui.GetWindowRect(hwnd)
        return (l, t, rr - l, b - t)
    except Exception:
        return None

def do_screenshot(send):
    import pyautogui
    region = get_shot_region()
    img = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SHOTS_DIR, "shot_" + ts + ".png")
    img.save(path)
    copy_image_to_clipboard(img)
    log("  已截图 -> " + path + " (已复制到剪贴板)")
    if send:
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "v")
        log("  已粘贴到当前输入框")
        time.sleep(0.4)
        pyautogui.press("enter")
        log("  已发送(回车)")

def type_text(t):
    import pyautogui
    if not t:
        return
    copy_text_to_clipboard(t)
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "v")
    log("  已输入: " + t)

APP_MAP = {
    "记事本": "notepad", "计算器": "calc", "画图": "mspaint",
    "资源管理器": "explorer", "浏览器": "explorer", "终端": "cmd", "cmd": "cmd",
}
def open_app(name):
    if not name:
        return
    cmd = APP_MAP.get(name)
    try:
        if cmd:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "", cmd], shell=False)
            log("  已打开: " + name)
        else:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "", name])
            log("  已尝试打开: " + name)
    except Exception as e:
        log("  打开失败: " + repr(e))

def click_at(x, y):
    import pyautogui
    if x is None or y is None:
        return
    pyautogui.click(int(x), int(y))
    log("  已点击 (%s, %s)" % (x, y))

def click_here(button="left", clicks=1):
    """点击鼠标当前所在位置(用户已把鼠标移到目标)。零配置, 最实用。
    若此前某条 click_target 失败进入教学态, 这次手动点击会被记录进记忆库。"""
    import pyautogui
    x, y = pyautogui.position()
    pyautogui.click(x, y, button=button, clicks=clicks)
    log("  已在鼠标当前位置 (%d, %d) %s点击 x%d" % (x, y, button, clicks))
    global _teach_pending
    if _teach_pending is not None:
        tgt = _teach_pending.get("target")
        _teach_pending = None
        if tgt:
            try:
                from locate import save_click, save_template
                save_click(tgt, x, y)
                tpl = save_template(tgt, x, y)
                extra = "，并已存模板" if tpl else ""
                log("  已把'点%s'记进记忆库%s, 下次直接点" % (tgt, extra))
                if OVN:
                    OVN.set("已学会：点%s（下次直接命中）" % tgt)
            except Exception as e:
                log("  记录记忆失败: " + repr(e))

def click_target(target, hover=False):
    """自主定位并点击(或悬停)前台窗口里的目标控件(用户无需移动鼠标)。
    顺序: 控件树 -> 记忆库(学过的相对坐标) -> 模板匹配(教学存的图) -> OCR 兜底。
    hover=True 时只移动鼠标不点击(防误触预览)。全失败则进入'教学'。"""
    import pyautogui
    try:
        from locate import find_control, recall_click, find_by_template, find_by_ocr
    except Exception:
        try:
            import locate as _loc
            find_control, recall_click, find_by_template, find_by_ocr = \
                _loc.find_control, _loc.recall_click, _loc.find_by_template, _loc.find_by_ocr
        except Exception as e:
            log("  定位模块不可用: " + repr(e))
            return False
    # 1) 控件树
    pt, info = find_control(target)
    # 2) 记忆库(从手动点击中学到的相对坐标)
    if pt is None:
        try:
            mpt, minfo = recall_click(target)
            if mpt:
                pt, info = mpt, minfo
        except Exception as e:
            log("  记忆库查询失败: " + repr(e))
    # 3) 模板匹配(按图找, 小窗文字不会误匹配目标小图, 无需藏小窗)
    if pt is None:
        try:
            tpt, tinfo = find_by_template(target)
            if tpt:
                pt, info = tpt, tinfo
            else:
                log("  模板匹配: " + str(tinfo))
        except Exception as e:
            log("  模板匹配出错: " + repr(e))
    # 4) OCR 兜底(默认关闭: 边聊天边用会误命中聊天窗口文字; config.ocr_fallback=true 才启用)
    if pt is None and CONFIG.get("ocr_fallback"):
        excl = None
        if OVN is not None:
            excl = getattr(OVN, "rect", None)
        pt, info = find_by_ocr(target, exclude=excl)
    if pt is None:
        global _teach_pending
        _teach_pending = {"target": target}
        log("  定位失败, 进入教学: 请手动点一下 '%s' 的位置" % target)
        if OVN:
            OVN.set("没找到'%s', 请手动点一下目标位置" % target)
        return False
    x, y = pt
    if hover:
        pyautogui.moveTo(int(x), int(y), duration=0.2)
        log("  已悬停到 (%d, %d): %s" % (x, y, info))
        if OVN:
            OVN.set("听到：移到" + (target or "") + "\n已悬停：" + str(info))
    else:
        pyautogui.click(int(x), int(y))
        log("  已自主定位并点击 (%d, %d): %s" % (x, y, info))
        if OVN:
            OVN.set("听到：点" + (target or "") + "\n已点击：" + str(info))
    return True

def press_key(key=None, keys=None):
    """按单键(如 enter/f5) 或热键(如 ctrl+s)。"""
    import pyautogui
    if keys and len(keys) >= 2:
        pyautogui.hotkey(*keys)
        log("  已按热键: " + "+".join(keys))
    elif key:
        pyautogui.press(str(key))
        log("  已按键: " + str(key))
    else:
        log("  按键参数为空, 跳过")

STOP_WORDS = ["退出", "停止", "别动", "关闭助手", "结束", "quit", "stop"]
CONFIRM_WORDS = ["确认", "确定", "对", "是的", "执行", "好", "yes", "ok"]
CANCEL_WORDS = ["取消", "不", "算了", "别", "no", "cancel"]

# ---- 安全层: 动作白名单 + JSON 校验 + 高风险确认 ----
ALLOWED_ACTIONS = {"screenshot", "screenshot_send", "type", "open",
                   "click", "click_here", "click_target", "press", "scroll", "none"}
HIGH_RISK_ACTIONS = {"screenshot_send", "type", "open", "click", "click_target"}
_pending = {"intent": None}
_teach_pending = None   # 教学态: click_target 失败时, 等用户手动点一下记进记忆库
_recording = None       # 录制宏: 非 None 时表示正在录制, {"name":..., "steps":[...]}
_pending_flow = None    # 重放流程前的整体二次确认, 存步骤列表

def validate_intent(intent):
    """校验 LLM 返回的意图 JSON。返回 (ok, reason)。解析失败/越界一律拒绝执行。"""
    if not isinstance(intent, dict):
        return False, "意图非对象"
    action = intent.get("action")
    if action not in ALLOWED_ACTIONS:
        return False, "动作不在白名单: " + str(action)
    params = intent.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return False, "params 非对象"
    if action == "type" and not isinstance(params.get("text"), str):
        return False, "type 缺 text"
    if action == "open" and not isinstance(params.get("app"), str):
        return False, "open 缺 app"
    if action == "click_target" and not isinstance(params.get("target"), str):
        return False, "click_target 缺 target"
    if action == "scroll":
        try:
            int(params.get("amount"))
        except Exception:
            return False, "scroll 缺 amount"
    if action == "click":
        try:
            int(params.get("x")); int(params.get("y"))
        except Exception:
            return False, "click 坐标非法"
    return True, ""

def _ensure_webrtcvad():
    """setuptools>=81 已删除 pkg_resources, 老 webrtcvad 2.0.x 启动会报 ModuleNotFoundError。
    这里自动把那两行替换成标准库 importlib.metadata, 免联网/免降级。"""
    try:
        import webrtcvad  # noqa
        return
    except Exception as e:
        if "pkg_resources" not in repr(e):
            raise
    # 自动补丁
    import importlib.metadata as _im
    try:
        import webrtcvad as _w
        fp = _w.__file__
    except Exception:
        return
    try:
        with open(fp, "r", encoding="utf-8") as f:
            src = f.read()
        src = src.replace(
            'import pkg_resources\n',
            'try:\n    import importlib.metadata as _md\nexcept Exception:\n    import importlib_metadata as _md\n')
        src = src.replace(
            '__version__ = pkg_resources.get_distribution(\'webrtcvad\').version',
            'try:\n    __version__ = _md.version(\'webrtcvad\')\nexcept Exception:\n    __version__ = \'2.0.10\'')
        with open(fp, "w", encoding="utf-8") as f:
            f.write(src)
        log("  已自动修复 webrtcvad 的 pkg_resources 依赖(setuptools>=81)")
    except Exception as ex:
        log("  webrtcvad 自动修复失败: " + repr(ex))

def execute(intent, raw_text):
    low = (raw_text or "").lower()
    for w in STOP_WORDS:
        if w in low:
            log("收到停止指令, 退出中...")
            if OVN:
                OVN.set("已停止。\n关闭黑窗口或按任意键退出。")
            global RUNNING
            RUNNING = False
            return
    action = intent.get("action")
    params = intent.get("params", {}) or {}
    log("执行：" + str(action))
    if OVN:
        OVN.set("听到：" + (raw_text or "") + "\n执行：" + str(action))
    try:
        if action == "screenshot":
            do_screenshot(send=False)
        elif action == "screenshot_send":
            do_screenshot(send=True)
        elif action == "type":
            type_text(params.get("text", ""))
        elif action == "open":
            open_app(params.get("app", ""))
        elif action == "click":
            click_at(params.get("x"), params.get("y"))
        elif action == "click_here":
            click_here(button=params.get("button", "left"),
                       clicks=int(params.get("clicks", 1)))
        elif action == "click_target":
            click_target(params.get("target", ""), hover=bool(params.get("hover")))
        elif action == "scroll":
            import pyautogui
            amt = int(params.get("amount", 3))
            pyautogui.scroll(amt)
            log("  已滚动滚轮: " + str(amt))
        elif action == "press":
            k = params.get("key")
            ks = params.get("keys")
            if k and not ks and str(k).lower() in ("c", "v", "x", "a", "s", "z"):
                ks = ["ctrl", str(k).lower()]
                k = None
                log("  单字母热键自动升级为 Ctrl+" + str(k).upper())
            press_key(key=k, keys=ks)
        elif action == "none":
            if intent.get("reply"):
                log("助手: " + intent.get("reply"))
                if OVN:
                    OVN.set("助手：" + intent.get("reply"))
        else:
            log("  未知动作: " + str(action))
    except Exception as e:
        log("执行动作出错: " + repr(e))

# ---- 录制宏: 一串语音指令录成命名流程, 一句话重放 ----
def _extract_after(text, kws):
    """提取关键词之后的名字(去标点/空格)。"""
    for kw in kws:
        i = text.find(kw)
        if i >= 0:
            rest = text[i + len(kw):]
            rest = "".join(ch for ch in rest if ch not in "，。,.?!！? 的着吧啊")
            return rest.strip()
    return ""

def _clean_flow_name(name):
    """去掉名字前的前缀词(叫/为/名为/命名为/保存为)。"""
    name = (name or "").strip()
    for pre in ("命名为", "名字叫", "名为", "保存为", "叫", "为"):
        if name.startswith(pre):
            name = name[len(pre):].strip()
            break
    return name

def _handle_macro(text):
    """处理录制宏 meta 指令(开始/停止/重放), 返回 True 表示已吞掉不再走 LLM。"""
    global _recording, _pending_flow
    # 录制中: "停止/结束/保存/完成" 都触发停止保存
    if _recording is not None:
        if any(w in text for w in ("停止", "结束", "保存", "完成")):
            name = _extract_after(text, ("停止", "结束", "保存", "完成"))
            _stop_record(name)
            return True
    # 开始录制: 不在录制中时, 只要含"录制/记录/录屏"即触发
    if _recording is None and any(w in text for w in ("录制", "记录", "录屏")):
        _start_record()
        return True
    # 重放流程
    for kw in ("执行流程", "运行流程", "跑流程", "重放", "执行宏", "运行宏"):
        if kw in text:
            name = _extract_after(text, (kw,))
            _run_flow(name)
            return True
    return False

def _start_record():
    global _recording
    _recording = {"name": "", "steps": []}
    log("  ▶ 开始录制：逐步说你的指令；说「停止」保存")
    if OVN:
        OVN.set("正在录制…\n逐步说指令，说「停止」保存")

def _stop_record(name):
    global _recording
    steps = (_recording or {}).get("steps", []) if _recording else []
    _recording = None
    if not steps:
        log("  录制为空，未保存")
        if OVN:
            OVN.set("录制为空，未保存")
        return
    from macro import save_flow, list_flows
    nm = _clean_flow_name(name) or ("flow_%d" % (len(list_flows()) + 1))
    path = save_flow(nm, steps)
    log("  已保存流程「%s」共 %d 步 -> %s" % (nm, len(steps), path))
    if OVN:
        OVN.set("已保存流程「%s」\n共 %d 步。说「执行流程%s」重放" % (nm, len(steps), nm))

def _run_flow(name):
    global _pending_flow
    from macro import load_flow, list_flows
    name = _clean_flow_name(name)
    if not name:
        flows = list_flows()
        if not flows:
            log("  还没有录制过流程，先说「开始录制」")
            if OVN:
                OVN.set("还没有录制过流程\n先说「开始录制」")
            return
        name = flows[-1]   # 默认最近一个
    data = load_flow(name)
    if not data:
        log("  找不到流程「%s」，已有：%s" % (name, "、".join(list_flows())))
        if OVN:
            OVN.set("找不到流程「%s」\n已有：%s" % (name, "、".join(list_flows())))
        return
    steps = data.get("steps", [])
    _pending_flow = steps
    log("  [待确认] 执行流程「%s」共 %d 步？说 确认 或 取消" % (data.get("name", name), len(steps)))
    if OVN:
        OVN.set("执行流程「%s」共 %d 步？\n说 确认 或 取消" % (data.get("name", name), len(steps)))

def _execute_flow(steps):
    for i, st in enumerate(steps, 1):
        if not RUNNING:
            break
        ok, reason = validate_intent(st)
        if not ok:
            log("  流程第 %d 步校验失败，跳过: %s" % (i, reason))
            continue
        log("  [流程 %d/%d] %s %s" % (i, len(steps), st.get("action"),
                                      json.dumps(st.get("params", {}), ensure_ascii=False)))
        try:
            execute(st, "")
        except Exception as e:
            log("  流程第 %d 步执行出错: %s" % (i, repr(e)))
        time.sleep(0.6)
    log("  流程执行完毕")
    if OVN:
        OVN.set("流程执行完毕")

# ---- 监听 ----
class Listener:
    _last_cmd = 0.0

    def __init__(self, device):
        self.device = device
        self.q = queue.Queue()
        self.running = False
        self.stream = None
        self.preroll = []
        self.seg = []
        self.triggered = False
        self.last_speech = 0.0
        self.seg_start = 0.0
        # ---- VAD 引擎: silero(主, 鲁棒) / webrtcvad(兜底) ----
        self.engine = (CONFIG.get("vad_engine") or "webrtcvad").lower()
        if self.engine == "silero":
            try:
                from silero_vad import load_silero_vad, VADIterator
                self._silero_model = load_silero_vad()
                ms = int(float(CONFIG.get("hangover_s", 0.6)) * 1000)
                self._silero = VADIterator(self._silero_model, threshold=0.5,
                                           sampling_rate=SAMPLE_RATE,
                                           min_silence_duration_ms=ms,
                                           speech_pad_ms=300)
                self.block = 512
                log("VAD 引擎: silero (噪声更鲁棒)")
            except Exception as e:
                log("silero 加载失败, 回退 webrtcvad: " + repr(e))
                self.engine = "webrtcvad"
        if self.engine == "webrtcvad":
            _ensure_webrtcvad()
            import webrtcvad
            self.vad = webrtcvad.Vad(int(CONFIG.get("vad_aggressiveness", 3)))
            self.block = SAMPLE_RATE * 30 // 1000   # 480
            log("VAD 引擎: webrtcvad")
        # ---- 唤醒词(可选): 先喊唤醒词再下命令, 防误触发 ----
        self.wake_model = None
        self._wake_buf = []
        self._armed_until = 0.0
        ww = (CONFIG.get("wake_word") or "").strip()
        if ww:
            try:
                from openwakeword import Model as _OWW
                self.wake_model = _OWW(wakeword_models=[ww], inference_framework="onnx")
                log("唤醒词已启用: %s (喊它后 8 秒内可下命令)" % ww)
            except Exception as e:
                log("唤醒词加载失败, 改持续监听: " + repr(e))

    def _armed(self):
        import time as _t
        if self.wake_model is None:
            return True
        return _t.time() < self._armed_until

    def _cb(self, indata, frames, t, status):
        if status:
            pass
        self.q.put(bytes(indata))

    def start(self):
        import sounddevice as sd
        self.running = True
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                     blocksize=self.block, device=self.device, callback=self._cb)
        self.stream.start()
        if self.wake_model:
            log("开始监听唤醒词... (喊 " + str(CONFIG.get("wake_word")) + " 后下命令; 说 退出 停止)")
        else:
            log("开始监听... (说句话试试；说 退出 停止)")
        if OVN:
            OVN.set("监听中…\n（说 退出 停止）")
        while self.running:
            if not RUNNING:
                break
            try:
                data = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            self._feed(data)

    def _feed(self, data):
        import numpy as np, time as _t
        # 唤醒词门控: 累积 1280 样本喂 openwakeword
        if self.wake_model is not None:
            self._wake_buf.append(data)
            while sum(len(b) for b in self._wake_buf) >= 1280 * 2:
                raw = b"".join(self._wake_buf)
                chunk = raw[:1280 * 2]
                self._wake_buf = [raw[1280 * 2:]]
                try:
                    a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    scores = self.wake_model.predict(a)
                    for k, v in scores.items():
                        if v > 0.5:
                            self._armed_until = _t.time() + 8.0
                            log("  [唤醒] %s (%.2f) — 8 秒内可下命令" % (k, v))
                            if OVN:
                                OVN.set("已唤醒，请下命令…")
                            break
                except Exception:
                    pass
            if not self._armed():
                return   # 未唤醒, 不做命令 VAD, 只继续听唤醒词
        if self.engine == "silero":
            self._feed_silero(data)
        else:
            self._feed_webrtc(data)

    def _feed_webrtc(self, data):
        import time as _t
        try:
            is_speech = self.vad.is_speech(data, SAMPLE_RATE)
        except Exception:
            is_speech = False
        self.preroll.append(data)
        if len(self.preroll) > PREROLL_FRAMES:
            self.preroll.pop(0)
        now = _t.time()
        if is_speech:
            if not self.triggered:
                self.triggered = True
                self.seg = list(self.preroll)
                self.seg_start = now
            self.seg.append(data)
            self.last_speech = now
        else:
            if self.triggered:
                self.seg.append(data)
                if now - self.last_speech > CONFIG.get("hangover_s", 0.6):
                    self._emit()
                elif now - self.seg_start > 20.0:
                    self._emit()

    def _feed_silero(self, data):
        import numpy as np, torch, time as _t
        a = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        x = torch.from_numpy(a)
        try:
            out = self._silero(x, return_seconds=False)
        except Exception:
            out = None
        now = _t.time()
        if out:
            if "start" in out and not self.triggered:
                self.triggered = True
                self.seg = [data]
                self.seg_start = now
            if "end" in out and self.triggered:
                self.seg.append(data)
                self._emit()
                return
        if self.triggered:
            self.seg.append(data)
            if now - self.seg_start > 20.0:
                self._emit()

    def _emit(self):
        import numpy as np, time as _t
        raw = b"".join(self.seg)
        self.seg = []
        self.triggered = False
        if self.engine == "silero":
            try:
                self._silero.reset_states()
            except Exception:
                pass
        audio = np.frombuffer(raw, dtype=np.int16)
        if len(audio) < 1600:   # <0.1s 忽略
            return
        now = _t.time()
        if now - self._last_cmd < CONFIG.get("cooldown_s", 1.0):
            return
        self._last_cmd = now
        on_segment(audio)

def on_segment(audio):
    global _pending_flow
    text = transcribe(audio)
    if not text or not text.strip():
        return
    log("")
    log("──────── 听到：" + text)
    if OVN:
        OVN.set("听到：" + text + "\n（理解中…）")
    # 录制宏 meta 指令(优先, 避免"停止录制"被 STOP_WORDS 的"停止"误判退出)
    if _handle_macro(text):
        return
    # 停止词(最高优先级)
    low = text.lower()
    if any(w in low for w in STOP_WORDS):
        log("收到停止指令, 退出中...")
        global RUNNING
        RUNNING = False
        return
    # 待确认状态: 流程重放确认(优先)
    if _pending_flow is not None:
        if any(w in text for w in CONFIRM_WORDS):
            steps = _pending_flow
            _pending_flow = None
            log("  确认执行流程")
            _execute_flow(steps)
            return
        if any(w in text for w in CANCEL_WORDS):
            log("  已取消流程执行")
            _pending_flow = None
            return
    # 待确认状态: 上一条高风险指令等着你确认
    if _pending["intent"] is not None:
        if any(w in text for w in CONFIRM_WORDS):
            intent = _pending["intent"]
            _pending["intent"] = None
            log("  确认执行: " + str(intent.get("action")))
            execute(intent, text)
            return
        if any(w in text for w in CANCEL_WORDS):
            log("  已取消: " + str(_pending["intent"].get("action")))
            _pending["intent"] = None
            return
        log("  丢弃未确认指令, 处理新指令")
        _pending["intent"] = None
    # 正常解析
    intent = llm_intent(text)
    ok, reason = validate_intent(intent)
    if not ok:
        log("  拒绝执行(校验失败): " + reason)
        if OVN:
            OVN.set("拒绝：" + reason)
        return
    # 录制态: 把这条有效指令记进当前流程
    action = intent.get("action")
    if _recording is not None and action != "none":
        _recording["steps"].append(intent)
        log("  [录制 %d] %s %s" % (len(_recording["steps"]), action,
                                   json.dumps(intent.get("params", {}), ensure_ascii=False)))
    # 高风险动作二次确认(可选)
    if CONFIG.get("confirm_high_risk") and action in HIGH_RISK_ACTIONS:
        _pending["intent"] = intent
        msg = "确认 " + str(action) + " 吗？说 确认 或 取消"
        log("  [待确认] " + msg)
        if OVN:
            OVN.set(msg)
        return
    execute(intent, text)

# ---- 主流程 ----
RUNNING = True

# ---- 全局热键停止(防自动化跑飞, 借鉴 RPA.exe) ----
_HOTKEY_VK = {"f5": 0x74, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "esc": 0x1B}

def stop_hotkey_watcher():
    """后台线程轮询全局热键: stop_hotkey=停止整个程序; stop_recording_hotkey=停止录制宏并保存。"""
    import win32api
    global RUNNING, _recording
    stop_key = str(CONFIG.get("stop_hotkey", "f8")).lower()
    stop_vk = _HOTKEY_VK.get(stop_key, 0x77)
    rec_key = str(CONFIG.get("stop_recording_hotkey", "f9")).lower()
    rec_vk = _HOTKEY_VK.get(rec_key, 0x78)
    log("  热键: %s=停止整个程序 | %s=停止录制并保存" % (stop_key.upper(), rec_key.upper()))
    while RUNNING:
        try:
            if win32api.GetAsyncKeyState(stop_vk) & 0x8000:
                log("  [热键 %s] 触发，停止整个程序" % stop_key.upper())
                _recording = None
                RUNNING = False
                if OVN:
                    OVN.set("已按 %s 停止程序" % stop_key.upper())
                break
            if win32api.GetAsyncKeyState(rec_vk) & 0x8000:
                if _recording is not None:
                    _stop_record("")
                    log("  [热键 %s] 已停止录制" % rec_key.upper())
                else:
                    log("  [热键 %s] 当前未在录制" % rec_key.upper())
                time.sleep(0.3)   # 防抖, 避免一次按下重复触发
        except Exception:
            pass
        time.sleep(0.1)

def main():
    global RUNNING, OVN
    import pyautogui
    pyautogui.FAILSAFE = False
    # 设置 DPI 感知: Windows 缩放(125%/150%)下, 截图/模板匹配/点击坐标才能和用户手动截的图一致
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    setup_logging()
    # 置顶小窗(可选, 失败也不影响主功能)
    try:
        OVN = Overlay()
        threading.Thread(target=OVN.run, daemon=True).start()
    except Exception as e:
        log("置顶窗不可用(仅控制台输出): " + repr(e))
        OVN = None
    load_asr()
    detect_model()
    device = choose_mic()
    lis = Listener(device)
    threading.Thread(target=stop_hotkey_watcher, daemon=True).start()
    try:
        lis.start()
    except KeyboardInterrupt:
        log("用户中断")
    finally:
        RUNNING = False
        if lis.stream:
            try:
                lis.stream.stop()
                lis.stream.close()
            except Exception:
                pass
        log("已退出。")

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--selftest":
        setup_logging()
        log("=== 自检开始(不启动麦克风) ===")
        load_config()
        detect_model()
        log("intent 测试:")
        print(json.dumps(llm_intent("帮我截个图发给我"), ensure_ascii=False))
        print(json.dumps(llm_intent("打开记事本"), ensure_ascii=False))
        print(json.dumps(llm_intent("今天天气不错啊"), ensure_ascii=False))
        print(json.dumps(llm_intent("点一下"), ensure_ascii=False))
        print(json.dumps(llm_intent("双击这里"), ensure_ascii=False))
        print(json.dumps(llm_intent("右键点击"), ensure_ascii=False))
        print(json.dumps(llm_intent("按回车"), ensure_ascii=False))
        print(json.dumps(llm_intent("保存一下"), ensure_ascii=False))
        print(json.dumps(llm_intent("复制"), ensure_ascii=False))
        print(json.dumps(llm_intent("点发送"), ensure_ascii=False))
        print(json.dumps(llm_intent("点确定按钮"), ensure_ascii=False))
        log("=== 自检结束 ===")
    else:
        main()
