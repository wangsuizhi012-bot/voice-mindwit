@echo off
title Laya decision service :8801
cd /d "%~dp0"
echo ============================================
echo  Laya decision service  http://127.0.0.1:8801
echo  checkpoint: convaiinnovations/laya (multilingual)
echo  first load ~30s, wait for "[laya] ready" in log
echo  close this window = stop service (assistant auto-falls back to LLM)
echo ============================================
set LAYA_PORT=8801
set LAYA_SUBFOLDER=multilingual
rem Python path must not hardcode a username: prefer PATH python, then local venv.
rem NOTE: keep this file ASCII-only (cmd.exe uses the GBK code page).
where python >nul 2>nul && (python laya_serve.py & goto :done)
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" laya_serve.py
) else (
    echo [ERR] python not found. Add it to PATH, or create a venv in this folder.
)
:done
pause
