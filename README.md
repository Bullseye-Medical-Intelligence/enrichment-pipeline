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

### GPT verification is selective, not universal

A second LLM (OpenAI GPT) acts as a **quality gate, not a guarantee**, and only runs
on the records where it adds the most value. It does not re-score every record:

- **Near-miss records** (score just below the Bullseye threshold, within
  `verify_near_miss_band`) are verified — this is the highest-value GPT spend.
- **Uncertain Bullseyes** — a would-be Bullseye that rests on at least one
  low-confidence "yes" signal — are verified. High-confidence Bullseyes are skipped
  (GPT would only agree).
- **Thin-context records** (`source_confidence` `limited`/`failed`) **skip GPT**
  entirely — the tier is capped at "Needs Verification" regardless, and GPT would
  see the same too-thin text.

Verification informs the tier and flags disagreement for the analyst; it never
auto-promotes a record and it does not make the output "verified-correct." Human QC
in the dashboard remains the authority. See `CLAUDE.md` → "The 8 Steps" (Step 5) for
the exact selection logic.

### Market Radar (discovery) — optional operator workflow

Before enrichment, an operator can run **Market Radar / Discovery**: upload an
Outscraper CSV and compare it against the Master Practice Registry to see which
practices are NEW, CHANGED, KNOWN, POSSIBLE_DUPLICATE, or INSUFFICIENT_DATA, then
send only the actionable ones into enrichment. Discovery never spends crawl/LLM
budget and never mutates the registry. See
[`docs/operator_market_radar_workflow.md`](docs/operator_market_radar_workflow.md)
and [`docs/discovery_architecture.md`](docs/discovery_architecture.md).

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
OPENAI_MODEL=gpt-4.1
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
| `--source` | (required) | `outscraper` or `manual` |
| `--output-dir` | `./output` | Where to write output files |
| `--config` | `config/run_config.json` | Run configuration file |
| `--icp` | `config/icp_checklist.json` | ICP signal definitions |
| `--dry-run` | off | Parse only, skip all API calls |
| `--limit N` | off | Process only the first N records |

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

### Manual CSV (`--source manual`)

CSV with column headers matching the Bullseye canonical schema. Required column:
`practice_name`. Useful optional columns: `website_url`, `specialty`, `address_city`,
`address_state`, `address_zip`, `phone`, `npi_optional`, `provider_names`.

---

## 5. Output Files

All output is written to `./output/` (or your `--output-dir`).

**`enriched_targets.json`** — the primary output. Full schema with all signal data,
scores, evidence text, and sales angles. Import this file into the dashboard.

**`enriched_targets.csv`** — flat version of the same records, without nested signal
detail. Useful for quick review in Excel or Google Sheets.

**`run_log.json`** — run metadata: record counts by outcome (excluded, needs_review,
failed), list of per-record errors, and any warnings. Check this first when a run
produces unexpected results.

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
| `bullseye_min_score` | Minimum score for Bullseye tier (default: 90) |

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
- `records_needs_review` — records where the two LLMs disagreed on a Bullseye score
- `errors` array — per-record error details with step name and error message

**`enrichment_status` values in the output:**

| Value | Meaning |
|---|---|
| `complete` | All pipeline steps succeeded |
| `partial` | Some steps succeeded; others returned no data |
| `failed` | Pipeline error on this record (see `internal_notes`) |
| `needs_review` | LLM disagreement on Bullseye score, or parse error — needs human review |

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

*Bullseye Medical Intelligence | Internal Use Only*
*leads@bullseyemedical.ai*
