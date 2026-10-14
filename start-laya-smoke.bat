@echo off
REM ============================================================
REM  start-laya-smoke.bat  --  Laya decision service :8801
REM
REM  WHY THIS FILE EXISTS
REM  The original Chinese-named launcher ("qi dong Laya fu wu.bat")
REM  resolves the interpreter with `where python`, which on this machine hits a system Python
REM  (3.12 / 3.14) that does NOT have the laya package -> startup
REM  fails with ModuleNotFoundError. Only the WorkBuddy managed
REM  venv has laya 0.3.6 + torch 2.14.0+cu130.
REM  So we try the managed venv FIRST, then fall back.
REM
REM  Keep this file ASCII-only (cmd.exe uses the GBK code page).
REM ============================================================
title Laya decision service :8801
cd /d "%~dp0"

echo ============================================
echo  Laya decision service  http://127.0.0.1:8801
echo  checkpoint: convaiinnovations/laya (multilingual)
echo  first load ~30s, wait for "[laya] ready"
echo  close this window = stop service
echo ============================================

set LAYA_PORT=8801
set LAYA_SUBFOLDER=multilingual

set "PY="
REM 1) preferred: WorkBuddy managed venv (has laya)
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
  goto :run
)
REM 2) local project venv
if exist "%~dp0venv\Scripts\python.exe" (
  set "PY=%~dp0venv\Scripts\python.exe"
  goto :run
)
REM 3) fallback: whatever python is on PATH (may fail on laya import)
where python >nul 2>nul && set "PY=python"

:run
if not defined PY (
  echo [ERR] no python interpreter found.
  echo Fix: install laya into one of:
  echo   %USERPROFILE%\.workbuddy\binaries\python\envs\default
  echo   %~dp0venv
  pause
  exit /b 1
)

echo [i] interpreter: %PY%
"%PY%" -c "import laya" 2>nul
if errorlevel 1 (
  echo [ERR] this interpreter does NOT have the laya package.
  echo Interpreter: %PY%
  echo Fix: install it, e.g.
  echo   "%PY%" -m pip install laya --index-url https://mirrors.aliyun.com/pypi/simple
  pause
  exit /b 1
)

echo [i] laya import OK, starting server...
echo.
"%PY%" laya_serve.py
echo.
echo [i] server stopped.
pause