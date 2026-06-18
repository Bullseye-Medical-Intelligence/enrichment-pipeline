# BEMI Pipeline API

## 1. What This Service Does

The BEMI Pipeline API is a thin process manager that sits between the BEMI dashboard and the enrichment pipeline. It receives CSV uploads from the dashboard, spawns the enrichment pipeline as a background subprocess, and serves the pipeline's output files back to the dashboard over HTTP. It does nothing else.

## 2. What It Does NOT Do

- Run any enrichment, scoring, or signal extraction logic
- Make LLM or AI API calls **outside the ICP builder** (the only LLM calls are
  the ICP builder's signal/hypothesis/crawl-compression helpers; all other routes
  are LLM-free)
- Scrape websites
- Store data in a database
- Transform, reformat, or reinterpret pipeline output
- Duplicate any logic that exists in the pipeline repo

It **does** serve the operator UI: `ui.py` renders the server-side HTML pages
(run dashboard, review, discovery, ICP builder, etc.) behind session auth.

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
# Install BOTH sets: the API's own deps AND the enrichment pipeline's deps.
pip install -r requirements.txt
pip install -r ../requirements.txt

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

The authoritative, complete route list is in `CLAUDE.md` ("Locked API Surface").
The table below is a small illustrative subset, not the full surface.

Session-auth HTML UI (`ui.py`):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/projects` | List configured projects |
| GET | `/projects/new` | Create-project form |
| POST | `/projects` | Create a project |
| GET | `/projects/{project_id}` | Project detail |
| GET | `/icp-profiles` | List loaded ICP profiles |
| GET | `/runs/{run_id}/export/approved` | Approved targets CSV |
| GET | `/runs/{run_id}/export/excluded` | Excluded targets CSV |
| GET | `/runs/{run_id}/client-package` | Client deliverable ZIP (complete runs) |

## 6. Run Directory Structure

Each run creates a directory at `{OUTPUT_RUNS_PATH}/{run_id}/` containing:

```
RUN-20260527-143000/
  input.csv                      ← uploaded CSV, saved before pipeline starts
  project_config_snapshot.json   ← frozen copy of the project config (--config)
  icp_snapshot.json              ← frozen copy of the ICP profile (--icp)
  status.json                    ← run state (written and updated by this API)
  run_log.json                   ← pipeline metadata and counts (written by pipeline)
  enriched_targets.json          ← enriched prospect records (written by pipeline)
  enriched_targets.csv           ← flat CSV version (written by pipeline)
```

The two snapshots are frozen at run start, so editing a project later never
changes what a past run was enriched against.

## 7. How to Inspect a Failed Run

1. Find the run ID from `GET /runs` — look for runs with `"status": "failed"`.
2. Call `GET /runs/{run_id}` to see the `error_summary` field.
3. Navigate to `{OUTPUT_RUNS_PATH}/{run_id}/` on the server filesystem.
4. Open `status.json` — the `error_summary` field contains the first 2,000 characters of stderr.
5. If `run_log.json` exists, open it — the `errors` array shows per-record failures.
6. If neither file has a clear error, check the server logs for the full traceback.

## 8. Environment Variables

Authentication is **session-cookie only** (`UI_USERNAME`, `UI_PASSWORD`,
`SESSION_SECRET_KEY`). There is no Bearer-token / API-key auth. `PIPELINE_API_KEY`
exists in config but is **not** used for authentication.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `UI_USERNAME` | Yes | — | Operator login username (session-cookie auth) |
| `UI_PASSWORD` | Yes | — | Operator login password (constant-time compared) |
| `SESSION_SECRET_KEY` | Yes | — | Signing key for the session cookie |
| `SESSION_COOKIE_SECURE` | No | `0` | Mark the session cookie Secure (HTTPS-only). Set `1` on HTTPS deployments |
| `PIPELINE_REPO_PATH` | Yes | — | Absolute path to the enrichment pipeline repo |
| `OUTPUT_RUNS_PATH` | Yes | — | Absolute path where run directories are written |
| `PROJECTS_PATH` | No | `{output}/projects` | Where project_config.json files are stored |
| `ICP_PROFILES_PATH` | No | `{output}/icp_profiles` | Where ICP profile JSON files are stored |
| `PYTHON_EXECUTABLE` | No | `python3` | Python interpreter used to launch pipeline.py |
| `MAX_CSV_SIZE_MB` | No | `50` | Maximum upload size in megabytes |
| `MAX_CSV_ROWS` | No | `10000` | Maximum number of rows per CSV |
| `MAX_CONCURRENT_RUNS` | No | `3` | Maximum simultaneous pipeline runs |
| `HOST` | No | `0.0.0.0` | Server bind address |
| `PORT` | No | `8000` | Server port |

`PROJECTS_PATH` and `ICP_PROFILES_PATH` default to siblings of `OUTPUT_RUNS_PATH`
(`{output}/projects` and `{output}/icp_profiles`).

## 9. Projects, ICP Profiles, and Running a Pilot

An enrichment run is always tied to a **project**. A project pins a client
config and an **ICP profile** (the signal checklist the pipeline scores
against). This keeps every run reproducible and stops operators from typing ad
hoc config paths.

### 9.1 ICP profile file format

ICP profiles are JSON files in `ICP_PROFILES_PATH`, one file per profile, named
`{icp_id}.json`. They can be hand-authored or created with the AI-assisted ICP
builder at `/icp-profiles/new`, which calls Claude to draft a signal checklist
from a product brief (a starting point — a domain expert must review and approve
the generated signals before saving). Each file must contain `icp_id`, `name`,
`version`, and a non-empty `signals` array:

```json
{
  "icp_id": "obgyn-independent-v1",
  "name": "OBGYN Independent Practice",
  "version": "icp-v1",
  "signals": [
    {
      "signal_id": "S-ICP-001",
      "signal_label": "IUD insertion listed",
      "prompt_instruction": "Does the practice list IUD insertion as a service?",
      "positive_weight": 15
    }
  ]
}
```

`signals` is what the pipeline enriches against. Positive weights raise fit;
negative weights lower it. Confirm a profile is loaded at `GET /icp-profiles`.

A copy-paste starting template lives at `pipeline-api/examples/icp_profile.example.json`.
To load it: copy it into `ICP_PROFILES_PATH`, rename the file to `{icp_id}.json`,
set a matching `icp_id`, and replace the signals for your client/specialty. Example
files are never loaded automatically.

### 9.2 Create a project

1. Sign in and go to **Projects → New Project**.
2. Enter a `project_id` (letters, digits, `-`, `_`; no spaces — it becomes a
   folder name), client name, target specialty, and target geography.
3. Select a loaded ICP profile from the dropdown.
4. Save. This writes `{PROJECTS_PATH}/{project_id}/project_config.json` with
   generic scoring defaults (`bullseye_min_score`, structural exclusion rules).

### 9.3 Start from an Outscraper CSV

1. Export your lead list from Outscraper as CSV. It must include the columns
   `name`, `full_address`, `phone`, `site`, `type` (max 10,000 rows, 50 MB).
2. Go to **New Run**, select the project, choose **Outscraper export**, attach
   the CSV, and start. The API validates the CSV, snapshots the project config
   and ICP into the run folder, and launches the pipeline with
   `--config {run}/project_config_snapshot.json --icp {run}/icp_snapshot.json`.

### 9.4 Review records (junior-operator safe)

On the results page each record expands to show its signals, evidence, scores,
and sales angles. Reviewers set QC status (approve / reject / reset) and may
override the tier — an override requires a reason. Guidance shown on the page:

- Review the evidence before approving.
- Overrides require a reason.
- Excluded records are exported separately.
- Hard-excluded records appear only in the excluded CSV by default. An explicit
  `override_tier` on an EXCLUDED record + `qc_status=approved` bypasses the pipeline's
  hard exclusion and includes the record in the approved export. Without an explicit
  `override_tier`, the hard exclusion stands regardless of QC status.

Reviews are saved to `reviews.json` (additive metadata); `enriched_targets.json`
is never modified.

### 9.5 Export the client deliverable

From a completed run, **Download Client Package** produces a ZIP built from the
immutable enriched output plus the review overlay. It contains exactly 5 files:

- `Bullseye_Target_Report.html` — self-contained Bullseye target report (HTML)
- `Sales_Handoff.html` — client-facing handoff covering all 5 tiers (Bullseye, Contender, Needs Verification, Manual Review, Excluded; NV/MR omitted only if an analyst rejects them)
- `bullseye_accounts.csv` — Bullseye-tier accounts
- `contender_accounts.csv` — Contender-tier accounts
- `excluded_targets.csv` — records whose effective tier is Excluded

Client-facing CSVs and reports show tier + confidence band only — numeric scores
are stripped. The package never includes `run_log.json`, `reviews.json`, analyst
notes, or the raw `enriched_targets.json`. The individual **Export Approved** /
**Export Excluded** CSV buttons remain available.

### 9.6 Run a pilot

1. Create an ICP profile for the specialty and drop it in `ICP_PROFILES_PATH`.
2. Create a project pointing at that profile.
3. Start a run with a small Outscraper CSV (10–25 rows) to validate signal
   coverage and timing before processing a full batch.
4. QC the results, then download the client package for the handoff.

### 9.7 Still out of scope

- No database, task queue, or Redis — state stays on the filesystem.
- No client portal, CRM sync, billing, or role-based permissions — internal tool.
- The pipeline itself is unchanged; this layer only selects and snapshots its
  inputs and packages its outputs.

## 10. What Phase 2 Will Add

- `POST /runs/{run_id}/cancel` — interrupt a running pipeline process
- WebSocket endpoint for real-time run progress streaming
- Database-backed run history (replaces filesystem JSON)
- Docker containerization for consistent deployments
- Multi-operator support with per-operator run filtering
- Cloud file storage for input CSVs and output files
- Run retry on partial failure
- CI/CD pipeline
