# Bullseye Enrichment Pipeline

Converts raw prospect lists (Outscraper CSV exports or manually prepared CSVs) into
structured, LLM-enriched intelligence ready for human review in the Bullseye dashboard.
The pipeline scores each practice against a configurable ICP, extracts evidence from
public websites, and outputs `enriched_targets.json` for dashboard import.

---

## 1. What This Pipeline Does

The pipeline takes a list of medical practices from an Outscraper CSV or manual CSV,
visits each practice's **public-facing website footprint only**, sends the text to
Claude (Anthropic API) for signal analysis, and produces a scored, structured output
file. The output file is imported into the review dashboard for human QC.

### GPT verification is a separate, operator-triggered pass

A second LLM (OpenAI GPT) acts as a **quality gate, not a guarantee**. It does **not**
run inline during `pipeline.py`. Verification is a separate, operator-triggered
**post-run pass** (`verify_run.py` → `enrichment/verifier.py`) over a completed run's
`enriched_targets.json`, invoked from the dashboard. It targets only **`Needs
Verification`** records:

- **Anchor-check (free)** — confirm each `"yes"` signal's evidence appears verbatim in
  the page text; any anchor failure skips GPT (compromised evidence).
- **Blind GPT re-extraction** (survivors) — GPT independently re-extracts the
  unconfirmed gating signals.

Results are written as an additive `verification` object (`promote` / `hold` /
`disqualify`); signals, tier, and score are never overwritten, and a promote still
requires an operator override. The pass is idempotent. `verify_near_miss_band` is a
retained no-op — it is **not** consumed by the current verification design. See
`CLAUDE.md` → "The 8 Steps" (Step 5) for the exact contract.

### Market Radar (discovery) — optional operator workflow

Before enrichment, an operator can run **Market Radar / Discovery**: upload an
Outscraper CSV and compare it against the Master Practice Registry to see which
practices are NEW, CHANGED, KNOWN, POSSIBLE_DUPLICATE, or INSUFFICIENT_DATA, then
send only the actionable ones into enrichment. Discovery never spends crawl/LLM
budget and never mutates the registry. See
[`docs/operator_market_radar_workflow.md`](docs/operator_market_radar_workflow.md)
and [`docs/discovery_architecture.md`](docs/discovery_architecture.md).

### The platform around this CLI

This repo contains the whole platform, in two layers that share one filesystem:

```
Operator browser ──▶ pipeline-api/  (FastAPI, server-rendered HTML, session auth)
                         │  subprocess + shared /output/runs/<run_id>/
                         ▼
                     pipeline.py    (this CLI — all enrichment/scoring logic)
```

- **The CLI owns every scoring, signal, tier, and exclusion decision.** The API is
  a process manager and review UI; it never re-implements pipeline logic
  (`pipeline-api/CLAUDE.md`, RULE 1-3).
- **Operator workflow:** create a project (client config + ICP profile) → upload a
  CSV → *ingest* (roster only, no spend) → review the roster → *Enrich All* →
  analyst QC in the dashboard (tier overrides, notes, signal overrides — stored as
  an additive `reviews.json` overlay, never touching pipeline output) → client
  package export (ZIP) and published briefs.
- **Post-run passes** (operator-triggered, on a completed run): GPT verification,
  re-score (new weights, no LLM), re-extract (Claude, no re-crawl — page text
  rehydrated from the Evidence Vault), suppression re-check, and per-record /
  batch browser re-crawls that merge in place. Passes are serialized per run and
  refuse to clobber concurrent writes.
- **Master Practice Registry** (`master_practice_registry.json`): the platform's
  only cross-run memory — written **only** by the explicit "Update Registry"
  action, never automatically. Market Radar classifies uploads against it
  read-only. If no one clicks the button, the platform has no memory of a
  practice. See `docs/registry_lifecycle.md` and `docs/data-boundary-model.md`
  for its current limits (no client scoping) and the proposed target model.
- **State is files.** No database, no queue, no cache — JSON on disk with atomic
  writes and advisory locks, single-host by design (~10 operators, ≤1000-record
  batches). Scale-out is a documented deferred item, not an accident.

---

## 2. Setup

Do this once before your first run.

**Step 1.** Clone the repo and open a terminal in the project folder:
```
cd enrichment-pipeline
```

**Step 2.** Create a Python virtual environment:
```
python -m venv venv
```

**Step 3.** Activate it:
- Mac / Linux: `source venv/bin/activate`
- Windows: `venv\Scripts\activate`

**Step 4.** Install dependencies:
```
pip install -r requirements.txt
```

**Step 5.** Copy the environment template:
```
cp .env.example .env
```

**Step 6.** Open `.env` in any text editor and paste in your API keys:
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
CLAUDE_MODEL=claude-sonnet-4-6
OPENAI_MODEL=gpt-5.5
```

Save the file. Never share or commit `.env` — it is already in `.gitignore`.

---

## 3. Running the Pipeline

**Basic run (full enrichment) — OBGYN / Femasys engagement:**
```
python pipeline.py --input data/your_export.csv --source outscraper \
  --config config/clients/obgyn_femasys/run_config.json \
  --icp    config/clients/obgyn_femasys/icp_checklist.json
```

**For a new engagement**, copy the client folder and customise:
```
cp -r config/clients/obgyn_femasys config/clients/<your_slug>
# Edit config/clients/<your_slug>/run_config.json and icp_checklist.json
```

**Test with 5 records first (recommended before a full batch):**
```
python pipeline.py --input data/your_export.csv --source outscraper \
  --config config/clients/<your_slug>/run_config.json \
  --icp    config/clients/<your_slug>/icp_checklist.json \
  --limit 5
```

**Dry run (parse and normalize only — no API calls, no HTTP requests):**
```
python pipeline.py --input data/your_export.csv --source outscraper \
  --config config/clients/<your_slug>/run_config.json \
  --icp    config/clients/<your_slug>/icp_checklist.json \
  --dry-run
```

**Manual CSV (already in Bullseye canonical format):**
```
python pipeline.py --input data/manual_list.csv --source manual \
  --config config/clients/<your_slug>/run_config.json \
  --icp    config/clients/<your_slug>/icp_checklist.json
```

**All available flags:**

| Flag | Default | What it does |
|---|---|---|
| `--input` | (required) | Path to input CSV file |
| `--source` | (required) | `outscraper`, `apify_places`, or `manual` |
| `--output-dir` | `./output` | Where to write output files |
| `--config` | `config/run_config.json` | Run configuration file |
| `--icp` | `config/icp_checklist.json` | ICP signal definitions |
| `--dry-run` | off | Parse only, skip all API calls |
| `--limit N` | off | Process only the first N records |
| `--playwright` | off | Use headless Chromium (Playwright) instead of `requests` for web extraction |
| `--auto-browser-retry` | off | After the standard crawl, re-crawl blocked/thin sites once with headless Chromium before signal extraction (ignored when `--playwright` is set) |
| `--manual-content-path PATH` | off | Operator-provided HTML/text file used instead of crawling (repeatable, once per page); for CAPTCHA-blocked sites |
| `--ingest-only` | off | Ingest + normalize + structural exclusions only; write the roster and exit before any crawl or LLM call |
| `--run-id ID` | auto | Use this run identifier instead of generating one (the API passes its own) |

---

## 4. Input Requirements

### Outscraper export (`--source outscraper`)

Export from Outscraper with at minimum these columns:

| Column | What it contains |
|---|---|
| `name` | Practice name |
| `state` | State (full name like "Texas" or abbreviation "TX") |
| `city` | City |
| `postal_code` | ZIP code |
| `phone` | Phone number |
| `site` | Practice website URL |
| `type` | Business category (used for specialty matching) |

Optional: `full_address`, `npi`. Missing optional fields are skipped without error.

### Apify Google Places export (`--source apify_places`)

Export from Apify's "Google Places crawler" actor (flattened CSV). Only these
columns are read — the hundreds of `additionalInfo/*` columns are ignored:

| Column | What it contains |
|---|---|
| `title` | Practice name (required) |
| `website` | Practice website URL — the `url` column is the Google Maps link and is **never** used as the website |
| `phone` / `phoneUnformatted` | Phone (formatted preferred, unformatted fallback) |
| `city`, `state`, `postalCode` | Location; full state names ("California") are normalized to 2-letter codes |
| `categoryName` (fallback `categories/0`) | Business category, used for specialty matching |
| `placeId` | Google Place ID (registry priority-1 match key) |
| `permanentlyClosed` | Rows marked `true` are dropped on import (with a printed count); `temporarilyClosed` practices are kept |

### Manual CSV (`--source manual`)

CSV with column headers matching the Bullseye canonical schema. Required column:
`practice_name`. Useful optional columns: `website_url`, `specialty`, `address_city`,
`address_state`, `address_zip`, `phone`, `npi_optional`, `provider_names`.

---

## 5. Output Files

All output is written to `./output/` (or your `--output-dir`).

**`enriched_targets.json`** — the primary output. Full schema with all signal data,
scores, evidence text, and sales angles. The operator dashboard reads it directly
from the shared runs directory (no import step).

**`enriched_targets.csv`** — flat version of the same records, without nested signal
detail. Useful for quick review in Excel or Google Sheets.

**`run_log.json`** — run metadata: record counts by outcome (excluded, needs_review,
failed), list of per-record errors, and any warnings. Check this first when a run
produces unexpected results.

**`step4_checkpoint.ndjson`** — per-record signal-extraction checkpoint. A killed or
crashed run resumes from it instead of re-spending on Claude. It is scoped to its
inputs (first line stamps a fingerprint of the config + ICP + input CSV) and deleted
on successful completion — a checkpoint from different inputs, or from a finished
run, is never reused, so editing the ICP and re-running always re-extracts.

**`evidence/<record_id>/`** — the Evidence Vault: per-page crawl snapshots
(`index.json` + `page-NN.txt`) proving what the crawler saw; also the text source
for the post-run verification and re-extraction passes.

> Output files are in `.gitignore` and will not be committed to git. Do not commit
> real client data to this repository.

---

## 6. Key Configuration

Client configs live under `config/clients/<slug>/`. Do not edit the root
`config/run_config.json` or `config/icp_checklist.json` — those are generic
templates. Always pass `--config` and `--icp` explicitly.

### `config/clients/<slug>/run_config.json` — change this per engagement

| Field | What to change |
|---|---|
| `client_name` | Client or project name (appears in run log) |
| `target_specialty` | Specialty to match (e.g. `"OBGYN"`) |
| `target_geography` | List of 2-letter state codes (e.g. `["TX", "FL", "GA"]`) |
| `active_exclusion_rules` | Which exclusion rules fire for this engagement |
| `bullseye_min_score` | Bullseye score gate for ICPs that define no must-have signals (default: 90). When the ICP flags `required_for_bullseye` signals, confirming all of them defines Bullseye and this threshold does not hold a record down |

### `config/clients/<slug>/icp_checklist.json` — change this per engagement

Defines the signals Claude evaluates for each practice. Each signal has:
- `signal_id` — unique ID (e.g. `S-ICP-001`)
- `signal_label` — human-readable name
- `prompt_instruction` — the question Claude answers for this signal
- `positive_weight` — how much this signal adds to (or subtracts from) the fit score.
  Negative weight = negative signal (e.g. hospital affiliation reduces fit score).

See `config/clients/obgyn_femasys/` for a complete reference implementation.

---

## 7. How to Inspect a Bad Run

**Start with `run_log.json`:**
- `records_failed` — records where the pipeline threw an error (API failure, etc.)
- `records_needs_review` — records whose LLM response could not be parsed (JSON decode failure or a missing required key); flagged `enrichment_status: "needs_review"` for an operator to re-extract
- `errors` array — per-record error details with step name and error message

**`enrichment_status` values in the output:**

| Value | Meaning |
|---|---|
| `complete` | All pipeline steps succeeded |
| `partial` | Some steps succeeded; others returned no data |
| `failed` | Pipeline error on this record (see `internal_notes`) |
| `needs_review` | The LLM response could not be parsed (JSON decode / missing key); flag for operator re-extraction |

**`source_confidence` values:**

| Value | Meaning |
|---|---|
| `complete` | 2+ pages crawled, substantial text extracted |
| `partial` | Homepage only, or very short text extracted |
| `limited` | URL failed, no website, or minimal public presence |
| `failed` | Pipeline could not retrieve any data |

**Common issues:**
- All records `failed` on `signal_extraction` step → check that `ANTHROPIC_API_KEY`
  is set correctly in `.env`
- Records excluded as "outside geography" → confirm `target_geography` in
  `run_config.json` uses 2-letter state codes (e.g. `"TX"` not `"Texas"`)
- `source_confidence: limited` on many records → Outscraper export may not include
  website URLs; check the `site` column is populated

---

## 8. Testing & CI

Run the test suite:
```
python -m pytest tests/ -q
```

**Tests are deterministic and do not call LLM APIs or external websites.** They
require no `.env`, no `ANTHROPIC_API_KEY`, and no `OPENAI_API_KEY`, and they never
launch a browser. GitHub Actions (`.github/workflows/ci.yml`) runs the same suite
plus an ingest-only `--dry-run` smoke test on every push to `main` and every pull
request.

---

## 9. What This Pipeline Does NOT Do

- **No PHI.** The pipeline only reads public-facing practice websites. It does not
  access patient data, EMRs, appointment records, or any login-gated system.
- **No authenticated sources.** If a data source requires a login, it is not used.
- **No dashboard QC.** This pipeline produces the file; human review, approval, and
  CSV export are handled separately by the operator UI (`pipeline-api/`), not by this
  CLI.
- **No live database.** All state is in files. There is no running server, database,
  or background job queue in the MVP pipeline.
- **Browser automation is opt-in.** The default crawler is HTTP-only (`requests`).
  Bot-gated / JS-heavy sites are handled by headless Chromium (Playwright) via
  `--playwright` (whole run) or `--auto-browser-retry` (re-crawl only the blocked
  subset). See CLAUDE.md "The 8 Steps" for the auto browser-retry flow.

---

## 10. Documentation Map

Where the platform's knowledge lives, and what each file is authoritative for:

| Document | Authoritative for |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Working guide for the pipeline engine: the 8 steps, scoring model, ICP signal fields, tier ladder, concurrency/checkpointing rules, anti-fabrication policy |
| [`PIPELINE.md`](PIPELINE.md) | **The output schema contract** — every field of `enriched_targets.json` and per-step contracts. Schema changes update this file and the validator together |
| [`pipeline-api/CLAUDE.md`](pipeline-api/CLAUDE.md) | Working guide for the operator API: absolute rules, locked tech stack, UI architecture, locking model, known performance debt, deferred roadmap |
| [`pipeline-api/RUNBOOK.md`](pipeline-api/RUNBOOK.md) | Deploying and operating the API |
| [`PROJECT_HANDOFF.md`](PROJECT_HANDOFF.md) | Engagement state: clients, cartridge history, next deliverables |
| [`docs/operator-sop.md`](docs/operator-sop.md) | Operator standard procedure for running an engagement |
| [`docs/client-onboarding-form.md`](docs/client-onboarding-form.md) | Client onboarding form: what a client provides to build their cartridge, plus the operator-only answer→cartridge mapping (client-ready Word copy alongside as `Bullseye_Client_Onboarding_Form.docx`) |
| [`docs/operator_market_radar_workflow.md`](docs/operator_market_radar_workflow.md) / [`docs/discovery_architecture.md`](docs/discovery_architecture.md) | Market Radar: operator workflow and internals |
| [`docs/registry_lifecycle.md`](docs/registry_lifecycle.md) | Master Practice Registry: when it changes and by whom |
| [`docs/data-boundary-model.md`](docs/data-boundary-model.md) | Client/project data-boundary analysis: confirmed contamination risks, proposed client-scoping model, open business decisions |
| [`docs/review-backlog.md`](docs/review-backlog.md) | Verified open findings from the latest code review — ranked, with fix directions |
| [`evals/README.md`](evals/README.md) / [`evals/LABELING_SOP.md`](evals/LABELING_SOP.md) | Signal-extraction eval harness and golden-dataset labeling procedure |
| [`docs/bmi-product-brief.md`](docs/bmi-product-brief.md) | Product framing for the business side |

---

*Bullseye Medical Intelligence | Internal Use Only*
*leads@bullseyemedical.ai*
