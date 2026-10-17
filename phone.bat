@echo off
rem Double-click: connects your iPhone (needs Tailscale on this PC and on the iPhone, same login).
cd /d "%~dp0"
title LazyTrack: connect your iPhone
set PYTHONUTF8=1
echo.
echo  Connect your iPhone
echo  -------------------
echo  Before this: install Tailscale on this PC and on the iPhone (tailscale.com/download)
echo  and log in with the SAME account on both. LazyTrack (start.bat) must be running.
echo.
pause
.venv\Scripts\python -m lazytrack phone
echo.
echo  If a window asks to allow Python on networks: click Allow, then run phone.bat again.
pause
