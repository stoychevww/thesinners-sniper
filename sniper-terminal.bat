@echo off
rem BACKUP: runs the drop without the web UI, straight in this window.
rem   Press Enter once the agent is running to stop it and take over.
rem   Type quit + Enter to end the run and close the browser.
setlocal
title Sniper - drop (terminal)
cd /d "%~dp0"
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
  echo Run sniper.bat once first to set things up.
  pause
  exit /b 1
)

set "DROP=%~1"
if "%DROP%"=="" set "DROP=drops\fyut.toml"

echo.
echo   Arming for the drop (%DROP%). A Chrome window opens now.
echo   KEEP THIS WINDOW OPEN. Enter = stop the agent and take over.  quit = end.
echo.
".venv\Scripts\python.exe" -m sniper run "%DROP%"

echo.
echo Run finished.
pause
