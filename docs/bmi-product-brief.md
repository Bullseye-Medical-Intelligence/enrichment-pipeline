# Bullseye Medical Intelligence — Product Brief
**For LLM Context Import | May 2026 | Internal Use Only**

---

## WHAT BMI IS

Bullseye Medical Intelligence is a **physician targeting research service**. We identify which medical practices are most likely to adopt a client's product, score them on practice fit and observable public signals, and deliver that intelligence before a sales rep makes a call.

BMI is **not** a list vendor. **Not** an outreach tool. **Not** a SaaS platform. The deliverable is scored research intelligence — a ranked shortlist with evidence, sales angles, and explicit disqualification reasoning.

The core product promise: tell a rep who to call, who to skip, and why — backed by sourced public signals, not guesses.

---

## ARCHITECTURE: CHASSIS + CARTRIDGE

The system separates the **engine** (fixed) from the **ICP layer** (swappable). This is the architecture's central design decision.

### The Chassis

The **Asset Bank schema** is a fixed 65-field structure across 8 sections. It never changes per client.

| Section | Description |
|---|---|
| 1. Core Identity | Practice name, address, phone, website, NPI (when available) |
| 2. Practice Profile | Specialty, provider count, location count, group/solo |
| 3. Procedure & Service Signals | **CARTRIDGE SWAP POINT** — ICP-specific signal fields |
| 4. Business Model Indicators | Cash-pay vs. insurance, financing, cosmetic marketing |
| 5. State & Regulatory | Mandate status, licensure context (fertility-specific proxy; omit for elective cartridges) |
| 6. Exclusion Flags | **CARTRIDGE SWAP POINT** — ICP-specific hard gates |
| 7. Source Evidence | URLs, source confidence, crawl metadata |
| 8. Computed Output | Scores, tier, fit_confidence_status, call brief, sales angles |

### The Three Cartridge Swap Points

A cartridge is the ICP layer that loads into the chassis. Exactly three things change per client:

1. **Section 3** — the procedure and service signal field set (what signals Claude evaluates)
2. **Section 6** — the hard exclusion gate set (what disqualifies a record before scoring)
3. **Scoring emphasis** — the weighting instructions in the prompt layer

Everything else — the schema, the scoring model, the exclusion-first evaluation order, the output format, the API, the pipeline — is engine. Reused unchanged.

**Engine integrity rule:** Each cartridge must live in its own config folder and must not modify engine code. If a client-specific need requires code changes, treat it as an engine enhancement and document why it applies across future cartridges. This prevents client work from corrupting the reusable engine.

---

## THE TWO ENGINES

### Engine One — Sales Hook (3-Target Dossier)
Cross-references a client ICP against existing Asset Bank profiles (or sources live). Outputs exactly three target cards: **Bullseye / Contender / Excluded**. Each card carries signals with source URLs, a fit_confidence_status, and a tailored sales angle. The Excluded card is a deliberate proof point — showing the client who NOT to call is the move no list vendor makes.

### Engine Two — Bulk Processing
Processes 10,000+ record legacy lists. Zero chain-of-thought, no markdown, schema-compliant raw JSON output for clean CRM import. Same chassis and cartridge system, different output mode.

### Free Brief Sourcing Path
Given only a company name and website, the pipeline crawls the client's site, generates a product hypothesis, sources practices live from public directories, and populates the 3-target dossier. **No client list required.**

This is a controlled sample-generation workflow, not a full market map. The goal is to produce a credible sales conversation asset, not to exhaustively source a territory.

---

## THE PIPELINE (TECHNICAL IMPLEMENTATION)

### Architecture

```
BEMI-pipeline-api (FastAPI)  ←  operator UI + run management
        │ subprocess + shared /output/runs/
BEMI-enrichment-pipeline (Python CLI)  ←  all enrichment/scoring logic lives here
```

The API wraps the CLI. It never reimplements scoring, signals, or enrichment logic. All of that lives in the CLI.

### The 8 Pipeline Steps (`pipeline.py`)

1. **Ingest** — load CSV, normalize to canonical schema, dedup
2. **Structural pre-filter** — drop wrong-specialty / outside-geography records before any API spend
3. **URL validate** — reachability check, HEAD→GET fallback, http:// fallback for broken HTTPS
4. **Web extract** — crawl homepage + relevant subpages
5. **Signal extract (Claude)** — per-record LLM signal extraction + call brief generation. Records with fewer than 150 characters of website text skip the LLM call entirely; all signals are set to `not_found` to prevent hallucination on thin context.
6. **Verification (GPT)** — Bullseye-tier candidates only; second-opinion quality gate
7. **Exclusion check** — hard gates + tier assignment
8. **Output** — write JSON, CSV, run log (atomic writes)

### Signal Extraction

Claude evaluates each ICP signal against the crawled website text. Three-state values only: **yes / no / not_found**. Strict textual anchoring — a signal is present word-for-word in the public source or it is `not_found`. No inference from absence.

A `"yes"` signal requires both `evidence_text` (direct quote or close paraphrase) and `source_url`. A claim without both is downgraded to `not_found` by the evidence gate before scoring.

`"no"` means the website contains an **explicit contradiction or statement of absence**. Absence of a service from a page is `not_found`, not `no`. This distinction matters: `"no"` on a positive-weight signal applies a scoring penalty (`no_weight`); `not_found` does not.

### Scoring Model

Two independent dimensions — **never averaged**:

- **`fit_signal_score`** (0–100): share of achievable positive weight the practice captures, credit-weighted by evidence confidence (high=1.0, medium=0.75, low=0.5). A low-confidence "yes" contributes less than a verbatim-quoted "yes".
- **`confidence_score`** (0–100): mean evidence quality across confirmed signals.
- **`bullseye_score`** = `0.6 × fit + 0.4 × confidence`, clamped 0–100. Threshold: ≥ 90 for Bullseye.

**`HIGH FIT / LOW EVIDENCE` must survive to output.** A practice with strong procedure signals and a single thin source keeps a high fit score and a low confidence score — it is never collapsed into one number that hides the evidence weakness.

### Tier Ladder

```
Bullseye       score ≥ bullseye_min, all must-have signals confirmed, source confidence complete
Needs Verification  would-be Bullseye but a required must-have signal is not_found
Contender      solid fit a notch below Bullseye
Manual Review  low score / no confirmed evidence, or source_confidence limited/failed (operator review)
Excluded       any hard exclusion gate fired
```

Caps only ever pull down. Nothing lifts a Contender record to Bullseye without re-running.

### Hard Exclusion Gates

Deterministic structural exclusion gates run before LLM spend whenever they can be decided from normalized input, such as wrong specialty or outside geography. Signal-dependent exclusions run after extraction but before final tiering. In all cases, exclusion logic is applied before client-facing prioritization. A single gate hit: caps score at 40, labels EXCLUDED, routes to disqualification log. Gates must be **structural** — the account either cannot buy or cannot be scored. Brand loyalty is not a gate; it lives in fit scoring.

### Source Confidence

| Value | Meaning | Scoring consequence |
|---|---|---|
| `complete` | 2+ pages crawled, substantial text | No cap |
| `partial` | Homepage only or short text | No cap |
| `limited` | URL failed, 403, thin/blocked crawl | Routed to Manual Review (operator review) |
| `failed` | Pipeline error | Routed to Manual Review (operator review) |

Records with `source_confidence: limited` or `failed` route to Manual Review for operator attention. The client-package download is gated on Bullseye review only — Contenders ship unless an analyst rejects them.

### The Call Brief

Every record carries a `call_brief` object with:
- `opening_line` — one sentence grounded in a confirmed signal (LLM-generated)
- `likely_objection` — most likely pushback (LLM-generated)
- `discovery_question` — one question to advance the conversation (LLM-generated)
- `hours_of_operation` — office hours stated on the site, or empty string (LLM-generated)
- `top_evidence` — list of confirmed signals with their source URLs (derived from signals)
- `missing_to_verify` — unconfirmed required signals not covered by inference (derived)
- `disqualifier_risk` — confirmed friction signals and cap_tier signals (derived)
- `why_contact` — summary rationale grounded in confirmed signal labels (derived)

**Integrity gate:** when no signals survive as confirmed "yes", the three claim-based LLM prep lines (`opening_line`, `likely_objection`, `discovery_question`) are cleared to empty strings. `hours_of_operation` is factual (office hours stated on the site) and is preserved regardless of signal state. A rep never sees a fabricated opener when the data doesn't support one.

---

## ICP SIGNAL FIELDS (SCHEMA CONTRACT)

Defined per signal in the cartridge's ICP checklist. Required fields: `signal_id`, `signal_label`, `prompt_instruction`, `positive_weight`. Optional fields:

| Field | Type | Effect |
|---|---|---|
| `positive_weight` | number | Desirability weight. Negative = friction signal. |
| `not_found_weight` | number | Score delta when signal is `not_found` |
| `no_weight` | number | Score delta when positive signal is confirmed `"no"` |
| `required_for_bullseye` | bool | Must-have gate: `"no"` → caps at Contender; `not_found` → caps at Needs Verification |
| `verification_required` | bool | When `not_found`, caps a would-be Bullseye at Needs Verification |
| `cap_tier` | string | When signal is `"yes"`, caps tier at this ceiling (e.g. hospital affiliation → Contender) |
| `reinforces` | string signal_id | When this signal is "yes" and target is not_found, target is marked inferred |

---

## NON-NEGOTIABLE TECHNICAL RULES

1. **Three-state signal values only:** yes, no, not_found. Never null, never empty, never boolean.
2. **Strict textual anchoring:** present word-for-word in the public source or it is `not_found`.
3. **Exclusion-first:** hard gates run before any LLM tokens. No exceptions.
4. **Gates must be structural:** brand loyalty is not a gate. It lives in fit scoring.
5. **Asymmetric wedge:** `fit_signal` and `confidence` are independent dimensions, stored separately, never averaged. HIGH FIT / LOW EVIDENCE must survive to output.
6. **Store source URLs, extraction metadata, and the extracted page text the crawler saw.** The page text is kept in the internal Evidence Vault (`output/evidence_writer.py`) for audit and post-run rehydration (verify / re-extract); it is operator-facing only and never shipped to clients. Do not store screenshots, raw HTML, login-gated content, or patient-level data. Never infer from absence.
7. **Public sources only:** practice websites, Google Business, Healthgrades, NPI registry, directories. Never login-gated, patient portals, EMRs, paywalled, or claims/patient-level data.
8. **Matching anchors in priority order:** website URL → clinic phone → zip code. NPI is often missing — do not rely on it for dedup.
9. **Secrets in `.env` only:** API keys never in source, never in config files.
10. **No hardcoded client, specialty, or product logic in the engine:** everything is ICP config.

---

## THE OPERATOR INTERFACE (PIPELINE-API)

FastAPI server at `pipeline-api/`. Server-rendered HTML UI for internal operators.

**Key flows:**
- **Project creation:** operator defines target specialty, geography, exclusion rules, score threshold. Saved as `project_config.json`.
- **ICP Profile builder:** AI-assisted signal generation via Claude. Three-stage: crawl client site → generate hypothesis → generate signal checklist. Operator reviews and approves before saving. Draft signals require human review — the builder is a starting point, not a source of truth.
- **Run launch:** operator uploads CSV, selects project + ICP profile, launches pipeline as subprocess.
- **QC review:** operator reviews every record, labels each (approve / exclude / override tier). Download locked until all records labeled.
- **Client package export:** ZIP containing Executive Target Report (self-contained HTML), Sales Handoff (HTML), bullseye_accounts.csv, contender_accounts.csv, excluded_targets.csv. The run manifest (provenance summary) is internal-only and downloaded separately by operators, not shipped to the client.

---

## ACTIVE CARTRIDGES

### Cartridge 1: OBGYN / Femasys (fertility)
**Status:** Reference implementation. Config at `config/clients/obgyn_femasys/`.

**ICP in one line:** Independent private OBGYN practice offering IUD insertion and contraception services, not hospital-owned, not REI-staffed, not running in-house IVF.

**Key signals:** IUD insertion listed, infertility workup listed, contraception counseling, hormone/PCOS management, independent practice, hospital affiliation (hard negative).

**Asset Bank pool:** ACTIVE. Fertility records are the existing deep pool.

**Geography:** TX, FL, GA.

---

## WHAT WAS BUILT — SESSION LOG (MAY 2026)

### Shipped to `main` this session

**1. Bullseye HTML Target Report**
Added `Bullseye_Target_Report.html` to the client deliverable ZIP. Self-contained dark-theme HTML with one card per Bullseye-tier practice: confirmed signals, score, tier, rep hook, sales angles, dimension bars. Jinja2 template at `pipeline-api/reports/templates/bullseye_cards.html`. Renders via `pdf_report.build_bullseye_cards_html()`. Included alongside the existing Executive Target Report PDF in every client package.

**2. QC auto-collapse after save**
After an analyst saves a QC review, the account card auto-collapses after a 900ms delay so the analyst can move immediately to the next record. Applied in `pipeline-api/static/app.js::saveReview()`.

**3. PDF/HTML error diagnostics**
`_logo_data_uri()` exception widened from `FileNotFoundError` to `OSError`. Both PDF and HTML error fallbacks now include the actual exception message and type so operators can diagnose production failures without log access.

**4. Pipeline agnosticism sprint** (commit: `ca74706`)

- **Config restructure:** Default `config/run_config.json` and `config/icp_checklist.json` replaced with generic placeholder templates. OBGYN/Femasys config preserved at `config/clients/obgyn_femasys/` as the canonical reference implementation. Running without `--config`/`--icp` now produces an obvious placeholder output rather than silently applying OBGYN defaults to a different engagement.

- **Prompt de-specialization:** `prompts/signal_extraction_v2.txt` — removed all OBGYN-specific examples (IUD insertion signal example, REI/IUD sales angles, IUD opening line, OBGYN critical rule). Replaced with specialty-neutral generic forms. Added explicit critical rule: *"Use 'no' ONLY when the website contains an explicit contradiction or statement of absence. Absence of a service from a page is 'not_found', not 'no'."* This distinction matters for scoring: `"no"` applies a `no_weight` penalty; `not_found` does not.

- **URL validator hardening:** HEAD→GET fallback widened from 405-only to 403/405/406. Added http:// scheme retry when https:// fails with SSL or connection error — catches practice sites with broken certificates that respond on plain HTTP.

- **Score threshold alignment:** `DEFAULT_BULLSEYE_MIN_SCORE` raised 75 → 90 in `enrichment/constants.py` to match the run_config default. Eliminates silent 75-fallback when a config omits the field.

**5. ICP review UX** (commit: `7d2cd1f`)
- Approve button disables and shows "Generating…" during LLM call (prevents double-submit)
- Post-approve button state: approve goes gray ("Demo Brief Generated"), Save Profile becomes the primary CTA (terracotta)
- Active Exclusion Rules display as readable pill badges instead of raw snake_case strings

---

## CONFIGURATION PATTERN

Client configs live at `config/clients/<slug>/`. Never edit the root templates.

```
config/
  run_config.json           ← generic placeholder (do not run directly)
  icp_checklist.json        ← generic placeholder (do not run directly)
  clients/
    obgyn_femasys/
      run_config.json       ← Femasys / OBGYN reference implementation
      icp_checklist.json    ← 8-signal OBGYN ICP
```

**Running a client engagement:**
```bash
python pipeline.py \
  --input data/input.csv \
  --source outscraper \
  --config config/clients/<slug>/run_config.json \
  --icp    config/clients/<slug>/icp_checklist.json
```

---

## ASSET BANK STATUS (MAY 2026)

| Specialty | Pool Status |
|---|---|
| Fertility / OBGYN | ACTIVE — deep pool, production-ready |
| Orthodontics / General Dental | EMPTY — no records ingested |
| Aesthetics | ICP template exists (`icp_templates/aesthetics.json`), no records ingested |
| Orthopedics | ICP template exists, no records ingested |
| Urology | ICP template exists, no records ingested |

---

## OPEN ITEMS / KNOWN GAPS

1. **Bot-blocking on practice sites.** Some practice websites (Cloudflare WAF and similar) return 403 to automated crawlers. The pipeline's `requests`-based extractor with browser-spoofed headers bypasses most of these, and `--auto-browser-retry` / `--playwright` recover the rest; the Free Brief WebFetch path does not. The system routes 403'd records to `source_confidence: limited` → Manual Review, which forces operator review — but operators currently have no in-system signal distinguishing "retry-able 403" from "genuinely no content."

2. **`not_found_reason` field.** Three situations produce `signal_state: not_found` with different rep implications: (a) crawled successfully, service absent; (b) site couldn't be crawled; (c) LLM claimed "yes" but evidence gate downgraded it. The `not_found_reason` field (`""` / `"no_context"` / `"evidence_gate"`) is in the schema and surfaced in the UI but the distinction is not yet exposed in client exports.

3. **`inferred_from` field.** When a signal is inferred via reinforcement (`state_inferred: true`), `inferred_from` carries the reinforcing signal's ID. Present in the schema, surfaced in the UI as a tooltip, not yet in client exports.

---

*Bullseye Medical Intelligence | Internal Use Only | Not for Client Distribution*
*leads@bullseyemedical.ai*
