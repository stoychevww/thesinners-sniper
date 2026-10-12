@echo off
setlocal
title Sniper Control
cd /d "%~dp0"
chcp 65001 >nul

rem ---- first run: create the Python environment and install packages
if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating the Python environment...
  python -m venv .venv 2>nul || py -3 -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python 3.11 or newer is required: https://www.python.org/downloads/
    pause
    exit /b 1
  )
)
if not exist ".venv\.installed" (
  echo [setup] Installing packages, this takes a minute the first time...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Package installation failed. See the messages above.
    pause
    exit /b 1
  )
  type nul > ".venv\.installed"
)

rem ---- which drop: the first argument, or drops\fyut.toml
set "DROP=%~1"
if "%DROP%"=="" set "DROP=drops\fyut.toml"

echo.
echo   ==========================================================
echo     Sniper Control  -  http://127.0.0.1:8800
echo     Drop file: %DROP%
echo.
echo     KEEP THIS WINDOW OPEN during the drop.
echo     Closing it (or Ctrl+C) ends the run.
echo   ==========================================================
echo.
".venv\Scripts\python.exe" -m sniper ui "%DROP%" %2 %3 %4

echo.
echo The UI has stopped.
pause
