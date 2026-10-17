@echo off
rem Double-click: opens the camera with the live smoking score. Press Q in the camera window to close it.
cd /d "%~dp0"
title LazyTrack camera test
set PYTHONUTF8=1
echo Opening the camera (the first time it downloads the AI, about 400 MB, so give it a few minutes)...
echo Hold a pen like a cigarette to see the score go up. Press Q in the camera window to close.
echo.
.venv\Scripts\python -m lazytrack camtest
echo.
echo Camera test finished. If something went wrong, take a screenshot of this window.
pause
