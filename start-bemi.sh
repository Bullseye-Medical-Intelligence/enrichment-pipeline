#!/bin/bash
# BEMI Pipeline Launcher
# Run: ./start-bemi.sh
# Press Ctrl+C to stop the server.

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/pipeline-api"

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

# Resolve the venv Python explicitly. The server spawns the pipeline with its
# own interpreter, so uvicorn MUST run from the venv that has Playwright and
# Chromium. Check the repo root first (.venv), then pipeline-api/.venv.
if [ -x "$ROOT/.venv/bin/python" ]; then
    PYEXE="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/pipeline-api/.venv/bin/python" ]; then
    PYEXE="$ROOT/pipeline-api/.venv/bin/python"
else
    echo "ERROR: No virtual environment found."
    echo "Expected .venv at:"
    echo "  $ROOT/.venv"
    echo "  or $ROOT/pipeline-api/.venv"
    echo
    echo "Create one from the repo root, then install dependencies:"
    echo "  python -m venv .venv"
    echo "  .venv/bin/python -m pip install -r pipeline-api/requirements.txt"
    echo "  .venv/bin/python -m playwright install chromium"
    exit 1
fi

echo "Using Python: $PYEXE"

# Ensure the Playwright headless browser is installed (needed for "Re-crawl
# with Browser"). Idempotent: a quick no-op when Chromium is already present.
if "$PYEXE" -c "import playwright" 2>/dev/null; then
    echo "Ensuring headless browser is installed..."
    "$PYEXE" -m playwright install chromium
else
    echo "NOTE: Playwright is not installed in this environment."
    echo "      Re-crawl with Browser will produce no data until you run:"
    echo "        \"$PYEXE\" -m pip install -r requirements.txt"
    echo "        \"$PYEXE\" -m playwright install chromium"
fi

# Open browser after 3s delay (server needs time to start)
(sleep 3 && (open "http://localhost:8000/login" 2>/dev/null || xdg-open "http://localhost:8000/login" 2>/dev/null || true)) &

"$PYEXE" -m uvicorn main:app --host 0.0.0.0 --port 8000
