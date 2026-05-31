#!/bin/bash
# BEMI Pipeline Launcher
# Run: ./start-bemi.sh
# Press Ctrl+C to stop the server.

cd "$(dirname "$0")/pipeline-api"

if [ ! -f ".env" ]; then
    echo "ERROR: .env not found in pipeline-api/."
    echo "Copy .env.example to .env and fill in your credentials, then try again."
    exit 1
fi

if lsof -i :8000 -sTCP:LISTEN -t >/dev/null 2>&1 || ss -tlnp 2>/dev/null | grep -q ':8000 '; then
    echo "WARNING: Port 8000 is already in use."
    echo "Open http://localhost:8000/login in your browser, or stop the existing process first."
    exit 1
fi

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Ensure the Playwright headless browser is installed (needed for "Re-crawl
# with Browser"). Idempotent: a quick no-op when Chromium is already present.
if python -c "import playwright" 2>/dev/null; then
    echo "Ensuring headless browser is installed..."
    python -m playwright install chromium
else
    echo "NOTE: Playwright is not installed in this environment."
    echo "      Re-crawl with Browser will fall back to basic mode."
    echo "      To enable it, run:"
    echo "        python -m pip install -r requirements.txt"
    echo "        python -m playwright install chromium"
fi

# Open browser after 3s delay (server needs time to start)
(sleep 3 && (open "http://localhost:8000/login" 2>/dev/null || xdg-open "http://localhost:8000/login" 2>/dev/null || true)) &

python -m uvicorn main:app --host 0.0.0.0 --port 8000
