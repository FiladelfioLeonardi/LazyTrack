@echo off
rem Double-click to start LazyTrack. Keep this window open (you can minimize it).
cd /d "%~dp0"
title LazyTrack
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
  echo LazyTrack isn't installed yet. Double-click install.bat first.
  pause
  exit /b 1
)
:run
.venv\Scripts\python -m lazytrack run
rem exit code 3 = "restart me" (after changing settings)
if %errorlevel%==3 goto run
echo.
echo LazyTrack stopped. Close this window, or press a key to start it again.
pause >nul
goto run
