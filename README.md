# 本地语音控制 RPA (Voice Control Assistant)

张嘴说话，电脑自己干活。**全本地、零云端**：语音识别 → 本地大模型理解意图 → 鼠标键盘自主执行。

- 识别：FunASR / SenseVoiceSmall（阿里达摩院，中文事实标准）
- 端点检测：webrtcvad（说完自动截断，不用按按钮）
- 意图理解：本地大模型（llama.cpp / Ollama 等 OpenAI 兼容接口，如 Qwen2.5-7B）
- 执行：pyautogui（截图 / 点击 / 按键 / 打字 / 打开程序）
- **自主定位**：Windows UI 自动化树（uiautomation），说"点发送"尝试在前台窗口定位该控件并点击，无需你先移鼠标

## 能干什么

| 你说 | 它做 |
|---|---|
| 帮我截个图 | 截图存盘 + 进剪贴板（默认当前窗口） |
| 截左上角 / 右半边 / 下半部分 / 整个屏幕 | 截对应屏幕区域（tl/tr/bl/br/left/right/top/bottom/full） |
| 帮我截个图发给我 | 截图 + 粘贴到当前输入框 + 回车发送 |
| 点发送 / 点确定（标准软件带文字控件） | **尝试在前台窗口定位并点击该控件**（无需移动鼠标） |
| 点一下 | 点击鼠标当前位置（双击 / 右键也支持） |
| 保存 / 复制 / 粘贴 / 全选 | 触发对应键盘热键（Ctrl+S / C / V / A） |
| 按回车 | 回车 |
| 往下滚 / 往上滚 | 滚动滚轮（scroll，正数向下负数向上） |
| 悬停 / 移到 发送 上 | 只移动鼠标到控件不点击（hover 预览，防误触） |
| 打开记事本 | 启动程序 |
| 按 F8 | 停止整个程序（安全兜底） |
| 按 F9 | 停止录制宏 / 演示录制并保存（不退出程序） |
| 请你跟我这样做，我就跟你这样做 | **演示录制**：开始录你的键鼠操作，说「停止」或按 F9 存成可重放脚本 |
| 退出 | 停止 |
| 聊天模式 / 退出聊天 | 开启/关闭多轮对话（非命令语音走本地大模型闲聊，可选 TTS 朗读） |
| 清空对话 | 重置对话历史 |
| 学习技能 发邮件 | 截取当前界面 → 本地视觉模型(:1235)推断步骤 → 存为技能（云端大模型可选） |
| 录制技能 发邮件 | 手动演示：逐步说指令，说「完成」存为技能 |
| 执行技能 发邮件 | 一句话重放整段技能（带二次确认） |
| 列出技能 / 删除技能 X | 查看 / 删除已学技能 |

## 2026-09-28 更新：按键语音输入 + 准确率专项 + 终端式管道可视化

### 1. 按键语音输入（Typeless 式听写，`dictation.py` + `dictation_ui.py`）
光标放在任意输入框 → **长按 `Ctrl+Alt+Space` 说话 → 松开即上屏**。交互照抄
local-dictate / VoiceSnap 的已验证手感：

| 操作 | 行为 |
|---|---|
| 长按热键 | 按住说话，松开识别上屏 |
| 短按热键（<250ms） | 切换为持续模式，说完再按一次结束（自动判别，无需切配置） |
| 录音中按 `Esc` | 取消本次，不输出 |
| 切换模式下静音 | 可配 `dictation_silence_stop_s` 自动结束 |

- **上屏走剪贴板 + Ctrl+V，粘贴后自动还原原剪贴板**（剪贴板保护）
- 实时 partial：每 0.7s 对已录音频重识别一次，只把新增字符推给指示器**逐字浮现**
- 悬浮卡片可拖动、自动淡出、`WS_EX_NOACTIVATE` 不抢焦点（粘贴不粘错窗口）
- 识别历史落 `state/dictation_history.jsonl`（默认留 200 条）

### 2. 准确率专项（全部有实测依据，见 `asr_better.py` 文件头）
- **音频前端**：去直流 + 峰值归一化（小声说话的最大收益项）+ 增益上限防噪声放大
- **噪声门**：整段 RMS 过低直接丢弃 —— 实测日志里 `L, A A`、`S SY A` 就是
  噪声段被硬识别出来的
- **短音频强制 zh**：`auto` 语言检测在 1~2s 片段上会翻车（实测输出 `Right了`）
- **silero VAD 补预滚**：修掉句首吞字（旧代码 silero 分支没用 preroll）
- **文本后处理**：纠错词典按最长匹配优先 + 语气词过滤（嗯/啊/呃）+ 中英之间自动加空格
- **LLM 校对（`asr_polish.py`）**：本地大模型做同音错字修正/标点恢复/术语归一，
  带**中文骨架相似度闸门** —— 模型一旦自由发挥（改写/扩写/答非所问）就丢弃，
  宁可不改也不改坏。实测闸门拦下过 2 次模型的自由发挥

### 3. 终端式管道面板（`dictation_console`）
听写卡片内嵌终端风格事件流：`MIC ─▶ VAD ─▶ ASR ─▶ POLISH ─▶ PASTE` 当前阶段
高亮，逐条打印时间戳 / 音频指标（rms·peak·增益）/ 各阶段耗时 / partial 文本流。
配置 `"dictation_console": false` 可关。

> 顺手修了一个老 bug：置顶小窗（Overlay）此前把 `Tk` 建在主线程、`mainloop`
> 跑在子线程，Windows 的 Tk 非线程化会抛 `Calling Tcl from different apartment`
> 且被吞掉 —— **小窗其实一直是冻结的**。现改为「Tk 全程在自己的线程 + 命令队列
> 派发」，真正实时刷新了。


## 新增功能（2026-08-18）：对话 / 技能 / 视觉点击

针对「识别率低、打开慢、功能单一」三个痛点做了增强，**原有脚本录制（宏）功能完全保留**。

### 1. 对话功能（多轮 + 可选 TTS）
非命令类语音默认走本地大模型（:1234）做多轮闲聊，回复显示在置顶小窗，并可用 **Windows SAPI 朗读**（零依赖、全程本地，不需要任何云端 TTS）。
- 说「聊天模式」开启 / 「退出聊天」关闭 / 「清空对话」清历史。
- 想关掉朗读：config 设 `"tts": false`。

### 2. 技能系统（截图→大模型→可重放的点击序列）
复刻社区主流做法（参考 harvis 的 skills/ 目录、Personal-PC-Assistant 的 learned commands）：把一串操作固化成**命名技能**，下次一句话重放。
- **学习技能 X**：截取当前界面 → 发给本地视觉模型 Qwen3-VL(:1235) → 推断在该界面上完成「X」所需的点击/输入步骤 → 存为技能。
  - 视觉模型不可用时自动降级为**录制技能 X**（你手动演示一遍，说「完成」保存）。
- **执行技能 X**：重放该技能（带二次确认，说「确认」执行）。
- **列出技能 / 删除技能 X**：管理。
- 技能文件存 `skills/<名字>.json`，步骤格式与宏完全同构（intent JSON），可直接手编。

> 云端大模型训练（可选）：config 的 `trainer` 填 `base/key/model` 后，「学习技能」会改走云端多模态模型（更强，但走网络），留空则默认用本地 VL。

### 3. 视觉点击兜底（本地显卡 + Python 低本方案）
控件树/模板/OCR 都定位不到时（游戏、自绘 UI、无文字控件），新增 `click_visual` 动作：用本机 VL(:1235) 看截图，按「网格动作空间」（见 `E:/AI/knowledge/nuphus-desktop-automation`）只回答目标在哪一格，坐标纯算术得出 → 可复现、token 极省。说「点红色登录按钮」且控件树找不到时，大模型会返回 `click_visual` 兜底。

### 4. 识别率 & 启动速度
- **识别率**：SenseVoice-Small 在中文上本就优于 Whisper（CER 4.2% vs 5.8%），识别率低多半是 VAD 截断 / 麦克风 / GPU 未启用 / 领域词，而非模型。针对性做了：
  - `asr_mode: "server"`：走常驻 `funasr-server`（OpenAI 兼容，默认 :8000），启动**零等待**且识别率一致 → 这是「打开快」的最佳解。先 `funasr-server --device cuda` 起服务即可。
  - `correction_dict`：纠错词典 `{"误识别":"正确写法"}`，零成本逼近热词效果（SenseVoice 不支持热词）。
  - 后台加载 ASR，启动即弹窗，不再干等模型。
- **启动速度**：置顶小窗先弹，ASR 后台加载，首条指令自动等模型就绪；想秒开用上面的 server 模式。

## 环境要求

- Windows 10/11
- Python 3.10+（建议用脚本自带的虚拟环境）
- 一个本地 LLM 服务，暴露 OpenAI 兼容接口（默认 `http://localhost:1234/v1`，即 llama.cpp）
  - 例：`llama-server -m Qwen2.5-7B-Instruct-Q4_K_M.gguf -c 4096 --port 1234`
- 麦克风

## 安装与运行

```bat
# 1. 双击 run_assistant.bat 即可（首次会自动建 venv 并装依赖，需几分钟）
#    或者手动：
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe voice_assistant.py
```

说"退出"停止。也可以 `voice_assistant.py --selftest` 不连麦跑意图解析自检。

## 配置 (config.json)

| 字段 | 说明 | 默认 |
|---|---|---|
| `mic_keyword` | 麦克风关键字，留空=跟随 Windows 默认输入设备 | `""` |
| `llm_base` | 本地 LLM 的 OpenAI 兼容地址 | `http://localhost:1234/v1` |
| `screenshot_mode` | `active_window` / `full` / `region` | `active_window` |
| `auto_send` | 截图后是否自动回车发送 | `false` |
| `vad_aggressiveness` | webrtcvad 灵敏度 0-3 | `3` |
| `vad_engine` | `silero`（主，抗噪声）/ `webrtcvad`（兜底） | `silero` |
| `wake_word` | openWakeWord 唤醒词模型名（如 `hey_jarvis`）；留空=持续监听 | `""` |
| `confirm_high_risk` | 高风险动作（发送/打字/打开/点击）执行前是否要语音二次确认 | `false` |
| `cooldown_s` | 两次指令最小间隔 | `1.0` |
| `tts` | 对话回复是否用 Windows SAPI 朗读 | `true` |
| `chat_enabled` | 非命令语音是否走多轮对话 | `true` |
| `asr_mode` | `local`(SenseVoice GPU) / `server`(funasr-server 秒开) | `local` |
| `asr_server` | funasr-server 地址 | `http://localhost:8000/v1` |
| `correction_dict` | 纠错词典：`{"误识别":"正确写法"}` | `{}` |
| `vl_base` | 本地视觉模型(用于技能训练/视觉点击) | `http://localhost:1235/v1` |
| `trainer` | 云端多模态(可选)：`{base,key,model}`，留空=用本地 VL | `{}` |

模型路径：SenseVoice 走 ModelScope 缓存（`~/.cache/modelscope`），首次自动下载。

## 安全设计（防误触发）

配合 pyautogui 直接操作电脑，误触发是危险的，所以有三层防护：

1. **VAD 引擎可选 Silero**（`vad_engine: "silero"`）：神经网络 VAD，对环境底噪/键盘声更鲁棒，比 webrtcvad 少误触发。加载失败自动回退 webrtcvad。
2. **唤醒词门控**（`wake_word: "hey_jarvis"`）：启用后必须先喊唤醒词，8 秒内下命令才生效，平时只听唤醒词、不解析命令。彻底杜绝环境音误触。
3. **动作白名单 + JSON 校验 + 高风险确认**：LLM 返回的 JSON 先过 `validate_intent()`（动作必须在白名单、参数类型合法），不合法一律拒绝执行；`confirm_high_risk: true` 时，发送/打字/打开/点击类动作会先问"确认吗？说 确认 或 取消"，二次确认才执行。

## 自主点击怎么实现的

`locate.py` 枚举当前前台窗口的所有控件（名字 + 类型 + 坐标），LLM 从对话里抽出"发送"等目标文字，
在控件树里做匹配，取中心坐标交给 pyautogui 点击。**标准软件中带可识别文字的控件通常能命中**，
且不依赖额外视觉模型。游戏 / 自绘 UI 等取不到无障碍树的界面，建议**手动截图放 `templates/` 按图匹配**；
OCR 兜底（`easyocr`）默认关闭——边聊天边用会误命中聊天窗口文字，需要时在 `config.json` 设 `"ocr_fallback": true`。

## 手动放模板图（推荐兜底，比 OCR 稳）

控件树取不到控件时，最可靠的做法是自己截一张目标图丢进 `templates/`：

1. 把目标按钮截图，命名成 `目标文字.png`（如 `默认权限.png`）
2. 放进 `E:\AI\voice-assistant\templates\`
3. 说「点默认权限」→ 按图匹配直接点中

不用截图软件也行：说"点xxx"失败进入教学态，手动点一下，系统会自动截 120×120 存进 `templates/`。

## 自我学习与教学（提高点击成功率）

纯控件树/模板仍有死角（比如记事本没有叫"设置"的按钮）。本助手内置**失败驱动的自学习记忆库**：

- 说"点设置"找不到 → 置顶小窗提示"请手动点一下目标位置" → 你把鼠标移到目标说"点一下"
- 系统同时记两样：①该目标在**当前窗口的相对坐标**进 `click_memory.json`；②点击点周围 **120×120 截图**存 `templates/<目标>.png`（借鉴 waterRPA 模板匹配）
- 下次同一应用再说"点设置" → 控件树没命中就先用记忆坐标，再按模板图全屏匹配

命中优先级：`控件树` → `记忆库` → `模板匹配` → `OCR 兜底（默认关）`。记忆库为失败驱动的自学习机制，随使用积累命中率会提升（本机实测样本尚少，欢迎补充）。记忆可手动编辑 `click_memory.json` 增删；`templates/` 里的模板图也可自行替换。
辅助诊断：运行 `inspect_foreground.py` 列出前台窗口所有带名字的控件，确认该喊什么目标文字。

## 录制宏（一句话重放一串操作）

借鉴 waterRPA 的"脚本播放"思路，把一串语音指令录成**命名流程**，下次一句话整段重放：

- 说「**开始录制**」→ 进入录制态，之后说的每条指令都会**边执行边记录**
- 说「**停止录制叫 开机流程**」→ 保存成流程（存 `flows/<名字>.json`）
- 以后说「**执行流程 开机流程**」→ 先整体二次确认（说"确认"）→ 按顺序逐条重放，每步间隔 0.6 秒

流程文件是标准 JSON，可手动编辑 `flows/` 增删步骤；说「执行流程」不带名字则重放最近一个。

## 演示录制（口令一句话：动手做一遍，系统自己抄下来）

跟上面的「录制宏」是**两套并存**的东西，别混：

| | 录制宏 | 演示录制（本节） |
|---|---|---|
| 怎么录 | 你**口头说**一串指令 | 你**动手做**一遍（鼠标键盘真实操作） |
| 触发 | 说「开始录制」 | 说口令「**请你跟我这样做，我就跟你这样做**」 |
| 产物 | intent 列表（LLM 意图） | agent_core DAG（带**文字锚点**，抗窗口移动） |
| 重放 | 说「执行流程 X」 | 说「执行流程 demo_XXXX」（同名入口，自动分路） |

用法：

- 说口令（带名字也行：「…这样做 **叫 打卡流程**」）→ 后台开始录键鼠，右上角进「演示录制」态
- **正常操作你的电脑**，每次点击会存截图 + 坐标 + 当前窗口
- 说「**停止**」/「录好了」/「就到这里」，或按 **F9**，或到 120 秒自动停 → 自动编译保存
- 编译阶段：OCR 反解出你点了哪个**文字**（不是坐标），再用本地 LLM 修 OCR 错字；可用 `demo_ai_compile:false` 关掉

录制期间**所有语音指令都被吞掉**（只认停止词），避免你边操作边误触发别的动作。
中文输入 pynput 拿不到输入法组字结果，录完建议 `python recorder.py show --name <名字>` 检查，缺的手工补 `{"do":"type","text":"..."}`。

相关配置项（`config.json`）：`demo_max_seconds` / `demo_max_events` / `demo_ai_compile` / `demo_strict`。
依赖：`pip install pynput`（已写入 requirements.txt）。

## 已知限制

- 动作范围受脚本已实现的函数限制（想加新动作改 `execute()` + `SYS_PROMPT`）。
- 复杂 UI 定位（如"点红色按钮"）仍依赖控件名/OCR 文字，纯视觉语义定位需接入 OmniParser / UI-TARS 等视觉模型。
- 无障碍树对 DirectX 游戏、部分 Electron 自绘控件可能取不到。

## 目录结构

```
voice-assistant/
  voice_assistant.py   # 主程序：监听→识别→意图→执行（含录制宏）
  locate.py            # 定位层：口语指令→屏幕坐标（控件树为主，模板匹配+OCR 兜底，记忆库学习）
  macro.py             # 录制宏：一串指令录成命名流程，一句话重放
  recorder.py          # 演示录制：键鼠操作→截图→OCR 反解→编译成可重放 DAG
  config.json          # 配置
  requirements.txt     # 依赖
  run_assistant.bat    # 一键启动（首次自动装环境）
  click_memory.json    # 自学习点击记忆库（自动生成，可手动编辑）
  templates/           # 模板图（教学时自动截的 120×120 小图，gitignored 含隐私）
  flows/               # 录制宏的流程文件（自动生成，gitignored）
  inspect_foreground.py# 诊断：列出前台窗口所有带名字的控件
  shots/               # 截图输出
  run_assistant.log    # 运行日志
  dialogue.py          # 对话模块：多轮闲聊 + TTS(Windows SAPI)
  asr_better.py        # ASR 抽象层：local GPU / server + 纠错词典 + 音频前端
  asr_polish.py        # LLM 校对：同音错字/标点/术语归一（带骨架相似度闸门）
  dictation.py         # 按键语音输入：按住说话/短按切换/实时partial/剪贴板保护
  dictation_ui.py      # 听写悬浮卡片：逐字动画 + 终端式管道面板
  test_asr_upgrade.py  # 准确率改造自检（27 项纯逻辑断言）
  test_dictation_smoke.py  # 听写端到端冒烟（13 项，含 UI 渲染截图）
  test_real_asr.py     # 真模型识别 + 真 LLM 校对联调
  skills.py            # 技能存储（复用 macro 步骤格式）
  skill_trainer.py     # 截图→视觉模型→技能
  visual_click.py      # 运行时视觉点击（网格动作空间）
  skills/              # 已学技能（自动生成，可手编）
```

## 致谢（Acknowledgements）

本项目站在以下开源项目的肩膀上，特此感谢：

- **FunASR / SenseVoice**（[ModelScope/FunASR](https://github.com/modelscope/FunASR)，阿里巴巴达摩院）—— 中文语音识别主干，事实标准级准确率
- **Silero VAD**（[snakers4/silero-vad](https://github.com/snakers4/silero-vad)）—— 神经网络端点检测，比 webrtcvad 更抗噪声
- **openWakeWord**（[dscripka/openWakeWord](https://github.com/dscripka/openWakeWord)）—— 唤醒词门控，防环境音误触发
- **Qwen**（[QwenLM/Qwen](https://github.com/QwenLM/Qwen)，阿里巴巴通义千问）+ **llama.cpp**（[ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)）—— 本地大模型意图解析，全程离线
- **pyautogui**（[asweigart/pyautogui](https://github.com/asweigart/pyautogui)）—— 鼠标键盘执行
- **uiautomation**（[yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)）—— Windows UI 控件树，自主定位的核心
- **EasyOCR**（[JaidedAI/EasyOCR](https://github.com/JaidedAI/EasyOCR)）—— 视觉兜底定位
- **webrtcvad**（[wiseman/py-webrtcvad](https://github.com/wiseman/py-webrtcvad)）—— VAD 兜底引擎
- **sounddevice / torch / Pillow / pywin32** —— 音频采集与底层支撑

按键语音输入（听写）功能的交互设计，参考并借鉴了以下开源项目，特此致谢：

- **local-dictate**（[ydesh/local-dictate](https://github.com/ydesh/local-dictate)）——
  「长按说话 / 短按自动切换为持续模式」的交互范式、自定义词表思路
- **VoiceSnap**（[vorojar/VoiceSnap](https://github.com/vorojar/VoiceSnap)）——
  剪贴板保护（上屏后还原原剪贴板）、语气词过滤、识别历史、Esc 取消、静音自动停止
- **OpenLess**（[MurphyLo/openless](https://github.com/MurphyLo/openless)）——
  热词/术语表注入 ASR 与润色端做语义修正、剪贴板兜底不丢内容
- **Typeless / Wispr Flow** —— 商业产品定义了这一品类的体验标准，本项目按其
  交互做了完全本地的等价实现

也感谢社区大量 RPA / 语音助手实践带来的设计启发。

## 许可证

[MIT](LICENSE) —— 可自由使用、修改、再分发，请保留原作者声明。

## 授权与素材说明（合规）

**字体**：界面仅按**字体名**引用系统字体（`Microsoft YaHei UI`、`Consolas`），
本项目**不随附、不打包任何字体文件**。字体本身受各自版权方（Microsoft）许可约束，
由操作系统提供；如需商业分发请自行确认目标环境的字体授权。

**第三方依赖**：均为宽松许可证，可商用 —— MIT：funasr / sounddevice / webrtcvad /
silero-vad；Apache-2.0：modelscope / requests / openwakeword / easyocr / uiautomation /
torch；BSD：torchaudio / pyautogui / numpy；PSF：pywin32；MIT-CMU：Pillow。
> ⚠️ 唯一弱 copyleft：**pynput 为 LGPL-3.0**。本项目仅以未修改的 pip 依赖形式动态
> 导入（演示录制 `recorder.py` 用），未分发其源码。若你需要**闭源静态分发**，
> 请先自行评估 LGPL 的链接条款，或把 pynput 换成 MIT/Apache 的键盘库。

**图片/音频/图标**：仓库内**不含可分发的媒体素材** —— 截图、模板图、录音等运行产物
均在 `.gitignore` 中（`shots/`、`templates/`、`*.png`、`*.wav`），不会进仓库。

**代码片段**：本项目仅借鉴设计思路，未整段复制第三方代码。参考来源（OpenAdapt、
waterRPA、harvis、local-dictate、VoiceSnap、OpenLess、nuphus）已在上方致谢章节列明并附链接。


## 决策引擎可选：原模型（LLM） / Laya（2026-09-26）

意图理解这一步现在可以换引擎，说一句话就能切，切换结果写回 `config.json`。

| 你说 | 效果 |
|---|---|
| 用laya / 切换到laya / 用决策模型 | 意图走本地 Laya 决策模型 |
| 用原模型 / 换回原模型 / 用本地大模型 | 意图走原模型（llama-swap 网关 :9292） |
| 自动模式 | Laya 优先，置信度不够自动回退原模型 |

`config.json` 新增键：

```json
"decision_engine": "llm",      // llm | laya | auto
"laya_base": "http://127.0.0.1:8801",
"laya_min_conf": 0.70,         // 低于此置信度回退原模型
"laya_fallback_llm": true
```

跑法：

1. 双击 `启动Laya服务.bat`（用托管 venv 起 `laya_serve.py`，首次加载权重约 25–35 秒，日志出现 `[laya] ready` 即可用）
2. 正常启动语音助手，说「用laya」即可切换（不重启）
3. 关掉服务窗口 = 停止；语音助手检测不到服务会自动回退原模型，不会卡死

文件：`laya_serve.py`（服务，跑在托管 venv，只依赖标准库）、`decision.py`（引擎开关与回退逻辑）、
`test_decision.py`（对比 demo：`venv\Scripts\python.exe test_decision.py`）。

实测（16 条常用口令）：

- 命中 **16/16**（与 LLM 结果一致，`往下滚` 一项 Laya 更符合文档约定：正数向上、负数向下）
- 延迟 **30–60ms/次**（原模型约 500ms/次，快一个数量级）
- 权重：`convaiinnovations/laya` 的 `multilingual` 子目录（646.8 MB，走 hf-mirror 22 秒下完）

三个踩过的坑（改代码前先看）：

1. **criteria 要用英文**：中文选项描述会把「点发送按钮」判成 screenshot_send，换成英文后判对（该权重指令微调以英文为主）。
2. **别把 noul 和 11 选项的 choice 放同一批推理**：会互相干扰，noul 概率从 0.66 掉到 0.06，把所有动作都压成 none。改用 `is_command = 1 - P(none)`。
3. **确定性动作走规则快通道**：保存/复制/粘贴/输入/坐标点击/右键双击，Laya 也会判错，规则命中直接返回（返回体里 `via=rule` 表示走的规则）。
