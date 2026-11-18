@echo off
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"

rem --- 1) Create a dedicated Python 3.14 virtual environment on first run ---
if not exist "%VPY%" (
    echo [Setup] Creating Python 3.14 virtual environment ^(first run only^)...
    py -3.14 -c "import sys" >nul 2>&1
    if errorlevel 1 goto :nopython
    py -3.14 -m venv "%VENV%"
    if errorlevel 1 goto :fail
)

rem --- 2) Install packages when requirements.txt is new or has changed ---
fc /b "requirements.txt" "%VENV%\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo [Setup] Installing packages ^(first run may take several minutes^)...
    "%VPY%" -m pip install --upgrade pip
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
    copy /y "requirements.txt" "%VENV%\requirements.installed" >nul
)

rem --- 3) Launch the app (data download / DB build is done from the web UI) ---
echo [Run] Starting EU MDR search app...
"%VPY%" -m streamlit run app.py
goto :end

:nopython
echo.
echo [ERROR] Python 3.14 (64-bit) is not installed.
echo         Install it from https://www.python.org/downloads/ and run this file again.
goto :end

:fail
echo.
echo [ERROR] Setup failed. Check the messages above (network / proxy / disk space).
echo         Delete the ".venv" folder and run this file again to retry from scratch.

:end
pause
