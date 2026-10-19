@echo off
rem Agent HUD installer. Finds a Python 3.8+ that has tkinter, then runs app\install.py, which does the real work.
rem This file stays ASCII-only on purpose: cmd reads .bat files in the system code page, so Chinese text here could garble.
setlocal
cd /d "%~dp0"
if not exist "app\install.py" goto notextracted
set "CHECK=import sys, tkinter; sys.exit(sys.version_info < (3, 8))"
set "PY="
py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "%CHECK%" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "%CHECK%" >nul 2>nul && set "PY=python3"
if not defined PY goto nopython
%PY% "app\install.py" %*
if errorlevel 1 pause
exit /b

:notextracted
echo The "app" folder is missing next to this file.
echo Extract the whole zip to a folder first, then run this file from that folder.
echo.
pause
exit /b 1

:nopython
powershell -NoProfile -Command "Get-Content -Encoding UTF8 'app\no-python.txt'"
if errorlevel 1 echo Python 3.8+ with tkinter was not found. Install it from https://www.python.org/downloads/ and run this again.
echo.
pause
exit /b 1
