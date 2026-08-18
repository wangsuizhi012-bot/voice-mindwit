# -*- coding: utf-8 -*-
"""技能存储: 复用 macro 的「意图步骤」格式(intent JSON 列表), 增加触发词/来源/说明。

技能文件存 skills/<name>.json:
  {
    "name": "发邮件",
    "triggers": ["发邮件", "发送邮件"],
    "steps": [ {"action":"open","params":{"app":"记事本"}}, ... ],
    "source": "trained | manual",
    "note": "",
    "created": "2026-08-18 23:00:00"
  }

steps 与 macro 的 flow 完全同构 -> 执行时直接复用 _execute_flow()。
"""
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(HERE, "skills")


def _safe(name):
    return "".join(ch for ch in (name or "").strip() if ch not in '\\/:*?"<>|').strip()


def save_skill(name, steps, triggers=None, source="manual", note=""):
    if not name or not steps:
        return None
    name = _safe(name)
    if not name:
        return None
    try:
        os.makedirs(SKILLS_DIR, exist_ok=True)
    except Exception:
        return None
    path = os.path.join(SKILLS_DIR, name + ".json")
    data = {
        "name": name,
        "triggers": triggers or [name],
        "steps": steps,
        "source": source,
        "note": note,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None


def load_skill(name):
    name = _safe(name)
    if not name:
        return None
    path = os.path.join(SKILLS_DIR, name + ".json")
    if not os.path.exists(path):
        try:
            for f in sorted(os.listdir(SKILLS_DIR)):
                if f.endswith(".json") and name in f[:-5]:
                    path = os.path.join(SKILLS_DIR, f)
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


def list_skills():
    if not os.path.isdir(SKILLS_DIR):
        return []
    try:
        return sorted(f[:-5] for f in os.listdir(SKILLS_DIR) if f.endswith(".json"))
    except Exception:
        return []


def delete_skill(name):
    name = _safe(name)
    if not name:
        return False
    path = os.path.join(SKILLS_DIR, name + ".json")
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    return False


def match_skill(text):
    """按触发词/名称匹配技能, 命中返回技能 dict, 否则 None。"""
    text = (text or "").lower()
    for name in list_skills():
        sk = load_skill(name)
        if not sk:
            continue
        for t in (sk.get("triggers") or [sk.get("name", "")]):
            if t and t.lower() in text:
                return sk
    return None
