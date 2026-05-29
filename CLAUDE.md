# CLAUDE.md — BEMI Enrichment Pipeline

Every session working in this repo begins by reading this file and `PIPELINE.md`.
If this file and the code conflict, fix the code — not this file. `PIPELINE.md`
is the canonical spec for the output schema and step contracts; this file is the
working guide for how the pipeline behaves and the rules a session must hold.

---

## What This Repo Is

The **enrichment pipeline**: a Python CLI (`pipeline.py`) that turns a raw
prospect list (Outscraper or manual CSV) into scored, tiered, sales-ready
account intelligence. It runs 8 steps, calls Claude for signal extraction and
GPT for verification, and writes immutable JSON/CSV output plus a run log.

This is one of three repos:

```
BEMI-dashboard (React, demo only)
        │  HTTP
BEMI-pipeline-api (FastAPI, ./pipeline-api/)  ← spawns this CLI as a subprocess
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
2. **URL validate** — reachability check (`extraction/url_validator.py`), `io_concurrency` workers.
3. **Web extract** — crawl homepage + relevant subpages (`extraction/web_extractor.py`), `io_concurrency` workers.
4. **Signal extract (Claude)** — per-record LLM signal extraction + scoring (`enrichment/signal_extractor.py`), `llm_concurrency` workers, checkpointed.
5. **Verification (GPT)** — Bullseye-tier records only (`enrichment/verifier.py`).
6. **Exclusion check** — hard + configurable rules, tier assignment (`enrichment/exclusion_checker.py`).
7. **Scoring validation** — clamp, validate, enforce invariants (`enrichment/scorer.py`).
8. **Output** — write JSON, CSV, run_log.json (`output/`, atomic writes).

---

## Scoring Model (commercial-fit confidence)

The score answers one question for a sales rep with limited dials: **how
confident should I be that this is a fast commercial close?** It is NOT a tally —
matching more signals does not mean a higher score.

`enrichment/signal_extractor.py::_calculate_scores`:

- **`fit_signal_score`** = the share of the *achievable* positive weight a
  practice actually captures, scaled 0–100.
  - `max_positive` = sum of every positive (desirable) `positive_weight` — the ideal.
  - A confirmed `"yes"` desirable signal adds its full weight.
  - An **inferred** signal (`state_inferred`, see reinforcement) adds
    `INFERENCE_CREDIT` of its weight (partial credit for indirect evidence).
  - A `"not_found"` desirable signal applies its `not_found_weight` penalty (usually ≤ 0).
  - A confirmed **friction** signal (negative weight, `"yes"`) subtracts its weight.
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
verification gate), `disqualifier_risk`, and `why_contact`. Three prep lines come
from the LLM: `opening_line`, `likely_objection`, `discovery_question`. The empty
shape lives in `constants.py::empty_call_brief`; `scorer.py` defaults it so the
field is always present. **Contact Priority** in the UI is a display relabel of
`target_tier` (`record_adapter.contact_priority`), never a stored field.

---

## ICP Signal Fields

Defined per signal in `config/icp_checklist.json` / ICP profiles. Required:
`signal_id`, `signal_label`, `prompt_instruction`, `positive_weight`. Optional
(all default to off), validated in `pipeline-api/icp_profiles.py`:

| Field | Type | Effect |
|-------|------|--------|
| `positive_weight` | number | Desirability weight. Negative = friction (a `"yes"` is bad). |
| `not_found_weight` | number | Score delta when the signal is `not_found` (use negative to penalize an expected-but-absent signal). |
| `verification_required` | bool | When `not_found` (and not inferred), caps a would-be Bullseye at `"Needs Verification"`. |
| `cap_tier` | `"Watchlist"` \| `"Needs Verification"` | When the signal is `"yes"`, caps the tier at this ceiling regardless of score (e.g. confirmed hospital affiliation → `"Watchlist"`). |
| `reinforces` | string `signal_id` | When this signal is `"yes"` and the named target is `not_found`, the target is marked `state_inferred`. Must reference a signal_id in the same profile. |

**Reinforcement** lets an observable signal stand in for one rarely printed
verbatim. Example: listed elective/cosmetic procedures (`reinforces` cash pay)
imply cash pay even when "cash pay" never appears on the site. The inferred
target earns partial fit credit and **skips its verification gate** — a clearly
cash-pay practice is not parked on the watchlist over missing copy.
`_apply_reinforcement` runs after signal validation, before scoring.

### Derived signal field (output)
- **`state_inferred`** (bool): set `true` by reinforcement when a `not_found`
  signal's presence was inferred. `false` for directly observed signals. Written
  to every signal object in the output.

---

## Tier Ladder (`enrichment/exclusion_checker.py`)

CLEAR records are tiered by `_assign_tier` using a numeric rank so any
combination resolves by `min()`:

```
TIER_RANK = {"Excluded": 0, "Watchlist": 1, "Needs Verification": 2, "Bullseye": 3}
```

1. Start at `Bullseye` if `score >= bullseye_min`, else `Watchlist`.
2. Any `"yes"` signal with a `cap_tier` pulls the ceiling down (`min`).
3. A `verification_required` signal that is `not_found` **and not** `state_inferred`
   caps a would-be Bullseye at `Needs Verification`.
4. `cap_tier` beats verification; verification never lifts a low-score Watchlist.

`"Excluded"` is never assigned here — it comes only from an exclusion rule, and
the invariant `target_tier == "Excluded" iff exclusion_status == "EXCLUDED"` is
enforced in `enrichment/scorer.py`. Exported tiers: Bullseye / Needs Verification
/ Watchlist / Excluded. (Analyst overrides in the API may add Strong/Warm/Cold;
that is a UI concern, not the pipeline's.)

`"Needs Verification"` is UI-visible but **not** included in client exports until
an analyst confirms it with an override.

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

- **`config/run_config.json`** (committed, no secrets): client name, target
  specialty/geography, `active_exclusion_rules`, `bullseye_min_score`, crawl
  limits, `io_concurrency`, `llm_concurrency`, `subpage_keywords`.
- **`config/icp_checklist.json`** (committed, no secrets): the signal checklist —
  see ICP Signal Fields above.
- **`.env`** (never committed): API keys, `CLAUDE_MODEL`,
  `LLM_REQUEST_TIMEOUT_SECONDS`, `SESSION_SECRET_KEY`.

---

## Testing

```
python -m pytest tests/ -q
```

All tests are **deterministic — no API calls, no HTTP**. Key suites in
`tests/test_pipeline.py`: signal normalization, scoring (`TestScoring`),
reinforcement (`TestReinforcement`), tier assignment (`TestTierAssignment`),
specialty inference, exclusions. Any scoring/tier/signal change must keep these
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
