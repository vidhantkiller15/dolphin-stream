@echo off
title Dolphinestreme Controller DEV
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python not found.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -c "import PIL" 2>nul
if errorlevel 1 (
    python -m pip install -q -r requirements.txt
)

echo Starting controller...
python -m dolphinestreme.client_app
if errorlevel 1 pause
