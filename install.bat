@echo off
rem One-time setup for Windows. Double-click this file.
cd /d "%~dp0"
title LazyTrack setup
set PYTHONUTF8=1
echo.
echo  LazyTrack setup
echo  ---------------
echo.

rem find a Python that the camera AI (PyTorch) supports: 3.10 to 3.13
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
if not defined PY py -3.10 -c "import sys" >nul 2>&1 && set "PY=py -3.10"
if not defined PY python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo  Python 3.12 is needed and wasn't found.
  echo  Get it from https://www.python.org/downloads/release/python-3128/
  echo  ^(Windows installer 64-bit^) and tick "Add python.exe to PATH" in the installer.
  echo  Then double-click install.bat again.
  echo.
  pause
  exit /b 1
)
echo  Using:
%PY% --version
echo.

if not exist .venv (
  echo  Making a private Python folder for LazyTrack...
  %PY% -m venv .venv || goto fail
)
echo  Installing what LazyTrack needs. This downloads about 1 GB the first time, so it takes a while...
.venv\Scripts\python -m pip install --upgrade pip || goto fail
.venv\Scripts\python -m pip install -r requirements-windows.txt || goto fail
rem the camera AI pulls in a second OpenCV that breaks the first one: keep exactly one
.venv\Scripts\python -m pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python opencv-contrib-python-headless >nul 2>&1
.venv\Scripts\python -m pip install opencv-python || goto fail
.venv\Scripts\python -m lazytrack init || goto fail

echo.
choice /c YN /m " Start LazyTrack by itself when Windows starts"
if errorlevel 2 goto done
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\LazyTrack.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()" && echo  Done: it starts when you log in.

:done
echo.
echo  All set! Double-click start.bat to start LazyTrack.
echo  Your browser opens with the welcome screen: make your account there.
echo.
pause
exit /b 0

:fail
echo.
echo  Something went wrong above. Take a screenshot of this window and send it.
echo.
pause
exit /b 1
