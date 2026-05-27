# CLAUDE.md — BEMI Pipeline API

Every session working in this repo begins by reading this file.
If this file and the code conflict, fix the code — not this file.

---

## What This Repo Is

A thin FastAPI service that acts as a process manager and file bridge between:
- The BEMI dashboard (React frontend, calls this API over HTTP)
- The BEMI enrichment pipeline (Python CLI, spawned as a subprocess)

This API does **not** contain enrichment logic, scoring, signal extraction,
LLM calls, web scraping, schema transforms, or any frontend code.

---

## Three-Repo Architecture

```
┌────────────────────┐    HTTP     ┌────────────────────┐
│   BEMI-dashboard   │ ──────────► │  BEMI-pipeline-api │
│  (React frontend)  │ ◄────────── │   (this repo)      │
└────────────────────┘             └────────┬───────────┘
                                            │  subprocess.Popen
                                            │  shared filesystem
                                            ▼
                                   ┌────────────────────┐
                                   │ BEMI-enrichment-   │
                                   │    pipeline        │
                                   │  (Python CLI)      │
                                   └────────────────────┘

Communication:
  dashboard  ↔  API:       HTTP (JSON)
  API        ↔  pipeline:  subprocess + shared /output/runs/ directory
  Shared files:            enriched_targets.json, run_log.json, status.json
```

---

## Absolute Rules

### RULE 1: This API wraps the pipeline. It does not become the pipeline.

The API spawns pipeline.py as a subprocess. It does not contain enrichment
logic, scoring formulas, signal definitions, or LLM prompt strings.

Bad:
```python
if bullseye_score > 75:
    tier = "Bullseye"
```

Good:
```python
subprocess.Popen(["python", "pipeline.py", "--input", path, ...])
```

### RULE 2: Output schema is the contract. Never redefine it here.

`enriched_targets.json` and `run_log.json` are defined in the pipeline
repo's PIPELINE.md. This API reads and serves them. It does not transform,
reformat, or reinterpret them.

### RULE 3: No duplicate logic. Ever.

If the pipeline already does something, the API does not do it again.
No re-scoring. No re-parsing. No re-deduplication. No field remapping.

### RULE 4: status.json is the source of truth for run state.

Every run has exactly one status.json. The API reads and writes it.
No in-memory state. No database. No global variables that survive a restart.

### RULE 5: No unauthenticated endpoints. Ever.

Every route requires a valid API key from the first commit.
The API can trigger LLM spend. It will never be unprotected.

### RULE 6: One function, one responsibility.

No function does more than one thing. If you find yourself writing "and"
in a function docstring, the function needs to be split.

### RULE 7: Fail loudly, recover cleanly.

Never swallow exceptions silently. Every error gets logged with context.
Every failed run gets a status.json update. Operators must always be able
to open a run directory and understand what happened.

---

## Locked Tech Stack

| Layer       | Decision         | Reason                            |
|-------------|------------------|-----------------------------------|
| Language    | Python 3.11+     | Matches pipeline                  |
| Framework   | FastAPI          | Async, clean, built-in validation |
| Server      | Uvicorn          | Standard FastAPI server           |
| Auth        | API key (Bearer) | Minimal, sufficient, no overhead  |
| State store | Filesystem JSON  | Simple, debuggable, no DB needed  |
| Process mgmt| subprocess.Popen | Isolates pipeline cleanly         |
| Env vars    | python-dotenv    | Keys out of source code           |
| Validation  | Pydantic/FastAPI | Already included, use it fully    |

**Banned for MVP** (do not introduce under any circumstance):
- Celery, RQ, or any task queue
- SQLite, PostgreSQL, or any database
- Redis or any cache layer
- Django or Flask
- LangChain or any LLM orchestration
- Any library that transmits data externally
- WebSockets (Phase 2)
- Docker (Phase 2)
- Any frontend framework or templating engine

---

## File Structure

```
/BEMI-pipeline-api
  main.py       ← FastAPI app, route registration, startup
  auth.py       ← API key validation dependency
  runner.py     ← subprocess management, pipeline invocation
  runs.py       ← run state: create/read/update/list via status.json
  validator.py  ← pre-flight CSV and config validation
  schema.py     ← Pydantic models for all request/response types
  config.py     ← environment variable loading, path constants
  requirements.txt
  .env.example
  .gitignore
  README.md
  CLAUDE.md     ← this file

/output/runs/   ← shared with pipeline (lives outside this repo)
  {run_id}/
    input.csv
    status.json
    run_log.json
    enriched_targets.json
```

---

## UI Architecture Decision (Permanent)

The server-rendered HTML UI (ui.py + templates/) is the production internal operator tool.
The React/Vite BEMI Dashboard is a demo reference only and is NOT integrated with this API.
Do not build React integration unless that decision is explicitly reversed.

---

## enriched_targets.json Schema

The pipeline writes this wrapper object (not a raw array):
```json
{
  "run_id": "RUN-20260527-143000",
  "generated_at": "2026-05-27T14:30:00Z",
  "record_count": 47,
  "records": [...]
}
```

When reading enriched_targets.json, always extract the records array:
```python
data = json.load(f)
records = data.get("records", data) if isinstance(data, dict) else data
```
Never iterate `data` directly — it will iterate dict keys, not records.

---

## Locked API Surface

Bearer-auth JSON API (main.py):
```
POST   /runs                            Upload CSV, start pipeline, return run_id
GET    /runs                            List all runs (newest first, max 50)
GET    /runs/{run_id}                   Full status.json for a run
GET    /runs/{run_id}/log               run_log.json (run must have exited)
GET    /runs/{run_id}/results           enriched_targets.json wrapper (run must be complete)
GET    /runs/{run_id}/export/approved   CSV of approved non-excluded records
GET    /runs/{run_id}/export/excluded   CSV of excluded records
```

Session-auth HTML UI (ui.py):
```
GET    /login                                    Login form
POST   /login                                    Validate credentials, set cookie
GET    /logout                                   Clear session
GET    /                                         Main menu
GET    /dashboard                                Run list
GET    /dashboard/{run_id}                       Results + inline review
GET    /runs/{run_id}/download/json              Full enriched_targets.json download
GET    /runs/{run_id}/download/csv               Full enriched_targets.csv download
GET    /runs/{run_id}/export/approved            Filtered CSV: approved, non-excluded
GET    /runs/{run_id}/export/excluded            Filtered CSV: excluded records
POST   /api/ui/runs                              Create run from browser upload
POST   /api/ui/reviews/{run_id}/{record_id}      Save review edit
```

Phase 2 additions (do not build now):
- `POST /runs/{run_id}/cancel`
- WebSocket progress streaming

---

## Locked status.json Schema

```json
{
  "run_id": "RUN-20260527-143000",
  "project_id": "P-001",
  "source_type": "outscraper",
  "input_filename": "femasys-florida-2026-05-27.csv",
  "status": "pending|running|complete|failed",
  "created_at": "2026-05-27T14:30:00Z",
  "completed_at": "2026-05-27T14:52:00Z",
  "operator": "Rajiv",
  "output_path": "/output/runs/RUN-20260527-143000/enriched_targets.json",
  "records_input": 50,
  "records_output": 47,
  "bullseye_count": 12,
  "watchlist_count": 28,
  "excluded_count": 7,
  "error_count": 3,
  "pipeline_version": "v1.0",
  "error_summary": ""
}
```

Status transitions: `pending` → `running` → `complete` or `failed`

---

## Clean Code Standards

- **Functions**: snake_case, verb-first (`get_run_status`, `create_run_dir`)
- **Classes**: PascalCase (`RunStatus`, `ValidationFailure`)
- **Constants**: SCREAMING_SNAKE_CASE (`MAX_CSV_ROWS`)
- **No utility files**: No `utils.py`, `helpers.py`, or `common.py`
- **Docstrings**: Every function gets a one-line docstring minimum
- **No magic numbers or strings**: All constants in `config.py` or module top
- **Pydantic for all I/O**: Every request/response through a model in `schema.py`
- **No wildcard imports**: `from x import *` is never acceptable
- **No commented-out code**: Delete dead code; use git for history
- **No TODOs in merged code**: Finish it or open an issue

---

## UI Layer Rules

The web UI is server-rendered HTML served by FastAPI. These rules are permanent:

- **ui.py only**: All HTML routes live in `ui.py`. API routes in `main.py` are not touched.
- **No React, npm, or build step**: Jinja2 + plain CSS + minimal vanilla JS only.
- **Pipeline output is immutable**: `reviews.py` reads `enriched_targets.json` but never writes to it.
- **Reviews are additive**: Analyst edits go to `reviews.json` only (atomic writes via tempfile + os.replace).
- **No business logic in templates or app.js**: Tier display logic in Jinja2 (`override_tier or target_tier`). JS only handles UI interactions and fetch() calls.
- **No hardcoded client, product, or specialty rules**: The UI is generic. No OBGYN, Femasys, fertility, etc.
- **No client portal, billing, or multi-tenant logic**: Internal team tool only.
- **When a feature is removed**: Delete its routes, templates, CSS, JS, imports, and tests. No dead code.
- **Override requires reason**: `override_tier` set → `override_reason` required. Enforced in `reviews.py`.
- **Server is source of truth**: `reviewed_at` always set server-side. JS never sets timestamps.
- **Final displayed tier**: `override_tier` if set, else pipeline `target_tier`. Always show both.

---

## BEMI Design System (Permanent)

All UI in this repo must match the BEMI Dashboard identity. These rules are permanent.

### Palette
| Token | Value | Usage |
|-------|-------|-------|
| `--ink` | `#0a0a0a` | Body text, dark backgrounds, near-black buttons |
| `--surface` | `#f7f6f4` | Page background, form inputs, light panels |
| `--accent` | `#c84b2f` | Terracotta — CTAs, eyebrow labels, active borders, override indicators |
| `--muted-dark` | `rgba(247,246,244,0.45)` | Labels on dark (ink) backgrounds |

### Typography
- **Display / numerals**: `Instrument Serif` (Google Fonts) — page headings, stats, login mark
- **UI text**: `DM Sans` (Google Fonts) — all body copy, labels, buttons
- **Eyebrow labels**: 10–11px, `font-weight: 600`, `letter-spacing: 0.12em`, `text-transform: uppercase`, color `--accent`

### Components
- **Navbar**: ink background, `Instrument Serif` "BEMI" wordmark in surface color
- **Stat blocks**: ink background, `Instrument Serif` 28px numerals in surface color, labels in `--muted-dark`
- **Badges**: `border-radius: 100px`, 10px uppercase text — tier/status/QC
- **Primary button**: ink background, surface text, `border-radius: 100px`
- **Submit / CTA button**: accent background (`#c84b2f`), white text, `border-radius: 100px`
- **Secondary / ghost button**: transparent, ink border, ink text
- **Filter pills**: same pill shape; active state uses ink fill
- **Signal rows**: left border — green = yes, red = no, slate = not_found
- **Detail panel border-top**: 2px solid accent
- **Section headers in detail panel**: eyebrow style (uppercase, accent color)

### Copy rules
- No em dashes (use commas or short sentences instead)
- No filler openers ("Great", "Sure", "Absolutely")
- Eyebrow labels always ALL CAPS
- Buttons are imperative: "Sign In", "Upload and Start Enrichment", "Save"

---

## What Future Sessions Must Never Add to This Repo

- Enrichment logic, scoring formulas, or signal definitions
- LLM API calls (Anthropic, OpenAI, or any other provider)
- Web scraping or HTTP calls to external sites (except passing paths to pipeline)
- A database or any persistent state beyond filesystem JSON
- A task queue (Celery, RQ, etc.)
- A second auth system — Bearer token (API) and session cookie (UI) are the two auth models, nothing more
- Direct imports from the pipeline repo (subprocess only, no shared code)
- Re-implementation of any logic that exists in the pipeline repo
- Hardcoded client, product, specialty, or campaign names

---

## Phase 2 Backlog (Do Not Build Now)

- `POST /runs/{run_id}/cancel` — interrupt a running pipeline process
- WebSocket run progress streaming
- Database-backed run history
- Docker containerization
- Multi-operator support
- Cloud file storage
- Run retry on partial failure
- CI/CD pipeline
