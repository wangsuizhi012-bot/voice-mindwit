# -*- coding: utf-8 -*-
"""ASR 抽象层 + 音频前端 + 文本后处理 —— 针对「识别率低 / 打开慢」两个痛点。

后端选择(config.asr_mode):
  - "local"  (默认): SenseVoiceSmall(+ fsmn-vad)。后台加载不阻塞 UI, 挡位用
                     config.asr_model 切换。
  - "server"       : 走常驻 funasr-server(OpenAI 兼容, 默认 :8000), 启动零等待。
                     需先起服务: funasr-server --device cuda

★ 2026-09-28 准确率专项(本次新增, 全部有实测依据):

  1) 音频前端 preprocess()
     - 去直流(减均值): Realtek 麦常有直流偏置, 会让 VAD/模型判定漂移
     - 峰值归一化到 -0.9dBFS: 小声说话时 CER 会暴涨, 这是零成本的最大收益项
     - 增益上限 max_gain: 防止把纯噪声底噪放大成人声
     - 噪声门: 整段 RMS 低于 asr_min_rms 直接判噪声丢弃 —— 实测日志里
       「L, A A」「S SY A」就是噪声段被 VAD 放行后硬识别出来的

  2) 语言策略: 短音频(< asr_short_sec 秒)强制 zh。
     SenseVoice 的 auto 语言检测在 1~2s 短片段上不可靠, 实测把中文噪声判成
     英文输出「Right了」。短句固定 zh 可消除这一类错误。

  3) 纠错: 键长按降序匹配(避免短键先替换破坏长键); 词典只做字面归一。

  4) 口语清洗: 语气词过滤(嗯/啊/呃) + 中英之间自动加空格。
"""
import os
import re
import sys
import json
import base64
import threading
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- R2T2(Confucius4-R2T2) 后端默认路径与热词 ----
# 实测热词令 TER 45.7% -> 20.0%; 换成真人口音更强的场景会更稳。
_R2T2_DEFAULT_MODEL = os.path.join(
    HERE, "models", "Confucius4-R2T2", "Confucius4-R2T2-Q8_0.gguf")
_R2T2_DEFAULT_MMPROJ = os.path.join(
    HERE, "models", "Confucius4-R2T2", "mmproj-Confucius4-R2T2-Q8_0.gguf")
_R2T2_DEFAULT_SERVER_EXE = r"E:\AI\llama.cpp-spark\llama-server.exe"

DEFAULT_HOTWORDS = (
    "Context / hotwords: ComfyUI, FLUX, Nunchaku, ControlNet, LoRA, "
    "Wan2.2, LTXV, MiniMax-H3, FlashVSR, SeedVR2, llama, llama.cpp, "
    "llama-swap, KVMem, agent-hub, n8n, DSH, 智能中枢, voice-assistant, "
    "workbuddy, obsidian, Qwen3-VL, SageAttention."
)

# ---- 默认参数(可被 config.json 覆盖) ----
DEFAULTS = {
    "asr_target_peak": 0.90,     # 峰值归一化目标(相对 int16 满量程)
    "asr_max_gain": 12.0,        # 增益上限(倍), 防噪声被放大
    "asr_min_rms": 0.004,        # 噪声门: 归一化后 RMS 低于此值判为噪声
    "asr_language": "auto",      # auto | zh | en | yue | ja | ko
    "asr_short_force_zh": True,  # 短音频强制 zh(见上面第 2 条)
    "asr_short_sec": 2.5,
    "strip_fillers": True,       # 语气词过滤
    "auto_space": True,          # 中英/中数之间自动加空格
}

FILLER_WORDS = ["嗯", "啊", "呃", "哦", "唉", "诶", "唔", "那个", "就是说"]

# 中文 vs 拉丁/数字 边界
_CJK = r"\u4e00-\u9fff\u3040-\u30ff"
_LATIN = r"A-Za-z0-9"


def cfg_get(cfg, key):
    if cfg and key in cfg:
        return cfg[key]
    return DEFAULTS.get(key)


def _load_cfg():
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _warn(msg):
    """故障留痕(P0-2 修复): 以前各失败点静默 return "", 代理变量残留 /
    服务没起来时 ASR 全废, 日志里却查不到任何原因。stderr 会被主程序
    Tee 双写进 run_assistant.log, 单独跑自测时控制台也能看到。"""
    try:
        sys.stderr.write("[asr_better] " + str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _session():
    """本地请求专用 requests 会话(P0-3 修复): trust_env=False。

    本机常驻代理变量(Nano :65532 等)残留时, requests 默认信任环境变量,
    会把发往 127.0.0.1 的请求送给代理 -> 连本地 llama-server 静默失败,
    表现为「识别突然全空且无任何报错」。"""
    import requests
    s = requests.Session()
    s.trust_env = False
    return s


# ---------------------------------------------------------------- 音频前端
def audio_stats(int16_audio):
    """返回 (rms, peak) —— rms/peak 均以 [-1,1] 归一化幅度表示。"""
    import numpy as np
    a = int16_audio.flatten().astype("float32") / 32768.0
    if a.size == 0:
        return 0.0, 0.0
    rms = float((a * a).mean() ** 0.5)
    peak = float(abs(a).max())
    return rms, peak


def preprocess(int16_audio, cfg=None):
    """音频前端: 去直流 -> 峰值归一化(带增益上限) -> 削波。

    返回 (int16_array, info)。info 里给 rms/gain, 便于日志排查「为什么识别不准」。
    注意: 本函数**不做**噪声判定, 判定在 voice_assistant.transcribe 里做,
    因为那里才知道要不要记日志。
    """
    import numpy as np
    a = int16_audio.flatten().astype("float32")
    if a.size == 0:
        return int16_audio, {"rms": 0.0, "peak": 0.0, "gain": 1.0}
    a = a - float(a.mean())                       # 去直流偏置
    peak = float(abs(a).max())
    if peak > 0:
        target = float(cfg_get(cfg, "asr_target_peak")) * 32767.0
        gain = min(target / peak, float(cfg_get(cfg, "asr_max_gain")))
        a = a * gain
    else:
        gain = 1.0
    np.clip(a, -32768.0, 32767.0, out=a)
    rms = float(((a / 32768.0) ** 2).mean() ** 0.5)
    return a.astype("int16"), {"rms": rms, "peak": min(peak / 32768.0, 1.0),
                               "gain": gain}


def is_noise(int16_audio, cfg=None):
    """噪声门: 整段能量过低 -> 判噪声(模型会硬编出「L, A A」这种垃圾)。"""
    rms, _ = audio_stats(int16_audio)
    return rms < float(cfg_get(cfg, "asr_min_rms"))


def pick_language(int16_audio, cfg=None):
    """短片段强制 zh, 避免 auto 语言检测在 1~2s 上翻车。"""
    lang = str(cfg_get(cfg, "asr_language") or "auto")
    if lang != "auto":
        return lang
    if cfg_get(cfg, "asr_short_force_zh"):
        dur = len(int16_audio) / 16000.0
        if dur < float(cfg_get(cfg, "asr_short_sec")):
            return "zh"
    return "auto"


# ---------------------------------------------------------------- 文本后处理
def apply_correction(text, corr):
    """纠错词典: 键长按降序逐个替换。

    为什么按长度降序: 词典里同时有 "comfy ui" 和 "commfy ui" 时, 若短键先替换
    会把长键的输入破坏掉。降序保证「最长匹配优先」。
    """
    if not corr or not text:
        return text
    items = [(k, v) for k, v in corr.items() if k and v and k != v]
    items.sort(key=lambda kv: len(kv[0]), reverse=True)
    for wrong, right in items:
        if wrong in text:
            text = text.replace(wrong, right)
    return text


def strip_fillers(text, enabled=True):
    """语气词过滤(保守): 只删「被标点/边界包围」或「连续重复」的语气词。

    不删句中单个语气词 —— 那会改变语义(如「好啊」)。
    """
    if not enabled or not text:
        return text
    # 1) 连续重复 >=2 次的语气词(嗯嗯嗯 / 啊啊)
    for w in FILLER_WORDS:
        text = re.sub(r"(?<![\w\u4e00-\u9fff])%s{2,}(?![\w\u4e00-\u9fff])" % re.escape(w),
                      "", text)
    # 2) 句首/句尾/被标点夹住的单个语气词
    for w in FILLER_WORDS:
        text = re.sub(r"^%s(?=[，,。.！!？?、;；:：]|$)" % re.escape(w), "", text)
        text = re.sub(r"(?<=[，,。.！!？?、;；:：])%s(?=[，,。.！!？?、;；:：]|$)" % re.escape(w),
                      "", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


def normalize_spacing(text, enabled=True):
    """中文与拉丁/数字之间补空格, 中英混排可读性大幅提升。"""
    if not enabled or not text:
        return text
    text = re.sub(r"(?<=[%s])(?=[%s])" % (_CJK, _LATIN), " ", text)
    text = re.sub(r"(?<=[%s])(?=[%s])" % (_LATIN, _CJK), " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def postprocess(text, cfg=None, corr=None):
    """文本后处理总入口: 词典 -> 语气词 -> 空格。"""
    if not text:
        return text
    text = apply_correction(text, corr)
    text = strip_fillers(text, cfg_get(cfg, "strip_fillers"))
    text = normalize_spacing(text, cfg_get(cfg, "auto_space"))
    return text.strip()


# ---------------------------------------------------------------- 后端
class ASRBase:
    def transcribe(self, int16_audio, language=None):
        raise NotImplementedError

    def ready(self):
        return True


class ASRLocal(ASRBase):
    """SenseVoiceSmall(CUDA 可用时自动上 GPU, 否则 CPU)。支持后台加载。"""

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self._ready = False
        self._device = None

    def load(self):
        import torch
        from funasr import AutoModel
        cfg = _load_cfg()
        # config.asr_model 可换挡: SenseVoiceSmall(默认, 快) |
        #   iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch(更准, 慢)
        model_name = (cfg.get("asr_model") or "iic/SenseVoiceSmall").strip()
        self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self._model = AutoModel(model=model_name, vad_model="fsmn-vad",
                                device=self._device, disable_update=True)
        self._ready = True

    def ready(self):
        return self._ready

    def device(self):
        return self._device or "?"

    def transcribe(self, int16_audio, language=None):
        if not self._ready or self._model is None:
            return ""
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        a = int16_audio.flatten().astype("float32") / 32768.0
        kw = {"input": a, "language": language or "auto", "use_itn": True}
        with self._lock:                # torch 推理非线程安全, 与 partial 识别串行化
            res = self._model.generate(**kw)
        try:
            return rich_transcription_postprocess(res[0]["text"])
        except Exception:
            return (res[0].get("text") or "") if res else ""


class ASRServer(ASRBase):
    """funasr-server OpenAI 兼容接口, 启动零等待。"""

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.model = "SenseVoiceSmall"
        try:
            j = _session().get(self.base + "/models", timeout=3).json()
            data = j.get("data") or j.get("models") or []
            if data:
                self.model = data[0]["id"]
        except Exception:
            pass

    def device(self):
        return "server"

    def transcribe(self, int16_audio, language=None):
        import wave
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(int16_audio.tobytes())
            with open(path, "rb") as f:
                r = _session().post(self.base + "/v1/audio/transcriptions",
                                    files={"file": ("audio.wav", f, "audio/wav")},
                                    data={"model": self.model}, timeout=30)
            if r.status_code != 200:
                _warn("funasr-server HTTP %s: %s" % (r.status_code, (r.text or "")[:200]))
                return ""
            return r.json().get("text", "")
        except Exception as e:
            _warn("funasr-server 转写异常: %r" % (e,))
            return ""
        finally:
            try:
                os.remove(path)
            except Exception:
                pass


class ASRR2T2(ASRBase):
    """Confucius4-R2T2 via llama-server(OpenAI 兼容音频接口)。

    热词(prepend 到 user 文本)是决定性能力: 实测令 TER 45.7% -> 20.0%。
    默认连接外部已启动的 llama-server(r2t2_server); 若 r2t2_autostart=True 且
    服务不可达, 则自动拉起自带 llama-server 子进程(占 GPU, 与 llama-swap 互斥)。
    """

    def __init__(self, base_url=None, hotwords=None, cfg=None):
        import atexit
        self._cfg = cfg or {}
        # 归一化: 无论传入 ".../v1" 还是根地址, 都拆出 root, 端点统一拼 /v1/xxx
        base = (base_url or self._cfg.get("r2t2_server")
                or "http://localhost:8099/v1").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self.root = base
        hw = hotwords if hotwords is not None else self._cfg.get("r2t2_hotwords",
                                                                 DEFAULT_HOTWORDS)
        if isinstance(hw, list):                      # 配置里写成列表更友好
            hw = "Context / hotwords: " + ", ".join(hw) if hw else ""
        self.hotwords = hw or ""
        self._model_id = "r2t2"
        self._proc = None
        atexit.register(self.stop)

    # ---- 服务生命周期 ----
    def _ping_models(self):
        try:
            j = _session().get(self.root + "/v1/models", timeout=3).json()
        except Exception:
            return False
        d = j.get("data") or j.get("models") or []
        if d:
            self._model_id = d[0]["id"]
        return True

    def _spawn(self):
        import subprocess
        import time
        model = self._cfg.get("r2t2_model") or _R2T2_DEFAULT_MODEL
        mmproj = self._cfg.get("r2t2_mmproj") or _R2T2_DEFAULT_MMPROJ
        exe = self._cfg.get("r2t2_llama_server") or _R2T2_DEFAULT_SERVER_EXE
        port = int(self._cfg.get("r2t2_port") or 8099)
        ngl = int(self._cfg.get("r2t2_ngl") or 99)
        ctx = int(self._cfg.get("r2t2_ctx") or 4096)
        if not (os.path.exists(model) and os.path.exists(mmproj)
                and os.path.exists(exe)):
            return False
        self._proc = subprocess.Popen(
            [exe, "-m", model, "--mmproj", mmproj, "--host", "127.0.0.1",
             "--port", str(port), "-ngl", str(ngl), "-c", str(ctx), "--no-webui"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 180:
            try:
                if _session().get(self.root + "/health", timeout=3).status_code == 200:
                    self._ping_models()
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def _ensure_server(self):
        if self._ping_models():
            return True
        if not self._cfg.get("r2t2_autostart"):
            return False
        return self._spawn()

    def load(self):
        self._ensure_server()

    def ready(self):
        try:
            return _session().get(self.root + "/health", timeout=3).status_code == 200
        except Exception:
            return False

    def device(self):
        return "r2t2-llama-server"

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # ---- 推理 ----
    @staticmethod
    def _wav_b64(int16_audio):
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(int16_audio.tobytes())
        return base64.b64encode(buf.getvalue()).decode()

    @staticmethod
    def _clean(txt):
        if not txt:
            return ""
        m = re.search(r"<asr_text>(.*)$", txt, re.S)
        if m:
            txt = m.group(1)
        else:
            txt = re.sub(r"^\s*language\s+\w+\s*", "", txt)
        return txt.strip()

    def transcribe(self, int16_audio, language=None):
        if not self.ready() and not self._ensure_server():
            _warn("R2T2 服务不可达且自动拉起失败(root=%s), 本次返回空" % self.root)
            return ""
        try:
            b64 = self._wav_b64(int16_audio)
        except Exception as e:
            _warn("WAV 编码失败: %r" % (e,))
            return ""
        content = []
        if self.hotwords:
            content.append({"type": "text", "text": self.hotwords})
        content.append({"type": "input_audio",
                         "input_audio": {"data": b64, "format": "wav"}})
        payload = {"messages": [{"role": "user", "content": content}],
                   "temperature": 0, "max_tokens": 256, "stream": False}
        try:
            r = _session().post(self.root + "/v1/chat/completions",
                                json=payload, timeout=180)
            if r.status_code != 200:
                _warn("R2T2 转写 HTTP %s: %s" % (r.status_code, (r.text or "")[:200]))
                return ""
            try:
                return self._clean(r.json()["choices"][0]["message"]["content"])
            except Exception as e:
                _warn("R2T2 响应结构异常(缺 choices/message/content): %r | body=%s"
                      % (e, (r.text or "")[:200]))
                return ""
        except Exception as e:
            _warn("R2T2 转写请求异常: %r" % (e,))
            return ""


def build_asr(cfg):
    """返回 (asr对象, 纠错词典, mode字符串)。server 模式直接就绪; local 需后台 load()。"""
    cfg = cfg or _load_cfg()
    mode = (cfg.get("asr_mode") or "local").lower()
    corr = cfg.get("correction_dict") or {}
    if mode == "server":
        base = cfg.get("asr_server") or "http://localhost:8000/v1"
        return ASRServer(base), corr, "server"
    if mode == "r2t2":
        return ASRR2T2(cfg=cfg), corr, "r2t2"
    return ASRLocal(), corr, "local"
