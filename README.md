# 本地语音控制 RPA (Voice Control Assistant)

张嘴说话，电脑自己干活。**全本地、零云端**：语音识别 → 本地大模型理解意图 → 鼠标键盘自主执行。

- 识别：FunASR / SenseVoiceSmall（阿里达摩院，中文事实标准）
- 端点检测：webrtcvad（说完自动截断，不用按按钮）
- 意图理解：本地大模型（llama.cpp / Ollama 等 OpenAI 兼容接口，如 Qwen2.5-7B）
- 执行：pyautogui（截图 / 点击 / 按键 / 打字 / 打开程序）
- **自主定位**：Windows UI 自动化树（uiautomation），说"点发送"鼠标自己飞过去点，不用你移鼠标

## 能干什么

| 你说 | 它做 |
|---|---|
| 帮我截个图 | 截图存盘 + 进剪贴板 |
| 帮我截个图发给我 | 截图 + 粘贴到当前输入框 + 回车发送 |
| 点发送 / 点确定 / 点设置 | **自主在前台窗口找到该控件并点击**（无需移动鼠标） |
| 点一下 | 点击鼠标当前位置（双击 / 右键也支持） |
| 保存 / 复制 / 粘贴 / 全选 | 触发对应键盘热键（Ctrl+S / C / V / A） |
| 按回车 | 回车 |
| 打开记事本 | 启动程序 |
| 退出 | 停止 |

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

模型路径：SenseVoice 走 ModelScope 缓存（`~/.cache/modelscope`），首次自动下载。

## 安全设计（防误触发）

配合 pyautogui 直接操作电脑，误触发是危险的，所以有三层防护：

1. **VAD 引擎可选 Silero**（`vad_engine: "silero"`）：神经网络 VAD，对环境底噪/键盘声更鲁棒，比 webrtcvad 少误触发。加载失败自动回退 webrtcvad。
2. **唤醒词门控**（`wake_word: "hey_jarvis"`）：启用后必须先喊唤醒词，8 秒内下命令才生效，平时只听唤醒词、不解析命令。彻底杜绝环境音误触。
3. **动作白名单 + JSON 校验 + 高风险确认**：LLM 返回的 JSON 先过 `validate_intent()`（动作必须在白名单、参数类型合法），不合法一律拒绝执行；`confirm_high_risk: true` 时，发送/打字/打开/点击类动作会先问"确认吗？说 确认 或 取消"，二次确认才执行。

## 自主点击怎么实现的

`locate.py` 枚举当前前台窗口的所有控件（名字 + 类型 + 坐标），LLM 从对话里抽出"发送"等目标文字，
在控件树里做匹配，取中心坐标交给 pyautogui 点击。**标准软件（微信/记事本/浏览器/Office）命中率最高**，
且不依赖任何额外视觉模型。对于游戏 / 自绘 UI 等取不到无障碍树的界面，可装 `easyocr` 走 OCR 兜底
（`pip install easyocr`，自动启用）。

## 自我学习与教学（提高点击成功率）

纯控件树/OCR 仍有死角（比如记事本没有叫"设置"的按钮）。本助手内置**失败驱动的自学习记忆库**：

- 说"点设置"找不到 → 置顶小窗提示"请手动点一下目标位置" → 你把鼠标移到目标说"点一下"
- 系统把该目标在**当前窗口的相对坐标**记进 `click_memory.json`（按窗口 rect 还原，窗口挪动/缩放也不怕）
- 下次同一应用再说"点设置" → 控件树没命中就直接用记忆坐标点，不用再找、不依赖 OCR

命中优先级：`控件树` → `记忆库` → `OCR 兜底`。越用越准；记忆可手动编辑 `click_memory.json` 增删。
辅助诊断：运行 `inspect_foreground.py` 列出前台窗口所有带名字的控件，确认该喊什么目标文字。

## 已知限制

- 动作范围受脚本已实现的函数限制（想加新动作改 `execute()` + `SYS_PROMPT`）。
- 复杂 UI 定位（如"点红色按钮"）仍依赖控件名/OCR 文字，纯视觉语义定位需接入 OmniParser / UI-TARS 等视觉模型。
- 无障碍树对 DirectX 游戏、部分 Electron 自绘控件可能取不到。

## 目录结构

```
voice-assistant/
  voice_assistant.py   # 主程序：监听→识别→意图→执行
  locate.py            # 定位层：口语指令→屏幕坐标（控件树为主，OCR 兜底，记忆库学习）
  config.json          # 配置
  requirements.txt     # 依赖
  run_assistant.bat    # 一键启动（首次自动装环境）
  click_memory.json    # 自学习点击记忆库（自动生成，可手动编辑）
  inspect_foreground.py# 诊断：列出前台窗口所有带名字的控件
  shots/               # 截图输出
  run_assistant.log    # 运行日志
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

也感谢社区大量 RPA / 语音助手实践带来的设计启发。

## 许可证

[MIT](LICENSE) —— 可自由使用、修改、再分发，请保留原作者声明。
