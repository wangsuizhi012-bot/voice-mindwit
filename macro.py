# -*- coding: utf-8 -*-
"""录制宏: 把一串语音指令录成命名流程, 一句话重放(借鉴 waterRPA 的脚本播放思路)。

流程文件存 flows/<name>.json, 形如:
  {"name": "xxx", "steps": [{"action":"open","params":{"app":"记事本"}}, ...]}
"""
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
FLOWS_DIR = os.path.join(HERE, "flows")


def _safe_name(name):
    return "".join(ch for ch in (name or "").strip() if ch not in '\\/:*?"<>|').strip()


def save_flow(name, steps):
    if not name or not steps:
        return None
    name = _safe_name(name)
    if not name:
        return None
    try:
        os.makedirs(FLOWS_DIR, exist_ok=True)
    except Exception:
        return None
    path = os.path.join(FLOWS_DIR, name + ".json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"name": name, "steps": steps}, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None


def load_flow(name):
    name = _safe_name(name)
    if not name:
        return None
    path = os.path.join(FLOWS_DIR, name + ".json")
    if not os.path.exists(path) and os.path.isdir(FLOWS_DIR):
        # 子串兜底: 说名字的一部分也能找到
        try:
            for f in sorted(os.listdir(FLOWS_DIR)):
                if f.endswith(".json") and name in f[:-5]:
                    path = os.path.join(FLOWS_DIR, f)
                    break
        except Exception:
            pass
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_flows():
    if not os.path.isdir(FLOWS_DIR):
        return []
    try:
        return sorted(f[:-5] for f in os.listdir(FLOWS_DIR) if f.endswith(".json"))
    except Exception:
        return []
