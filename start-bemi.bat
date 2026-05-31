@echo off
:: BEMI Pipeline Launcher
:: Double-click this file to start the server and open the browser.
:: Close this window (or press Ctrl+C) to stop the server.

set "ROOT=%~dp0"
cd /d "%ROOT%pipeline-api"

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

:: Resolve the venv Python explicitly. The server spawns the pipeline with its
:: own interpreter, so uvicorn MUST run from the venv that has Playwright and
:: Chromium. Check the repo root first (.venv), then pipeline-api\.venv.
set "PYEXE="
if exist "%ROOT%.venv\Scripts\python.exe" set "PYEXE=%ROOT%.venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOT%pipeline-api\.venv\Scripts\python.exe" set "PYEXE=%ROOT%pipeline-api\.venv\Scripts\python.exe"

if not defined PYEXE (
    echo ERROR: No virtual environment found.
    echo Expected .venv at:
    echo   %ROOT%.venv
    echo   or %ROOT%pipeline-api\.venv
    echo.
    echo Create one from the repo root, then install dependencies:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r pipeline-api\requirements.txt
    echo   .venv\Scripts\python.exe -m playwright install chromium
    pause
    exit /b 1
)

echo Using Python: %PYEXE%

:: Ensure the Playwright headless browser is installed (needed for "Re-crawl
:: with Browser"). Idempotent: a quick no-op when Chromium is already present.
"%PYEXE%" -c "import playwright" 2>nul
if %errorlevel% equ 0 (
    echo Ensuring headless browser is installed...
    "%PYEXE%" -m playwright install chromium
) else (
    echo NOTE: Playwright is not installed in this environment.
    echo       Re-crawl with Browser will produce no data until you run:
    echo         "%PYEXE%" -m pip install -r requirements.txt
    echo         "%PYEXE%" -m playwright install chromium
)

:: Open browser after a 3-second delay so the server has time to start
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul && start http://localhost:8000/login"

"%PYEXE%" -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
