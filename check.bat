@echo off
rem Double-click: tests what LazyTrack can see (apps, idle time, camera, notifications).
cd /d "%~dp0"
title LazyTrack check
set PYTHONUTF8=1
echo For the next 15 seconds, click between a few apps and browser tabs...
echo.
.venv\Scripts\python -m lazytrack check
echo.
echo Done. Take a screenshot of this window if anything says NOT or looks wrong.
pause
