# -*- coding: utf-8 -*-
"""最小验证 demo: 同一句话分别用「原模型(LLM)」和「Laya」跑一遍意图解析。

用法(项目 venv):
    venv\\Scripts\\python.exe test_decision.py             # 对比两种引擎
    venv\\Scripts\\python.exe test_decision.py --engine laya
前置: 另开一个终端跑 启动Laya服务.bat(或 laya_serve.py), 等 /health 返回 ready。
"""
import json
import os
import sys
import time

import decision

try:
    import voice_assistant as va       # 拿 SYS_PROMPT / llm_intent(需要完整依赖: 麦克风/ASR 等)
    _VA_OK = True
except Exception as e:                 # 只验证 Laya 一侧时不必拉起整套依赖
    va = None
    _VA_OK = False
    print("[提示] voice_assistant 导入失败(%r), 仅测 Laya 一侧" % (e,))

CASES = [
    "帮我截个图",
    "截右上角发给我",
    "打开记事本",
    "点发送按钮",
    "点一下",
    "双击这里",
    "按回车",
    "保存一下",
    "往下滚",
    "输入你好世界",
    "今天天气不错啊",
]


def main():
    eng = "compare"
    if "--engine" in sys.argv:
        eng = sys.argv[sys.argv.index("--engine") + 1]
    if _VA_OK:
        decision.bind(va.CONFIG, va.llm_intent)
    else:
        decision.bind(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "config.json"), encoding="utf-8")),
                      lambda t: {"action": "none", "params": {}, "reply": ""})

    if eng != "llm":
        ok, info = decision.health()
        print("[laya服务] %s  %s" % ("就绪" if ok else "未就绪", json.dumps(info, ensure_ascii=False)))
        if not ok:
            print("  -> 先运行 启动Laya服务.bat, 等日志出现 [laya] ready")

    print("%-18s | %-34s | %s" % ("输入", "原模型(LLM)", "Laya"))
    print("-" * 96)
    for t in CASES:
        row = []
        if eng in ("compare", "llm"):
            t0 = time.time()
            va.CONFIG["decision_engine"] = "llm"
            r1 = decision.intent(t)
            row.append("%s %s(%dms)" % (r1.get("action"),
                                        json.dumps(r1.get("params", {}), ensure_ascii=False)[:20],
                                        int((time.time() - t0) * 1000)))
        else:
            row.append("-")
        if eng in ("compare", "laya"):
            va.CONFIG["decision_engine"] = "laya"
            va.CONFIG["laya_fallback_llm"] = False     # 纯 Laya, 不回退, 才能看到它自己的判断
            t0 = time.time()
            r2 = decision.intent(t)
            if r2.get("engine") == "laya":
                row.append("%s %s conf=%.2f(%dms)" % (r2.get("action"),
                                                      json.dumps(r2.get("params", {}), ensure_ascii=False)[:20],
                                                      float(r2.get("confidence", 0)),
                                                      int((time.time() - t0) * 1000)))
            else:
                row.append("服务不可用/回退: %s" % r2.get("action"))
        else:
            row.append("-")
        print("%-18s | %-34s | %s" % (t, row[0], row[1]))
    print("\n当前 config.decision_engine =", va.CONFIG.get("decision_engine"))


if __name__ == "__main__":
    main()
