@echo off
rem Shortcut: lt <command>   e.g. lt check, lt log gym, lt summary, lt phone
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python -m lazytrack %*
