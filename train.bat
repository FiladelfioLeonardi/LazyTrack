@echo off
rem Double-click: teaches the smoking detector what YOU look like smoking, in YOUR room and light.
cd /d "%~dp0"
title LazyTrack: train the camera on you
set PYTHONUTF8=1
echo.
echo  Train the smoking camera on you (about 5 minutes)
echo  --------------------------------------------------
echo  Close the LazyTrack window (start.bat) first: Windows lets only one program use the camera.
echo  Use the same light you usually have on (yes, the RGB light too).
echo.
pause
echo.
echo  STEP 1 of 3: 60 seconds WITHOUT smoking.
echo  Sit like you normally do: type, drink, touch your face, hold your phone or a pen.
pause
.venv\Scripts\python -m lazytrack collect not_smoking --seconds 60 || goto fail
echo.
echo  STEP 2 of 3: 60 seconds WITH a cigarette or joint.
echo  Hold it and smoke like you normally do. Move around, turn your head, use both hands.
pause
.venv\Scripts\python -m lazytrack collect smoking --seconds 60 || goto fail
echo.
echo  STEP 3 of 3: training. This takes a few minutes, let it run...
.venv\Scripts\python -m lazytrack train || goto fail
echo.
echo  Done! Start LazyTrack again (start.bat): it now uses the model trained on you.
echo  Every time you answer the "cigarette or joint?" question it saves a photo,
echo  so running this again later makes it even better.
pause
exit /b 0
:fail
echo.
echo  Something went wrong above. Take a screenshot of this window and send it.
pause
exit /b 1
