#!/bin/bash
# BEMI Pipeline Launcher
# Run: ./start-bemi.sh
# Press Ctrl+C to stop the server.

cd "$(dirname "$0")/pipeline-api"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Open browser after 2s delay (server needs time to start)
(sleep 2 && (open "http://localhost:8000/login" 2>/dev/null || xdg-open "http://localhost:8000/login" 2>/dev/null || true)) &

python -m uvicorn main:app --host 0.0.0.0 --port 8000
