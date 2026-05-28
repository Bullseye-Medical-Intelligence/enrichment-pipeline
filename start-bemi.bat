@echo off
:: BEMI Pipeline Launcher
:: Double-click this file to start the server and open the browser.
:: Close this window (or press Ctrl+C) to stop the server.

cd /d "%~dp0\pipeline-api"

if not exist ".env" (
    echo ERROR: .env file not found in pipeline-api\.
    echo Copy .env.example to .env and fill in your credentials, then try again.
    pause
    exit /b 1
)

netstat -an 2>nul | findstr ":8000 " | findstr "LISTENING" >nul
if %errorlevel% equ 0 (
    echo WARNING: Port 8000 is already in use.
    echo The server may already be running. Open http://localhost:8000/login in your browser,
    echo or stop the existing process first, then try again.
    pause
    exit /b 1
)

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

:: Open browser after a 3-second delay so the server has time to start
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul && start http://localhost:8000/login"

python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
