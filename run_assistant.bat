@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 让 modelscope 把模型缓存放到 E 盘, 不再占 C 盘用户目录(~/.cache/modelscope)
if "%MODELSCOPE_CACHE%"=="" set "MODELSCOPE_CACHE=E:\AI\modelscope_cache"

set "PY="
if exist venv\Scripts\python.exe set "PY=venv\Scripts\python.exe"
if "%PY%"=="" if exist ..\funasr-test\venv\Scripts\python.exe set "PY=..\funasr-test\venv\Scripts\python.exe"
if not "%PY%"=="" goto run
echo First run: creating venv and installing deps - may take a few minutes
python -m venv venv
venv\Scripts\python.exe -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
set "PY=venv\Scripts\python.exe"
:run
echo Starting voice assistant - say exit or quit to stop
echo Overlay window shows live results - console and run_assistant.log too
"%PY%" voice_assistant.py
echo.
echo Exited. Press any key to close.
pause >nul