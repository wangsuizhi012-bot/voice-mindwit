@echo off
chcp 65001 >nul
title Laya decision service :8801
cd /d "%~dp0"
echo ============================================
echo  Laya decision service  http://127.0.0.1:8801
echo  checkpoint: convaiinnovations/laya (multilingual)
echo  first load ~60s, wait for "[laya] ready" in log
echo  close this window = stop service (assistant auto-falls back to LLM)
echo ============================================

set "LAYA_PORT=8801"
set "LAYA_SUBFOLDER=multilingual"
rem System proxy hijacks loopback HTTP calls; clear it (same as laya_serve.py).
set "NO_PROXY=127.0.0.1,localhost"
set "HF_HUB_OFFLINE=1"

rem ---------------------------------------------------------------------------
rem Python selection (fixed 2026-10-14): must prefer the interpreter that has
rem laya installed. The old version ran "where python" first, which resolved to
rem the system Python312 (no laya) -> ModuleNotFoundError every time, so this
rem service never actually started. The project venv only has requests and no
rem laya/torch either. The only laya environment on this machine is the
rem WorkBuddy managed venv below.
rem NOTE: keep this file ASCII-only (cmd.exe uses the GBK code page).
rem ---------------------------------------------------------------------------
set "PY="
set "WV=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if exist "%WV%" set "PY=%WV%"
if not "%PY%"=="" goto run

rem Fallback: managed venv missing (other machine / cleaned up). Say what to do
rem instead of silently picking the wrong interpreter.
echo [ERR] WorkBuddy managed venv not found:
echo       %WV%
echo.
echo   That venv is the only one with laya + torch installed.
echo   Options:
echo     A) reinstall the WorkBuddy python env (restores laya/torch)
echo     B) create a venv here, then: pip install laya torch transformers
echo        and point PY in this file at that python.
echo.
echo   Until then the assistant falls back to the big LLM for every command
echo   (decision_engine=auto degrades gracefully; nothing breaks).
echo.
pause
exit /b 1

:run
"%PY%" laya_serve.py
echo.
echo [laya] service exited.
pause
exit /b 0
