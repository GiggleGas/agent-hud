@echo off
rem Agent HUD uninstaller. Runs app\install.py --uninstall with whatever Python 3 is available.
setlocal
cd /d "%~dp0"
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>nul && set "PY=python3"
if not defined PY goto nopython
%PY% "app\install.py" --uninstall %*
if errorlevel 1 pause
exit /b

:nopython
echo Python was not found, so the uninstaller cannot run. To remove Agent HUD by hand:
echo   1. Close the HUD window.
echo   2. Delete the folder %USERPROFILE%\.claude\agent-hud
echo   3. Delete the "Agent" shortcuts on the desktop and in the Start menu.
echo   4. Remove the block between "agent-hud:begin" and "agent-hud:end" in %USERPROFILE%\.claude\CLAUDE.md
echo.
pause
exit /b 1
