# -*- coding: utf-8 -*-
"""专用词识别评测 —— 对应 `测试文档-专用词识别验收-2026-09-28.md`。

用法:
    :: 1) 用 Windows SAPI 合成 30 句 (16kHz/mono, 零人工, 可重复)
    python test_term_accuracy.py --make-tts

    :: 2) 评测并出报告
    python test_term_accuracy.py --set testset/term_tts
    python test_term_accuracy.py --set testset/term_tts --out 评测报告-专用词-TTS.md

    :: 其他
    --no-polish      跳过 LLM 校对(网关离线/只想看声学能力)
    --limit N        只跑前 N 条(快速冒烟)
    --no-va          不跑真实入口 voice_assistant.transcribe()(省一半时间)

三列口径(见测试文档 §4.3):
    raw   = 模型原始输出(仅音频前端, 无词典/无校对) —— 模型到底认没认出来
    post  = 原始 + 词典/语气词/空格 后处理        —— 词典能不能救
    final = 真实入口 voice_assistant.transcribe()  —— 含 LLM 校对

注意: TTS 念英文缩写比真人规范, 分数系统性偏高 —— 只用于回归对比, 不做绝对验收。
"""
import os
import re
import sys
import json
import time
import wave
import difflib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CASES = os.path.join(HERE, "testset", "term_cases.json")
DEFAULT_SET = os.path.join(HERE, "testset", "term_tts")

# 失败/阻断线(与测试文档 §5 保持一致)
BLOCK_TER = 0.20
TARGET_TER = 0.10
GOOD_TER = 0.05


# ---------------------------------------------------------------- 工具
_CN_DIGIT = str.maketrans({"零": "0", "一": "1", "二": "2", "两": "2", "三": "3",
                           "四": "4", "五": "5", "六": "6", "七": "7", "八": "8",
                           "九": "9", "点": "."})


def norm(s):
    """用于术语匹配: 小写 + 中文数字转阿拉伯 + 去掉 . - _ 空格。

    2026-09-29 补: 中文数字归一。R2T2 会把 `n8n` 写成 `n八n`、`MiniMax-H3` 写成
    `minimax h三` —— 这是**书写形式差异**, 不是识别错误, 若不算命中会系统性冤枉
    输出中文数字的引擎。测试文档 §4.1「数字必须一致」按此修订为「数字形式等价」。
    """
    s = (s or "").lower().translate(_CN_DIGIT)
    return re.sub(r"[\.\-_\s]+", "", s)


def hit(hay, term):
    return norm(term) in norm(hay)


def lev(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def skeleton(s):
    return re.sub(r"[\s，。！？、；：,.!?;:\"'()（）\-]+", "", (s or "").lower())


def cer(expected, got):
    e, g = skeleton(expected), skeleton(got)
    if not e:
        return 1.0
    return lev(e, g) / len(e)


def read_wav(path):
    """读成 16k 单声道 int16; 非 16k 用线性插值重采样。"""
    import numpy as np
    with wave.open(path, "rb") as w:
        sr, ch, sw, n = (w.getframerate(), w.getnchannels(),
                         w.getsampwidth(), w.getnframes())
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("only 16-bit PCM supported: %s" % path)
    a = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        x = np.linspace(0, len(a) - 1, int(len(a) * 16000.0 / sr))
        a = np.interp(x, np.arange(len(a)), a.astype("float32")).astype(np.int16)
    return a


# ---------------------------------------------------------------- TTS
def make_tts(limit=None):
    """Windows SAPI -> 16kHz/16bit/mono wav。"""
    import win32com.client
    cases = load_cases()["cases"]
    if limit:
        cases = cases[:limit]
    out = os.path.join(HERE, "testset", "term_tts")
    os.makedirs(out, exist_ok=True)
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    # 优先中文音色
    try:
        for i in range(voice.GetVoices().Count):
            v = voice.GetVoices().Item(i)
            desc = v.GetDescription()
            if "Chinese" in desc or "中文" in desc or "Huihui" in desc or "Yaoyao" in desc:
                voice.Voice = v
                print("TTS voice: %s" % desc)
                break
        else:
            print("TTS voice: (default) %s" % voice.Voice.GetDescription())
    except Exception as e:
        print("TTS voice select failed: %r" % e)
    SAFT16kHz16BitMono = 18
    SSFMCreateForWrite = 3
    ok = 0
    for c in cases:
        path = os.path.join(out, c["id"] + ".wav")
        st = win32com.client.Dispatch("SAPI.SpFileStream")
        try:
            st.Format.Type = SAFT16kHz16BitMono
            st.Open(path, SSFMCreateForWrite)
            voice.AudioOutputStream = st
            voice.Speak(c["text"])
            ok += 1
        except Exception as e:
            print("FAIL %s: %r" % (c["id"], e))
        finally:
            try:
                st.Close()
            except Exception:
                pass
    print("generated %d/%d wav -> %s" % (ok, len(cases), out))
    for c in cases[:2]:
        p = os.path.join(out, c["id"] + ".wav")
        if os.path.exists(p):
            with wave.open(p, "rb") as w:
                print("  %s: %dHz ch=%d %.2fs" % (c["id"], w.getframerate(),
                      w.getnchannels(), w.getnframes() / float(w.getframerate())))
    return 0


# ---------------------------------------------------------------- 评测
def load_cases():
    with open(CASES, encoding="utf-8") as f:
        return json.load(f)


def gateway_ok(cfg):
    try:
        import requests
        s = requests.Session()
        s.trust_env = False
        r = s.get(cfg.get("llm_base", "").rstrip("/") + "/models", timeout=4)
        return r.status_code == 200
    except Exception:
        return False


def evaluate(a, set_dir, out_path, no_polish=False, no_va=False, limit=None):
    import numpy as np
    data = load_cases()
    cases = data["cases"][:limit] if limit else data["cases"]
    total_terms = sum(len(c["terms"]) for c in cases)

    import asr_better
    import asr_polish
    import voice_assistant as va

    cfg = va.CONFIG
    device = "?"
    print("building ASR backend ...")
    t0 = time.time()
    asr, corr, mode = asr_better.build_asr(cfg)
    if mode == "local":
        asr.load()
        try:
            device = asr.device()
        except Exception:
            pass
    load_s = time.time() - t0
    print("ASR ready in %.1fs (mode=%s device=%s)" % (load_s, mode, device))

    # 真实入口需要这些全局
    va._asr = asr
    va._asr_corr = corr
    va._dictation_ui = None
    va._MODEL_ID = cfg.get("model") or None
    real_log = va.log
    va.log = lambda m: None          # 静音, 结果只进报告

    gw = gateway_ok(cfg)
    gwtxt = "在线" if gw else "离线（final 列将退化为 post）"
    print("gateway(:9292): %s | polish=%s" % (
        "up" if gw else "down", (not no_polish) and gw))

    rows = []
    t_start = time.time()
    for i, c in enumerate(cases, 1):
        wid = c["id"]
        path = os.path.join(set_dir, wid + ".wav")
        rec = {"id": wid, "group": c["group"], "text": c["text"],
               "terms": c["terms"], "raw": "", "post": "", "final": "",
               "note": ""}
        if not os.path.exists(path):
            rec["note"] = "缺音频文件"
            rec["miss"] = {"raw": list(c["terms"]), "post": list(c["terms"]),
                           "final": list(c["terms"])}
            rows.append(rec)
            print("[%2d/%d] %s MISSING" % (i, len(cases), wid))
            continue
        audio = read_wav(path)
        secs = len(audio) / 16000.0
        dropped = asr_better.is_noise(audio, cfg)
        if dropped:
            rec["note"] = "噪声门丢弃"
            rec["miss"] = {"raw": list(c["terms"]), "post": list(c["terms"]),
                           "final": list(c["terms"])}
            rows.append(rec)
            print("[%2d/%d] %s %.2fs DROPPED(noise gate)" % (i, len(cases), wid, secs))
            continue

        # --- raw: 只过音频前端 + ASR ---
        fin_audio, info = asr_better.preprocess(audio, cfg)
        lang = asr_better.pick_language(fin_audio, cfg)
        raw = (asr.transcribe(fin_audio, language=lang) or "").strip()
        # --- post: 加词典/语气词/空格 ---
        post = asr_better.postprocess(raw, cfg, corr)
        # --- final: 真实入口(含 LLM 校对) ---
        final = post
        if no_va:
            if not no_polish and gw:
                final, _ = asr_polish.polish(
                    post, base=cfg.get("llm_base"),
                    model=cfg.get("model"), vocab=cfg.get("asr_vocab") or [],
                    timeout=float(cfg.get("asr_polish_timeout_s", 6.0)))
        else:
            save = cfg.get("asr_polish")
            if no_polish:
                cfg["asr_polish"] = False
            final = (va.transcribe(audio, partial=False, command=False) or "").strip()
            cfg["asr_polish"] = save

        rec.update({"raw": raw, "post": post, "final": final,
                    "rms": round(info["rms"], 4), "gain": round(info["gain"], 2),
                    "sec": round(secs, 2)})
        rec["miss"] = {k: [t for t in c["terms"] if not hit(rec[k], t)]
                       for k in ("raw", "post", "final")}
        rows.append(rec)
        print("[%2d/%d] %s %.2fs  miss raw=%d post=%d final=%d" % (
            i, len(cases), wid, secs, len(rec["miss"]["raw"]),
            len(rec["miss"]["post"]), len(rec["miss"]["final"])))
    va.log = real_log
    elapsed = time.time() - t_start

    # ---------------------------------------------------------- 汇总
    def agg(col):
        miss = sum(len(r["miss"][col]) for r in rows)
        ser = sum(1 for r in rows if not r["miss"][col])
        cers = [cer(r["text"], r[col]) for r in rows]
        return {"miss": miss,
                "ter": miss / float(total_terms) if total_terms else 1.0,
                "ser": ser / float(len(rows)) if rows else 0.0,
                "cer": sum(cers) / len(cers) if cers else 1.0}

    S = {c: agg(c) for c in ("raw", "post", "final")}
    groups = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"n": 0, "terms": 0,
                                          "raw": 0, "post": 0, "final": 0})
        g["n"] += 1
        g["terms"] += len(r["terms"])
        for col in ("raw", "post", "final"):
            g[col] += len(r["miss"][col])

    must = []
    for mid in data.get("must_pass", []):
        r = next((x for x in rows if x["id"] == mid), None)
        if r:
            must.append((mid, r["text"], not r["miss"]["final"]))

    verdict = ("🔴 阻断（TER>20%）" if S["final"]["ter"] > BLOCK_TER else
               "🟢 优良（TER≤5%）" if S["final"]["ter"] <= GOOD_TER else
               "🟡 达标（TER≤10%）" if S["final"]["ter"] <= TARGET_TER else
               "🟠 未达标（TER>10%）")

    # ---------------------------------------------------------- 报告
    L = []
    L.append("# 评测报告 · 专用词识别（%s 集）\n" % (
        "TTS 回归" if "tts" in set_dir.replace("\\", "/").lower() else "音频"))
    L.append("日期：**%s**　用例：**%d 例 / %d 术语**　耗时：**%.0fs**（模型加载 %.1fs）\n"
             % (time.strftime("%Y-%m-%d %H:%M"), len(rows), total_terms,
                elapsed, load_s))
    L.append("判定口径见 `测试文档-专用词识别验收-2026-09-28.md`；"
             "断言集 `testset/term_cases.json`。\n")

    L.append("## 0. 结论\n")
    L.append("| 列 | 术语错误 TER | 句级准确 SER | 字符错误 CER | 未命中/总 |")
    L.append("|---|---|---|---|---|")
    for col, name in (("raw", "raw 模型原始"), ("post", "post 词典后处理"),
                      ("final", "final 真实入口(含LLM)")):
        s = S[col]
        L.append("| %s | **%.1f%%** | %.1f%% | %.1f%% | %d/%d |"
                 % (name, s["ter"] * 100, s["ser"] * 100, s["cer"] * 100,
                    s["miss"], total_terms))
    L.append("")
    L.append("- **判定（按 final 列）：%s**" % verdict)
    L.append("- 验收线：阻断 TER>20%　达标 ≤10%　优良 ≤5%　回归红线：劣化 ≥5pp")
    for mid, txt, ok in must:
        L.append("- 必过项 %s「%s」：%s" % (mid, txt, "✅ 命中" if ok else "❌ **未命中**"))
    L.append("")

    L.append("## 1. 分组表现（TER）\n")
    L.append("| 组 | 内容 | 例数 | 术语 | raw | post | final |")
    L.append("|---|---|---|---|---|---|---|")
    for g in sorted(groups):
        d = groups[g]
        f = lambda k: ("%.0f%%" % (d[k] * 100.0 / d["terms"])) if d["terms"] else "-"
        L.append("| %s | %s | %d | %d | %s | %s | %s |"
                 % (g, data["group_names"].get(g, ""), d["n"], d["terms"],
                    f("raw"), f("post"), f("final")))
    L.append("")

    L.append("## 2. 逐例明细\n")
    L.append("| # | 组 | 朗读文本 | 期望术语 | raw | post | final |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rows:
        def cell(k):
            if r["note"]:
                return r["note"]
            m = r["miss"][k]
            return "✅" if not m else "❌ " + "/".join(m)
        L.append("| %s | %s | %s | %s | %s | %s | %s |"
                 % (r["id"], r["group"], r["text"],
                    "/".join(r["terms"]), cell("raw"), cell("post"),
                    cell("final")))
    L.append("")

    L.append("## 2b. 识别原文（**归因必需**，否则只有 ❌ 无法判断是模型错还是后处理错）\n")
    L.append("| # | 朗读（期望） | raw 模型原始 | final 最终 |")
    L.append("|---|---|---|---|")
    for r in rows:
        e = lambda s: (s or "").replace("|", "\\|")
        L.append("| %s | %s | %s | %s |"
                 % (r["id"], e(r["text"]),
                    e(r["raw"]) or ("（" + r["note"] + "）" if r["note"] else ""),
                    e(r["final"]) or ("（" + r["note"] + "）" if r["note"] else "")))
    L.append("")

    L.append("## 3. 配置快照（不可缺，否则分数无法归因）\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    for k in ("asr_model", "asr_mode", "asr_min_rms", "asr_target_peak",
              "asr_short_force_zh", "asr_short_sec", "asr_language",
              "strip_fillers", "auto_space", "asr_polish",
              "asr_polish_command", "asr_polish_min_chars", "correction_dict",
              "asr_vocab", "llm_base", "model"):
        v = cfg.get(k)
        if k == "asr_vocab":
            v = "%d 条" % len(v or [])
        elif k == "correction_dict":
            v = "%d 条" % len(v or {})
        L.append("| `%s` | %s |" % (k, v))
    L.append("| 音频前端 device | %s |" % device)
    L.append("| LLM 网关(:9292) | %s |" % gwtxt)
    L.append("| polish 实际启用 | %s |" % (not no_polish and gw and bool(cfg.get("asr_polish"))))
    L.append("")

    L.append("## 4. 未命中形态（看后处理覆盖度）\n")
    kinds = {}
    for r in rows:
        for t in r["miss"]["final"]:
            kinds.setdefault(t, []).append(r["id"])
    if not kinds:
        L.append("无未命中项。")
    else:
        L.append("| 未命中术语 | 出现用例 | 次数 |")
        L.append("|---|---|---|")
        for t, ids in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            L.append("| `%s` | %s | %d |" % (t, ", ".join(ids), len(ids)))
    L.append("")

    L.append("## 5. 限制声明\n")
    L.append("1. **TTS ≠ 真人，且偏差方向与直觉相反**：SAPI 中文音色（Huihui）会把英文词按"
             "中文音节读（`Wan2.2` → 「汪二点二」），这往往比真人念得更难识别。"
             "故 **TTS 集的绝对分数不能当真人基线**，只能用于「同一套音频下改动前后对比」。"
             "真人基线须另跑 `testset/term` 主集。")
    L.append("2. 样本 %d 例 / %d 术语：单次 ±1 术语即 ±%.1f 个百分点。"
             % (len(rows), total_terms, 100.0 / max(total_terms, 1)))
    L.append("3. 本次 polish 启用=%s；网关=%s。"
             % (not no_polish, "在线" if gw else "离线"))
    L.append("4. 相似度闸门可能拦掉合法术语替换，明细见 §2 的 `post→final` 差异。")
    L.append("5. 个别用例（如 T03）TTS 可能读得异常导致「术语整体丢失」，"
             "此类项需人耳复听音频后再判定归属。")
    L.append("")

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(L))
    print("\n" + "=" * 62)
    for col in ("raw", "post", "final"):
        print("%-6s TER=%5.1f%%  SER=%5.1f%%  CER=%5.1f%%" % (
            col, S[col]["ter"] * 100, S[col]["ser"] * 100, S[col]["cer"] * 100))
    print("verdict(final) =", verdict)
    print("report ->", out_path)
    return 0 if S["final"]["ter"] <= BLOCK_TER else 1


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--make-tts", action="store_true")
    p.add_argument("--set", default=DEFAULT_SET)
    p.add_argument("--out", default=None)
    p.add_argument("--no-polish", action="store_true")
    p.add_argument("--no-va", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    a = p.parse_args()
    if a.make_tts:
        return make_tts(a.limit)
    out = a.out or os.path.join(
        HERE, "评测报告-专用词-%s-%s.md" % (
            "TTS" if "tts" in a.set.replace("\\", "/").lower() else "音频",
            time.strftime("%Y%m%d")))
    return evaluate(a, a.set, out, no_polish=a.no_polish,
                    no_va=a.no_va, limit=a.limit)


if __name__ == "__main__":
    sys.exit(main())
