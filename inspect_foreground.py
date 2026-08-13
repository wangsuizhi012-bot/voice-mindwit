# -*- coding: utf-8 -*-
"""小工具：列出当前前台窗口里所有可点击/可命名的控件。
用法：
    E:\AI\funasr-test\venv\Scripts\python.exe inspect_foreground.py
先把目标窗口切到前台，再运行，看输出里有没有你要点的文字。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from locate import _enum_uia


def main():
    print("=" * 60)
    print("当前前台窗口控件扫描")
    print("=" * 60)
    items, err = _enum_uia(timeout=3.0)
    if err:
        print("扫描失败：", err)
        return
    if not items:
        print("未找到可见控件（可能是游戏/自绘 UI，需 OCR 兜底）。")
        return
    # 过滤出有名字的控件排前面
    named = [i for i in items if (i[0] or "").strip()]
    unnamed = [i for i in items if not (i[0] or "").strip()]
    print(f"共 {len(items)} 个控件，有名字 {len(named)} 个\n")
    print(f"{'类型':<22} {'文字/名称':<30} {'中心坐标':<12}")
    print("-" * 70)
    for name, ctype, x, y, w, h in named:
        cx, cy = x + w // 2, y + h // 2
        print(f"{ctype:<22} {name[:28]:<30} ({cx}, {cy})")
    if unnamed:
        print(f"\n... 还有 {len(unnamed)} 个无名字控件未列出")
    print("\n提示：让语音助手点上面'文字/名称'列里的文字，命中率最高。")


if __name__ == "__main__":
    main()
