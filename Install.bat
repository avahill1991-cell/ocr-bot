@echo off
rem ---------------------------------------------------------------
rem  OCR BOT - install the python dependencies.  Run this once.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not on PATH.
    echo Install it from https://python.org and tick
    echo "Add python.exe to PATH" on the first screen.
    pause
    exit /b 1
)

echo Installing python packages...
python -m pip install --user -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo pip failed. Try: python -m pip install --user --upgrade pip
    pause
    exit /b 1
)

rem --- Tesseract OCR is a separate install, pip does not provide it --------
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    echo.
    echo Tesseract found in Program Files.
) else (
    where tesseract >nul 2>&1
    if %errorlevel% equ 0 (
        echo.
        echo Tesseract found on PATH.
    ) else (
        echo.
        echo Tesseract OCR is NOT installed.  HP and chat reads need it.
        echo Install it from https://github.com/UB-Mannheim/tesseract/wiki
        echo and tick "Add to PATH" during its install.
        echo.
    )
)

echo.
echo Done.  Run Start.bat as administrator.
pause
