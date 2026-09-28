# -*- coding: utf-8 -*-
"""语音控制助手 / Voice Control Assistant
监听麦克风(webrtcvad 端点检测) -> SenseVoice 中文转写 -> 本地 Qwen(:1234) 意图理解 -> pyautogui 执行
复用 funasr-test 里已验证的 SenseVoice 模型与麦克风逻辑。

日志实时同时输出到: 控制台(GBK) + run_assistant.log(UTF-8) + 常驻置顶小窗(可选)。
"""
import sys, os, json, time, queue, threading, re
import requests
import asr_better          # ASR 抽象层(local GPU / server + 纠错词典 + 音频前端)
import asr_polish          # LLM 校对(同音错字 / 标点 / 术语归一)
import dictation           # 按键触发语音输入(Typeless 式听写)
import dictation_ui        # 听写悬浮指示器(逐字显示)
import dialogue            # 多轮对话 + TTS(Windows SAPI)
import skills              # 技能存储(复用 macro 步骤格式)
import skill_trainer       # 截图->视觉模型->技能
import visual_click        # 运行时视觉点击(网格动作空间)
import decision            # 决策引擎开关(原 LLM / Laya / auto)

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
    "llm_base": "http://localhost:9292/v1",   # llama-swap 网关唯一入口（原 :1234 已废）
    "model": "spark",                          # 显式指定；不填则自动探测（网关按字母序返回，data[0] 会是 kv27b，错配）
    "screenshot_mode": "active_window",  # active_window | full | region
    "screenshot_region": None,             # [x,y,w,h] 当 mode=region
    "auto_send": False,           # 截图粘贴后是否自动按回车发送(默认关, 安全第一)
    "hangover_s": 0.8,            # 末尾静音多久算一句话说完(调大: 减少话说到一半被截断)
    "vad_aggressiveness": 3,
    "cooldown_s": 1.0,            # 两次指令最小间隔
    "confirm_high_risk": False,   # 高风险动作(发送/打字/打开/点击)执行前是否要语音二次确认
    "wake_word": "",              # 唤醒词模型名(openwakeword, 如 hey_jarvis); 留空=持续监听(不安全但省事)
    "vad_engine": "webrtcvad",    # webrtcvad | silero (silero 更鲁棒, 需装 silero-vad)
    "stop_hotkey": "f8",         # 全局热键停止整个程序(防自动化跑飞); 值: f8/f9/f10/f11/esc/f5
    "stop_recording_hotkey": "f9",  # 停止录制宏并保存(不退出程序); 值: f8/f9/f10/f11/esc/f5
    "ocr_fallback": False,       # OCR 兜底(默认关: 边聊天边用会误命中聊天窗口文字; 需要时设 true)
    "tts": True,                 # 对话回复是否用 Windows SAPI 朗读(零依赖, 全程本地)
    "chat_enabled": True,        # 非命令语音是否走多轮对话(关=原行为, none 不回复)
    "asr_mode": "local",         # local(SenseVoice) | server(funasr-server, 秒开)
    "asr_server": "http://localhost:8000/v1",
    "correction_dict": {},       # 纠错词典: {"误识别":"正确写法"} 低成本提升识别率
    # ---- 2026-09-28 准确率专项(详见 asr_better.py 文件头) ----
    "asr_target_peak": 0.90,     # 峰值归一化目标(0~1): 小声说话的最大收益项
    "asr_max_gain": 12.0,        # 增益上限(倍): 防把噪声底噪放大成人声
    "asr_min_rms": 0.004,        # 噪声门: 整段 RMS 低于此值判噪声丢弃(防「L, A A」)
    "asr_language": "auto",      # auto | zh | en | yue | ja | ko
    "asr_short_force_zh": True,  # 短音频(<asr_short_sec)强制 zh: auto 在 1~2s 上会翻车
    "asr_short_sec": 2.5,
    "strip_fillers": True,       # 语气词过滤(嗯/啊/呃)
    "auto_space": True,          # 中文与英文/数字之间自动加空格
    "asr_vocab": [],             # 领域术语表(用于 LLM 校对偏置, 逼近 ASR 热词)
    "asr_polish": True,          # 听写结果是否走本地 LLM 校对
    "asr_polish_command": False, # 指令模式是否也校对(默认关, 以免改变指令语义)
    "asr_polish_min_chars": 6,
    "asr_polish_timeout_s": 6.0,
    # ---- 按键语音输入(听写), 详见 dictation.py 文件头 ----
    "dictation_enabled": True,
    "dictation_hotkey": "ctrl+alt+space",
    "dictation_hotkey_mode": "auto",     # auto(长按=按住说, 短按=切换) | hold | toggle
    "dictation_tap_ms": 250,
    "dictation_cancel_key": "esc",
    "dictation_output": "paste",         # paste | clipboard | none
    "dictation_partial": True,
    "dictation_partial_interval_s": 0.7,
    "dictation_partial_min_new_s": 0.5,
    "dictation_max_seconds": 60,
    "dictation_silence_stop_s": 0,       # 0=关; >0 则在切换模式下静音自动结束
    "dictation_restore_clipboard": True, # 上屏后还原原剪贴板
    "dictation_paste_delay_s": 0.35,
    "dictation_history": True,
    "dictation_ui": True,
    "dictation_console": True,   # 终端风格管道面板(可视化进程, 不黑箱)
    "dictation_font_size": 20,
    "dictation_char_ms": 30,             # 逐字动画速度(ms/字)
    "dictation_idle_hide_s": 3.0,
    "dictation_bottom_margin": 120,
    "vl_base": "http://localhost:1235/v1",   # 本地视觉模型(Qwen3-VL)用于技能训练/视觉点击
    "trainer": {"base": "", "key": "", "model": ""},  # 云端多模态(可选, 留空=用本地VL)
    # ---- 演示录制(说口令 -> 录真实键鼠操作 -> 编译成可重放脚本) ----
    "demo_max_seconds": 120,      # 单次演示录制上限(秒), 到点自动停并保存
    "demo_max_events": 300,       # 单次最多记录多少个事件, 防跑飞
    "demo_ai_compile": True,      # 录完用本地 LLM 修正 OCR 锚点(关=纯坐标, 更快)
    "demo_strict": False,         # 重放时断言拿不到证据是否中止(False=放行, 适合演示脚本)
    # ---- 决策引擎: 意图理解用哪个模型 ----
    "decision_engine": "llm",     # llm(原模型, 走 llm_base) | laya(本地决策模型) | auto(Laya 优先, 低置信度回退 LLM)
    "laya_base": "http://127.0.0.1:8801",   # laya_serve.py 的地址(托管 venv 里跑)
    "laya_min_conf": 0.70,        # Laya 置信度低于此值则回退原模型
    "laya_fallback_llm": True,    # laya 模式下低于阈值是否回退(True=稳妥; False=纯 Laya, 低置信度直接 none)
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

# ---- 常驻置顶小窗(实时显示识别/执行 + 右上角模式色条) ----
class Overlay:
    # 模式 -> 顶部色条背景色(实心, 一眼可辨)
    MODE_BAR = {
        "listen":    "#9aa0a6",   # 灰 监听中
        "thinking":  "#ffd54f",   # 黄 理解中
        "command":   "#3ddc84",   # 绿 指令模式
        "chat":      "#4aa3ff",   # 蓝 聊天模式
        "recording": "#ffb020",   # 橙 录制宏
        "skill":     "#c678dd",   # 紫 录制技能
        "pending":   "#ff5c5c",   # 红 待确认
    }
    MODE_TEXT = {
        "listen":    "● 监听中",
        "thinking":  "● 理解中",
        "command":   "● 指令模式",
        "chat":      "● 聊天模式",
        "recording": "● 录制宏",
        "skill":     "● 录制技能",
        "pending":   "● 待确认",
    }
    def __init__(self):
        # ★ Tk 在 Windows 上不是线程化的: Tk() 的创建线程 == mainloop() 的线程 ==
        # 所有界面调用的线程。跨线程调 `after` 会抛
        # "Calling Tcl from different apartment", 而旧写法把这个异常吞了 ——
        # 结果就是这个常驻小窗一直停在初始文案上(根本没在刷新)。
        # 修法: Tk 完全跑在自己的线程里, 外部只往 _q 丢命令, 由 Tk 线程自己排空执行。
        import queue as _q
        self._q = _q.Queue()
        self._ready = threading.Event()
        self.root = None
        self.rect = (0, 0, 0, 0)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self._ready.wait(timeout=5.0)

    def _run(self):
        import tkinter as tk
        self.tk = tk
        try:
            self.root = tk.Tk()
        except Exception:
            self.root = None
            self._ready.set()
            return
        self._build()
        self._ready.set()
        try:
            self.root.mainloop()
        except Exception:
            pass

    def _drain(self):
        """在 Tk 线程里排空命令队列(每 25ms 一轮)。"""
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.root.after(25, self._drain)
        except Exception:
            pass

    def _build(self):
        import tkinter as tk
        self.root.title("语音助手")
        self.root.attributes("-topmost", True)
        # 定位右上角(top-right)
        try:
            import pyautogui
            sw, sh = pyautogui.size()
        except Exception:
            sw, sh = 1920, 1080
        w, h = 460, 178
        x = max(12, sw - w - 12)
        y = 12
        self.rect = (x, y, w, h)   # 小窗屏幕位置, 供 OCR 排除该区域(不关窗)
        try:
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass
        self.root.configure(bg="#1e1e2e")
        # 顶部实心模式色条(整条变色, 黑字, 绝不漏看)
        self.bar = tk.Frame(self.root, bg="#9aa0a6", height=28)
        self.bar.pack(side="top", fill="x")
        self.bar.pack_propagate(False)
        self.bar_label = tk.Label(self.bar, text="● 监听中",
                                  bg="#9aa0a6", fg="#111111",
                                  font=("Microsoft YaHei UI", 12, "bold"),
                                  anchor="center")
        self.bar_label.pack(fill="both", expand=True)
        # 主内容
        self.var = tk.StringVar(value="监听中…\n（说 退出 停止）")
        self.label = tk.Label(self.root, textvariable=self.var,
                              font=("Microsoft YaHei UI", 14), wraplength=w-32,
                              justify="left", anchor="nw",
                              bg="#1e1e2e", fg="#f2f2f6")
        self.label.pack(fill="both", expand=True, padx=16, pady=10)
        self.root.update_idletasks()
        self._drain()
    def _apply(self, text):
        try:
            self.var.set(text)
            self.root.update_idletasks()
        except Exception:
            pass
    def _apply_mode(self, mode, detail):
        try:
            bg = self.MODE_BAR.get(mode, self.MODE_BAR["listen"])
            text = self.MODE_TEXT.get(mode, "● 监听中")
            full = text + ((" · " + detail) if detail else "")
            self.bar.configure(bg=bg)
            self.bar_label.configure(bg=bg, text=full)
            self.root.update_idletasks()
        except Exception:
            pass
    def set(self, text):
        try:
            self._q.put(lambda: self._apply(text))
        except Exception:
            pass
    def set_mode(self, mode, detail=""):
        """更新右上角模式色条(线程安全 —— 走命令队列, 不跨线程碰 Tk)。"""
        try:
            self._q.put(lambda: self._apply_mode(mode, detail))
        except Exception:
            pass
    def run(self):
        """兼容旧调用: 阻塞跑 mainloop(应在创建它的同一线程里调)。"""
        try:
            self.root.mainloop()
        except Exception:
            pass

OVN = None

# ---- 新功能全局状态 ----
_asr = None
_asr_corr = {}
_asr_mode = "local"
_dialogue = None
_dictation = None          # 按键听写(dictation.Dictation)
_dictation_ui = None       # 听写悬浮指示器(dictation_ui.DictationUI)
_skill_recording = None     # 录制技能态: 非 None 时逐条记录意图, 说"完成"存为技能

# ---- 模式状态机(右上角状态栏) ----
# listen=监听中 thinking=理解中 command=指令 chat=聊天 recording=录制宏 skill=录制技能 pending=待确认
MODE = "listen"
_tts_until = 0.0   # TTS 播放期间的静音屏蔽窗口(避免助手听到自己声音触发空识别)
def set_mode(m, detail=""):
    """切换当前模式并更新右上角状态栏(线程安全)。"""
    global MODE
    MODE = m
    if OVN is not None:
        try:
            OVN.set_mode(m, detail)
        except Exception:
            pass

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

# ---- ASR (抽象层: local GPU / server, 见 asr_better.py) ----
def build_asr_backend():
    """构建 ASR 后端。local 模式后台加载模型(不阻塞 UI); server 模式立即可用。"""
    global _asr, _asr_corr, _asr_mode
    _asr, _asr_corr, _asr_mode = asr_better.build_asr(CONFIG)
    if _asr_mode == "local":
        def _load():
            try:
                _asr.load()
                try:
                    dev = _asr.device()
                except Exception:
                    dev = "?"
                log("ASR 模型就绪(device=%s)%s" % (
                    dev, "" if dev == "cuda:0" else "  ← CPU 推理，装 CUDA 版 torch 可提速"))
            except Exception as e:
                log("ASR 加载失败: " + repr(e))
        threading.Thread(target=_load, daemon=True).start()
    else:
        log("ASR 后端: server (" + str(CONFIG.get("asr_server", "")) + ") 立即可用")

def ensure_asr_ready(timeout=120):
    if _asr is None:
        return False
    if _asr.ready():
        return True
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _asr.ready():
            return True
        time.sleep(0.3)
    return _asr.ready()

def _clog(msg, kind="dim", stage=None):
    """把管道事件送进听写窗口的终端面板(可视化进程)。

    stage: MIC/VAD/ASR/POLISH/PASTE, 非 None 时同时高亮该阶段。
    """
    if _dictation_ui is None:
        return
    try:
        _dictation_ui.log(msg, kind)
        if stage:
            _dictation_ui.set_stage(stage)
    except Exception:
        pass


def transcribe(int16_audio, partial=False, command=True):
    """统一转写入口: 音频前端 -> 语言策略 -> 识别 -> 文本后处理 -> (可选)LLM 校对。

    partial=True : 听写实时中间结果。只跑「前端 + 词典」, 跳过 LLM 校对
                   —— 中间结果本来就会变, 校对纯属浪费时间。
    command=True : 指令模式。是否走 LLM 校对由 asr_polish_command 决定(默认关)。
    """
    if _asr is None:
        return ""
    if asr_better.is_noise(int16_audio, CONFIG):
        rms, _ = asr_better.audio_stats(int16_audio)
        _clog("[前端] 噪声门丢弃 rms=%.4f < %.4f"
              % (rms, float(CONFIG.get("asr_min_rms", 0.004))), "err")
        if not partial:
            log("  [噪声门] 丢弃低能量段 rms=%.4f < %.4f"
                % (rms, float(CONFIG.get("asr_min_rms", 0.004))))
        return ""
    audio, _info = asr_better.preprocess(int16_audio, CONFIG)
    lang = asr_better.pick_language(audio, CONFIG)
    _clog("[前端] rms=%.4f peak=%.2f gain=%.1f lang=%s"
          % (_info["rms"], _info["peak"], _info["gain"], lang), "mic", "ASR")
    text = _asr.transcribe(audio, language=lang)
    text = asr_better.postprocess(text, CONFIG, _asr_corr)
    if not text:
        return ""
    # ---- LLM 校对(仅最终结果) ----
    if not partial:
        want = CONFIG.get("asr_polish_command", False) if command \
            else CONFIG.get("asr_polish", True)
        if want and len(text) >= int(CONFIG.get("asr_polish_min_chars", 6)):
            new, note = asr_polish.polish(
                text, base=CONFIG.get("llm_base"), model=(_MODEL_ID or CONFIG.get("model")),
                vocab=CONFIG.get("asr_vocab") or [],
                timeout=float(CONFIG.get("asr_polish_timeout_s", 6.0)))
            if new != text:
                log("  [校对] " + note)
                _clog("[校对] " + note, "polish")
                _clog("[校对] %s → %s" % (text, new), "polish", "PASTE")
                text = new
    return text

# ---- LLM 意图 ----
_MODEL_ID = None
def detect_model():
    global _MODEL_ID
    if _MODEL_ID:
        return _MODEL_ID
    # 优先用显式配置。网关 /v1/models 是字母序，盲取 data[0] 会选中 kv27b
    # （244K 长上下文重档：冷启动 18s、32 tok/s、需 12GB 空闲内存），对意图解析是错配。
    explicit = CONFIG.get("model") or ""
    if explicit:
        _MODEL_ID = explicit
        log("本地 LLM(配置指定): " + _MODEL_ID)
        return _MODEL_ID
    try:
        r = requests.get(CONFIG["llm_base"] + "/models", timeout=5)
        j = r.json()
        data = j.get("data") or j.get("models") or []
        if data:
            _MODEL_ID = data[0]["id"]
            log("本地 LLM(自动探测): " + _MODEL_ID)
            return _MODEL_ID
    except Exception as e:
        log("探测 LLM 模型失败(用占位名): " + repr(e))
    return "local-model"

SYS_PROMPT = """你是一个本地语音助手的中文意图解析器。用户说一句话，判断他想做什么，严格只返回 JSON，不要多余文字。
可用动作:
- "screenshot": 截取屏幕并复制到剪贴板, 不发送。默认截当前活动窗口。params:{"region":可选} region 可为: tl/tr/bl/br(左上/右上/左下/右下角)、left/right(左半/右半)、top/bottom(上半/下半)、full(全屏)。当用户说"截左上角/右上角/右下角/右半边/下半部分/整个屏幕"等时填对应 region; 只说"截图/截个图"不填则截当前窗口。
- "screenshot_send": 截图并粘贴到当前光标所在输入框然后发送(按回车)。仅当用户明确说"发送/发给我/发到..."时用。params:{"region":可选}
- "type": 把文字打到当前光标处。params:{"text":"要打的内容"}
- "open": 打开程序。params:{"app":"应用名, 如 微信/记事本/资源管理器"}
- "click_here": 在鼠标当前所在位置点击。**仅当用户只说"点一下/点这里/按一下"这类话、后面没有任何目标文字时**才用。params:{"button":"left","clicks":1}  (button 可 left/right, clicks 可 1 或 2 表示双击)
- "click_target": 点击屏幕上指定的控件(按钮/输入框/菜单项等), 系统会自动在前台窗口里找到它并移动鼠标点击, 用户无需移动鼠标。**只要用户说"点/点击/按"后面跟了具体目标文字(如"点发送""点击一下默认权限""点确定按钮""点搜索"), 就必须用 click_target**, 让系统自己移动鼠标去找。params:{"target":"控件上的文字, 如 发送/确定/设置/搜索","hover":false}  当用户说"悬停/移到/放到 xxx 上"(只移动鼠标不点击)时 hover 填 true
- "click": 点击屏幕精确坐标(仅当用户明确说出坐标数字如"点 100 200"时才用, 否则绝不用)。params:{"x":0,"y":0}
- "click_visual": 当控件树/记忆库/模板/OCR 都定位不到目标, 但界面上肉眼能看到该元素(如游戏按钮/图片按钮/无文字控件)时, 用本地视觉模型看截图点它。params:{"target":"对目标的文字描述, 如 红色登录按钮/左下角开始"}。仅作 click_target 失败后的兜底。
- "press": 按键或热键。单键用 params:{"key":"enter"} (key 可为 enter/space/f5/esc/backspace/delete/home/end/up/down/left/right/tab 等); 组合键用 params:{"keys":["ctrl","s"]} (ctrl/alt/shift + 字母或功能键)。常见: "保存"->ctrl+s, "复制"->ctrl+c, "粘贴"->ctrl+v, "全选"->ctrl+a, "刷新"->f5, "回车/确认/发送"->enter
- "scroll": 滚动鼠标滚轮。params:{"amount":3} 正数向上滚、负数向下滚。当用户说"往下滚/向上滚/往下翻/往上翻/滚动页面/滚轮"时用
- "none": 闲聊或无需操作。params:{}
返回格式: {"action":"...","params":{...},"reply":"一句话回复(中文,可选)"}
如果是命令类(截图/打开/打字/点击/按键/发送)务必返回对应 action；普通聊天返回 none。口语映射: 只"点一下/点这里/按一下"(无目标文字)->click_here; "点/点击 + 具体目标文字"(如"点发送""点击一下默认权限")->click_target; "双击"->click_here{clicks:2}; "右键"->click_here{button:right}; "回车/确认"->press{key:enter}; "保存/复制/粘贴/全选/刷新/撤销/剪切"->对应热键 press; 若 click_target 找不到且能描述目标外观, 用 click_visual{target:"描述"} 兜底。
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

decision.bind(CONFIG, llm_intent)

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

# 区域截图: 把口语"左上角/右半边"等映射成屏幕 (x,y,w,h)
_REGION_MAP = {
    "tl": lambda w, h: (0, 0, w // 2, h // 2),
    "tr": lambda w, h: (w // 2, 0, w // 2, h // 2),
    "bl": lambda w, h: (0, h // 2, w // 2, h // 2),
    "br": lambda w, h: (w // 2, h // 2, w // 2, h // 2),
    "left": lambda w, h: (0, 0, w // 2, h),
    "right": lambda w, h: (w // 2, 0, w // 2, h),
    "top": lambda w, h: (0, 0, w, h // 2),
    "bottom": lambda w, h: (0, h // 2, w, h // 2),
}

def resolve_region(region):
    """把意图里的 region 解析成 (x,y,w,h) 或 None(全屏)。region 可为字符串(tl/tr/bl/br/left/right/top/bottom/full)或 [x,y,w,h]。"""
    if not region:
        return get_shot_region()
    if isinstance(region, (list, tuple)) and len(region) == 4:
        try:
            return tuple(int(v) for v in region)
        except Exception:
            return get_shot_region()
    if isinstance(region, str):
        key = region.strip().lower()
        if key in ("full", "全屏"):
            return None
        if key in _REGION_MAP:
            import pyautogui
            w, h = pyautogui.size()
            return _REGION_MAP[key](w, h)
    return get_shot_region()

def do_screenshot(send, region=None):
    import pyautogui
    r = resolve_region(region)
    img = pyautogui.screenshot(region=r) if r else pyautogui.screenshot()
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
    # ---- 预置技能启动项(2026-08-30): 绝对路径防解析失败; bat/cmd 由 start 拉起新控制台 ----
    "微信": r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    "哔哩哔哩": r"C:\Program Files\bilibili\哔哩哔哩.exe",
    "obsidian": r"E:\AI\_apps\Obsidian\Obsidian\Obsidian.exe",
    "ComfyUI": r"D:\AIPAINT\Comfy Desktop\Comfy Desktop.exe",
    "本地大模型": r"E:\AI\_scripts\start-llama-server.bat",
    "视觉模型": r"E:\AI\_scripts\start-vl-server.bat",
    "服务栈": r"E:\AI\_scripts\start-stack.bat",
    "智能中枢": r"E:\AI\agent-hub\start-agent-hub.bat",
    "DSH": r"E:\AI\start_dsh.cmd",
    "桌面识别": r"E:\AI\_scripts\start-uitars-server.bat",
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

STOP_WORDS = ["停止", "别动", "关闭助手", "结束", "quit", "stop", "退出助手", "退出程序"]
# 精确判定是否真要退出整个程序: 区分「退出」(退出助手) 与 「退出X」(关闭某应用, 不退出程序)
_STOP_RE = re.compile(r"^(退出|停止|结束)(吧|了|哦|么|啊)?$")
def is_stop_command(text):
    """裸『退出/停止/结束』或带语气词(退出吧/退出了)才退程序；『退出记事本/退出微信』视为关应用, 不退。"""
    t = (text or "").strip().strip("。.，,!！?？、 ").lower()
    if t in ("退出", "停止", "结束", "quit", "stop", "关闭助手", "退出助手", "退出程序", "别动"):
        return True
    return bool(_STOP_RE.match(t))
CONFIRM_WORDS = ["确认", "确定", "对", "是的", "执行", "好", "yes", "ok"]
CANCEL_WORDS = ["取消", "不", "算了", "别", "no", "cancel"]

# ---- 安全层: 动作白名单 + JSON 校验 + 高风险确认 ----
ALLOWED_ACTIONS = {"screenshot", "screenshot_send", "type", "open",
                   "click", "click_here", "click_target", "click_visual", "press", "scroll", "none"}
HIGH_RISK_ACTIONS = {"screenshot_send", "type", "open", "click", "click_target"}
_pending = {"intent": None}
_teach_pending = None   # 教学态: click_target 失败时, 等用户手动点一下记进记忆库
_recording = None       # 录制宏: 非 None 时表示正在录制, {"name":..., "steps":[...]}
_pending_flow = None    # 重放流程前的整体二次确认, 存步骤列表
_demo = None            # 演示录制: 非 None 时正在录键鼠脚本, {"rec","name","state"}

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
    if action == "click_visual" and not isinstance(params.get("target"), str):
        return False, "click_visual 缺 target"
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
    action = intent.get("action")
    if action and action != "none":
        set_mode("command", str(action))   # 右上角状态: 指令模式·动作
    if is_stop_command(raw_text):
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
            do_screenshot(send=False, region=params.get("region"))
        elif action == "screenshot_send":
            do_screenshot(send=True, region=params.get("region"))
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
        elif action == "click_visual":
            try:
                visual_click.click_visual(params.get("target", ""),
                                         vl_base=CONFIG.get("vl_base"))
            except Exception as e:
                log("  视觉点击失败: " + repr(e))
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
            set_mode("listen")   # 录制结束 -> 回到监听
            return True
    # 开始录制: 不在录制中时, 只要含"录制/记录/录屏"即触发
    if _recording is None and any(w in text for w in ("录制", "记录", "录屏")):
        _start_record()
        set_mode("recording")   # 右上角状态: 录制宏
        return True
    # 重放流程
    for kw in ("执行流程", "运行流程", "跑流程", "重放", "执行宏", "运行宏"):
        if kw in text:
            name = _extract_after(text, (kw,))
            _run_flow(name)
            return True
    return False


# ---- 技能指令(截图训练 / 录制 / 执行 / 列出 / 删除 / 聊天开关) ----
def _handle_skill(text):
    """处理技能相关指令, 返回 True 表示已吞掉不再走 LLM。"""
    global _skill_recording, _dialogue
    # 录制技能中: 停止保存
    if _skill_recording is not None:
        if any(w in text for w in ("停止", "结束", "保存", "完成")):
            _stop_skill_record()
            set_mode("listen")   # 录制结束 -> 回到监听
            return True
        return True   # 录制中吞掉, 不进 LLM
    # 聊天开关 / 对话历史
    if any(k in text for k in ("聊天模式", "开始聊天", "进入聊天", "打开聊天")):
        CONFIG["chat_enabled"] = True
        log("  进入聊天模式"); 
        set_mode("chat")   # 右上角状态: 聊天模式(语音切换)
        if OVN: OVN.set("已进入聊天模式\n随便聊，说「退出聊天」结束")
        return True
    if any(k in text for k in ("退出聊天", "关闭聊天", "别聊了", "停止聊天")):
        CONFIG["chat_enabled"] = False
        log("  退出聊天模式"); 
        set_mode("command")   # 回到指令/自动识别态
        if OVN: OVN.set("已退出聊天模式")
        return True
    if any(k in text for k in ("清空对话", "忘记对话", "重置对话", "清除对话")):
        if _dialogue: _dialogue.reset()
        log("  已清空对话历史"); 
        if OVN: OVN.set("已清空对话历史")
        return True
    # 列出技能
    if any(k in text for k in ("列出技能", "有哪些技能", "技能列表", "看看技能", "技能有哪些")):
        _list_skills(); return True
    # 删除技能
    for kw in ("删除技能", "删掉技能", "移除技能", "去掉技能"):
        if kw in text:
            _delete_skill(_extract_after(text, (kw,))); return True
    # 执行技能
    for kw in ("执行技能", "运行技能", "用技能", "跑技能", "施展技能"):
        if kw in text:
            _run_skill(_extract_after(text, (kw,))); return True
    # 学习/训练技能(截图 -> 视觉模型)
    for kw in ("学习技能", "训练技能", "教技能", "学会技能"):
        if kw in text:
            _learn_skill(_extract_after(text, (kw,))); return True
    # 录制技能(手动演示)
    for kw in ("录制技能", "记录技能", "录个技能", "录技能", "手动技能"):
        if kw in text:
            _start_skill_record(_extract_after(text, (kw,)))
            set_mode("skill")   # 右上角状态: 录制技能
            return True
    return False


def _learn_skill(name):
    if not name:
        name = "skill_%d" % (len(skills.list_skills()) + 1)
    log("  ▶ 学习技能「%s」: 截取当前界面并发给视觉模型..." % name)
    if OVN: OVN.set("正在看屏幕学习「%s」…" % name)
    steps = skill_trainer.train_skill(name, task=name, region=None, cfg=CONFIG)
    if steps:
        path = skills.save_skill(name, steps, triggers=[name], source="trained")
        log("  ✅ 已用视觉训练技能「%s」共 %d 步 -> %s" % (name, len(steps), path))
        if OVN: OVN.set("已学会「%s」\n共 %d 步，说「执行技能%s」重放" % (name, len(steps), name))
    else:
        log("  视觉训练失败, 改为手动演示: 逐步说指令, 说「完成」保存")
        if OVN: OVN.set("视觉不可用，请手动演示\n逐步说指令，说「完成」保存")
        _start_skill_record(name)


def _start_skill_record(name):
    global _skill_recording
    _skill_recording = {"name": name or "", "steps": []}
    log("  ▶ 录制技能: 逐步说指令; 说「完成」保存")
    if OVN: OVN.set("录制技能中…\n逐步说指令，说「完成」保存")


def _stop_skill_record():
    global _skill_recording
    steps = (_skill_recording or {}).get("steps", []) if _skill_recording else []
    name = (_skill_recording or {}).get("name", "")
    _skill_recording = None
    if not steps:
        log("  录制为空，未保存")
        if OVN: OVN.set("录制为空，未保存")
        return
    nm = name or ("skill_%d" % (len(skills.list_skills()) + 1))
    path = skills.save_skill(nm, steps, triggers=[nm], source="manual")
    log("  ✅ 已保存技能「%s」共 %d 步 -> %s" % (nm, len(steps), path))
    if OVN: OVN.set("已保存技能「%s」\n共 %d 步，说「执行技能%s」重放" % (nm, len(steps), nm))


def _run_skill(name):
    sk = skills.load_skill(name) if name else None
    if not sk:
        sk = skills.match_skill(name or "")
    if not sk:
        avail = "、".join(skills.list_skills()) or "（暂无）"
        log("  找不到技能「%s」，已有: %s" % (name, avail))
        if OVN: OVN.set("找不到技能「%s」\n已有：%s" % (name, avail))
        return
    steps = sk.get("steps", [])
    global _pending_flow
    _pending_flow = steps
    set_mode("pending", "执行技能确认")   # 右上角状态: 待确认
    log("  [待确认] 执行技能「%s」共 %d 步？说 确认 或 取消" % (sk.get("name", name), len(steps)))
    if OVN: OVN.set("执行技能「%s」共 %d 步？\n说 确认 或 取消" % (sk.get("name", name), len(steps)))


def _list_skills():
    ls = skills.list_skills()
    if not ls:
        log("  还没有技能，说「学习技能 X」或「录制技能 X」创建")
        if OVN: OVN.set("还没有技能\n说「学习技能 X」创建")
        return
    log("  已有技能: " + "、".join(ls))
    if OVN: OVN.set("已有技能:\n" + "\n".join("· " + n for n in ls))


def _delete_skill(name):
    if skills.delete_skill(name):
        log("  已删除技能「%s」" % name)
        if OVN: OVN.set("已删除技能「%s」" % name)
    else:
        log("  没找到技能「%s」" % name)
        if OVN: OVN.set("没找到技能「%s」" % name)

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

# ---- 演示录制: 口令「请你跟我这样做」-> 录真实键鼠操作 -> 编译成可重放脚本 ----
# 与「录制宏」的区别(两套并存, 别混):
#   录制宏 = 你口头说一串指令, 存 intent 列表(说「录制」开始)
#   演示录制 = 你动手做一遍, 系统录键鼠+截图, 编译成带文字锚点的 DAG(说口令开始)
_DEMO_CORES = ("跟我这样做", "跟你这样做", "跟我做", "跟你做", "跟我学", "跟你学")
_DEMO_STOP_WORDS = ("停止录制", "结束录制", "录完了", "录好了", "就到这里", "到此为止",
                    "停止", "结束", "完成", "保存", "好了", "就这样")


def _norm_zh(s):
    """归一化: 只留中英文数字, 去掉标点空格(ASR 会乱加标点)。"""
    return "".join(ch for ch in (s or "") if ch.isalnum())


def _is_demo_wake(text):
    """口令模糊匹配。ASR 常漏字/加字(如「你就跟我这样做」), 只认核心片段。"""
    n = _norm_zh(text)
    return len(n) >= 4 and any(c in n for c in _DEMO_CORES)


def _is_demo_stop(text):
    n = _norm_zh(text)
    return bool(n) and any(w in n for w in _DEMO_STOP_WORDS)


def _demo_name_from(text):
    """口令里带名字: 「…这样做 叫 打卡流程」 -> 打卡流程; 没有则用时间戳名。"""
    m = re.search(r"(?:叫|名为|命名为|保存为|存为)\s*([\w\u4e00-\u9fa5]{1,20})", text or "")
    return _clean_flow_name(m.group(1)) if m else ""


def _start_demo_record(name=""):
    """启动键鼠演示录制(后台线程), 到点/口令/热键停。"""
    global _demo, _tts_until
    if _demo is not None:
        log("  已在演示录制中(编译中或录制中)")
        return
    try:
        import recorder
    except Exception as e:
        log("  演示录制不可用: " + repr(e))
        if OVN:
            OVN.set("演示录制不可用\n请 pip install pynput")
        return
    nm = name or ("demo_" + time.strftime("%m%d_%H%M%S"))
    dur = int(CONFIG.get("demo_max_seconds", 120) or 120)
    rec = recorder.Recorder(max_events=int(CONFIG.get("demo_max_events", 300)))
    _demo = {"rec": rec, "name": nm, "state": "recording", "t0": time.time()}
    threading.Thread(target=_demo_worker, args=(rec, nm, dur), daemon=True).start()
    set_mode("recording", "演示录制")
    log("  ▶ 演示录制开始(最多 %ds)：正常操作鼠标键盘即可；说「停止」或按 F9 保存" % dur)
    if OVN:
        OVN.set("演示录制中…\n正常操作即可，说「停止」/按 F9 保存")
    _tts_until = time.time() + 2.0   # 开场瞬间屏蔽回采, 防助手听到自己说话


def _stop_demo_record():
    """请求停止录制(录音线程随后编译保存)。"""
    global _demo
    if _demo is None:
        return
    rec = _demo.get("rec")
    if _demo.get("state") == "recording":
        _demo["state"] = "stopping"
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass
        log("  演示录制停止，正在编译…")
        if OVN:
            OVN.set("录制结束，编译中…")


def _fallback_nodes(events):
    """编译链(nuphus/OCR)不可用时的降级: 直接按坐标记录, 至少能重放。"""
    nodes = []
    for i, ev in enumerate(events, 1):
        k = ev.get("kind")
        if k == "type":
            nodes.append({"do": "type", "text": ev.get("text", "")})
        elif k == "key":
            nodes.append({"do": "hotkey", "keys": ev.get("keys", [])})
        elif k == "click":
            n = {"do": "double_click" if ev.get("button") == "double" else "click",
                 "id": "s%d" % i, "target": "", "at": [ev.get("x"), ev.get("y")]}
            w = ev.get("win") or {}
            if w.get("process"):
                n["window"] = w["process"]
            nodes.append(n)
    return nodes


def _demo_worker(rec, name, duration):
    """录制 -> 编译 -> 保存。全程后台, 不阻塞语音监听。"""
    global _demo
    try:
        import recorder
        events = rec.run(duration)
        if not events:
            log("  没录到任何操作，未保存")
            if OVN:
                OVN.set("没录到操作，未保存")
            return
        try:
            nodes, notes = recorder.compile_events(events, name)
        except Exception as e:
            log("  编译失败(%r)，降级为坐标脚本" % (e,))
            nodes, notes = _fallback_nodes(events), ["编译降级：按坐标记录，移植性差"]
        if not nodes:
            log("  编译后无有效步骤，未保存")
            return
        if CONFIG.get("demo_ai_compile", True):
            try:
                nodes, aimsg = recorder.ai_clean(nodes)
                notes.append("AI 编译：" + aimsg)
            except Exception as e:
                notes.append("AI 编译失败：" + repr(e))
        path = recorder.save_flow(name, nodes, extra={
            "source": "voice_demo", "raw_event_count": len(events),
            "compile_notes": notes})
        log("  ✅ 已保存脚本「%s」共 %d 步 -> %s" % (name, len(nodes), path))
        for n in notes[:5]:
            log("    · " + str(n))
        if OVN:
            OVN.set("已保存脚本「%s」\n共 %d 步，说「执行流程%s」重放" % (name, len(nodes), name))
    except Exception as e:
        log("  演示录制出错: " + repr(e))
    finally:
        _demo = None
        set_mode("listen")


def _handle_demo(text):
    """演示录制口令: 命中即开始; 录制中吞掉所有语音(只认停止词), 防误执行指令。"""
    global _demo
    if _demo is not None:
        if _is_demo_stop(text):
            _stop_demo_record()
        return True
    if _is_demo_wake(text):
        _start_demo_record(_demo_name_from(text))
        return True
    return False


def _execute_dag(steps):
    """重放演示录制的脚本: 按文字锚点运行时重新定位, 抗窗口移动/分辨率变化。"""
    try:
        import agent_core as ac
    except Exception as e:
        log("  agent_core 不可用: " + repr(e))
        return
    set_mode("command", "重放脚本")
    try:
        ok, lines, _res = ac.run_dag(steps, dry_run=False, reflect_retries=1,
                                     strict=bool(CONFIG.get("demo_strict", False)))
    except Exception as e:
        log("  重放出错: " + repr(e))
        if OVN:
            OVN.set("重放出错：" + str(e)[:40])
        return
    for line in (lines or [])[-20:]:
        log("  " + str(line))
    log("  脚本执行完毕: " + ("成功" if ok else "有步骤未通过(见日志)"))
    if OVN:
        OVN.set("脚本执行" + ("成功" if ok else "未完成") + "\n详见控制台/日志")


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
    set_mode("pending", "执行流程确认")   # 右上角状态: 待确认
    log("  [待确认] 执行流程「%s」共 %d 步？说 确认 或 取消" % (data.get("name", name), len(steps)))
    if OVN:
        OVN.set("执行流程「%s」共 %d 步？\n说 确认 或 取消" % (data.get("name", name), len(steps)))

def _execute_flow(steps):
    # 演示录制产出的脚本是 agent_core DAG(键"do"), 与语音宏(action/params)不同, 分路执行
    if steps and isinstance(steps[0], dict) and "do" in steps[0]:
        return _execute_dag(steps)
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
        self._subs = []            # 音频订阅者(按键听写复用同一路麦克风)
        # ---- VAD 引擎: silero(主, 鲁棒) / webrtcvad(兜底) ----
        self.engine = (CONFIG.get("vad_engine") or "webrtcvad").lower()
        if self.engine == "silero":
            try:
                from silero_vad import load_silero_vad, VADIterator
                self._silero_model = load_silero_vad()
                ms = int(float(CONFIG.get("hangover_s", 0.6)) * 1000)
                self._silero = VADIterator(self._silero_model, threshold=0.4,
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

    def subscribe(self, fn):
        """注册音频订阅者: 每块麦克风数据都会同步转发一份。

        为什么不给听写单独开一路 InputStream: PortAudio 在 WASAPI 下同一设备
        只允许一个独占流, 双开会直接报 PortAudioError。复用这一路最稳。
        """
        self._subs.append(fn)

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
        import time as _t
        # 预滚动缓冲(两个 VAD 引擎共用): 触发前先留一段, 否则句首会被吞掉
        self.preroll.append(data)
        if len(self.preroll) > PREROLL_FRAMES:
            self.preroll.pop(0)
        # 订阅者(按键听写): 录音期间独占音频, 命令 VAD 让位 —— 一句话只处理一次,
        # 否则听写的同时还会被当指令执行一遍。
        for fn in self._subs:
            try:
                fn(data)
            except Exception:
                pass
        if _dictation is not None and _dictation.recording:
            return
        import numpy as np
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
        # 必须归一化到 [-1,1], 否则 VAD 能量放大 ~1000 倍, 端点/语音判定全乱(没说完就识别/识别不准)
        a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        x = torch.from_numpy(a)
        try:
            out = self._silero(x, return_seconds=False)
        except Exception:
            out = None
        now = _t.time()
        if out:
            if "start" in out and not self.triggered:
                self.triggered = True
                # 补上预滚动: silero 判定 start 时首音节已经过去了, 不补就吞字
                self.seg = list(self.preroll) + [data]
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
    global _pending_flow, _tts_until
    # TTS 播放期间屏蔽麦克风回采(助手说话时麦克风会录到自己的声音)
    if time.time() < _tts_until:
        return
    if not ensure_asr_ready():
        log("  ASR 未就绪, 跳过本条")
        return
    text = transcribe(audio)
    if not text or not text.strip():
        return
    shown = text.rstrip("。.，,!！?？ ").strip()   # 去掉 SenseVoice 正常句末标点, 纯展示用
    log("")
    log("──────── 听到：" + shown)
    if OVN:
        OVN.set("听到：" + shown + "\n（理解中…）")
    set_mode("thinking")   # 右上角状态: 理解中
    # 演示录制口令(最高优先: 录制中要吞掉全部语音, 避免边操作边误触发指令)
    if _handle_demo(text):
        return
    # 录制宏 meta 指令(优先, 避免"停止录制"被 STOP_WORDS 的"停止"误判退出)
    if _handle_macro(text):
        return
    # 决策引擎切换口令: "用laya"/"用原模型"/"自动模式" —— 说一句即切, 并写回 config.json
    _sw = decision.match_switch(text)
    if _sw:
        ok, msg = decision.set_engine(_sw)
        log("  [引擎] " + msg)
        if OVN:
            OVN.set("决策引擎已切换：\n" + {"llm": "原模型(LLM)", "laya": "Laya 决策模型",
                                            "auto": "自动(Laya 优先)"}.get(_sw, _sw))
        return
    # 技能指令(学习/执行/录制/列出/删除/聊天开关)
    if _handle_skill(text):
        return
    # 技能快触发(借鉴 harvis 的 skills-first 路由): 短口令直接命中技能触发词, 免说"执行技能"前缀。
    # 仅限 ≤12 字的短句 + 触发词子串命中, 长句/聊天仍走 LLM 意图解析, 防误触; 执行仍需确认, 与「执行技能 X」同流程。
    if len(text) <= 12:
        _qsk = skills.match_skill(text)
        if _qsk and _qsk.get("steps"):
            log("  [快触发] 命中技能「%s」" % _qsk.get("name", ""))
            global _pending_flow
            _pending_flow = _qsk["steps"]
            set_mode("pending", "执行技能确认")
            log("  [待确认] 执行技能「%s」共 %d 步？说 确认 或 取消" % (_qsk.get("name", ""), len(_qsk["steps"])))
            if OVN: OVN.set("执行技能「%s」共 %d 步？\n说 确认 或 取消" % (_qsk.get("name", ""), len(_qsk["steps"])))
            return
    # 停止词(最高优先级): 仅裸『退出/停止』或『退出助手/退出程序』才退, 『退出记事本』等不算
    if is_stop_command(text):
        log("收到停止指令, 退出中...")
        global RUNNING
        RUNNING = False
        return
    # 待确认状态: 流程/技能重放确认(优先)
    if _pending_flow is not None:
        set_mode("pending", "执行确认")   # 右上角状态: 待确认
        if any(w in text for w in CONFIRM_WORDS):
            steps = _pending_flow
            _pending_flow = None
            log("  确认执行流程/技能")
            _execute_flow(steps)
            return
        if any(w in text for w in CANCEL_WORDS):
            log("  已取消流程/技能执行")
            _pending_flow = None
            set_mode("listen")
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
    # 正常解析(引擎由 config.decision_engine 决定: llm / laya / auto)
    intent = decision.intent(text)
    _eng = intent.get("engine", "?")
    if _eng == "laya":
        log("  [引擎] Laya  conf=%s  %dms" % (intent.get("confidence"), intent.get("ms", 0)))
    elif intent.get("_fallback"):
        log("  [引擎] 原模型(回退)  Laya=%s" % (intent.get("_laya") or {}).get("action"))
    else:
        log("  [引擎] 原模型(LLM)")
    ok, reason = validate_intent(intent)
    if not ok:
        log("  拒绝执行(校验失败): " + reason)
        if OVN:
            OVN.set("拒绝：" + reason)
        return
    action = intent.get("action")
    # 录制态: 把这条有效指令记进当前流程 / 技能
    if _recording is not None and action != "none":
        _recording["steps"].append(intent)
        log("  [录制 %d] %s %s" % (len(_recording["steps"]), action,
                                   json.dumps(intent.get("params", {}), ensure_ascii=False)))
    if _skill_recording is not None and action != "none":
        _skill_recording["steps"].append(intent)
        log("  [技能录制 %d] %s %s" % (len(_skill_recording["steps"]), action,
                                       json.dumps(intent.get("params", {}), ensure_ascii=False)))
    # 对话路由: 非命令语音 -> 多轮对话(可选 TTS)
    if action == "none":
        if _dialogue is not None and CONFIG.get("chat_enabled", True) and len(text.strip()) > 1:
            reply = _dialogue.ask(text)
            log("助手: " + reply)
            set_mode("chat")   # 右上角状态: 聊天模式
            if OVN:
                OVN.set("助手：" + reply)
            # 粗略估计朗读时长, 期间屏蔽麦克风, 避免助手听到自己声音又识别
            _tts_until = time.time() + max(2.0, len(reply) * 0.22)
            _dialogue.speak(reply)
            return
        if intent.get("reply"):
            log("助手: " + intent.get("reply"))
            set_mode("chat")   # 右上角状态: 聊天模式(无对话模块时仍按闲聊显示)
            if OVN:
                OVN.set("助手：" + intent.get("reply"))
        return
    # 高风险动作二次确认(可选)
    if CONFIG.get("confirm_high_risk") and action in HIGH_RISK_ACTIONS:
        _pending["intent"] = intent
        set_mode("pending", str(action))   # 右上角状态: 待确认
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
    global RUNNING, _recording, _demo
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
                elif _demo is not None:
                    _stop_demo_record()
                    log("  [热键 %s] 已停止演示录制" % rec_key.upper())
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
    # 置顶小窗(可选, 失败也不影响主功能) —— 先弹窗, 感知启动更快
    try:
        OVN = Overlay()
        if not OVN.start():
            log("置顶窗未启动(仅控制台输出)")
            OVN = None
    except Exception as e:
        log("置顶窗不可用(仅控制台输出): " + repr(e))
        OVN = None
    if OVN:
        OVN.set("启动中…\n（模型后台加载，稍候即可说话）")
    # ASR 后端(后台加载, 不阻塞 UI; 若 config.asr_mode=server 则立即可用)
    build_asr_backend()
    detect_model()
    # 对话模块(多轮 + 可选 TTS)
    global _dialogue
    _dialogue = dialogue.Dialogue(CONFIG["llm_base"], model_id=_MODEL_ID,
                                 tts_enabled=CONFIG.get("tts", True))
    log("对话模块就绪" + ("" if CONFIG.get("tts", True) else "（TTS 关闭）"))
    device = choose_mic()
    lis = Listener(device)
    # ---- 按键听写(Typeless 式): 悬浮指示器 + 全局热键, 复用同一路麦克风 ----
    if CONFIG.get("dictation_enabled", True):
        global _dictation, _dictation_ui
        try:
            _dictation_ui = dictation_ui.DictationUI(CONFIG)
            if not _dictation_ui.start():
                log("听写指示器未启动(仅日志输出)")
                _dictation_ui = None
        except Exception as e:
            log("听写指示器不可用(仅日志输出): " + repr(e)[:80])
            _dictation_ui = None
        try:
            _dictation = dictation.Dictation(CONFIG, ui=_dictation_ui, log=log)
            _dictation.bind_asr(
                lambda a, partial=False: transcribe(a, partial=partial, command=False))
            if _dictation.start_hotkeys():
                lis.subscribe(_dictation.feed)
        except Exception as e:
            log("按键听写不可用: " + repr(e)[:80])
            _dictation = None
    threading.Thread(target=stop_hotkey_watcher, daemon=True).start()
    try:
        lis.start()
    except KeyboardInterrupt:
        log("用户中断")
    finally:
        RUNNING = False
        if _demo is not None:      # 退出前把正在录的演示存下来, 别白录
            try:
                _stop_demo_record()
                time.sleep(1.0)
            except Exception:
                pass
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
    elif len(_sys.argv) > 1 and _sys.argv[1] == "--demo-match":
        # 纯逻辑自检: 不开麦克风、不动鼠标
        ok = 0
        cases = [
            ("请你跟我这样做，我就跟你这样做", True),
            ("请你跟我这样做我就跟你这样做", True),
            ("跟我这样做", True),
            ("你跟我学一下", True),
            ("今天天气不错", False),
            ("打开记事本", False),
        ]
        for t, want in cases:
            got = _is_demo_wake(t)
            ok += (got == want)
            print("%-6s wake=%-5s want=%-5s | %s" % ("PASS" if got == want else "FAIL", got, want, t))
        for t in ("停止", "录好了", "就到这里", "打开微信"):
            print("  stop(%-6s)=%s" % (t, _is_demo_stop(t)))
        print("  name=%r" % _demo_name_from("请你跟我这样做 叫 打卡流程"))
        print("  fallback=%s" % json.dumps(_fallback_nodes(
            [{"kind": "click", "x": 10, "y": 20, "win": {"process": "notepad.exe"}},
             {"kind": "type", "text": "hi"}]), ensure_ascii=False))
        print("PASS %d/%d" % (ok, len(cases)))
    else:
        main()
