@echo off
rem Runs LazyTrack with no window (started by LazyTrack.vbs). Restarts it when it asks to (exit code 3, e.g. after an update).
cd /d "%~dp0.."
set PYTHONUTF8=1
:run
.venv\Scripts\python.exe -m lazytrack run >> "%USERPROFILE%\.lazytrack\lazytrack.log" 2>&1
if %errorlevel%==3 (
  set LAZYTRACK_RESTARTED=1
  goto run
)
