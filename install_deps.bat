@echo off
title Dolphinestreme install
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.10+ from python.org and check Add to PATH
    pause
    exit /b 1
)

python --version
if not exist ".venv\Scripts\python.exe" python -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist "games" mkdir games
echo.
echo OK. Run:  start_pc.bat  /  start_tv.bat  /  start_client.bat
pause
