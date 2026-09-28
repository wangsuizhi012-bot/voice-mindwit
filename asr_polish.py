# -*- coding: utf-8 -*-
"""ASR 文本后处理(LLM 纠错) —— 用本地大模型把「能听懂」变成「能直接读」。

为什么需要这一层:
    SenseVoice 是非自回归模型, 没有语言模型先验, 同音字错误(「蛇富」/「设伏」/
    「社服」)是它的固有短板, 词典只能覆盖已知词。本地 LLM 有完整语言先验,
    做「同音错字纠错 + 标点恢复」是投入产出比最高的一步, 且全程本地不出网。

★ 安全策略(关键, 否则小模型会自由发挥, 把原文改坏):
    1) 强约束 prompt: 只纠错, 不改写/不扩写/不翻译/不回答
    2) 相似度闸门: 输出与输入的字符相似度低于 min_similarity 直接丢弃
       —— 这是防「模型自由发挥」的兜底, 实测 GPT 级小模型最常见的失败就是扩写
    3) 长度闸门: 输出长度超出 [0.6x, 1.5x] 区间丢弃
    4) 超时 + 异常: 一律返回原文(降级, 绝不阻塞听写)
    5) 热词表 vocab 注入: 领域词给模型做偏置, 逼近 ASR 热词效果

用法:
    from asr_polish import polish
    new_text, note = polish(text, base, model, vocab, timeout=6.0)
"""
import re
import difflib

# 相似度闸门: 低于它说明模型在自由发挥, 宁可不改
MIN_SIMILARITY = 0.75

_SYS = (
    "你是语音识别结果的校对器。输入是一段中文语音转写文本，可能含有同音错字、"
    "缺少标点、中英文之间没有空格。\n"
    "你的任务：\n"
    "1. 根据上下文把明显的同音错字改成正确写法；\n"
    "2. 补上缺失的标点（句号、逗号、问号）；\n"
    "3. 中文与英文/数字之间补一个空格；\n"
    "4. 如果提供了【术语表】，文本中发音相近的词必须优先改成术语表里的写法。\n"
    "严格禁止：改变原意、增删内容、润色改写、翻译成英文、添加解释、输出标点以外"
    "的任何说明文字、使用 markdown、加粗、输出【术语表】等标签字样。\n"
    "只输出校对后的这一行文本本身。"
)


def _post(base, model, payload, timeout):
    import requests
    s = requests.Session()
    s.trust_env = False          # 回环必须直连, 否则被系统代理劫持
    if model:
        payload["model"] = model
    r = s.post(base.rstrip("/") + "/chat/completions", json=payload,
               timeout=timeout)
    return r.json()["choices"][0]["message"]["content"]


def _clean(out):
    """剥掉模型爱加的外壳: markdown / 加粗 / 引号 / 【标签】行 / 前缀说明。

    实测 spark 会 ① 把术语包成 **加粗** ② 把「【术语表】xxx」模板整段复读,
    这里逐项剥掉, 取剩下的最长一行交给骨架相似度把关。
    """
    if not out:
        return ""
    t = out.strip()
    t = t.replace("```", "")
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^【", ln)]      # 模板标签行
    lines = [re.sub(r"[*`>#_]+", "", ln).strip() for ln in lines]  # markdown 记号
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    best = max(lines, key=len)
    best = re.sub(r"^(校对后|校对结果|结果|输出|改正后)\s*[:：]\s*", "", best).strip()
    if len(best) >= 2 and best[0] == best[-1] and best[0] in "\"'“”‘’":
        best = best[1:-1].strip()
    return best


def _skeleton(s):
    """中文骨架: 剥掉拉丁字母/数字/空格/标点。

    为什么: 合法的「术语替换」(康福有爱 -> ComfyUI) 会大幅降低字符相似度,
    直接拿原文比会误杀。只比中文骨架 —— 改写句子会掉分, 换术语不会。
    """
    return re.sub(r"[A-Za-z0-9\s.,，。！!？?、;；:：'\"()（）\-\*#>【】\[\]]+", "", s or "")


def _similarity(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _skeleton(a), _skeleton(b)).ratio()


def polish(text, base=None, model=None, vocab=None, timeout=6.0,
           min_similarity=MIN_SIMILARITY):
    """返回 (校对后文本, 说明)。任何异常都返回原文 —— 绝不阻塞听写。"""
    if not text or not text.strip():
        return text, "空文本, 跳过"
    if not base:
        return text, "未配置 LLM 端点, 跳过校对"
    if vocab:
        vlist = [str(v) for v in vocab if str(v).strip()]
    else:
        vlist = []
    user = text
    if vlist:
        user = "【术语表】" + "、".join(vlist[:60]) + "\n【原文本】" + text
    payload = {
        "messages": [{"role": "system", "content": _SYS},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max(128, min(1024, len(text) * 3 + 128)),
    }
    try:
        out = _post(base, model, payload, timeout)
    except Exception as e:
        return text, "校对不可用(%s), 保留原文" % repr(e)[:60]

    new = _clean(out)
    if not new:
        return text, "校对返回空, 保留原文"
    if new == text:
        return text, "校对无改动"
    # ---- 闸门 ----
    sim = _similarity(text, new)
    if sim < min_similarity:
        return text, "校对结果相似度 %.2f < %.2f, 判定为自由发挥, 保留原文" % (
            sim, min_similarity)
    lo, hi = len(text) * 0.5, len(text) * 2 + 16   # 长度闸门放宽: 主防线是骨架相似度
    if not (lo <= len(new) <= hi):
        return text, "校对结果长度异常(%d vs %d), 保留原文" % (len(new), len(text))
    return new, "已校对 (相似度 %.2f)" % sim
