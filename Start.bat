@echo off
rem ---------------------------------------------------------------
rem  OCR BOT - start it all.  Double-click this.
rem
rem  Run as administrator if you are not already.  The game runs elevated, and
rem  Windows drops input from a lower process, so an unelevated bot clicks and
rem  nothing happens - which looks exactly like a broken bot.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem --- self-elevate -------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Not elevated. Relaunching as administrator, accept the prompt...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem --- python --------------------------------------------------------------
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo Python is not on PATH.  Install it from python.org and tick
    echo "Add python.exe to PATH" on the first screen of the installer.
    echo.
    pause
    exit /b 1
)

rem --- run ----------------------------------------------------------------
echo.
echo Starting OCR BOT.  The panel docks on the right of the game.
echo.
python agent.py %*
if %errorlevel% neq 0 pause
exit /b %errorlevel%
