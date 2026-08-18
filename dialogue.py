# -*- coding: utf-8 -*-
"""对话模块: 多轮闲聊 / 问答, 可选 TTS 朗读(Windows SAPI, 零依赖, 全程本地)。

- 意图被判定为 none(非命令)的语音, 默认走这里做多轮对话, 回复显示在置顶小窗并可选朗读。
- 也可说「聊天模式」进入连续对话, 「退出聊天 / 清空对话」退出或清历史。
- 语音回复走 win32com SAPI(SAPI.SpVoice), 不依赖任何外部 TTS 服务, 符合低成本本地化。
"""
import os
import json
import threading
import requests

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


def _detect_model(llm_base):
    try:
        r = requests.get(llm_base.rstrip("/") + "/models", timeout=5)
        j = r.json()
        data = j.get("data") or j.get("models") or []
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return "local-model"


DEFAULT_SYS = (
    "你是用户的本地语音助手，用简洁自然的中文口语回答，像朋友聊天一样。"
    "不要输出 markdown 代码块、不要列 1.2.3. 编号清单，直接说话。"
    "回答控制在 3 句话以内，信息密度高。用户用语音提问，你是在用嘴回答他。"
)


class Dialogue:
    def __init__(self, llm_base, model_id=None, tts_enabled=True,
                 system_prompt=None, max_history=10):
        self.llm_base = llm_base.rstrip("/")
        self.model_id = model_id or _detect_model(self.llm_base)
        self.tts_enabled = bool(tts_enabled)
        self.sys = system_prompt or DEFAULT_SYS
        self.max_history = max_history
        self.history = []
        self._spk = None
        self._spk_lock = threading.Lock()
        self._spk_unavailable = False

    # ---- TTS (Windows SAPI, 零依赖) ----
    def _get_speaker(self):
        if not self.tts_enabled or self._spk_unavailable:
            return None
        if self._spk is not None:
            return self._spk
        try:
            import win32com.client
            spk = win32com.client.Dispatch("SAPI.SpVoice")
            try:
                voices = spk.GetVoices()
                for v in voices:
                    desc = (v.GetDescription() or "")
                    lang = ""
                    try:
                        lang = v.GetAttribute("Language") or ""
                    except Exception:
                        lang = ""
                    if any(k in (desc + lang) for k in
                           ("Chinese", "中文", "ZH", "409", "Huihui", "Yaoyao", "Kangkang", "Xiaoxiao")):
                        spk.Voice = v
                        break
            except Exception:
                pass
            self._spk = spk
        except Exception:
            self._spk_unavailable = True
            self._spk = None
        return self._spk

    def speak(self, text):
        spk = self._get_speaker()
        if not spk:
            return
        t = (text or "").replace("*", "").replace("`", "").replace("#", "")
        try:
            with self._spk_lock:
                spk.Speak(t)
        except Exception:
            pass

    def reset(self):
        self.history = []

    def ask(self, text):
        """返回助手回答(纯文本)。多轮上下文保留最近 max_history 轮。"""
        text = (text or "").strip()
        if not text:
            return ""
        self.history.append({"role": "user", "content": text})
        recent = self.history[-self.max_history * 2:] if self.max_history else self.history
        msgs = [{"role": "system", "content": self.sys}] + recent
        try:
            r = requests.post(self.llm_base + "/chat/completions", json={
                "model": self.model_id,
                "messages": msgs,
                "temperature": 0.7,
                "max_tokens": 400,
            }, timeout=45)
            ans = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            ans = "（对话模型调用失败：" + repr(e) + "）"
        if ans:
            self.history.append({"role": "assistant", "content": ans})
        return ans
