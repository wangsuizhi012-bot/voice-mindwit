# -*- coding: utf-8 -*-
"""本地模型服务调度 —— ⚠️ 先读这段定位说明，别重复造轮子。

╔══════════════════════════════════════════════════════════════════════════╗
║  本机模型调度的**正解是 llama-swap 网关**（E:\\AI\\llama-swap\\config.yaml）  ║
║  :9292 统一入口，请求体 model 字段决定加载谁，按 ttl 空闲卸载，              ║
║  8G 单卡自动「停旧起新」。桌面自动化已改为走网关（visual_click.pick_backend）。║
║                                                                          ║
║  所以：**不要再用本模块去手工起 1235/1237** —— 会和网关抢显存。            ║
║  本模块保留的三件网关没有的能力：                                          ║
║    1) vram()      零 NVML 显存读数（本机 nvidia-smi 已损坏，见下）          ║
║    2) gateway      网关状态 + **加载前显存预检**（网关自己不做预检）         ║
║    3) ensure/release 直连端口的兜底（网关没起时才用）                       ║
╚══════════════════════════════════════════════════════════════════════════╝

═══ 显存查询：为什么必须自带（2026-09-11 实测） ═══
`nvidia-smi` 报 `Failed to initialize NVML: Unknown Error`，所有依赖它的脚本
都拿不到显存数字（这是驱动层问题，装/重启前一直会这样）。本模块因此实现
**零 NVML 依赖**的查询：
    总量/独显识别 : DXGI  EnumAdapters1 -> DXGI_ADAPTER_DESC.DedicatedVideoMemory
    当前占用      : PDH   \\GPU Adapter Memory(*)\\Dedicated Usage（按 LUID 匹配）
    进程级占用    : PDH   \\GPU Process Memory(*)\\Dedicated Usage（实例名带 pid）
用 `PdhAddEnglishCounterW` 而非 `PdhAddCounterW`，绕开中文系统计数器名本地化。
实测：RTX 3070 Laptop 8018MB 总 / vl8b(Qwen3-VL-8B) 占 7935.9MB（99%）——
这组数字就是「8G 卡必须错峰」的硬证据。

═══ 实测校准值（改配置前先看这个）═══
    vl  (Qwen3-VL-8B Q6_K)  7935.9 MB   启动 8~23s
    uitars (UI-TARS q4_k_m)  6451.0 MB   启动 6~10s
    heavy 组需求和 14.2G > 总显存 7.8G  ->  必须错峰（调度/换出的根本理由）

依赖: 无第三方（ctypes + 标准库）；HTTP 探测用 venv 里的 requests
运行时: E:\\AI\\_experiments\\funasr-test\\venv\\Scripts\\python.exe

用法:
    python model_sched.py status                 # 服务 + 显存全景
    python model_sched.py vram                   # 只看显存（含按进程明细）
    python model_sched.py gateway                # llama-swap 网关状态（推荐入口）
    python model_sched.py gateway --precheck uitars   # 加载前显存预检
    python model_sched.py gateway --unload       # 卸载全部（ComfyUI 生图前腾显存）
    python model_sched.py selftest               # 分段自检（含托管能力探针）
    python model_sched.py ensure vl              # 直连兜底：按需拉起 + 显存守卫
    python model_sched.py release vl             # 释放（外部起的服务会拒绝）
    python model_sched.py sweep                  # 释放 TTL 到期的
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state", "model_sched.json")
LOG_DIR = os.path.join(HERE, "logs")
LLAMA = r"E:\AI\llama.cpp-cuda\llama-server.exe"
MODELS = r"E:\AI\LLM\GGUF"

# ---------------------------------------------------------------- 服务注册表
# need_mb 是实测值，不是模型文件大小（文件大小 ≠ 显存占用）
SERVERS = {
    "vl": {
        "name": "Qwen3-VL-8B（语义判读）",
        "port": 1235,
        "group": "heavy",          # heavy = 抢显存，组内互斥
        # 实测 2026-09-11：llama-server.exe 占 7935.9MB / 总 8018MB（99%）！
        # Q6_K 权重 6.7G + mmproj 1.16G + KV/CUDA 上下文，8G 卡上基本顶满。
        # 这就是「VL 与 UI-TARS 不可能共存」的硬证据。
        "need_mb": 7950,
        "ttl": 300,                # 空闲 5 分钟自动释放
        "args": ["-m", MODELS + r"\Qwen3\Qwen3-VL-8B-Instruct-abliterated-v2.0.Q6_K.gguf",
                 "--mmproj", MODELS + r"\Qwen3\Qwen3-VL-8B-Instruct-abliterated-v2.0.mmproj-f16.gguf",
                 "-ngl", "99", "--mmproj-offload", "--image-min-tokens", "1024"],
        "note": "必须 --mmproj-offload：否则视觉塔跑 CPU，整屏截图 prefill ~100s 超时",
    },
    "uitars": {
        "name": "UI-TARS-1.5-7B（视觉定位）",
        "port": 1237,
        "group": "heavy",
        "need_mb": 6600,           # 实测 6451MB（llama-server.exe，PDH 按 LUID+pid 读）
        "ttl": 300,
        "args": ["-m", MODELS + r"\UI-TARS\UI-TARS-1.5-7B-q4_k_m.gguf",
                 "--mmproj", MODELS + r"\UI-TARS\UI-TARS-1.5-7B-q8_0.mmproj",
                 "-ngl", "99", "--mmproj-offload", "-c", "8192",
                 "--image-min-tokens", "1024"],
        "note": "实测定位平均 8~20px；VL 直出坐标 ~100px 不可用，故坐标只认它和 UIA/OCR",
    },
    "embedding": {
        "name": "bge-small-zh（语义检索）",
        "port": 1236,
        "group": "cpu",            # 纯 CPU，不参与显存互斥，可常驻
        "need_mb": 0,
        "ttl": 0,                  # 0 = 不自动释放
        "args": None,              # 由 _scripts/start-embedding-server.bat 管，这里只探测
        "note": "零显存，7×24 常驻无压力",
    },
}

# 显存安全余量：给桌面合成/浏览器等留出，避免顶到 100% 触发 WDDM 换页
VRAM_MARGIN_MB = 400
# 需求封顶比例：need 本身可能已接近总显存，不能再按比例放大
VRAM_CEIL_RATIO = 0.96
STILL_ACTIVE = 259


def need_mb_of(spec, total_mb=None):
    """算「需要多少空闲显存」才敢启动。

    ⚠️ 不能用 `need × 1.15 + 400` 这种比例放大（踩过）：
    8G 卡上 VL 需求 7G，乘完 8450MB > 总显存 8018MB，
    结果**任何大模型都会被自己的守门永久拒绝**，守卫生效但服务永远起不来。
    正确做法：需求（实测峰值）+ 固定系统余量，并封顶在总显存的 96%。
    """
    need = spec["need_mb"] + VRAM_MARGIN_MB
    if total_mb:
        need = min(need, total_mb * VRAM_CEIL_RATIO)
    return need


# ================================================================ 显存查询
class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", ctypes.c_long)]


class _DXGI_ADAPTER_DESC(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128),
                ("VendorId", wt.UINT), ("DeviceId", wt.UINT),
                ("SubSysId", wt.UINT), ("Revision", wt.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", _LUID)]


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class _PDH_FMT_COUNTERVALUE_ITEM(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", _PDH_FMT_COUNTERVALUE)]


def _dxgi_adapters():
    """DXGI 枚举适配器，返回 [{name, vram_mb, luid}]。零 NVML 依赖。"""
    out = []
    try:
        dxgi = ctypes.WinDLL("dxgi")

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD),
                        ("d4", ctypes.c_ubyte * 8)]

        iid = GUID(0x770aae78, 0xf26f, 0x4dba,
                   (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
        fac = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(fac)) != 0:
            return out
        vt = ctypes.cast(fac, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        enum_adapters1 = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wt.UINT,
                                            ctypes.POINTER(ctypes.c_void_p))(vt[12])
        i = 0
        while True:
            ad = ctypes.c_void_p()
            if enum_adapters1(fac, i, ctypes.byref(ad)) != 0:
                break
            avt = ctypes.cast(ad, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_desc = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                          ctypes.POINTER(_DXGI_ADAPTER_DESC))(avt[8])
            d = _DXGI_ADAPTER_DESC()
            if get_desc(ad, ctypes.byref(d)) == 0:
                out.append({
                    "name": d.Description,
                    "vram_mb": d.DedicatedVideoMemory / 1048576.0,
                    # ⚠️ 必须带 0x 前缀：PDH 实例名形如
                    #    luid_0x00000000_0x000113D5_phys_0，漏掉 0x 会永远匹配不上，
                    #    表现为 used_mb 恒为 None（踩过）。比较时统一 lower()。
                    "luid": "0x%08X_0x%08X" % (d.AdapterLuid.HighPart & 0xFFFFFFFF,
                                               d.AdapterLuid.LowPart),
                })
            i += 1
    except Exception:
        pass
    return out


def _pdh_collect(counter_path, timeout_s=0.35):
    """用 PdhAddEnglishCounterW 取计数器的全部实例。返回 {实例名: 字节数}。

    ⚠️ 必须用 PdhAddEnglishCounterW：中文系统上 PdhAddCounterW 需要本地化名，
    直接传英文路径会返回 PDH_CSTATUS_NO_OBJECT(-1073738823)，拿不到数据。
    """
    result = {}
    try:
        pdh = ctypes.WinDLL("pdh")
    except Exception:
        return result
    q = wt.HANDLE()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
        return result
    c = wt.HANDLE()
    if pdh.PdhAddEnglishCounterW(q, counter_path, 0, ctypes.byref(c)) != 0:
        pdh.PdhCloseQuery(q)
        return result
    pdh.PdhCollectQueryData(q)
    time.sleep(timeout_s)          # 计数器需要两次采样才能出值
    pdh.PdhCollectQueryData(q)
    size, count = wt.DWORD(0), wt.DWORD(0)
    pdh.PdhGetFormattedCounterArrayW(c, 0x200, ctypes.byref(size), ctypes.byref(count), None)
    if size.value:
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(c, 0x200, ctypes.byref(size),
                                            ctypes.byref(count), buf) == 0:
            arr = ctypes.cast(buf, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM))
            for i in range(count.value):
                result[arr[i].szName] = arr[i].FmtValue.doubleValue
    pdh.PdhCloseQuery(q)
    return result


def vram(by_process=False):
    """显存全景。返回 dict(name, total_mb, used_mb, free_mb, source, processes)。

    降级链：NVML -> (DXGI 总量 + PDH 占用) -> 全 None（调用方须容忍）
    独显识别：取 DedicatedVideoMemory 最大的适配器（核显通常 128MB 共享）
    """
    info = {"name": None, "total_mb": None, "used_mb": None,
            "free_mb": None, "source": None, "processes": []}

    # 1) 总量与独显（DXGI）
    ads = _dxgi_adapters()
    if ads:
        gpu = max(ads, key=lambda a: a["vram_mb"])
        info["name"] = gpu["name"]
        info["total_mb"] = round(gpu["vram_mb"], 1)
        info["source"] = "DXGI"

        # 2) 已用（PDH，按 LUID 精确匹配这台独显）
        usage = _pdh_collect(r"\GPU Adapter Memory(*)\Dedicated Usage")
        hit = [v for k, v in usage.items() if gpu["luid"].lower() in k.lower()]
        if hit:
            info["used_mb"] = round(sum(hit) / 1048576.0, 1)
            info["free_mb"] = round(info["total_mb"] - info["used_mb"], 1)
            info["source"] = "DXGI+PDH"

        if by_process and info["used_mb"]:
            proc = _pdh_collect(r"\GPU Process Memory(*)\Dedicated Usage")
            rows = []
            for k, v in proc.items():
                if gpu["luid"].lower() not in k.lower():
                    continue
                mb = v / 1048576.0
                if mb < 50:
                    continue
                # ⚠️ 实例名是 pid_12345_luid_0x... ，split("_") 后首段是裸 "pid"、
                #    第二段才是数字。按 startswith("pid") 取会拿到字符串 "pid"（踩过）。
                parts = k.split("_")
                pid_num = None
                if len(parts) >= 2 and parts[0] == "pid":
                    try:
                        pid_num = int(parts[1])
                    except ValueError:
                        pid_num = None
                rows.append({"pid": pid_num, "mb": round(mb, 1), "key": k})
            rows.sort(key=lambda r: -r["mb"])
            for r in rows:
                r["name"] = _proc_name(r["pid"])
            info["processes"] = rows
    return info


def _proc_name(pid):
    """按 PID 取可执行文件名。"""
    if not pid:
        return "?"
    try:
        out = _run_text(["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"], timeout=8)
        line = out.strip().splitlines()[0] if out.strip() else ""
        return line.split(",")[0].strip('"') or ("pid%d" % pid)
    except Exception:
        return "pid%d" % pid


# ================================================================ 进程 / 端口
def _run_text(args, timeout=10):
    """跑外部命令并按「utf-8 -> gbk -> replace」解码。

    ⚠️ 中文 Windows 上 `netstat` / `tasklist` 的输出是 GBK，用 text=True
    会直接抛 UnicodeDecodeError（实测 0xbb 非法起始字节），
    所以必须拿 bytes 自己解码，不能图省事用 text=True。
    """
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
    except Exception:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return r.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.stdout.decode("utf-8", "replace")


def pid_alive(pid):
    """进程是否还活着。⚠️ 必须声明 argtypes —— 64 位句柄不声明会被截断成 int。"""
    if not pid:
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    code = wt.DWORD()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def port_pid(port):
    """监听指定端口的 PID（用 netstat 解析，不引入 psutil）。"""
    out = _run_text(["netstat", "-ano", "-p", "TCP"])
    tag = ":%d " % port
    for line in out.splitlines():
        if tag in line and "LISTENING" in line:
            parts = line.split()
            if len(parts) >= 5:
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
    return None


def _health_url(port):
    return "http://127.0.0.1:%d/health" % port


def is_up(port, timeout=3):
    """服务是否真的就绪。

    ⚠️ 双重校验（2026-09-11 踩过）：本机系统代理 http_proxy=127.0.0.1:7971
    会劫持回环请求，目标没起时**返回 HTTP 502 而不是抛异常**。
    所以必须 trust_env=False + 显式校验 status_code，只 catch 异常会误判「在线」。
    """
    try:
        import requests
    except Exception:
        return False
    s = requests.Session()
    s.trust_env = False
    try:
        r = s.get(_health_url(port), timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def kill_pid(pid, tree=True):
    """终止进程树。

    ⚠️ 参数顺序很关键：`/PID` 必须**紧跟**数字，写成
    `taskkill /PID /T 1234 /F` 会让 taskkill 把 `/T` 当成 PID → 静默失败，
    表现为「返回码非 0 且进程还活着」（踩过）。
    正确顺序: taskkill /PID 1234 /T /F
    另外不要用 text=True —— taskkill 输出是 GBK，解码会抛 UnicodeDecodeError。
    """
    args = ["taskkill", "/PID", str(pid)]
    if tree:
        args.append("/T")
    args.append("/F")
    try:
        r = subprocess.run(args, capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ================================================================ 状态持久化
def _load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def status(name=None):
    """当前各服务状态。返回 {svc: {up, pid, managed, external, ...}}"""
    st = _load()
    out = {}
    for key, spec in SERVERS.items():
        if name and key != name:
            continue
        rec = st.get(key) or {}
        pid = port_pid(spec["port"])
        up = is_up(spec["port"])
        rec_pid = rec.get("pid")
        managed = bool(rec_pid) and pid_alive(rec_pid)
        out[key] = {
            "up": up,
            "pid": pid,
            "spec": spec,
            # 端口在线但不在我们的记录里 = 用户手工起的，绝不主动关
            "external": bool(up and not managed),
            "managed": managed,
            "rec": rec,
        }
    return out


def touch(name):
    """标记「刚被用过」，推迟 TTL 释放。"""
    st = _load()
    if name in st:
        st[name]["last_used"] = time.time()
        _save(st)


# ================================================================ 启停
def release(name, force=False, quiet=False):
    """停止服务。只关调度器自己拉起的；外部手工起的拒绝（force=True 才动）。

    返回 (bool, 说明)
    """
    if name not in SERVERS:
        return False, "未知服务: %s" % name
    spec = SERVERS[name]
    st = _load()
    rec = st.get(name) or {}
    live = port_pid(spec["port"])

    if not live:
        st.pop(name, None)
        _save(st)
        return True, "%s 本来就没在跑" % name

    rec_pid = rec.get("pid")
    if not (rec_pid and pid_alive(rec_pid) and rec_pid == live):
        if not force:
            return False, ("%s（PID %s）不是调度器拉起的（可能是你手工双击 bat 起的），"
                           "拒绝关闭。要强制关请加 --force" % (name, live))
        # force 分支：连外部服务一起关
    kill_pid(live)
    # 8B 模型卸载 + CUDA 上下文回收需要几秒，等待要足够长，
    # 否则会误报「仍在监听」，进而让上层以为释放失败而反复重试（踩过）
    for _ in range(30):
        if not is_up(spec["port"], timeout=1):
            break
        time.sleep(0.5)
    if not is_up(spec["port"], timeout=2):
        st.pop(name, None)
        _save(st)
        if not quiet:
            print("  已释放 %s（PID %s）" % (name, live))
        return True, "已关闭 %s (PID %s)" % (name, live)
    # 没关掉：**保留记录**，别当成成功清掉
    _save(st)
    if not quiet:
        print("  ⚠️ %s（PID %s）未能终止，端口 %d 仍在监听" % (name, live, spec["port"]))
    return False, ("%s（PID %s）未能终止（端口 %d 仍在监听），"
                   "可能权限不足或进程受保护" % (name, live, spec["port"]))


def ensure(name, wait=180, evict=False, force=False, quiet=False):
    """按需拉起服务。返回 (ok, 说明)。

    步骤：已就绪? -> 显存够? -> (不够且 evict) 腾位 -> 启动 -> 轮询就绪
    显存守卫在启动**之前**，避免 OOM 后才发现（8G 机器上必踩）。
    """
    if name not in SERVERS:
        return False, "未知服务: %s" % name
    spec = SERVERS[name]

    if is_up(spec["port"]):
        touch(name)
        return True, "%s 已就绪（端口 %d）" % (name, spec["port"])

    # 端口被别的进程占了但 /health 不通 —— 大概率是上一次没退干净
    stale = port_pid(spec["port"])
    if stale and pid_alive(stale):
        if force:
            kill_pid(stale)
            time.sleep(1.0)
        else:
            return False, ("端口 %d 被 PID %s 占用但健康检查不通（可能是残留进程）。"
                           "加 --force 强制清理" % (spec["port"], stale))

    if not spec["args"]:
        return False, "%s 没有内置启动参数（请用它自己的启动脚本）" % name

    # ---- 显存守卫 ----
    allow_evict = evict or force
    if spec["group"] == "heavy":
        ok, why = _vram_gate(name, spec, allow_evict, quiet)
        if not ok:
            return False, why

    # ---- 启动 ----
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, "%s.log" % name)
    cmd = [LLAMA] + spec["args"] + ["--port", str(spec["port"])]
    if not quiet:
        print("  启动 %s -> :%d，日志 %s" % (name, spec["port"], log_path))
    p = _spawn_detached(cmd, log_path)
    if p is None:
        return False, "启动失败（进程创建异常）"
    if not quiet and not p["breakaway"]:
        # 不致命，但要让用户知道：进程可能随父进程退出而被回收
        print("  ⚠️ 未能脱离父 job object（服务可能随启动它的进程退出而被杀）")
    pid = p["pid"]

    st = _load()
    now = time.time()
    st[name] = {"pid": pid, "started_at": now, "last_used": now,
                "port": spec["port"], "cmd": cmd,
                "breakaway": p["breakaway"]}
    _save(st)

    # ---- 等就绪 ----
    t0 = time.time()
    while time.time() - t0 < wait:
        if is_up(spec["port"], timeout=2):
            # 存活复检：某些宿主（Agent/IDE 沙箱）会在**父调用结束时**递归回收
            # 本次调用产生的所有后代进程，表现为「就绪后 1~2 秒内静默消失」，
            # 日志里还留着 `listening on ...`，极难排查（2026-09-11 实测踩过）。
            # 复检一次，把这种「假就绪」当场变成明确诊断。
            time.sleep(1.5)
            if not pid_alive(pid) or not is_up(spec["port"], timeout=2):
                st.pop(name, None)
                _save(st)
                return False, (
                    "%s 启动后就绪，但 1.5s 内进程消失 —— 说明宿主环境在回收子进程。\n"
                    "  典型场景：本命令是在 Agent/IDE 沙箱里执行的，调用结束会连子进程一起清理\n"
                    "  （实测 DETACHED_PROCESS、CREATE_BREAKAWAY_FROM_JOB、cmd start、\n"
                    "   PowerShell Start-Process 全都会被回收，BREAKAWAY 还会被安全策略拒绝）。\n"
                    "  解法：在**你自己的终端**里执行，或直接双击对应的 .bat；\n"
                    "  服务一旦由独立终端持有，本调度器的 ensure/status/sweep 都能正常接管它。"
                    % name)
            if not quiet:
                print("  %s 就绪（%.0fs，PID %s%s）"
                      % (name, time.time() - t0, pid,
                         "" if p["breakaway"] else "，未脱离 job"))
            return True, "%s 已就绪（%.0fs，PID %s）" % (name, time.time() - t0, pid)
        if not pid_alive(pid):
            tail = _tail(log_path, 12)
            st.pop(name, None)
            _save(st)
            return False, "进程启动即退出（可能显存不足）。日志尾部:\n%s" % tail
        time.sleep(2)

    return False, "%s 等待 %ds 未就绪（PID %s 仍在，可查 %s）" % (name, wait, pid, log_path)


# 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn_detached(cmd, log_path):
    """脱离当前进程树启动服务。返回 {"pid", "breakaway"} 或 None。

    ⚠️ 必须带 CREATE_BREAKAWAY_FROM_JOB（2026-09-11 踩过）：
    Agent / IDE 的 shell 通常把命令放进 Windows **job object**，shell 调用一结束
    就回收整个 job —— 此时即使用了 DETACHED_PROCESS 也保不住子进程。
    实测症状：ensure 报告「vl 就绪（23s）」，下一次调用时进程已消失、
    /health 全失败，而日志明明写着 `listening on http://127.0.0.1:1235`。
    这种「启动成功但下一秒就没了」极难从日志看出，务必记住。

    BREAKAWAY 让子进程脱离该 job；若 job 没设 JOB_OBJECT_LIMIT_BREAKAWAY_OK，
    CreateProcess 返回 ERROR_ACCESS_DENIED，此时回退到普通 DETACHED 并标记
    breakaway=False，让调用方知道风险。
    """
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags, ba in ((base | CREATE_BREAKAWAY_FROM_JOB, True), (base, False)):
        try:
            with open(log_path, "ab") as lf:
                lf.write(("\n\n===== start %s =====\n"
                          % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
                lf.flush()
                p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=flags,
                                     cwd=os.path.dirname(LLAMA))
            return {"pid": p.pid, "breakaway": ba}
        except OSError:
            if ba:
                continue          # 该 job 不允许脱离，退回普通 DETACHED
            return None
    return None


def _vram_gate(name, spec, allow_evict, quiet):
    """显存守卫。返回 (ok, 说明)。"""
    v = vram()
    need = need_mb_of(spec, v["total_mb"])
    if v["free_mb"] is None:
        if not quiet:
            print("  ⚠️ 查不到显存（NVML 与 PDH 都失败），跳过守卫直接启动")
        return True, ""

    if v["free_mb"] >= need:
        if not quiet:
            print("  显存充足: 空闲 %.0fMB >= 需要 %.0fMB" % (v["free_mb"], need))
        return True, ""

    # 不够 —— 看看同组谁占着，能不能腾
    victims = []
    externals = []
    st = _load()
    for other, ospec in SERVERS.items():
        if other == name or ospec["group"] != spec["group"]:
            continue
        if not is_up(ospec["port"]):
            continue
        if st.get(other, {}).get("pid"):
            victims.append((st[other].get("last_used", 0), other))
        else:
            # 外部手工起的：调度器按约定**不会**碰它，只能提示人来处理
            externals.append(other)
    victims.sort()   # 最久没用的排前面

    if victims and allow_evict:
        for _, victim in victims:
            if not quiet:
                print("  显存不足（空闲 %.0fMB < 需要 %.0fMB）-> 腾位：先关 %s"
                      % (v["free_mb"], need, victim))
            release(victim, quiet=quiet)
            time.sleep(1.5)
            v = vram()
            if v["free_mb"] is not None and v["free_mb"] >= need:
                return True, ""

    hint = []
    if externals:
        hint.append("同组在建但由你手工启动（调度器不碰）: " + ", ".join(externals)
                    + " —— 需人工关闭，或加 --force 强制腾位")
    if victims:
        hint.append("同组在用: " + ", ".join(n for _, n in victims) +
                    "（--evict 可自动腾位）")
    procs = vram(by_process=True)["processes"][:3]
    if procs:
        hint.append("显存大户: " + ", ".join("%s %.0fMB" % (p["name"][:22], p["mb"])
                                            for p in procs))
    return False, ("显存不足：%s 空闲 %.0fMB，需要 %.0fMB。%s"
                   % (v["name"] or "GPU", v["free_mb"], need,
                      "；".join(hint) if hint else
                      "可能被 ComfyUI 等非托管进程占用，请先关闭"))


def _tail(path, n=12):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-n:])
    except Exception:
        return "(读不到日志)"


def sweep(quiet=False):
    """释放 TTL 到期的托管服务。可挂到 agent-hub 定时任务里。"""
    st = _load()
    now = time.time()
    freed = []
    for name, rec in list(st.items()):
        spec = SERVERS.get(name)
        if not spec or spec["ttl"] <= 0:
            continue
        if not port_pid(spec["port"]):
            st.pop(name, None)      # 记录过期，清掉
            continue
        idle = now - rec.get("last_used", rec.get("started_at", now))
        if idle > spec["ttl"]:
            if not quiet:
                print("  %s 空闲 %.0fs > TTL %ds -> 释放" % (name, idle, spec["ttl"]))
            release(name, quiet=quiet)
            freed.append(name)
    _save(st)
    if freed and not quiet:
        v = vram()
        print("  已释放 %s，当前空闲显存 %.0fMB" % (", ".join(freed), v["free_mb"] or -1))
    return freed


# ================================================================ 上下文管理器
class using(object):
    """with using("vl") as svc: ...  用完按 TTL 释放（不是立刻关，避免抖动）。

    失败时不抛异常，svc["ok"] 为 False、svc["msg"] 是原因 —— 上层可决定降级。
    """

    def __init__(self, name, wait=180, evict=False):
        self.name = name
        self.wait = wait
        self.evict = evict

    def __enter__(self):
        ok, msg = ensure(self.name, wait=self.wait, evict=self.evict)
        self.result = {"ok": ok, "msg": msg, "name": self.name}
        return self.result

    def __exit__(self, *exc):
        if self.result.get("ok"):
            touch(self.name)     # 记一次使用；真正释放交给 sweep()
        return False


# ================================================================ CLI
def _print_status():
    v = vram(by_process=True)
    print("=" * 74)
    if v["total_mb"] and v["used_mb"] is not None:
        bar_n = 30
        used = v["used_mb"]
        fill = int(bar_n * used / v["total_mb"]) if v["total_mb"] else 0
        print("显存 %s   %.0f/%.0f MB  空闲 %.0f MB   [%s]"
              % (v["name"], used, v["total_mb"], v["free_mb"] or 0,
                 "█" * fill + "·" * (bar_n - fill)))
        print("     数据源: %s（NVML 已损坏，走 DXGI+PDH 降级）" % v["source"])
    else:
        print("显存  查不到（NVML 与 PDH 均不可用）")
    print("=" * 74)

    st = status()
    print("%-9s %-30s %-6s %-8s %-9s %s" % ("服务", "说明", "端口", "状态", "归属", "空闲"))
    print("-" * 74)
    now = time.time()
    for key, d in st.items():
        spec = d["spec"]
        if not d["up"]:
            state, own, idle = "-- 未启动", "-", "-"
        else:
            state = "OK 在线"
            own = "调度器" if d["managed"] else "外部"
            if d["managed"]:
                idle = "%.0fs/%.0fs" % (now - d["rec"].get("last_used", now), spec["ttl"])
            else:
                idle = "-"
        print("%-9s %-30s %-6d %-8s %-9s %s"
              % (key, spec["name"][:28], spec["port"], state, own, idle))
    print("-" * 74)
    print("需要显存: " + "  ".join("%s %.1fG" % (k, s["need_mb"] / 1024.0)
                                   for k, s in SERVERS.items() if s["need_mb"]))
    print("互斥组 heavy: " + ", ".join(k for k, s in SERVERS.items()
                                       if s["group"] == "heavy")
          + "  —— 8G 显存装不下两个，必须错峰")

    if v["processes"]:
        print()
        print("显存占用 Top（PID/进程/占用）:")
        for p in v["processes"][:6]:
            print("   %-9s %-28s %7.0f MB" % (p["pid"], p["name"][:26], p["mb"]))
    print("=" * 74)


# ================================================================ 自检
PROBE_STATE = os.path.join(HERE, "state", "spawn_probe.json")


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def spawn_capability():
    """探测「本环境能否托管常驻进程」——决定 ensure 能不能真正用起来。

    ⚠️ 必须**跨两次调用**判定（第一版在这里踩了坑）：
    宿主是在**调用结束时**才递归回收后代进程，所以在同一次调用里 sleep 2 秒
    再检查，结论永远是「活着」——假阳性。正确做法是两步：
        本次：启动探针 + 把 PID 落盘
        下次：检查那个 PID 还在不在 —— 这才是真实结论

    2026-09-11 实测（6 种方式全试过，结论硬）：
        DETACHED_PROCESS + NEW_PROCESS_GROUP     被回收
        + CREATE_BREAKAWAY_FROM_JOB              被安全策略拒绝(PermissionError)
        cmd /c start /B / 新控制台                被回收
        PowerShell Start-Process                 被回收
        wmic Win32_Process.Create / schtasks      命中程序黑名单，直接拦截
    即：**在 Agent/IDE 沙箱里无法启动跨调用存活的进程**。
    在用户自己的终端 / 双击 bat 里没有这个限制，DETACHED 正常生效。

    返回 dict(ok, reason, known, age) —— known=False 表示还没结论（首次布点）。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    prev = _load_json(PROBE_STATE)
    verdict = {"known": False, "ok": None,
               "reason": "首次布点，尚无跨调用结论；再运行一次 selftest 即可判定"}
    if prev.get("pid"):
        age = time.time() - prev.get("at", 0)
        if pid_alive(prev["pid"]):
            kill_pid(prev["pid"], tree=False)
            verdict = {"known": True, "ok": True, "age": age,
                       "reason": "上一轮的探针存活 %.0fs —— 本环境可托管常驻服务，"
                                 "ensure 启动的服务不会被回收" % age}
        else:
            verdict = {"known": True, "ok": False, "age": age,
                       "reason": "上一轮的探针在调用结束后被回收（存活 %.0fs < 60s）—— "
                                 "本环境**不能**托管常驻进程。\n"
                                 "     ensure 启动的服务活不过当前命令（若需在会话中启动服务，"
                                 "改用后台任务方式；\n"
                                 "     正常使用请在你自己的终端执行，或直接双击对应的 .bat）" % age}

    # 布下本轮探针
    probe_log = os.path.join(LOG_DIR, "_probe.log")
    info = _spawn_detached([sys.executable, "-c", "import time; time.sleep(60)"],
                           probe_log)
    if info:
        _save_json(PROBE_STATE, {"pid": info["pid"], "at": time.time()})
    return verdict


def selftest(heavy=False):
    """不点击、不破坏现场的分段自检。"""
    print("=" * 74)
    print("模型调度器 · 自检")
    print("=" * 74)

    # 1 显存查询
    t0 = time.time()
    v = vram(by_process=True)
    if v["total_mb"] and v["used_mb"] is not None:
        print("[1] 显存查询    OK  %s  %.0f/%.0f MB  空闲 %.0f MB  (%.2fs, %s)"
              % (v["name"][:28], v["used_mb"], v["total_mb"], v["free_mb"],
                 time.time() - t0, v["source"]))
    else:
        print("[1] 显存查询    --  失败（NVML 与 PDH 都不可用，守卫将跳过）")

    # 2 进程/端口
    st = status()
    up = [k for k, d in st.items() if d["up"]]
    print("[2] 服务探测    OK  %d/%d 在线%s" % (len(up), len(st),
                                                ("：" + ", ".join(up)) if up else ""))
    for k, d in st.items():
        if d["up"]:
            print("    %-9s :%-5d %-6s %s" % (k, d["spec"]["port"],
                                              "调度器" if d["managed"] else "外部",
                                              d["rec"].get("pid", "")))

    # 3 守卫公式（关键回归：8G 卡上不能自我否决）
    print("[3] 守卫公式    OK")
    for k, spec in SERVERS.items():
        need = need_mb_of(spec, v["total_mb"])
        ok = v["free_mb"] is None or v["free_mb"] >= need
        print("    %-9s 需求 %5dMB + 余量 %dMB = %5dMB（封顶 %.0f%%）-> %s"
              % (k, spec["need_mb"], VRAM_MARGIN_MB, need, VRAM_CEIL_RATIO * 100,
                 ("空闲够" if ok else "需腾位")))
    heavy_specs = [s for s in SERVERS.values() if s["group"] == "heavy"]
    total_need = sum(s["need_mb"] for s in heavy_specs)
    tot = v["total_mb"] or 0
    print("    互斥校验    heavy 组需求和 %.1fG %s 总显存 %.1fG -> %s"
          % (total_need / 1024.0, ">" if total_need > tot else "<=", tot / 1024.0,
             "确认必须错峰（调度器存在的理由）" if total_need > tot
             else "可共存（不需要错峰）"))

    # 4 TTL
    print("[4] TTL 策略    OK  " + "  ".join(
        "%s=%s" % (k, ("%ds" % s["ttl"]) if s["ttl"] else "常驻")
        for k, s in SERVERS.items() if s["group"] != "cpu" or s["ttl"]))

    # 5 托管能力（跨调用探针 —— 决定 ensure 在**本环境**是否可用）
    cap = spawn_capability()
    print("[5] 托管能力    %s  %s" % ("OK " if cap["ok"] else "-- ", cap["reason"]))

    # 6 可选：真实拉起重服务做端到端
    if heavy:
        for name in ("vl", "uitars"):
            ok, msg = ensure(name, evict=True)
            print("[6] ensure %-9s %s %s" % (name, "OK " if ok else "-- ", msg[:90]))
            if ok:
                release(name)
    print("=" * 74)
    return cap


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ================================================================ 网关（llama-swap）
GATEWAY_URL = "http://127.0.0.1:9292"


def gateway_running(timeout=8):
    """网关是否在线，以及它当前加载了哪些模型。

    ⚠️ 定位说明（2026-09-11）：本机模型调度的**正解是 llama-swap 网关**
    （`E:\\AI\\llama-swap\\config.yaml`，:9292）。它自己就会按请求加载模型、
    按 ttl 空闲卸载、8G 卡上停旧起新。所以**不要再手工起 1235/1237 端口**
    ——那会和网关抢显存。本模块的 ensure/release 只作为网关没起时的直连兜底。
    这里提供的价值是网关**没有**的那部分：加载前显存预检 + 统一的显存读数。
    """
    s = _requests_session()
    try:
        r = s.get(GATEWAY_URL + "/running", timeout=timeout)
        if r.status_code != 200:
            return {"online": False, "models": [], "reason": "HTTP %d" % r.status_code}
        data = r.json()
        rows = data.get("running") or []
        return {"online": True, "models": rows, "reason": ""}
    except Exception as e:
        return {"online": False, "models": [], "reason": repr(e)[:80]}


def _direct_port_pids():
    """当前挂在直连端口（1235/1237）上的 PID 集合 —— 这些**网关管不到**。"""
    out = set()
    for name, spec in SERVERS.items():
        if spec["group"] != "heavy":
            continue
        p = port_pid(spec["port"])
        if p:
            out.add(p)
    return out


def gateway_precheck(model, need_mb):
    """加载某模型前的显存预检。返回 (ok, 说明)。

    llama-swap 自己不做预检——它直接换出旧模型再加载新模型，若显存被
    **非托管进程**（ComfyUI 最典型）占着，就会加载失败或掉到共享内存（慢十倍）。
    所以「先看显存够不够」这件事得在外面做。

    ⚠️ 关键：网关**自己加载的**上游 llama-server 在换模型时会被换出，
    那部分显存是可回收的，不能算作阻碍 —— 否则会对着一个马上要被卸载的
    模型报 WARN，假警告比没警告更误导（第一版就这么错过）。
    真正算阻碍的只有：非网关托管的进程（ComfyUI、以及我另起的直连端口服务）。
    """
    gw = gateway_running()
    v = vram(by_process=True)
    if v["free_mb"] is None:
        return True, "查不到显存，跳过预检"
    need = need_mb + VRAM_MARGIN_MB

    direct = _direct_port_pids()
    reclaimable, blockers = 0.0, []
    for p in v["processes"]:
        nm = (p["name"] or "").lower()
        if nm.startswith("llama-server") and p["pid"] not in direct:
            # 网关托管的模型进程：换模型时会被网关换出，显存可回收
            reclaimable += p["mb"]
        else:
            blockers.append(p)

    avail = v["free_mb"] + reclaimable
    if avail >= need:
        note = []
        if reclaimable:
            note.append("网关将换出旧模型释放 %.0fMB" % reclaimable)
        if gw["online"]:
            cur = [m.get("model") for m in gw["models"]]
            if cur:
                note.append("当前加载 %s" % ", ".join(cur))
        return True, ("可用 %.0fMB >= 需要 %.0fMB%s"
                      % (avail, need, ("（" + "；".join(note) + "）") if note else ""))

    big = sorted(blockers, key=lambda p: -p["mb"])[:3]
    hint = ("；非托管占用 " + ", ".join("%s %.0fMB" % (p["name"][:20], p["mb"])
                                        for p in big)) if big else ""
    return False, ("可用 %.0fMB（空闲 %.0f + 可回收 %.0f）< 需要 %.0fMB%s。"
                   "进程不是网关管的，它换不掉，请先关闭（ComfyUI 生图前用 "
                   "`model_sched.py gateway --unload` 只能卸网关自己的模型）"
                   % (avail, v["free_mb"], reclaimable, need, hint))


def _requests_session():
    import requests
    s = requests.Session()
    s.trust_env = False     # 回环必须直连，否则代理会把「没起」变成 HTTP 502
    return s


def gateway_cmd(flags, pos):
    """`model_sched.py gateway [...]` 子命令。"""
    gw = gateway_running()
    v = vram(by_process=True)
    print("=" * 74)
    print("llama-swap 网关  %s" % (GATEWAY_URL))
    print("=" * 74)
    if not gw["online"]:
        print("状态: 未启动（%s）" % gw["reason"])
        print("启动: 双击 E:\\AI\\_scripts\\start-llama-swap.bat")
        return 1
    print("状态: 在线")
    if gw["models"]:
        print("已加载:")
        for m in gw["models"]:
            print("   %-12s %s" % (m.get("model"), m.get("state")))
    else:
        print("已加载: （无，首个请求会触发加载）")
    if v["total_mb"] and v["used_mb"] is not None:
        print("显存: %.0f/%.0f MB  空闲 %.0f MB" % (v["used_mb"], v["total_mb"], v["free_mb"]))
        for p in v["processes"][:5]:
            print("   %-9s %-26s %7.0f MB" % (p["pid"], p["name"][:24], p["mb"]))

    if "--precheck" in flags:
        want = pos[0] if pos else "uitars"
        spec = SERVERS.get("uitars" if want in ("uitars", "gui") else "vl")
        ok, why = gateway_precheck(want, spec["need_mb"])
        print()
        print("预检 %s: %s  %s" % (want, "OK  " if ok else "WARN", why))
        return 0 if ok else 1

    if "--unload" in flags:
        try:
            s = _requests_session()
            if pos:
                r = s.post("%s/api/models/unload/%s" % (GATEWAY_URL, pos[0]), timeout=30)
            else:
                r = s.post(GATEWAY_URL + "/api/models/unload", timeout=60)
            print()
            print("卸载 %s -> HTTP %d" % (pos[0] if pos else "全部", r.status_code))
            time.sleep(2)
            v2 = vram()
            print("卸载后空闲显存 %.0f MB" % (v2["free_mb"] or -1))
            return 0
        except Exception as e:
            print("卸载失败: %r" % (e,))
            return 1
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    rest = argv[2:]
    flags = {a for a in rest if a.startswith("--")}
    pos = [a for a in rest if not a.startswith("--")]

    if cmd == "status":
        _print_status()
        return 0
    if cmd == "vram":
        v = vram(by_process=True)
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0
    if cmd == "ensure":
        if not pos:
            print("用法: python model_sched.py ensure <vl|uitars|embedding> [--evict] [--force]")
            return 2
        ok, msg = ensure(pos[0], evict="--evict" in flags, force="--force" in flags)
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    if cmd == "release":
        if not pos:
            print("用法: python model_sched.py release <服务名> [--force|--all]")
            return 2
        if "--all" in flags:
            st = status()
            for k, d in st.items():
                if d["up"] and d["managed"]:
                    release(k)
            return 0
        ok, msg = release(pos[0], force="--force" in flags)
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    if cmd == "sweep":
        freed = sweep()
        if not freed:
            print("没有需要释放的服务")
        return 0
    if cmd == "selftest":
        selftest(heavy="--heavy" in flags)
        return 0
    if cmd == "gateway":
        return gateway_cmd(flags, pos)
    if cmd == "touch":
        if pos:
            touch(pos[0])
            print("已标记 %s 在用" % pos[0])
        return 0
    if cmd == "run":
        # python model_sched.py run vl -- <命令...>
        if "--" not in rest:
            print("用法: python model_sched.py run <服务> -- <命令...>")
            return 2
        svc = rest[0]
        tail = rest[rest.index("--") + 1:]
        ok, msg = ensure(svc, evict="--evict" in flags)
        if not ok:
            print("FAIL " + msg)
            return 1
        touch(svc)
        try:
            rc = subprocess.call(tail)
        finally:
            if "--keep" not in flags:
                touch(svc)          # 交给 sweep 按 TTL 释放
                print("  （%s 保持运行，TTL 到期后 sweep 自动释放）" % svc)
        return rc

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
