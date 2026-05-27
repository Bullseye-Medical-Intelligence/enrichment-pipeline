# BEMI Pipeline API

## 1. What This Service Does

The BEMI Pipeline API is a thin process manager that sits between the BEMI dashboard and the enrichment pipeline. It receives CSV uploads from the dashboard, spawns the enrichment pipeline as a background subprocess, and serves the pipeline's output files back to the dashboard over HTTP. It does nothing else.

## 2. What It Does NOT Do

- Run any enrichment, scoring, or signal extraction logic
- Make LLM or AI API calls
- Scrape websites
- Store data in a database
- Render any UI or serve HTML pages
- Transform, reformat, or reinterpret pipeline output
- Duplicate any logic that exists in the pipeline repo

## 3. Setup

**Prerequisites:** Python 3.11+, the BEMI enrichment pipeline repo cloned locally.

```bash
# 1. Clone this repo (or navigate to the pipeline-api/ directory)
cd BEMI-pipeline-api

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
.venv\Scripts\activate             # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp .env.example .env
# Open .env in a text editor and set all required values

# 5. Create the output runs directory (if it doesn't exist)
mkdir -p /path/to/output/runs
```

## 4. Running the API

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

For development with auto-reload:

```bash
python -m uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`. Interactive documentation is at `http://localhost:8000/docs`.

## 5. API Endpoints

| Method | Path | Description | Auth Required |
|--------|------|-------------|---------------|
| POST | `/runs` | Upload CSV and start an enrichment run | Yes |
| GET | `/runs` | List all runs, newest first (max 50) | Yes |
| GET | `/runs/{run_id}` | Get full status for a single run | Yes |
| GET | `/runs/{run_id}/log` | Get the run log (run must have exited) | Yes |
| GET | `/runs/{run_id}/results` | Get enriched targets (run must be complete) | Yes |

## 6. Authentication

Every request must include a Bearer token in the `Authorization` header:

```
Authorization: Bearer your-secret-api-key-here
```

The token must exactly match the `PIPELINE_API_KEY` value in your `.env` file.

To generate a secure key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Requests without a valid token receive a `401 Unauthorized` response.

## 7. Run Directory Structure

Each run creates a directory at `{OUTPUT_RUNS_PATH}/{run_id}/` containing:

```
RUN-20260527-143000/
  input.csv                ← uploaded CSV, saved before pipeline starts
  status.json              ← run state (written and updated by this API)
  run_log.json             ← pipeline metadata and counts (written by pipeline)
  enriched_targets.json    ← enriched prospect records (written by pipeline)
  enriched_targets.csv     ← flat CSV version (written by pipeline)
```

## 8. How to Inspect a Failed Run

1. Find the run ID from `GET /runs` — look for runs with `"status": "failed"`.
2. Call `GET /runs/{run_id}` to see the `error_summary` field.
3. Navigate to `{OUTPUT_RUNS_PATH}/{run_id}/` on the server filesystem.
4. Open `status.json` — the `error_summary` field contains the first 2,000 characters of stderr.
5. If `run_log.json` exists, open it — the `errors` array shows per-record failures.
6. If neither file has a clear error, check the server logs for the full traceback.

## 9. Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PIPELINE_API_KEY` | Yes | — | Bearer token for all API requests |
| `PIPELINE_REPO_PATH` | Yes | — | Absolute path to the enrichment pipeline repo |
| `OUTPUT_RUNS_PATH` | Yes | — | Absolute path where run directories are written |
| `PYTHON_EXECUTABLE` | No | `python3` | Python interpreter used to launch pipeline.py |
| `MAX_CSV_SIZE_MB` | No | `50` | Maximum upload size in megabytes |
| `MAX_CSV_ROWS` | No | `10000` | Maximum number of rows per CSV |
| `HOST` | No | `0.0.0.0` | Server bind address |
| `PORT` | No | `8000` | Server port |

## 10. What Phase 2 Will Add

- `POST /runs/{run_id}/cancel` — interrupt a running pipeline process
- WebSocket endpoint for real-time run progress streaming
- Database-backed run history (replaces filesystem JSON)
- Docker containerization for consistent deployments
- Multi-operator support with per-operator run filtering
- Cloud file storage for input CSVs and output files
- Run retry on partial failure
- CI/CD pipeline
