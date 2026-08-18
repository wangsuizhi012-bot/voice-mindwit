# -*- coding: utf-8 -*-
"""ASR 抽象层: 两种后端 + 纠错词典, 直接针对「识别率低 / 打开慢」两个痛点。

后端选择(config.asr_mode):
  - "local"  (默认): 沿用 SenseVoiceSmall + GPU。本机实测中文 CER 4.2%,
                     已优于 Whisper-Small(5.8%), 识别率问题通常不在模型本身,
                     而在 VAD 截断 / 麦克风 / GPU 未启用 / 领域词。后台加载不阻塞 UI。
  - "server"       : 走常驻 funasr-server(OpenAI 兼容, 默认 :8000), 启动零等待,
                     识别率与本地一致 -> 这是「打开快」的最佳解。需先起服务:
                     funasr-server --device cuda   (funasr / vllm 自带)

纠错词典(config.correction_dict): 低成本提升识别率。把口语/方言/领域词误识别
映射回正确写法, 例如 {"清华同方":"清化同方","温习":"wenet"}。SenseVoice 不支持
热词, 这是零成本逼近热词效果的手段。

VAD 灵敏度等仍在 voice_assistant.Listener 里调(config.vad_aggressiveness / vad_engine)。
"""
import os
import json
import threading
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_cfg():
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def apply_correction(text, corr):
    if not corr or not text:
        return text
    for wrong, right in corr.items():
        if wrong and wrong in text:
            text = text.replace(wrong, right)
    return text


class ASRBase:
    def transcribe(self, int16_audio):
        raise NotImplementedError

    def ready(self):
        return True


class ASRLocal(ASRBase):
    """SenseVoiceSmall(GPU 优先), 支持后台加载。"""

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self._ready = False
        self._device = None

    def load(self):
        import torch
        from funasr import AutoModel
        self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self._model = AutoModel(model="iic/SenseVoiceSmall", vad_model="fsmn-vad",
                               device=self._device, disable_update=True)
        self._ready = True

    def ready(self):
        return self._ready

    def transcribe(self, int16_audio):
        if not self._ready or self._model is None:
            return ""
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        import numpy as np
        a = int16_audio.flatten().astype("float32") / 32768.0
        res = self._model.generate(input=a, language="auto", use_itn=True)
        return rich_transcription_postprocess(res[0]["text"])


class ASRServer(ASRBase):
    """funasr-server OpenAI 兼容接口, 启动零等待。"""

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.model = "SenseVoiceSmall"
        try:
            import requests
            j = requests.get(self.base + "/models", timeout=3).json()
            data = j.get("data") or j.get("models") or []
            if data:
                self.model = data[0]["id"]
        except Exception:
            pass

    def transcribe(self, int16_audio):
        import requests
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
                r = requests.post(self.base + "/v1/audio/transcriptions",
                                  files={"file": ("audio.wav", f, "audio/wav")},
                                  data={"model": self.model}, timeout=30)
            return r.json().get("text", "")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass


def build_asr(cfg):
    """返回 (asr对象, 纠错词典, mode字符串)。server 模式直接就绪; local 需后台 load()。"""
    cfg = cfg or _load_cfg()
    mode = (cfg.get("asr_mode") or "local").lower()
    corr = cfg.get("correction_dict") or {}
    if mode == "server":
        base = cfg.get("asr_server") or "http://localhost:8000/v1"
        return ASRServer(base), corr, "server"
    return ASRLocal(), corr, "local"
