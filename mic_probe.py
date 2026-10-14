# -*- coding: utf-8 -*-
"""麦克风诊断探针 —— 10 秒内实时打印每 0.5s 的音量 RMS。

判读（终端直接看）:
    RMS 一直 0.000            → 设备无声: 麦克风权限/静音/选错设备(问题在系统层)
    RMS 随你说话波动 >0.01    → 麦克风正常, 问题在 VAD 触发(问题在程序层)
    有波动但 <0.01            → 声音太小, 增益不足
"""
import queue
import sys
import time

import numpy as np
import sounddevice as sd

SR = 16000
q = queue.Queue()


def cb(indata, frames, t, status):
    q.put(bytes(indata))


def main():
    devs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    print("输入设备:")
    for i, d in enumerate(devs):
        print("  [%d] %s" % (i, d["name"][:50]))
    # 与 voice_assistant 相同的默认选择逻辑: 系统默认输入
    idx = sd.default.device[0]
    name = sd.query_devices(idx)["name"] if idx >= 0 else "?"
    print("使用系统默认设备 [%d]: %s" % (idx, name))
    print("开始监听 10 秒, 请正常说话...\n")
    with sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                        blocksize=8000, device=idx, callback=cb):
        t0 = time.time()
        peak = 0.0
        n = 0
        while time.time() - t0 < 10:
            try:
                data = q.get(timeout=1.0)
            except queue.Empty:
                print("  %.1fs  (无数据! 流异常)" % (time.time() - t0))
                continue
            a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(a * a)))
            peak = max(peak, rms)
            n += 1
            bar = "#" * min(int(rms * 200), 50)
            print("  %.1fs  RMS=%.4f  %s" % (time.time() - t0, rms, bar))
    print("\n峰值 RMS = %.4f" % peak)
    if peak < 0.001:
        print("结论: 设备完全无声 -> 检查 1) Windows 设置>隐私>麦克风 权限")
        print("                        2) 麦克风未静音 3) 你是否戴着蓝牙耳机但系统在用内置麦")
    elif peak < 0.01:
        print("结论: 声音偏小 -> 离麦近一点, 或系统麦克风增益调高; VAD 可能不触发")
    else:
        print("结论: 麦克风正常 -> 问题在 VAD 触发层, 把此结果发我")


if __name__ == "__main__":
    main()
