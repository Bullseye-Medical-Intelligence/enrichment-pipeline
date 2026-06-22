# CLAUDE.md — BEMI Pipeline API

Every session working in this repo begins by reading this file.
If this file and the code conflict, fix the code — not this file.

---

## What This Repo Is

A thin FastAPI service that acts as the operator UI and a process manager / file
bridge for the BEMI enrichment pipeline:
- It serves operators through its own server-rendered HTML UI (`ui.py` + templates/) behind session auth.
- It spawns the BEMI enrichment pipeline (Python CLI) as a subprocess and serves its output files back.

This API does **not** contain enrichment logic, scoring, signal extraction,
LLM calls, web scraping, schema transforms, or any client-side framework code.

---

## Architecture (API + Pipeline)

```
┌────────────────────┐
│  BEMI-pipeline-api │  ← operator UI (server-rendered HTML) + run management
│   (this repo)      │
└────────┬───────────┘
         │  subprocess.Popen
         │  shared filesystem
         ▼
┌────────────────────┐
│ BEMI-enrichment-   │
│    pipeline        │
│  (Python CLI)      │
└────────────────────┘

Communication:
  operator   ↔  API:       HTTP (server-rendered HTML, session-cookie auth)
  API        ↔  pipeline:  subprocess + shared /output/runs/ directory
  Shared files:            enriched_targets.json, run_log.json, status.json
```

---

## Absolute Rules

### RULE 1: This API wraps the pipeline. It does not become the pipeline.

The API spawns pipeline.py as a subprocess. It does not contain enrichment
logic, scoring formulas, or signal definitions.

**Three exceptions — all strictly scoped to the ICP builder flow:**
1. **Signal generation** (`POST /icp-profiles/generate`, `POST /icp-profiles/regenerate-signals` in `ui.py`) calls Claude to draft signal definitions from a product brief.
2. **Hypothesis generation** — `narrative_generator.generate_hypothesis()` is called non-fatally after each signal generate/regenerate to pre-fill the commercial fit hypothesis fields. Uses the same `ANTHROPIC_API_KEY` from `.env`.
3. **Crawl compression** — `crawl_compressor.compress_crawl()` calls Claude during the builder's generate flow to condense sample crawl text before signal drafting (Stage 1 of generate). Builder-only.

All three are isolated to the builder; all other routes are LLM-free. If any fails (network error, quota), it logs a warning and degrades gracefully — no error shown to the operator.

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

Every route requires a valid session cookie (`auth.require_session`) from the
first commit — there is exactly one auth model (see the bottom-of-file rule
against adding Bearer tokens / API keys / OAuth). The API can trigger LLM spend.
It will never be unprotected.

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
| Auth        | Session cookie   | Single auth model; no Bearer/API-key (see RULE 5) |
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
  main.py            ← FastAPI app, route registration, startup
  auth.py            ← session-cookie validation dependency (require_session); constant-time UI password check
  reviews.py         ← analyst review overlay (reviews.json): read/write, override/QC, bulk approve, stamp re-enriched
  record_adapter.py  ← record field normalization, displayed/effective tier, confidence band, phone format
  narrative_generator.py ← ICP builder only: LLM hypothesis pre-fill (generate_hypothesis)
  signal_generator.py    ← ICP builder only: LLM signal-draft generation from a product brief
  crawl_compressor.py    ← ICP builder only: LLM compression of sample crawl text (Stage 1 of generate)
  discovery_runs.py  ← Market Radar discovery run management + JSON API router (/discovery-runs)
  registry_update.py ← explicit registry upsert with change_history; /enrichment-runs/.../update-registry
  practice_matching.py ← multi-key registry match (place_id > domain > phone > name+address)
  reports/pdf_report.py ← Bullseye cards + executive report HTML renderers (self-contained HTML)
  runner.py          ← subprocess management, pipeline invocation
  runs.py            ← run state: create/read/update/list via status.json
  projects.py        ← project config storage: create/read/update/list/validate
  icp_profiles.py    ← ICP profile listing/loading from disk
  exports.py         ← filtered CSV exports (approved / excluded)
  client_exports.py  ← client deliverable ZIP (5 files: Bullseye report, Sales Handoff, 3 CSVs)
  brief_publisher.py ← publish HTML briefs to Hostinger via SFTP/FTP; manages published_briefs.json per run
  sales_export.py    ← Sales Brief + internal Sales Handoff HTML generation
  validator.py       ← pre-flight CSV validation
  schema.py          ← Pydantic models for all request/response types
  config.py          ← environment variable loading, path constants; includes Hostinger SFTP/FTP settings; BUILD_VERSION/BUILD_DATE
  llm_pricing.py     ← the ONE home for LLM pricing constants (operator-maintained LAST_VERIFIED); cost-per-run estimate via estimate_run_cost() (averages past run token usage, falls back to defaults)
  preflight.py       ← system health checks: ANTHROPIC_API_KEY, pipeline repo, output dir writability, ICP profiles, projects, session key; returns CheckResult NamedTuples; overall status = worst individual check
  ui.py              ← server-rendered HTML routes (session auth)
  requirements.txt
  .env.example
  .gitignore
  README.md
  CLAUDE.md          ← this file

/output/                       ← shared with pipeline (lives outside this repo)
  projects/{project_id}/
    project_config.json        ← client config + ICP reference (== run config)
  icp_profiles/{icp_id}.json   ← signal checklist (operator-authored JSON)
  runs/{run_id}/
    input.csv
    project_config_snapshot.json   ← frozen --config for this run
    icp_snapshot.json              ← frozen --icp for this run
    status.json
    run_log.json
    enriched_targets.json
    published_briefs.json          ← per-run record of published brief URLs (storage_path, public_url, etc.)
```

---

## UI Architecture Decision (Permanent)

The server-rendered HTML UI (ui.py + templates/) is the production internal operator tool.
It is the only client of this API. Do not introduce a separate frontend application
(React/Vite or otherwise) — see "UI Layer Rules" below.

### Run Dashboard Header Layout
The results page header uses a two-tier layout:
- **Primary row**: Download Client Package | Sales Handoff ▾ (dropdown: View ↗, Copy Link, Re-Publish) | Sales Brief ▾ (dropdown: View ↗, Copy Link, Update Brief, Re-Publish)
- **Secondary row** (smaller, de-emphasized) — grouped into dropdowns to keep the header compact (all reuse the shared `.dropdown` / `toggleDropdown` pattern):
  - **Reprocess ▾**: Re-crawl Blocked Sites (N) (only when `limited_count > 0`), Preview Rescore, Apply Rescore, Re-extract Signals, Re-check Suppression (only when `has_suppression_list`), Re-run. POST actions inside the menu are `<form style="margin:0;">` wrapping a `<button type="submit">` so the `.dropdown-menu button` CSS renders them as full-width items; every confirm() dialog and disable-on-submit handler is preserved.
  - **Export ▾**: Full CSV, Run Manifest.
  - **Audit ▾**: Cartridge, Check Evidence Links.
  - Standalone **Update Registry** and **← All Runs** buttons.

### Bulk multi-select bottom bar (`#reenrich-bar`)
A fixed bar appears at the bottom of the viewport when records are checked on a complete run. Controls, in order: **Re-enrich (N)** (HTTP re-crawl, `use_playwright=''`), **Re-crawl with Browser (N)** (`use_playwright='1'`), **Review All ▾** (a `.dropdown` whose `.dropdown-menu` carries the `drop-up` modifier so it opens upward — the bar is pinned to the bottom — with Accept / Reject / Reset items), and **Clear**. Re-enrich and Re-crawl post the checked `record_ids` to `POST /dashboard/{run_id}/rerun-selected`. Each Review All item posts the SAME checked `record_ids` to `POST /dashboard/{run_id}/bulk-review` with `action=accept|reject|reset` (`prepareBulkReviewSubmit` copies the live selection into the form's hidden container at submit time, mirroring `prepareReenrichSubmit`). The Review All toggle is disabled with the other buttons when 0 records are selected. Bulk-review writes `reviews.json` only (QC status), never `enriched_targets.json`.

**Sales Handoff staleness indicator**: `_brief_stale(run_id, run_directory, brief_type)` in `ui.py` compares the `published_at` timestamp in `published_briefs.json` against the newest `reviewed_at` timestamp across all analyst reviews for that run. When any review is newer than the last publish, `results_page` passes `handoff_stale=True` to the template, which renders an amber dot (●) on the Sales Handoff button. This signals to the operator that re-publishing will incorporate the latest overrides. The dot disappears after republish.

### Stat Block Colors
Each tier stat block has a distinct solid background color: Bullseye=dark red (`#b91c1c`), Needs Verification=dark amber (`#b45309`), Contender=dark terracotta (`#9a3823`), Manual Review=slate (`#475569`), Excluded=near-black blue-gray (`#1e2530`), Pending Review=purple (`#5b21b6`). Excluded records are not counted in Pending Review (they require no QC sign-off unless reclassified).

**Evidence Vault snapshot viewer**: the pipeline archives each crawled page's
text under `<run_dir>/evidence/<record_id>/` (index.json with url, fetched_at,
sha256, provenance + page-NN.txt files). The dashboard's signal rows link to
`GET /dashboard/{run_id}/evidence/{record_id}` which renders the archived text
with the evidence quote highlighted and the capture metadata shown — proof of
what the crawler saw even after the live site changes. The API only reads these
files (`_load_evidence_entry`, `_records_with_evidence` in `ui.py`); it never
writes or re-derives them. Record ids are sanitized with the same charset rule
the pipeline writer uses, and only the basename in index.json is ever served.
Operator-facing only — snapshots are never included in client deliverables.

**Cartridge view**: `GET /dashboard/{run_id}/cartridge` renders the run's frozen
`project_config_snapshot.json` + `icp_snapshot.json` (via the existing
`projects.read_config_snapshot` / `icp_profiles.read_snapshot` readers — no
parallel loader): identity + snapshot dates, signals table, exclusion gates and
tier caps, `competitive_brands` ("Not configured for this ICP" when absent — a
valid state), and geography ("No geography restriction" when empty — also
valid). STRICTLY read-only: no edit controls, no file writes.

**ICP signal columns**: any ICP signal carrying an optional `column_label` is
surfaced as an at-a-glance column on the results table and the Contact Queue.
Column *definitions* come from the run's **live** ICP
(`icp_profiles.get_icp_profile(status.icp_profile_id)` via `_signal_columns`), so
existing runs gain columns immediately; each cell's *state* comes from that
record's frozen signals matched by `signal_id` (`record_adapter.signal_column_state`,
a Jinja global). Signals sharing a label roll up to the strongest state
(yes > inferred > no > not_found). Generic and RULE-3-compliant — no client/signal
names in code; other clients opt in by adding `column_label` to their seed profile.

**Evidence Link Checker**: manual, pre-delivery audit that evidence source URLs
in Bullseye/Contender (client-shipped tier) records still resolve. The API
collects signal `source_url`s and shells out to the pipeline's `check_links.py`
CLI via subprocess — this API never makes external HTTP calls itself. Results
persist to `link_check_report.json` in the run directory (audit trail; written
atomically). Report-only: no record is mutated based on results. Never runs
automatically on completion.

**Cost per run**: the pipeline captures Claude token usage into run_log.json
(`llm_input_tokens`, `llm_output_tokens`, `llm_call_count`); the monitor copies
the fields into status.json on completion. The results page shows an estimated
cost computed from `llm_pricing.py` (the single home for rates, with an
operator-maintained `LAST_VERIFIED` date shown as "estimate — rates as of").
Runs predating capture show "cost data not captured for this run" — never zero.
No per-record cost fields exist.

**Site Blocked — Needs Re-crawl section**: a dedicated table section (below the main scored table, above Excluded) for records where `source_confidence in ("limited", "failed")`. These records are removed from the main scored table entirely — they were never scored or scored on too little text to trust, so showing them alongside Bullseyes and Contenders was misleading. A "thin crawl" (a successful fetch returning under `THIN_CRAWL_CHARS` of text, set in `extraction/web_extractor.py`) is now labelled `"limited"` so JS-gated sites that returned only boilerplate land here for a browser re-crawl instead of being silently scored as having no signals. The section header shows a count badge and a "Retry All with Browser" button that fires `POST /runs/{run_id}/retry-with-browser`. `stats.blocked` tracks this count (replaces the former `stats.thin_context`). Blocked records are excluded from tier stats and from Pending Review — they need a re-crawl, not a QC sign-off.

**Browser re-crawl always stays in the same run (in place).** Every browser re-crawl — per-record, bulk-selected, and "Retry All with Browser" — merges results back into the source run; none forks a new run. `POST /runs/{run_id}/retry-with-browser` collects the run's `limited`/`failed` record IDs and calls `orchestrate_batch_reenrich(..., use_playwright=True)`, the same in-place batch path used by the bulk-selected button. (The legacy `orchestrate_playwright_retry`, which spawned a separate run, was removed.) The source run stays `complete` throughout; reload after a few minutes to see updated tiers/signals.

**Bulk browser re-crawl**: the "Re-enrich Selected" bulk bar (shown when a run is `complete`) carries a second button, "Re-crawl Selected with Browser", that posts the same selected `record_ids` to `POST /dashboard/{run_id}/rerun-selected` with `use_playwright=1`. `orchestrate_batch_reenrich(..., use_playwright=True)` then re-crawls the selection with headless Chromium (`--playwright`) and merges results in place.

**System Health banner** (`/dashboard` run list): shown when `preflight.run_checks()` returns status other than `"ok"`. Auto-expanded for errors, collapsed for warnings. Shows a table of individual check results (✓/⚠/✗). Hidden entirely when all checks pass. Health is re-evaluated on every page load.

**Pre-enrichment cost estimate**: on the run results page for ingested (not-yet-enriched) runs, the "Enrich All" button triggers a fetch to `GET /runs/{run_id}/enrich-estimate` before submitting. The estimate is computed by `llm_pricing.estimate_run_cost()` — averages token usage across up to 20 past completed runs; falls back to conservative defaults (6k input / 750 output tokens per record) when no history exists. Shown inline next to the button; clicking a second time confirms and submits.

**Run browser notification**: when the operator starts an enrichment run and the page auto-reloads every 5 seconds, a `sessionStorage` key tracks the watched run ID. When the status transitions to `complete` or `failed`, a toast banner and flashing tab title alert the operator — even if they've navigated away and returned. No server push or websocket required.

**Bulk approve in confirm queue**: the confirm queue header shows "Approve High-Confidence (N)" and "Approve All (N)" buttons when pending Bullseyes exist. Both POST to `/dashboard/{run_id}/confirm-queue/bulk-approve` with `confidence_filter=high|all`. The `high` filter only approves records where every confirmed `"yes"` signal is at `medium` or `high` confidence. Single atomic `reviews.json` write for the whole batch.

**Run comparison view** (`/dashboard/compare`): side-by-side tier comparison between any two completed runs. Records are matched by `(practice_name.lower(), address_city.lower())` key — not by `record_id` (which differs across runs). Shows tier changes (with ↑/↓/→ direction), unchanged records (collapsed), and records only in one run (collapsed). No writes.

**Post-run pass buttons** (complete runs, secondary header row): "Re-extract Signals" shells out to `reextract_run.py` — re-runs Claude signal extraction without re-crawling, using the run's frozen ICP snapshot. Page text is rehydrated from the Evidence Vault (`_context_text` is stripped from output at write time), so records with a vault snapshot are re-extractable. "Re-check Suppression" shells out to `suppress_run.py` — re-applies the project suppression list with no LLM cost; button only shown when the run's project config has `suppression_list_path` set and the file exists.

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

Session-auth HTML UI (ui.py):
```
GET    /login                                    Login form
POST   /login                                    Validate credentials, set cookie
GET    /logout                                   Clear session
GET    /                                         Main menu
GET    /projects                                 List projects
GET    /projects/new                             Create-project form
POST   /projects                                 Create a project
GET    /projects/{project_id}                    Project detail
GET    /icp-profiles                             List loaded ICP profiles
GET    /dashboard                                Run list (with system health banner)
GET    /dashboard/compare                        Side-by-side tier comparison between two completed runs
GET    /dashboard/{run_id}                       Results + inline review
GET    /dashboard/{run_id}/queue                 Contact Queue (rep call sheet, sorted by priority)
GET    /dashboard/{run_id}/confirm-queue         Analyst confirm queue (Bullseye + Contender pending review)
POST   /dashboard/{run_id}/confirm-queue/bulk-approve  Bulk-approve pending Bullseyes; body: confidence_filter=all|high
GET    /dashboard/{run_id}/evidence/{record_id}  Evidence Vault snapshot viewer (?url= picks the page, ?q= highlights the quote)
GET    /dashboard/{run_id}/cartridge             Read-only Cartridge view: the frozen config + ICP snapshot the run used
GET    /dashboard/{run_id}/link-check            Evidence link check report (flagged URLs only)
POST   /runs/{run_id}/check-links                Run the evidence link check (manual trigger; complete runs only)
GET    /runs/{run_id}/download/json              Full enriched_targets.json download
GET    /runs/{run_id}/download/csv               Full enriched_targets.csv download
GET    /runs/{run_id}/download/manifest          Internal run manifest JSON (not in client package)
GET    /runs/{run_id}/export/approved            Filtered CSV: approved, non-excluded
GET    /runs/{run_id}/export/excluded            Filtered CSV: excluded records
GET    /runs/{run_id}/client-package             Client deliverable ZIP (complete runs; requires all Bullseye/Contender reviewed)
GET    /runs/{run_id}/download/sales-brief       Prospect-facing methodology brief (select 1 Bullseye, 1 Contender, 1 Excluded via query params)
GET    /runs/{run_id}/enrich-estimate            JSON cost estimate for enriching an ingested run (reads token history from past runs)
POST   /runs/{run_id}/publish/{brief_type}       Publish brief HTML to Hostinger; saves URL to published_briefs.json
                                                   brief_type: sales-handoff | sales-brief | executive-report | bullseye-report
                                                   sales-brief requires: ?bullseye_id=&contender_id=&excluded_id=
                                                   Republish overwrites the existing file in place so the shared URL never changes
POST   /runs/{run_id}/records/{record_id}/recrawl          Re-crawl one record with headless browser; updates the record in place
POST   /runs/{run_id}/records/{record_id}/manual-content   Enrich one record from operator-pasted/uploaded page content; updates in place
POST   /api/ui/runs                              Create run from browser upload
POST   /api/ui/reviews/{run_id}/{record_id}      Save review edit
POST   /icp-profiles/simulate                     Dry-run score preview (no LLM, no crawl); shells out to simulate_icp.py
GET    /preflight                                 JSON system health check (API keys, pipeline repo, output dir, ICP profiles, projects, session key)

Market Radar (Discovery) — operator HTML routes (ui.py):
GET    /discovery                                 Discovery landing — upload form + recent runs list
POST   /discovery/upload                          Upload Outscraper CSV → create discovery run → redirect to results
GET    /discovery/runs/{run_id}                   Discovery results page — summary cards + classified record table + send actions
POST   /discovery/runs/{run_id}/send              Send selected / new / changed records → create ingested enrichment run

Market Radar (Discovery) — JSON API router (discovery_runs.py; session-cookie auth):
POST   /discovery-runs                            Create a discovery run (JSON)
GET    /discovery-runs/{run_id}                   Discovery run status/summary (JSON)
GET    /discovery-runs/{run_id}/results           Classified records (JSON)
POST   /discovery-runs/{run_id}/send-to-enrichment  Send selected records → create ingested enrichment run (JSON)

Registry Update routes (explicit operator action only):
GET    /dashboard/{run_id}/registry-update        Registry update form for a completed enrichment run
POST   /dashboard/{run_id}/registry-update        Execute registry update → show inserted/updated/rejected summary

Post-run pass routes (complete runs only; each shells out to a CLI at repo root):
POST   /dashboard/{run_id}/verify                 GPT verification pass on Needs Verification records (verify_run.py)
POST   /dashboard/{run_id}/rescore                Re-score with frozen ICP weights — Steps 6-7 only, no LLM (rescore_run.py)
POST   /dashboard/{run_id}/rescore-preview        Preview rescore tier transitions without writing (rescore_run.py --preview)
POST   /dashboard/{run_id}/reextract              Re-run Claude signal extraction; page text rehydrated from the Evidence Vault (reextract_run.py); LLM cost
POST   /dashboard/{run_id}/resuppress             Re-check all records against the project suppression list (suppress_run.py); no LLM
POST   /dashboard/{run_id}/recrawl                Re-crawl all blocked/thin records with Playwright (recrawl_run.py)
POST   /dashboard/{run_id}/bulk-review            Bulk-set QC status on selected records; body: record_ids[], action=accept|reject|reset. Writes reviews.json only

API (session-cookie auth, same as all routes; called by dashboard or automation):
POST   /enrichment-runs/{run_id}/update-registry  Explicit registry update; body: selection_mode, selected_record_ids, options
```

Phase 2 additions (do not build now):
- `POST /runs/{run_id}/cancel`
- WebSocket progress streaming

---

## Projects and ICP Profiles

Every run is tied to a project. A project owns a `project_config.json` (which
doubles as the pipeline's `--config`) and names an ICP profile (the pipeline's
`--icp`). Rules:

- **`projects.py` and `icp_profiles.py` own all project/ICP file logic.** `ui.py`
  only renders templates and calls these services. `runner.py` only orchestrates.
- **No ad hoc config paths.** Operators select a project; they never type a
  config or ICP path. `run_dir`-style guards reject traversal in `project_id`
  and `icp_id` before any filesystem access.
- **Runs snapshot their inputs.** `orchestrate_run` writes
  `project_config_snapshot.json` and `icp_snapshot.json` into the run folder and
  passes those frozen copies to the pipeline. Editing a project later never
  alters a past run.
- **Validate before spawning.** Reject the upload if the project is missing, the
  config lacks a required field (`config.REQUIRED_PROJECT_FIELDS`), or the ICP
  profile is missing/malformed/empty (`config.REQUIRED_ICP_FIELDS`).
- **No hardcoded specialty.** Project defaults are generic (structural exclusion
  rules, a default score threshold). ICP signal content lives in operator-authored
  profile files, never in source.
- **ICP profiles are operator-authored files** dropped into `ICP_PROFILES_PATH`.
  The one exception is the AI-assisted builder at `/icp-profiles/new`, which calls
  Claude to generate a draft checklist. Domain experts must review and approve
  generated signals before saving — the builder is a starting point, not a source
  of truth. `save_icp_profile()` in `icp_profiles.py` writes new profiles atomically.

**Score Simulator** (`/icp-profiles/simulate` POST, rendered in `icp_review.html`):
  A collapsible panel in the ICP review page lets operators set hypothetical signal
  states (Yes / Not Found / No) per signal and click "Run Simulation" to see the
  resulting tier and score instantly. The endpoint shells out to `simulate_icp.py`
  in the pipeline repo via subprocess — no scoring logic lives in the API. This
  lets operators validate weight choices before saving a profile or running a full
  enrichment. Read-only, stateless, never mutates any stored ICP or run data.

## Client Deliverable Export

`client_exports.py` builds the client package ZIP for a completed run.

- **Built from immutable output + review overlay.** Reads `enriched_targets.json`
  and the `reviews.json` overlay; never mutates either.
- **Reuses `exports.py`** for the approved/excluded CSVs — no duplicated filter
  logic. An analyst `override_tier` on a pipeline-excluded record bypasses the
  automatic exclusion when the analyst also approves it. Without an explicit
  override_tier, a hard `exclusion_status == "EXCLUDED"` record stays out of the
  approved set.
- **Client-safe only.** The ZIP contains exactly 5 files: `Bullseye_Target_Report.html`,
  `Sales_Handoff.html`, `bullseye_accounts.csv`, `contender_accounts.csv`, and
  `excluded_targets.csv`. It never includes `run_log.json`, `reviews.json`, analyst
  notes, numeric scores, or the raw `enriched_targets.json`. Client-facing CSVs and
  reports show tier + confidence band only — numeric scores are stripped
  (`exports._HIDDEN_SCORE_COLUMNS`).
- **Run manifest is internal-only.** `build_run_manifest` produces a provenance
  summary (scope, ICP version, counts, methodology). It is NOT in the client
  package; operators pull it via `GET /runs/{run_id}/download/manifest`.
- **Report generation.** `reports/pdf_report.py::build_bullseye_cards_html` renders
  `reports/templates/bullseye_cards.html` — a self-contained HTML file (embedded CSS,
  no external assets). `Sales_Handoff.html` is the client-facing handoff from
  `handoff_renderer` covering all five tiers (Bullseye, Contender, Needs
  Verification, Manual Review, Excluded) so the client sees the full screening
  picture; Needs Verification / Manual Review are omitted only when an analyst
  rejects them. No analyst notes. The client CSVs are unaffected — they still
  ship approved Bullseye/Contender plus all Excluded only.
- **Brief publishing.** `brief_publisher.py` uploads any brief HTML to Hostinger via
  SFTP (paramiko) with FTP fallback (ftplib). On first publish a tokenized URL is
  created; on republish the same file is overwritten in place so the shared URL never
  changes. URLs and storage paths are recorded in `published_briefs.json` inside the
  run directory, keyed by brief type. Requires `HOSTINGER_SFTP_HOST`, `HOSTINGER_SFTP_USER`,
  `HOSTINGER_SFTP_PASSWORD`, `HOSTINGER_BRIEFS_REMOTE_ROOT`, and `BRIEFS_PUBLIC_BASE_URL`
  in `.env`.

---

## Operational Safeguards

- **run_id is always validated** (`runs.is_valid_run_id` / `run_dir`) against
  `^RUN-\d{8}-\d{6}(-[a-f0-9]{4})?$` before building any filesystem path. This
  blocks path traversal. Invalid IDs read as 404, never 500.
- **Concurrent-run cap**: `MAX_CONCURRENT_RUNS` (config, default 3). `orchestrate_run`
  rejects new runs over the cap so a small host cannot be exhausted.
- **Orphan recovery**: on startup (`main.py` lifespan) any run still `pending`/`running`
  is marked `failed` — monitors do not survive a restart, so such runs are orphaned.
- **In-place single-record re-enrich**: the per-record re-crawl and manual-content
  routes do NOT create a new run. They run the one record through the pipeline in a
  hidden scratch dir (`<run_dir>/.recrawl_<token>/`, no `status.json` so it is
  invisible to `list_runs`/orphan recovery/the run cap), then merge the updated
  record back into the source run's `enriched_targets.json` by stable `id`
  (`runner._merge_recrawled_record`), recompute its status counts, and keep the run
  `complete`. The analyst's review is preserved and stamped with a dated
  "Re-enriched" note (`reviews.stamp_reenriched`). The route blocks until merge so
  the operator returns to fresh data on the same run.
- **Constant-time auth**: the UI password comparison uses `hmac.compare_digest` (`auth.py`). There is no API-key/Bearer comparison — session cookie is the only auth.
- **Atomic writes everywhere**: reviews.json (`reviews._atomic_write`) and all pipeline
  output (`output/atomic_write.py`) use temp-file + `os.replace()`.
- **Read-modify-write safety relies on single-process uvicorn**: mutating routes are
  `async def` with synchronous (no-await) read-modify-write bodies, so they cannot
  interleave on the event loop. This guarantee breaks under multi-worker uvicorn —
  add per-run file locking before any multi-worker deployment.

Scale note: current design targets ~10 operators / ≤1000-record batches on a single
host. Task queue / database / Redis remain out of scope until that ceiling is crossed.

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
  "needs_verification_count": 3,
  "contender_count": 28,
  "manual_review_count": 4,
  "excluded_count": 7,
  "error_count": 3,
  "pipeline_version": "v1.0",
  "error_summary": "",

  "client_name": "Femasys",
  "product_name": "FemaSeed",
  "target_specialty": "OBGYN",
  "target_geography": ["TX", "FL", "GA"],
  "icp_profile_id": "obgyn_femasys",
  "icp_profile_name": "OBGYN Femasys",
  "icp_profile_version": "obgyn-femasys-v11",
  "archived": false,

  "llm_input_tokens": 312000,
  "llm_output_tokens": 41000,
  "llm_call_count": 47,

  "run_type": "enrichment",
  "source_discovery_run_id": null,
  "source_discovery_selection_count": null,
  "source_discovery_selection_mode": null,

  "registry_updated_at": null,
  "registry_update_count": null,
  "registry_update_log_path": null
}
```

The canonical model is `schema.py::RunStatus`. All fields after `error_summary`
are optional with defaults so status.json files written before those layers
existed still load. `llm_*` token fields are `null` (not `0`) for runs predating
token capture. `run_type` is `"enrichment"` for normal runs; discovery runs write
their own status shape with `"discovery"`. The `source_discovery_*` fields trace a
run back to the discovery selection that created it; `registry_*` fields are set
only after an operator pushes the run into the registry.

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
- **No React, npm, or build step (permanent)**: the UI is Jinja2 + plain CSS + minimal vanilla JS only. Never add a frontend framework, a Node/npm toolchain, or a build/bundling step to this repo.
- **Pipeline output is immutable**: `reviews.py` reads `enriched_targets.json` but never writes to it.
- **Reviews are additive**: Analyst edits go to `reviews.json` only (atomic writes via tempfile + os.replace).
- **No business logic in templates or app.js**: Tier display logic in Jinja2 (`override_tier or target_tier`). JS only handles UI interactions and fetch() calls.
- **No hardcoded client, product, or specialty rules**: The UI is generic. No OBGYN, Femasys, fertility, etc.
- **No client portal, billing, or multi-tenant logic**: Internal team tool only.
- **When a feature is removed**: Delete its routes, templates, CSS, JS, imports, and tests. No dead code.
- **Override requires reason**: `override_tier` set → `override_reason` required. Enforced in `reviews.py`.
- **Server is source of truth**: `reviewed_at` always set server-side. JS never sets timestamps.
- **Final displayed tier**: `override_tier` if set, else pipeline `target_tier`. Always show both.
- **Contact Priority is a relabel**: the Contact Queue shows a rep-facing label mapped from the displayed tier (`record_adapter.contact_priority`). It is presentation only, never a stored field, and the pipeline's `call_brief` is served unchanged.

---

## BEMI Design System (Permanent)

All UI in this repo must match the BEMI design system. These rules are permanent.

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

### Brand Mark (Canonical — Never Deviate)

The Bullseye mark is **three probe lines converging inward to a central terracotta dot**.
`viewBox="0 0 30 30"` — three files in `pipeline-api/static/` and mirrored in `/assets/` on the live site:

| File | Stroke | Use on |
|------|--------|--------|
| `bullseye-mark-paper.svg` | `#f7f6f4` | Dark / ink backgrounds |
| `bullseye-mark-ink.svg` | `#0a0a0a` | Light / surface backgrounds |
| `bullseye-mark.svg` | `currentColor` | CSS/inline context where color is inherited |
| `bullseye-favicon.svg` | `#c84b2f` | Browser tab favicon only |

Exact geometry (do not reconstruct from memory — copy from the files):
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 30" width="30" height="30" fill="none">
  <line x1="10" y1="3"  x2="15" y2="15" stroke="…" stroke-width="1.4" stroke-linecap="round"/>
  <line x1="3"  y1="24" x2="15" y2="15" stroke="…" stroke-width="1.4" stroke-linecap="round"/>
  <line x1="28" y1="18" x2="15" y2="15" stroke="…" stroke-width="1.4" stroke-linecap="round"/>
  <circle cx="15" cy="15" r="3" fill="#c84b2f"/>
</svg>
```

**Rules:**
- Never use a ring/crosshair/concentric-circle design — that was a previous wrong design.
- Never recreate the geometry from memory; always read it from the files above.
- Inline SVGs in templates must use `viewBox="0 0 30 30"` and the coordinates above.
- For dark navbars, use paper variant strokes (`#f7f6f4`) inline.
- For light backgrounds (login, reports on white), use ink variant strokes (`#0a0a0a`).
- Reports embed marks as base64 data URIs from `pipeline-api/static/` via `_mark_data_uri()`.
- The full logo (mark + wordmark text) lives in `pipeline-api/reports/static/logo_dark.svg` and `logo_light.svg` — same geometry, placed in a `220×36` viewport with text at `x=44`.

### Components
- **Navbar**: ink background, paper-variant mark (inline SVG) + "Bullseye Medical Intelligence" in `Instrument Serif` surface color
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
- A second auth system — session cookie is the one auth model; do not add Bearer tokens, API keys, OAuth, or any other scheme
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

## Deferred Roadmap Items (Blocked on External Inputs)

These three items were scoped and deliberately deferred — do not begin without resolving the blocker first.

**Outcome Feedback Loop** — blocked by a source of real close/win/loss data per account. Shape: ingest an outcome CSV keyed by practice ID → correlation chart on the run dashboard showing which signals predicted wins → weight-tuning workflow to adjust ICP signal weights based on observed outcomes.

**Horizontal Scale / Job Queue** — blocked by deployment architecture decisions (single host vs. multi-worker, cloud vs. on-prem). Shape: replace subprocess+shared-filesystem with a job queue (Celery/RQ), per-job isolation, cancellable runs. Do not introduce Celery or RQ until this is resolved — they are explicitly banned for MVP.

**Genericity Validation** — blocked by a second client with a structurally different specialty and product. Shape: run end-to-end with a non-OBGYN ICP, identify and fix any hidden specialty assumptions in signal prompts, ICP builder defaults, or exclusion rules. The codebase is designed to be generic (RULE 3 in `CLAUDE.md`); this validates it holds.
