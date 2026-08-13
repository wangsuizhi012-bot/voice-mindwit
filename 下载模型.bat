@echo off
cd /d "%~dp0"
if not exist venv\Scripts\python.exe if not exist ..\funasr-test\venv\Scripts\python.exe (
  echo 请先运行 run_assistant.bat 建好环境
  pause
  exit /b 1
)
set "PY="
if exist venv\Scripts\python.exe set "PY=venv\Scripts\python.exe"
if "%PY%"=="" set "PY=..\funasr-test\venv\Scripts\python.exe"
echo [下载] 预拉取 SenseVoiceSmall 模型(首次运行也会自动下载)
"%PY%" -c "from modelscope import snapshot_download; snapshot_download('iic/SenseVoiceSmall'); print('done')"
echo [完成] 模型已缓存, 离线也能跑
pause
