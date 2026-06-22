# CLAUDE.md — BEMI Enrichment Pipeline

Every session working in this repo begins by reading this file and `PIPELINE.md`.
If this file and the code conflict, fix the code — not this file. `PIPELINE.md`
is the canonical spec for the output schema and step contracts; this file is the
working guide for how the pipeline behaves and the rules a session must hold.

---

## Communication Style

Responses must be brief business and product-focused summaries. Do not break down
code unless explicitly asked. Discuss everything from a business solution and
product development standpoint. Be efficient with tokens and elaborate only when
the user explicitly asks for more detail.

---

## What This Repo Is

The **enrichment pipeline**: a Python CLI (`pipeline.py`) that turns a raw
prospect list (Outscraper or manual CSV) into scored, tiered, sales-ready
account intelligence. It runs 8 steps, calls Claude for signal extraction and
GPT for verification, and writes immutable JSON/CSV output plus a run log.

The operator API and this CLI share one repo:

```
BEMI-pipeline-api (FastAPI, ./pipeline-api/)  ← operator UI; spawns this CLI as a subprocess
        │  subprocess + shared /output/runs/
THIS REPO: BEMI-enrichment-pipeline (the CLI)  ← all enrichment/scoring logic lives here
```

The API wraps this pipeline; it never reimplements scoring or signals (see
`pipeline-api/CLAUDE.md`). All enrichment, scoring, signal, tier, and exclusion
logic lives **here** and nowhere else.

---

## Absolute Rules

### RULE 1: Secrets live only in `.env`.
API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) are read from environment
via `python-dotenv`. Never in source, never in `run_config.json`, never in
`icp_checklist.json`. `.env` and `*.env` are gitignored — verify before any push.
`.env.example` carries placeholders only.

### RULE 2: Never commit real client data.
`/output/*.json`, `/output/*.csv`, and `/data/*.csv` are gitignored. Enriched
records, input lists, and run output stay out of git.

### RULE 3: No hardcoded client, product, or specialty logic.
The engine is generic. OBGYN, cash pay, elective, REI, Femasys, etc. are **ICP
config**, never code. Signal definitions and weights live in
`config/icp_checklist.json` (or an operator-authored profile), read at runtime.
A function that branches on a specialty name is a bug.

### RULE 4: Output schema is a contract.
`enriched_targets.json` / the record schema is defined in `PIPELINE.md`. Adding a
field means updating `PIPELINE.md` and the validator in `enrichment/scorer.py` in
the same change. Downstream (API, UI) serves output unchanged.

### RULE 5: One scoring constant, one home.
Every score bound, weight, threshold, and blend factor lives in
`enrichment/constants.py`. No magic numbers scattered across modules.

### RULE 6: Develop on the assigned branch; never push elsewhere without permission.

---

## The 8 Steps (`pipeline.py`)

1. **Ingest** — load CSV, normalize to canonical schema, dedup, drop rows missing `practice_name`.
   **Step 1b — NPI enrichment (opt-in)**: populates taxonomy codes and exclusion flags from the public NPPES registry before the structural pre-filter runs. Runs even in `--ingest-only` mode so the roster carries NPI fields. Skip via `npi_enrichment_enabled: false` in run_config.
   **Step 1c — Customer suppression (opt-in)**: excludes existing customers before any crawl or LLM spend. Runs even in `--ingest-only` mode so suppressed records appear as EXCLUDED in every roster view. Triggered by `suppression_list_path` in run_config.
   **Structural pre-filter** — after Steps 1b–1c, `check_structural_exclusions` drops records that are wrong specialty or outside geography before any crawl or LLM spend. Pre-excluded records skip Steps 2–6 and rejoin at Step 6 as Excluded. (Does NOT run in `--ingest-only` mode.)
2. **URL validate** — reachability check (`extraction/url_validator.py`), `io_concurrency` workers.
3. **Web extract** — crawl homepage + subpages (`extraction/web_extractor.py`), `io_concurrency` workers. Every internal page is a candidate except blog/news/legal/auth/commerce noise (`SKIP_PATH_SEGMENTS`); pages are crawled evidence-first (keyword-ranked) until the `MAX_COMBINED_CHARS` text budget is full or the `MAX_CRAWL_PAGES` (20) / `MAX_CRAWL_SECONDS` (45s) bounds are hit. No low per-site page cap.
   **Auto browser-retry (Step 3b, opt-in)** — with `--auto-browser-retry` (CLI) or `auto_browser_retry: true` (run_config), records that come back blocked/thin from the standard crawler (`source_confidence` limited/failed, or under `MIN_CONTEXT_CHARS` of text) are re-crawled once with headless Chromium before Step 4. `_records_needing_browser_retry` targets only the blocked subset, so most records keep the fast HTTP path; no-op when the whole run is already `--playwright`. This recovers bot-gated sites automatically instead of waiting for an operator to click "Re-crawl with Browser". Exposed in the API as a checkbox on "Enrich All".
   **Manual content (`--manual-content-path`)** — for a single site behind a hard CAPTCHA wall the crawler cannot clear, the operator captures the page in their own browser (Save Page As .html, or copy the visible text) and supplies it. The flag bypasses Steps 2-3 entirely: `_load_manual_content` loads that file into every record's `_context_text` (HTML converted with the crawler's `_extract_text_from_html`, plain text used as-is), sets `source_confidence = "partial"`, and Step 4 runs on it unchanged. Exposed in the API as a per-record "Paste site content" form (`orchestrate_manual_content_recrawl`).
4. **Signal extract (Claude)** — per-record LLM signal extraction + scoring (`enrichment/signal_extractor.py`), `llm_concurrency` workers, checkpointed. Records with fewer than `MIN_CONTEXT_CHARS` of website text skip the LLM call; all signals are set to `not_found` and `enrichment_status = "partial"` to prevent hallucinations from thin context.
5. **Verification (GPT)** — NOT an inline step in `pipeline.py`. Verification runs as a **separate, operator-triggered post-run pass** (`verify_run.py` → `enrichment/verifier.py::run_verification_pass`), invoked from the dashboard (`POST /dashboard/{run_id}/verify`). It operates on a completed run's `enriched_targets.json` and targets only `Needs Verification` records. Two phases per record: (a) **anchor-check** (free) — confirm each `"yes"` signal's `evidence_text` appears verbatim in the page text; any anchor failure skips GPT (compromised evidence). (b) **blind GPT re-extraction** (survivors) — GPT independently re-extracts the unconfirmed gating signals. Results are written as an additive `verification` object (`recommended_action`: promote / hold / disqualify); signals, tier, and score are never overwritten, and a promote still requires an operator override. The pass is idempotent (records with `verification.verified_at` are skipped). Because `_context_text` is stripped from output, the pass **rehydrates page text from the Evidence Vault** (`output/evidence_writer.py::read_record_context_text`) before anchor-check / GPT.
6. **Exclusion check** — hard + configurable rules, tier assignment (`enrichment/exclusion_checker.py`).
7. **Scoring validation** — clamp, validate, enforce invariants (`enrichment/scorer.py`).
8. **Output** — write JSON, CSV, run_log.json (`output/`, atomic writes).

### `--ingest-only` (roster pass, no spend)
`--ingest-only` runs Step 1 → Step 1b (NPI enrichment) → Step 1c (customer
suppression) → Step 8 (output), then exits before any crawl or LLM call
(`_finalize_ingest_only`). The structural pre-filter does NOT run (it fires
later in the full flow); customer-suppressed records are written as EXCLUDED.
Writes the full roster (`enrichment_status = "not_enriched"`, scores 0, no
signals). Lets an operator review the list before spending budget; enrichment
is triggered as a separate full run over the same `input.csv`. The API exposes
this as upload → `ingested` status → "Enrich All" (`pipeline-api/runner.py`:
`orchestrate_ingest` / `orchestrate_enrich_all`).

---

## Scoring Model (commercial-fit confidence)

The score answers one question for a sales rep with limited dials: **how
confident should I be that this is a fast commercial close?** It is NOT a tally —
matching more signals does not mean a higher score.

`enrichment/signal_extractor.py::_calculate_scores`:

- **`fit_signal_score`** = the share of the *achievable* positive weight a
  practice actually captures, scaled 0–100.
  - `max_positive` = sum of every positive (desirable) `positive_weight` — the ideal.
  - A confirmed `"yes"` desirable signal adds `weight × SIGNAL_CONFIDENCE_CREDIT[confidence]`
    to `achieved`. Credits: `high` = 1.0, `medium` = 0.75, `low` = 0.5. A low-confidence
    "yes" (weak evidence) contributes less than a verbatim-quoted "yes", so an LLM that
    guesses at low confidence cannot manufacture a Bullseye score.
  - An **inferred** signal (`state_inferred`, see reinforcement) adds
    `INFERENCE_CREDIT` of its weight (partial credit for indirect evidence).
  - A `"not_found"` desirable signal applies its `not_found_weight` penalty (usually ≤ 0).
  - A confirmed-absent (`"no"`) desirable signal applies its `no_weight` penalty
    (usually ≤ 0, default 0) — a missing must-have costs points, not just lost credit.
  - A confirmed **friction** signal (negative weight, `"yes"`) subtracts
    `|weight| × SIGNAL_CONFIDENCE_CREDIT[confidence]`.
  - `fit = round(achieved / max_positive * 100)`, clamped 0–100. (Falls back to
    `BASE_FIT_SCORE` only when an ICP defines no positive weight.)
  - Consequence: heavy signals dominate; a long tail of minor signals can never
    out-score the few that matter; a missing high-weight signal costs
    proportionally more than a missing minor one.
- **`confidence_score`** = mean of `CONFIDENCE_SCORE_MAP` across confirmed/inferred
  signals, else `NO_SIGNAL_CONFIDENCE`.
- **`bullseye_score`** = `FIT_WEIGHT * fit + CONFIDENCE_WEIGHT * confidence`, clamped.

### Rep call brief (`signal_extractor.py::_build_call_brief`)
Every record carries a `call_brief` object. Grounded fields are **derived from the
signals** (no extra LLM call): `top_evidence`, `missing_to_verify` (mirrors the
verification gate), `disqualifier_risk`, and `why_contact`. Four fields come from
the LLM: `opening_line`, `likely_objection`, `discovery_question`, and
`hours_of_operation` (office hours stated on the website, or empty string).

**Integrity gate:** when `top_evidence` is empty (no signals survived as confirmed
"yes"), all four LLM prep lines are cleared to `""`. The top-level `sales_angle`
field (rep-facing bullet points, also LLM-generated) is similarly cleared to `[]`.
This prevents a rep from seeing a fabricated opener or sales angle when the data
doesn't actually support any confirmed signals.

`sales_angle` is a top-level field on the enriched record, not inside `call_brief`.
Both `sales_angle` and the prep lines are gated together — either both have grounded
evidence or both are empty.

The empty shape lives in `constants.py::empty_call_brief`; `scorer.py` defaults it
so the field is always present. **Contact Priority** in the UI is a display relabel
of `target_tier` (`record_adapter.contact_priority`), never a stored field.

---

## ICP Signal Fields

Defined per signal in `config/icp_checklist.json` / ICP profiles. Required:
`signal_id`, `signal_label`, `prompt_instruction`, `positive_weight`. Optional
(all default to off), validated in `pipeline-api/icp_profiles.py`:

**Profile-level optional field — `contact_strategy`** (string): operator-authored
guidance for who the call brief's `key_contact` should be, injected into the
extraction prompt's primary_contact instruction (e.g. "prefer the treatment
coordinator or lead hygienist — workflow friction beats brand loyalty"). When
unset, the engine defaults to physician-first
(`signal_extractor.DEFAULT_CONTACT_STRATEGY`). Role names belong in cartridges,
never in engine code (RULE 3).

| Field | Type | Effect |
|-------|------|--------|
| `positive_weight` | number | Desirability weight. Negative = friction (a `"yes"` is bad). |
| `not_found_weight` | number | Score delta when the signal is `not_found` (use negative to penalize an expected-but-absent signal). |
| `no_weight` | number | Score delta when a positive-weight signal is confirmed `"no"` (use negative to penalize a confirmed-absent must-have). Default 0. |
| `verification_required` | bool | When `not_found` (and not inferred), caps a would-be Bullseye at `"Needs Verification"`. |
| `required_for_bullseye` | bool | Must-have gate. When the signal is **not** confirmed `"yes"` and **not** inferred: a confirmed `"no"` caps the tier at `"Contender"`; a `not_found` caps at `"Needs Verification"`. Supersedes `verification_required` (also covers the `not_found` case), so a must-have signal needs only this flag. |
| `required_for_contender` | bool | Qualifier gate, **stricter** than `required_for_bullseye`. When the signal is **not** confirmed `"yes"` and **not** inferred (`not_found` or confirmed `"no"`, no reinforcement), the record is routed to `"Manual Review"` regardless of score or any other confirmed signal — out of the call queue entirely. Where `required_for_bullseye` only *caps* the tier (record stays callable), this *disqualifies* it from every call tier until an operator confirms. Runs **after** reinforcement, so a proxy signal that infers the target suppresses the gate. Sets `tier_cap_reason` (e.g. "Cash pay / self-pay not confirmed (required to qualify)"). Use for a primary qualifier no call should proceed without. |
| `cap_tier` | `"Contender"` \| `"Needs Verification"` | When the signal is `"yes"`, caps the tier at this ceiling regardless of score (e.g. confirmed hospital affiliation → `"Contender"`). |
| `floor_tier` | `"Contender"` \| `"Needs Verification"` | When the signal is `"yes"`, guarantees the record reaches at least this tier, bypassing the low-score Manual Review gate. Use for a confirmed primary qualifier that always warrants a call even on a thin overall score (e.g. confirmed cash-pay → at least Contender). |
| `exclude_if_yes` | bool | When the signal is confirmed `"yes"`, the record is immediately EXCLUDED via the normal exclusion path. The only signal-driven route to `Excluded` (e.g. telehealth-only practice). Default off. |
| `inhibited_by` | string `signal_id` | Used alongside `exclude_if_yes`. When the named signal is also `"yes"`, this exclusion is suppressed — for mutually-exclusive pairs where the companion signal logically invalidates the exclusion. |
| `reinforces` | string `signal_id` | When this signal is `"yes"` and the named target is `not_found`, the target is marked `state_inferred`. Must reference a signal_id in the same profile. |

**Reinforcement** lets an observable signal stand in for one rarely printed
verbatim. Example: listed elective/cosmetic procedures (`reinforces` cash pay)
imply cash pay even when "cash pay" never appears on the site. The inferred
target earns partial fit credit and **skips its verification gate** — a clearly
cash-pay practice is not parked on the watchlist over missing copy.
`_apply_reinforcement` runs after signal validation, before scoring.

### Derived signal fields (output)
- **`state_inferred`** (bool): set `true` by reinforcement when a `not_found`
  signal's presence was inferred. `false` for directly observed signals. Written
  to every signal object in the output.
- **`inferred_from`** (string): the `signal_id` of the reinforcing signal that
  triggered inference, when `state_inferred` is `true`. Empty string for all other
  signals. Surfaced in the UI as a tooltip on inferred signals so reps know the
  source of indirect evidence.
- **`not_found_reason`** (string): explains why a `not_found` signal could not be
  confirmed. `""` = LLM returned `not_found` after a successful crawl (may be
  genuinely absent); `"no_context"` = site had insufficient text, no LLM call
  made; `"evidence_gate"` = LLM claimed "yes" but evidence_text or source_url was
  missing, downgraded by the sourcing enforcement pass. Always `""` for `"yes"`
  and `"no"` signals. Shown in the UI under the NOT FOUND state badge so reps can
  distinguish "we looked and didn't find it" from "we couldn't look".

---

## Tier Ladder (`enrichment/exclusion_checker.py`)

CLEAR records are tiered by `_assign_tier` using a numeric rank so any
combination resolves by `min()`:

```
TIER_RANK = {"Excluded": 0, "Contender": 1, "Needs Verification": 2, "Bullseye": 3}
```

(The middle tier was renamed from "Watchlist" to "Contender". A legacy alias maps
any stale `"Watchlist"` value to `"Contender"` so frozen snapshots still resolve.)

0. **Evidence gate (first):** a CLEAR record is sent directly to `Manual Review`
   if either of these holds — it is kept out of the call queue and client exports
   until an operator acts:
   - No confirmed `"yes"` signal and nothing `state_inferred` (zero evidence), OR
   - `bullseye_score` is below `LOW_SCORE_MANUAL_REVIEW_THRESHOLD` (50) and no
     `"yes"` signal carries a `floor_tier` guarantee.
   (Not-enriched roster rows from `--ingest-only` are exempt.) The steps below
   apply only to records that clear this gate.
1. Start at `Bullseye` if `score >= bullseye_min`, else `Contender`.
2. Any `"yes"` signal with a `cap_tier` pulls the ceiling down (`min`). A `"yes"`
   signal with a `floor_tier` lifts the minimum rank past the low-score
   Manual Review threshold (e.g. a confirmed cash-pay signal guarantees at least
   Contender even when the overall score is thin).
3. **Source confidence gate**: `source_confidence = "limited"` or `"failed"`
   returns `Manual Review` — the site could not be reliably crawled; the operator
   should trigger a browser re-crawl or paste content before calling.
4. **Qualifier gate (`required_for_contender`)**: a signal flagged
   `required_for_contender` that is **not** `"yes"` and **not** `state_inferred`
   returns `Manual Review` outright (not merely a cap) — the record is held out of
   every call tier until the qualifier is confirmed. Runs after reinforcement, so
   a proxy signal that infers the target suppresses it. Stricter than step 5.
5. A `required_for_bullseye` signal that is **not** `"yes"` and **not** `state_inferred`
   caps the tier: confirmed `"no"` → `Contender`, `not_found` → `Needs Verification`.
   This is how "Bullseye = all must-haves confirmed present" is enforced.
6. A `verification_required` signal that is `not_found` **and not** `state_inferred`
   caps a would-be Bullseye at `Needs Verification`.
7. `cap_tier` constraints only ever pull down; `floor_tier` guarantees only ever
   lift the low-score floor — neither can override the score-based Bullseye
   threshold in step 1.

`"Excluded"` is never assigned here — it comes only from an exclusion rule (a
structural/LLM trigger, or a signal flagged `exclude_if_yes` that is confirmed
`"yes"`, both handled in `apply_exclusions`), and the invariant
`target_tier == "Excluded" iff exclusion_status == "EXCLUDED"` is enforced in
`enrichment/scorer.py`. Exported tiers: Bullseye / Needs Verification / Contender
/ Manual Review / Excluded. Analyst overrides in the API use the four call tiers.
**QC sign-off is required only for Bullseye and Contender** (the client-shipped
tiers); Needs Verification / Manual Review / Excluded never block run readiness —
operators audit them ad hoc (`pipeline-api/ui.py::_compute_readiness`).

**Confidence band (client-facing).** Every record carries a `confidence_band`
(`High` / `Moderate` / `Low`) derived from `confidence_score` (`constants.confidence_band_for_score`).
Client-facing surfaces show the **tier + band only** — the numeric `bullseye_score`,
`fit_signal_score`, and `confidence_score` stay in the internal JSON and the
operator QC view but are stripped from every client export (PDF, HTML report,
client CSVs, ZIP). Tier and band are orthogonal: a record can be `Bullseye` + `Low`.

`"Needs Verification"` and `"Manual Review"` appear in the client **Sales Handoff
HTML** (`handoff_renderer`) so the client sees the full screening picture — they
are dropped only when an analyst explicitly rejects them (`qc_status == "rejected"`).
They remain **excluded from the client CSVs** (`exports.is_approved` still gates
them out without an analyst override), so the call-ready CSV lists stay limited to
approved Bullseye/Contender plus all Excluded.

---

## Specialty Inference (`ingestion/outscraper_adapter.py::infer_specialty`)

`infer_specialty(type_raw, practice_name)` resolves specialty from the `type`
column first, then falls back to keywords in the practice name. Returns
`"Unknown"` only when neither matches. **"Unknown" is not a confirmed mismatch** —
the `wrong_specialty` exclusion does NOT fire on it (absent data ≠ wrong fit);
let scoring and signals decide instead. The `type` column is optional on import.

---

## Concurrency & Reliability

- **`io_concurrency`** (run_config, default 6): worker count for Steps 2–3
  (network I/O) via `ThreadPoolExecutor`.
- **`llm_concurrency`** (run_config, default 3): worker count for Step 4 Claude
  calls. Each worker retries on Anthropic 429 / rate-limit / overloaded with
  exponential backoff (5s, 10s, 20s, 40s).
- **Step 4 checkpoint/resume**: each completed record is appended to
  `step4_checkpoint.ndjson` under a `threading.Lock`. On restart the pipeline
  loads the checkpoint and skips already-processed records — a killed/crashed run
  resumes from where it stopped instead of re-spending on Claude. A corrupted
  final line (process killed mid-write) is skipped and that record re-processed.
  Per-record append is intentional crash-recovery; do not batch it.
- **Web extraction errors surface**: `_fetch_html` returns `(html, url, error)`;
  the error reason flows into `ExtractionResult` and the run log. Never discard it.

---

## Configuration

- **`config/run_config.json`** — generic placeholder template. Copy to
  `config/clients/<client_slug>/run_config.json` and fill in client-specific
  values before running. Running with the default config without customisation
  will produce placeholder output.
- **`config/icp_checklist.json`** — generic placeholder template (two skeleton
  signals). Copy to `config/clients/<client_slug>/icp_checklist.json` and replace
  with the client's real ICP signals.
- **`verify_near_miss_band`** (run_config, default `0`): legacy knob retained for backward compatibility. It is **not consumed** by the current verification design — verification is a separate, operator-triggered post-run pass (see Step 5) that targets `Needs Verification` records regardless of this value. Safe to leave at `0`.
- **`config/clients/obgyn_femasys/`** — reference implementation for the first
  engagement (Femasys / OBGYN). Pass these with `--config` and `--icp`:
  ```
  python pipeline.py --input data/input.csv --source outscraper \
    --config config/clients/obgyn_femasys/run_config.json \
    --icp    config/clients/obgyn_femasys/icp_checklist.json
  ```
- **`.env`** (never committed): API keys, `CLAUDE_MODEL`,
  `LLM_REQUEST_TIMEOUT_SECONDS`, `SESSION_SECRET_KEY`.
- **Browser re-crawl knobs** (env, for bot-gated sites): `PIPELINE_BROWSER_HEADFUL=1`
  runs a visible (headed) Chromium window, which clears Cloudflare / "Just a moment"
  JS challenges far more reliably than headless — set it on a machine with a display
  (the operator's laptop). `PIPELINE_BROWSER_CHALLENGE_WAIT_MS` (default 25000) is how
  long the crawler patiently waits, nudging like a human, for a challenge timer to
  clear before giving up. Both are read in `extraction/playwright_extractor.py`.

---

## `simulate_icp.py` — ICP Scoring Simulator

`simulate_icp.py` (repo root) is a thin CLI that runs the scoring engine with
hypothetical signal states — no LLM, no crawl, no side effects. It exists so the
API can shell out to preview how weight/flag choices affect tier assignment without
the API ever importing pipeline internals.

Input (stdin JSON):
```json
{
  "icp_signals": [...],
  "signal_states": {"S-01": {"state": "yes", "confidence": "high"}, ...},
  "bullseye_min": 90
}
```

Output (stdout JSON):
```json
{"bullseye_score": 94, "fit_signal_score": 96, "confidence_score": 90, "tier": "Bullseye", "tier_cap_reason": ""}
```

Called by `pipeline-api/ui.py::icp_simulate` via `subprocess.run`. Never called
directly by operators. Do not add persistent side effects (file writes, network
calls) to this script — it must remain stateless and fast.

---

## Testing

```
python -m pytest tests/ -q
```

All tests are **deterministic — no API calls, no HTTP**. Key suites in
`tests/test_pipeline.py`: signal normalization, scoring (`TestScoring`),
reinforcement (`TestReinforcement`), tier assignment (`TestTierAssignment`),
specialty inference, exclusions. `tests/test_verifier.py` covers the post-run GPT
verification pass (anchor-check, blind re-extraction, Evidence Vault rehydration)
and `tests/test_reextract.py` the re-extraction pass. `tests/test_runner.py` covers
in-place re-enrichment merge safety. Any scoring/tier/signal change must keep these
green and add coverage for new behavior. Lint touched files with `pyflakes`.

---

## Clean Code Standards

- snake_case functions (verb-first), PascalCase classes, SCREAMING_SNAKE_CASE constants.
- No `utils.py` / `helpers.py` / `common.py` dumping grounds.
- Every function gets at least a one-line docstring; one function, one responsibility.
- No magic numbers — route scoring constants through `enrichment/constants.py`.
- No commented-out code, no TODOs in merged code. Delete dead code; git is the history.
- No wildcard imports. Fail loudly: never silently swallow exceptions in a step
  (the per-record catch-all in Step 4 records the error and marks the record
  failed — it does not hide it).
