@echo off
:: BEMI Pipeline Launcher
:: Double-click this file to start the server and open the browser.
:: Close this window (or press Ctrl+C) to stop the server.

cd /d "%~dp0\pipeline-api"

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

:: Open browser after a 2-second delay so the server has time to start
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul && start http://localhost:8000/login"

python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
