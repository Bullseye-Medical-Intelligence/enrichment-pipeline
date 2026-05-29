# PIPELINE.md — Bullseye Enrichment Pipeline
## Pipeline Constitution v1.0 | May 2026

---

## WHAT THIS FILE IS

This PIPELINE.md governs the offline enrichment pipeline for the Bullseye MVP System. It is the authoritative spec for what the pipeline does, how it is structured, what decisions are locked, and how it connects to the dashboard.

It is the pipeline's equivalent of the dashboard's CLAUDE.md. Like that file, it is a discipline document — not optional context. Every implementation decision must be consistent with what is written here.

If a request conflicts with anything in this file, surface the conflict immediately. Do not silently work around it.

**Primary operator:** Rajiv (co-founder, Bullseye Medical Intelligence)
**Technical background:** Non-developer. All instructions requiring terminal action must be numbered steps in plain English with exact commands.

---

## WHERE THIS SITS IN THE BULLSEYE MVP SYSTEM

The Bullseye MVP is two connected parts:

1. **Offline Enrichment Pipeline** — this repo. Converts raw prospect lists into structured, signal-backed intelligence.
2. **Review Dashboard** — separate repo (`bullseye-medical-intelligence/bemi`). Imports enriched files for human QC, approval, and CSV export.

**Connection between parts:** File handoff only for MVP. The pipeline produces `enriched_targets.json` or `enriched_targets.csv`. The dashboard imports that file. No live API connection between them until Phase 2.

**Operating principle:** Pipeline generates intelligence. Dashboard reviews intelligence. Shared schema keeps them aligned.

### Suggested directory structure (if combined into a monorepo later):

```
/bullseye-platform
  /dashboard            ← bemi repo
  /enrichment-pipeline  ← this repo
  /shared               ← shared schema definitions (future)
```

---

## WHAT THIS PIPELINE DOES

The pipeline receives a raw prospect list (default: Outscraper CSV export) and converts it into structured, LLM-enriched target intelligence ready for human QC in the dashboard.

**What it does:**
- Receives raw prospect CSV from Outscraper or other approved list sources
- Normalizes and deduplicates records against a canonical target schema
- Validates website URLs and attempts public-footprint extraction
- Extracts plain text from practice websites and relevant pages
- Sends relevant text plus ICP checklist to an LLM
- Populates signal fields, evidence text, source URLs, and confidence levels
- Generates exclusion rationale where applicable
- Generates rep-facing sales angle talking points
- Validates output against the shared dashboard schema
- Produces `enriched_targets.json` (full schema with signals) or `enriched_targets.csv` (flat export)
- Runs entirely outside and independently of the dashboard

**What it does NOT do:**
- Does not touch patient data, PHI, EMRs, or login-gated systems
- Does not call the dashboard or depend on any dashboard code
- Does not run inside a browser
- Does not manage QC, approval, or export (those belong to the dashboard)

---

## DATA COMPLIANCE CONSTRAINT (NON-NEGOTIABLE)

Use only public-facing physician/practice online footprint data.

**Approved sources:**
- Practice websites (public-facing pages only)
- Google Business profiles (public data)
- NPI registry (public)
- Public provider directories (Healthgrades, Zocdoc public pages, etc.)
- Public review platforms
- Outscraper exports of the above (already public data, delivered in CSV form)

**Never use:**
- PHI or patient-level data
- Claims files or appointment data
- Medical records or EMRs
- Login-gated systems or private portals
- Private databases of any kind

If a data source requires authentication to access, it is not approved for the MVP pipeline.

---

## LOCKED STACK (MVP — DO NOT SUBSTITUTE)

| Layer | Decision | Reason |
|---|---|---|
| Language | Python 3.11+ | Standard for ML/data work, strong library ecosystem |
| LLM — primary | Anthropic Claude API (Sonnet) | Main signal extraction and enrichment across all records |
| LLM — verification | OpenAI GPT-4.1 or successor (see note) | Second-opinion verification for Bullseye-tier assignments |
| Web extraction | `requests` + `BeautifulSoup4` | Lightweight, sufficient for static HTML MVP |
| JS site handling | Playwright (Phase 2) | For practices with JS-heavy sites; too complex for MVP |
| CSV parsing | `csv` (stdlib) | No extra dependency needed |
| JSON output | `json` (stdlib) | No extra dependency needed |
| HTTP retry | `tenacity` | Clean retry logic with exponential backoff |
| Environment vars | `python-dotenv` | Keep API keys out of source code |
| Package manager | `pip` + `requirements.txt` | Standard, no poetry or conda for MVP |

**GPT verification model note:** Verified against GPT-5.4 at architecture decision time. Use the most current high-capability OpenAI model available. Model ID should be configurable in `.env`, not hardcoded. Do not hardcode any model name in pipeline logic.

**Banned for MVP. Do not introduce under any circumstance:**
- Django, Flask, FastAPI, or any web server framework
- Celery, RQ, or any task queue
- SQLite, PostgreSQL, or any database engine
- Docker (Phase 2)
- Scrapy (too heavy, introduces architecture complexity)
- Selenium (use Playwright when needed, not Selenium)
- Any scraping-as-a-service library beyond Outscraper CSV
- LangChain or any LLM orchestration framework — call the APIs directly
- Any library that stores or transmits data externally
- Browser automation for MVP (Playwright is Phase 2)

---

## DUAL-LLM ARCHITECTURE

### Primary enrichment: Claude (Anthropic API)

Every record goes through Claude Sonnet for:
- Signal extraction against the ICP checklist
- Evidence text generation
- Sales angle (rep opening) generation
- Exclusion rationale generation (where applicable)
- `fit_confidence_status` determination
- Score generation (bullseye_score, fit_signal_score, confidence_score)

### Verification layer: GPT (OpenAI API)

Records where Claude scores `bullseye_score >= 75` (Bullseye-tier) go through a second GPT call for:
- Independent signal state verification (agree / disagree / insufficient data)
- Score cross-check: does GPT independently assess this as high-fit?
- Disagreement flag: if models substantially disagree, record is flagged for analyst review

**Verification disagreement rules:**
- If both models agree on Bullseye: `enrichment_status = "complete"`, proceed to output
- If GPT disagrees (would score < 75 or flags a signal differently): `enrichment_status = "needs_review"`, flag for analyst, document disagreement in `internal_notes`
- Verification is not a vote — Claude's output is the primary. GPT is a quality gate, not an override.
- Records that fail verification are still included in output. They are flagged for human review, not dropped.

### Prompt versioning

Every LLM call must include the prompt version used. Store prompt templates in `/prompts/`. Name them by function and version: `signal_extraction_v1.txt`, `sales_angle_v1.txt`, etc.

The prompt version must be recorded in the output record's `llm_prompt_version` field.

---

## INPUT: SOURCE-FLEXIBLE INGESTION LAYER

### Default input source: Outscraper CSV

Outscraper is the default candidate discovery tool. It is NOT the source of truth. It is a starting point.

Every record from Outscraper must pass:
1. Normalization to the Bullseye canonical target schema
2. Deduplication (by NPI if available, then by practice_name + address_state)
3. Website URL validation (reachable, returns 200, not a redirect loop)
4. Public-footprint extraction
5. LLM signal extraction
6. Human QC (in dashboard)

Outscraper field names must NOT leak into pipeline logic, scoring, or output. They are mapped once in the ingestion layer and discarded.

### Canonical Outscraper field mapping

| Outscraper field | Bullseye field |
|---|---|
| `name` | `practice_name` |
| `full_address` | Parsed → `address_city`, `address_state`, `address_zip` |
| `state` | `address_state` |
| `city` | `address_city` |
| `postal_code` | `address_zip` |
| `phone` | `phone` |
| `site` | `website_url` |
| `type` | Used for specialty matching only; discarded after mapping |
| `npi` (if present) | `npi_optional` |

Fields not present in the Outscraper export default to empty string or empty list. Do not error on missing optional fields.

### Source-flexible architecture

The ingestion layer must remain replaceable. Future sources may include:
- Client CRM exports (Salesforce, HubSpot CSV)
- Definitive Healthcare or IQVIA exports (commercially permissible public-level data only)
- Manual analyst lists (CSV in the Bullseye canonical format)
- NPI-derived lists (NPI registry public data)
- Other approved public or commercially permissible sources

**Implementation rule:** One ingestion adapter per source type. Each adapter maps source-specific fields to the Bullseye canonical schema and returns a normalized list of records. All downstream pipeline steps operate only on normalized records — they never see source-specific field names.

```
/ingestion
  outscraper_adapter.py   ← maps Outscraper CSV → canonical schema
  manual_adapter.py       ← passes through canonical CSVs already in schema format
  (future: crm_adapter.py, definitive_adapter.py, etc.)
```

### Generating record IDs

Each record needs a stable `id` field. Generate it at ingestion time:
- Prefer NPI if present: `T-{npi}`
- Otherwise: deterministic hash of `practice_name + address_state + address_zip` (first 8 chars of SHA256 hex, prefixed `T-`)
- ID must be stable across pipeline runs for the same practice — do not use random UUIDs

---

## PIPELINE PROCESSING STEPS

Records flow through these steps in order. Each step is a separate function or module. Do not combine steps.

```
Step 1: INGEST
  Load source CSV → normalize to canonical schema → deduplicate → validate required fields

Step 2: URL VALIDATION
  For each record with a website_url:
    - HEAD request to verify URL is reachable (200 or 301/302 followed)
    - If URL fails: flag website_url as unreachable, continue (do not skip record)
    - Set source_confidence = "limited" if URL unresolvable

Step 3: WEB EXTRACTION
  For records with a valid website_url:
    - GET request to homepage
    - Parse HTML with BeautifulSoup, extract visible text
    - Identify relevant subpages (services, providers/team, about, contact)
    - GET and extract text from each relevant subpage (max 5 pages per practice)
    - Concatenate extracted text into a single context block
    - Trim to token budget (stay under LLM context window limit — see prompt templates)
  For records where URL failed or returned no usable text:
    - context_text = ""
    - source_confidence = "limited"

Step 4: SIGNAL EXTRACTION (Claude API)
  For each record:
    - Build prompt from context_text + ICP checklist (see /prompts/)
    - Call Claude Sonnet
    - Parse structured response into signal fields (signal_state, evidence_text, source_url, confidence)
    - Generate fit_signal_score, confidence_score, bullseye_score
    - Generate fit_confidence_status
    - Generate sales_angle (rep talking points)
    - Generate exclusion rationale if any exclusion trigger fires
    - Set enrichment_status based on success/partial/failure

Step 5: BULLSEYE VERIFICATION (GPT API — conditional)
  If bullseye_score >= 75:
    - Build verification prompt (see /prompts/verification_v1.txt)
    - Call GPT
    - Compare signal states and scores
    - If agreement: no change
    - If disagreement: set enrichment_status = "needs_review", document in internal_notes

Step 6: EXCLUSION CHECK
  Apply exclusion rules from project config (equivalent of active_exclusion_rules in project.json)
  Hard exclusions always active:
    hospital_owned, health_system_affiliated, wrong_specialty, outside_geography,
    practice_closed, academic_medical_center
  Configurable exclusions applied only if listed in run config:
    rei_on_staff, no_web_presence, competitor_conflict, no_relevant_service_line
  Set exclusion_status = "EXCLUDED" and exclusion_reason if any rule fires

Step 7: SCORING VALIDATION
  - Excluded records: cap bullseye_score at 40
  - Validate all scores are in range 0–100
  - Validate all signal_state values are "yes", "no", or "not_found"
  - Validate all required fields are populated

Step 8: OUTPUT GENERATION
  - Write enriched_targets.json (full schema, preferred)
  - Write enriched_targets.csv (flat export, signals omitted)
  - Write run_log.json (run metadata, error summary, record counts)
```

---

## OUTPUT SCHEMA

The output schema is the contract between the pipeline and the dashboard. It must match the dashboard's data model exactly. Any field change here requires a corresponding change in the dashboard's `targets.json` structure and all components that reference it.

### Full output record (JSON):

```json
{
  "id": "T-001",
  "practice_name": "Sample Women's Health Practice",
  "provider_names": ["Dr. Sample Provider"],
  "specialty": "OBGYN",
  "npi_optional": "",
  "website_url": "https://example-practice.com",
  "phone": "555-000-0000",
  "address_city": "Dallas",
  "address_state": "TX",
  "address_zip": "75201",
  "metro_region_tag": "Dallas",
  "state_mandate_status": "non-mandate",

  "bullseye_score": 84,
  "fit_signal_score": 88,
  "confidence_score": 79,
  "fit_confidence_status": "HIGH FIT / HIGH EVIDENCE",

  "exclusion_status": "CLEAR",
  "exclusion_reason": null,

  "target_tier": "Bullseye",

  "signals": [
    {
      "signal_id": "S-001",
      "signal_label": "IUD insertion listed",
      "signal_state": "yes",
      "evidence_text": "Procedure page lists IUD insertion under contraception services.",
      "source_url": "https://example-practice.com/services",
      "source_type": "practice_website",
      "confidence": "high",
      "positive_weight": 15,
      "state_inferred": false,
      "analyst_note": ""
    }
  ],

  "sales_angle": [
    "Actively markets infertility workups — front and center on their services page.",
    "No REI on staff. No in-house IVF program competing for the conversation.",
    "Non-mandate state. Open with seed-to-cycle."
  ],

  "call_brief": {
    "why_contact": "OBGYN practice: Infertility workup listed + Independent private practice (fit 88).",
    "opening_line": "I saw your team lists infertility workups, so I wanted to reach out about how peers are streamlining that path.",
    "likely_objection": "They may feel their current vendor relationship already covers this.",
    "discovery_question": "How are you handling device sourcing for your in-office procedures today?",
    "hours_of_operation": "Mon-Fri 8am-5pm",
    "top_evidence": [
      {"point": "Infertility workup listed", "evidence": "Services page lists infertility evaluation.", "source_url": "https://example.com/services"}
    ],
    "missing_to_verify": ["Cash pay / self-pay visible"],
    "disqualifier_risk": []
  },

  "date_enriched": "2026-05-27",
  "source_confidence": "complete",

  "enrichment_run_id": "RUN-001",
  "source_pipeline_version": "v1.0",
  "raw_input_source": "outscraper_export_2026-05-27.csv",
  "llm_model_used": "claude-sonnet-4-6",
  "llm_prompt_version": "signal_extraction_v2",
  "enrichment_status": "complete",

  "qc_status": "pending",
  "analyst_override_classification": null,
  "override_reason": null,
  "internal_notes": "",
  "client_facing_rationale": null
}
```

### Field rules carried over from dashboard CLAUDE.md:

**signal_state:** `"yes"`, `"no"`, or `"not_found"` only. Never null, true, false, or empty string.

**positive_weight:** Carried over from the ICP signal definition. Positive for signals where a `"yes"` is good; negative where a `"yes"` is bad (e.g. "REI on staff"). Consumers use the sign to color a signal green or red: a `"no"` on a negative-weight signal is a positive indicator.

**state_inferred:** `true` when a `not_found` signal's presence was inferred from a confirmed `reinforces` signal (e.g. cash pay inferred from listed elective procedures). Inferred signals earn partial fit credit and skip the `verification_required` gate. `false` for directly observed signals.

**call_brief:** A rep preparation object, always present. Grounded fields are
derived from the signals (no LLM): `top_evidence` (highest-weight confirmed
signals with their evidence + source_url), `missing_to_verify` (unconfirmed
`verification_required` signals not covered by inference), `disqualifier_risk`
(confirmed friction or `cap_tier` signals), and `why_contact` (one-liner from the
top confirmed signals + fit). Generated/extracted fields come from the LLM
(reading the website text): `opening_line`, `likely_objection`,
`discovery_question`, and `hours_of_operation` (office hours if stated on the
site, else empty string). All fields default to empty (string or list) when
extraction fails. This is enrichment output — downstream serves it unchanged;
"Contact Priority" in the UI is a display relabel of `target_tier`, not a stored
field.

**exclusion_status:** `"CLEAR"` or `"EXCLUDED"` only.

**target_tier:** `"Bullseye"`, `"Needs Verification"`, `"Watchlist"`, or `"Excluded"` only. `"Needs Verification"` is a CLEAR record that scored as a candidate but has an unconfirmed `verification_required` signal (call to confirm before shipping). `"Excluded"` appears if and only if `exclusion_status == "EXCLUDED"`.

**qc_status:** Always `"pending"` in pipeline output. The dashboard sets all other values. Never set approved, needs_review, or rejected in pipeline output.

**source_confidence values:**
- `"complete"` — website, Google Business, at least one directory reviewed
- `"partial"` — some sources found, others unavailable
- `"limited"` — minimal public presence
- `"failed"` — pipeline could not retrieve sufficient data

**enrichment_status values:**
- `"complete"` — all pipeline steps succeeded
- `"partial"` — some steps succeeded, others failed or returned no data
- `"failed"` — pipeline failure, record may be incomplete
- `"needs_review"` — pipeline flagged for human attention (e.g. LLM disagreement)

**null usage:**
- `exclusion_reason`: null when exclusion_status is CLEAR
- `analyst_override_classification`: always null in pipeline output
- `override_reason`: always null in pipeline output
- `client_facing_rationale`: always null in pipeline output
- `npi_optional`: null if NPI unavailable
- `internal_notes`: empty string `""`, not null
- `analyst_note` on signals: empty string `""`, not null

---

## RUN LOG OUTPUT

Every pipeline run produces a `run_log.json` alongside the enriched targets file:

```json
{
  "run_id": "RUN-001",
  "run_timestamp": "2026-05-27T14:30:00Z",
  "pipeline_version": "v1.0",
  "input_file": "outscraper_export_2026-05-27.csv",
  "input_source_type": "outscraper",
  "records_input": 50,
  "records_output": 47,
  "records_excluded": 8,
  "records_needs_review": 3,
  "records_failed": 2,
  "records_skipped": 1,
  "llm_primary_model": "claude-sonnet-4-6",
  "llm_verification_model": "gpt-4.1",
  "prompt_version": "signal_extraction_v2",
  "errors": [
    {
      "record_id": "T-023",
      "step": "web_extraction",
      "error": "Connection timeout after 3 retries",
      "resolution": "source_confidence set to limited, continued with empty context"
    }
  ],
  "warnings": [
    "3 records triggered LLM disagreement and are flagged needs_review"
  ]
}
```

---

## FILE AND FOLDER STRUCTURE

```
/enrichment-pipeline
  /ingestion
    outscraper_adapter.py   ← Outscraper CSV → canonical schema
    manual_adapter.py       ← Pass-through for already-normalized CSVs
  /extraction
    web_extractor.py        ← requests + BeautifulSoup page text extraction
    url_validator.py        ← HEAD requests, redirect following, reachability check
  /enrichment
    signal_extractor.py     ← Claude API calls, prompt building, response parsing
    verifier.py             ← GPT verification calls for Bullseye-tier records
    exclusion_checker.py    ← Applies exclusion rules from run config
    scorer.py               ← Scoring logic (bullseye_score, fit_signal_score, etc.)
  /prompts
    signal_extraction_v2.txt
    sales_angle_v1.txt
    exclusion_check_v1.txt
    verification_v1.txt
  /output
    json_writer.py          ← Writes enriched_targets.json
    csv_writer.py           ← Writes enriched_targets.csv (flat, no signals)
    log_writer.py           ← Writes run_log.json
  /config
    icp_checklist.json      ← Active signal definitions for the current engagement
    run_config.json         ← Project-specific settings (active exclusion rules, geography)
  pipeline.py               ← Main entry point, orchestrates all steps
  requirements.txt
  .env.example              ← ANTHROPIC_API_KEY, OPENAI_API_KEY, etc. — never committed
  .gitignore                ← Must include .env, /output/*.json, /output/*.csv
  PIPELINE.md               ← This file
```

---

## CONFIGURATION

### `.env` (never committed — add to `.gitignore`):

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1
CLAUDE_MODEL=claude-sonnet-4-6
```

### `config/run_config.json` (committed — no secrets):

```json
{
  "project_id": "P-001",
  "client_name": "Sample MedDevice Co.",
  "target_specialty": "OBGYN",
  "target_geography": ["TX", "FL", "GA"],
  "active_exclusion_rules": [
    "hospital_owned",
    "health_system_affiliated",
    "rei_on_staff",
    "wrong_specialty",
    "outside_geography"
  ],
  "bullseye_min_score": 75,
  "max_pages_per_practice": 5,
  "request_timeout_seconds": 15,
  "request_retries": 3,
  "io_concurrency": 6,
  "subpage_keywords": ["service", "provider", "about", "contact", "..."]
}
```

Optional keys:
- `io_concurrency` (default 6): worker count for the I/O-bound steps (URL
  validation, web extraction). Set to 1 for fully sequential behavior.
- `subpage_keywords`: relevance keywords for subpage crawl scoring. Keep
  specialty-specific terms here, never hardcoded in source. Omit to use the
  generic default set in `extraction/web_extractor.py:DEFAULT_SUBPAGE_KEYWORDS`.

Env: `LLM_REQUEST_TIMEOUT_SECONDS` (default 60) caps every Claude/GPT call so a
stalled socket can never hang a run.

### `config/icp_checklist.json` (committed — defines signals for this engagement):

```json
{
  "version": "icp-v1",
  "signals": [
    {
      "signal_id": "S-ICP-001",
      "signal_label": "IUD insertion listed",
      "prompt_instruction": "Does this practice explicitly list IUD insertion as a service?",
      "positive_weight": 15
    },
    {
      "signal_id": "S-ICP-002",
      "signal_label": "REI on staff",
      "prompt_instruction": "Is there a reproductive endocrinologist (REI) on staff?",
      "positive_weight": -20,
      "note": "Negative signal — presence reduces fit score"
    }
  ]
}
```

#### Optional signal tiering fields

Each signal may also carry these optional fields (all default to off):

- **`not_found_weight`** (number, default `0`): score delta applied when the
  signal is `not_found`. Use a negative value when an unconfirmed signal should
  lower the score (e.g. cash-pay visibility you expect but could not find).
- **`no_weight`** (number, default `0`): score delta applied when a positive-weight
  signal is confirmed `"no"`. Use a negative value so a confirmed-absent must-have
  costs points directly, not just lost credit (e.g. cash pay confirmed absent).
- **`verification_required`** (bool, default `false`): when this signal is
  `not_found`, a would-be Bullseye is capped at `"Needs Verification"` so an
  analyst confirms it before the account ships.
- **`required_for_bullseye`** (bool, default `false`): must-have gate. When the
  signal is **not** confirmed `"yes"` and **not** inferred, the tier is capped: a
  confirmed `"no"` caps at `"Watchlist"`, a `not_found` caps at `"Needs
  Verification"`. This is how "Bullseye means all must-have signals are confirmed
  present" is enforced. Supersedes `verification_required` (it also covers the
  `not_found` case), so a must-have signal needs only this flag.
- **`cap_tier`** (`"Watchlist"` or `"Needs Verification"`): when the signal is
  `"yes"`, the record's tier is capped at this ceiling regardless of score. Use
  for near-disqualifying signals (e.g. a confirmed hospital affiliation caps at
  `"Watchlist"`).
- **`reinforces`** (string `signal_id`): names another signal this one supplies
  indirect evidence for. When this signal is `"yes"` and the named target is
  `"not_found"`, the target is marked inferred (`state_inferred`): it earns
  partial fit credit and its `verification_required` gate does not fire. Use to
  let an observable signal stand in for one that is rarely printed verbatim
  (e.g. listed elective/cosmetic procedures imply cash pay).

#### How fit is scored

`fit_signal_score` is the share of the **achievable** positive weight a practice
actually captures, expressed 0–100, not a running tally. `max_positive` is the
sum of every positive (desirable) `positive_weight`. A confirmed `"yes"` adds
its full weight; an inferred signal adds a fraction (`INFERENCE_CREDIT`); a
`not_found` applies its `not_found_weight` penalty; a confirmed `"no"` applies its
`no_weight` penalty; a confirmed friction signal (negative weight, `"yes"`)
subtracts. `fit = achieved / max_positive * 100`,
clamped 0–100. Matching every key signal lands near 100; a long tail of minor
signals can never out-score the few heavy ones, and a missing high-weight signal
costs proportionally more than a missing minor one. `bullseye_score` is then the
weighted blend `0.6 * fit + 0.4 * confidence`.

Example — cash pay gated, inferred from elective procedures:

```json
{
  "signal_id": "S-ICP-010",
  "signal_label": "Cash pay / self-pay visible",
  "prompt_instruction": "Does the site advertise cash-pay, self-pay, or membership pricing?",
  "positive_weight": 30,
  "required_for_bullseye": true,
  "not_found_weight": -10,
  "no_weight": -15
},
{
  "signal_id": "S-ICP-011",
  "signal_label": "Elective / cosmetic procedures listed",
  "prompt_instruction": "Does the practice list elective or cosmetic procedures patients pay for out of pocket?",
  "positive_weight": 18,
  "reinforces": "S-ICP-010"
}
```

A practice with elective procedures listed but no explicit cash-pay copy gets
cash pay inferred (partial credit, no gate — eligible for Bullseye). A practice
where cash pay is `not_found` falls to `"Needs Verification"`. A practice where
cash pay is confirmed `"no"` takes the `no_weight` penalty and is capped at
`"Watchlist"` — a must-have it definitively lacks keeps it off Bullseye.

---

## RUNNING THE PIPELINE

### First-time setup:

```
1. Clone this repo
2. Navigate to the folder:
   cd enrichment-pipeline

3. Create a virtual environment:
   python -m venv venv

4. Activate it:
   Mac/Linux:  source venv/bin/activate
   Windows:    venv\Scripts\activate

5. Install dependencies:
   pip install -r requirements.txt

6. Copy the environment template:
   cp .env.example .env

7. Open .env and paste in your API keys
```

### Running an enrichment batch:

```
python pipeline.py --input data/outscraper_export.csv --source outscraper
```

Optional flags:
```
--output-dir ./output          (default: ./output)
--config config/run_config.json
--dry-run                      (parse and normalize only, no LLM calls)
--limit 10                     (process only first N records — for testing)
```

### Output location:

```
/output
  enriched_targets.json        ← Import this into the dashboard
  enriched_targets.csv         ← Flat version for review
  run_log.json                 ← Run metadata and error summary
```

---

## ERROR HANDLING RULES

1. **Never crash the run on a single record failure.** Catch per-record errors, set `enrichment_status = "failed"`, log the error, continue to the next record.
2. **Never discard a record silently.** Every skipped or failed record must appear in `run_log.json` with reason.
3. **HTTP errors (timeout, 4xx, 5xx):** Retry up to `request_retries` times with exponential backoff (2s, 4s, 8s). If all retries fail, set `source_confidence = "limited"` and continue with empty context.
4. **LLM API errors:** Retry up to 3 times. If all retries fail, set `enrichment_status = "failed"`, log error, continue.
5. **LLM response parse failures:** If the LLM response cannot be parsed into the expected structure, set `enrichment_status = "needs_review"`, store raw response in `internal_notes`, continue.
6. **Rate limits:** Implement per-API rate limiting. Add delay between calls if approaching rate limits. Do not batch requests without rate limit awareness.
7. **Verification disagreement:** Not an error. Set `enrichment_status = "needs_review"`, document the disagreement clearly in `internal_notes`, continue.

---

## SECURITY AND API KEY RULES

1. API keys live only in `.env`. Never in source code. Never in `run_config.json`. Never in `icp_checklist.json`.
2. `.env` is always in `.gitignore`. Verify before first push.
3. Output files (`/output/*.json`, `/output/*.csv`) are in `.gitignore` by default. Never commit real client data to this repo.
4. The `.env.example` file shows variable names only — never values.
5. Log files must not contain full API responses if they include personally identifiable information. Truncate to error message only.

---

## PHASE 2 BACKLOG (DO NOT BUILD NOW)

- [ ] Playwright or Selenium for JS-heavy practice sites
- [ ] Docker containerization for reproducible runs
- [ ] Additional ingestion adapters (CRM export, Definitive, IQVIA, NPI-derived)
- [ ] Backend job queue for dashboard-triggered enrichment
- [ ] Pipeline monitoring UI in dashboard
- [ ] Retry management for failed records without re-running full batch
- [ ] Incremental enrichment (enrich only new records, skip already-enriched)
- [ ] Parallel processing with async or multiprocessing
- [ ] Persistent run history database
- [ ] Automated prompt regression testing

---

## VERSION LOG

| Version | Date | What Changed |
|---|---|---|
| 1.0 | May 2026 | Initial spec. Locked Python stack, dual-LLM architecture (Claude Sonnet primary + GPT verification), requests+BeautifulSoup extraction, Outscraper CSV with source-flexible ingestion layer, 8-step processing pipeline, full output schema, error handling rules, run log spec. |

---

*Bullseye Medical Intelligence | Internal Use Only*
*leads@bullseyemedical.ai | bullseyemedical.ai*
