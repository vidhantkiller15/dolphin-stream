@echo off
title Dolphinestreme TV Screen DEV
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating venv...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -c "import PIL" 2>nul
if errorlevel 1 (
    echo Installing deps...
    python -m pip install -q --upgrade pip
    python -m pip install -q -r requirements.txt
)

echo Starting TV app...
echo Pair with PIN shown on the PC console.
python -m dolphinestreme.tv_app
if errorlevel 1 pause
