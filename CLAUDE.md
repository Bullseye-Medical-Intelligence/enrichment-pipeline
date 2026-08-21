# CLAUDE.md — BEMI Enrichment Pipeline

Every session working in this repo begins by reading this file and `PIPELINE.md`.
If this file and the code conflict, fix the code — not this file. `PIPELINE.md`
is the canonical spec for the output schema and step contracts; this file is the
working guide for how the pipeline behaves and the rules a session must hold.

---

## Who Builds This

Claude Code is the development team. There is no separate group of engineers to
hand work to, and that has consequences a session must hold:

- **There is no one to escalate to.** "Flag this for the team" resolves to
  nobody. Either finish the change, or write it up in `docs/review-backlog.md`
  with the trigger that should reopen it. A TODO addressed to a future engineer
  is addressed to no one.
- **There is no separate QA pass.** Nothing between a change and a client
  deliverable but the session that made it. A change ships with tests that pin
  the behaviour it claims, or it is not finished.
- **The operator is the product owner, not the code reviewer.** They decide what
  the product should do and rule on judgement calls; they should not have to
  catch a defect in a diff. Verify the work before presenting it, and say
  plainly what was verified by execution versus assumed.
- **Nobody else remembers why.** Institutional memory lives in this file,
  `PIPELINE.md`, `docs/review-backlog.md`, commit messages, and the code
  comments explaining decisions that look wrong and are not. A reason left
  unwritten is a reason lost, and the next session will re-derive it wrongly.

`docs/code-review-brief.md` exists because outside reviewers have no access to
any of that context. Hand it over before any external review.

---

## Communication Style

Responses must be brief business and product-focused summaries. Do not break down
code unless explicitly asked. Discuss everything from a business solution and
product development standpoint. Be efficient with tokens and elaborate only when
the user explicitly asks for more detail.

---

## What This Repo Is

The **enrichment pipeline**: a Python CLI (`pipeline.py`) that turns a raw
prospect list (Outscraper, Apify Google Places, or manual CSV) into scored,
tiered, sales-ready
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
   **Step 1d — Practice-location consolidation**: collapses provider rows into practice locations (`ingestion/consolidator.py`), so the billable unit is the location, not the row. Pass 1 merges on an additive score from two candidate paths — a building block (zip5 + street) and a **contact block (phone + registrable domain)**; Pass 2 links locations sharing a registrable domain into groups without ever merging them. Runs in `--ingest-only` too. Output contract in `PIPELINE.md`.
   - **The contact block catches one practice behind one front desk.** Two offices of a group in different towns share no address key, so they were never compared — nothing rejected the merge, the comparison never happened, and the practice shipped as two billable accounts with identical signals and double the LLM spend. Both halves of the key are required: phone alone would compare everything behind an answering service, domain alone every clinic in a health system. Together they score exactly `MERGE_THRESHOLD`, so the path adds merges and never review work. **Umbrella domains are excluded here** even though Pass 1 counts them as merge evidence in the address block — that exemption exists only because street and ZIP had already pinned the location. Blocks over `MAX_CONTACT_BLOCK` (12) rows are a shared line, skipped and counted. Off via `consolidation.contact_blocking: false`.
   - **Within one building, a differing unit is a hard veto and a matching one is worth +3** (`SCORE_UNIT_MATCH`), so address + suite merges on its own. A suite is one leased unit with one front door; the standard is "would a rep knock once or twice", not "same legal entity". Settled by ruling 13 sampled decisions, all 13 merge.
   - **The veto is scoped to the building, and that qualifier is load-bearing.** A suite number answers "which door in this building" and nothing else, so it only carries information when both rows stand at the same street and ZIP. Comparing Suite 300 in one town against Suite 1201 in another and calling it a conflict applies a same-building rule across two buildings — and since every multi-office practice has different suite numbers at its different offices, an unscoped veto fired on precisely the pairs the contact block exists to recognise, before they were ever scored. The unit is therefore keyed as `(zip5, street, unit)` in both the pairwise gate and the cluster-level guard (`units_collide`); a unit on a row with no street or ZIP is not anchored to a building and carries no veto.
   - **Review queue** holds only what evidence cannot settle: `corroborated` (a second field agrees or conflicts), `phone_absent` (one side has no phone, so nothing is known), and `unit_gate_block` (scored a merge, stopped by the unit veto — mechanical, shown in its own bucket). Sharing a building alone is never a question.
   - **Rulings are additive**, written to the API's `reviews.json` overlay and taking effect on the next run, because consolidation happens at ingest.

   **Structural pre-filter** — after Steps 1b–1d, `check_structural_exclusions` drops records that are wrong specialty, outside geography, matched by an NPI taxonomy rule, or carrying **no website URL** (`no_web_presence`, configurable), before any crawl or LLM spend. Pre-excluded records skip Steps 2–6 and rejoin at Step 6 as Excluded. Runs in `--ingest-only` too, so a roster count never sheds accounts at the next step. `no_web_presence` is decided here because a row with no URL cannot be crawled, so no later step can learn anything about it; `apply_exclusions` keeps its own copy as the backstop for the manual-content path, where page text can arrive without a URL. **Keep it off for registry-sourced lists** — NPPES carries no website field, so the rule would exclude every row (the canary catches this, but knowing beats being warned).
   **Exclusion canary** — when exclusions remove more than `max_structural_exclusion_share` of the roster (default 0.90, `enrichment/constants.py`), the pre-filter HALTS the run with a diagnostic naming the rules that fired; nothing has been spent yet, and a pre-filter that empties a list is a config or mapping defect until proven otherwise. At Step 6 the same check reports instead of halting — the crawl and LLM spend already happened — and blocks client-package download and brief publishing until an operator acknowledges it with a reason.
2. **URL validate** — reachability check (`extraction/url_validator.py`), `io_concurrency` workers.
3. **Web extract** — crawl homepage + subpages (`extraction/web_extractor.py`), `io_concurrency` workers. Every internal page is a candidate except blog/news/legal/auth/commerce noise (`SKIP_PATH_SEGMENTS`); pages are crawled evidence-first (keyword-ranked) until the `MAX_COMBINED_CHARS` text budget is full or the `MAX_CRAWL_PAGES` (20) / `MAX_CRAWL_SECONDS` (30s) bounds are hit. No low per-site page cap.
   **Auto browser-retry (Step 3b, opt-in)** — with `--auto-browser-retry` (CLI) or `auto_browser_retry: true` (run_config), records that come back blocked/thin from the standard crawler (`source_confidence` limited/failed, or under `MIN_CONTEXT_CHARS` of text) are re-crawled once with headless Chromium before Step 4. `_records_needing_browser_retry` targets only the blocked subset, so most records keep the fast HTTP path; no-op when the whole run is already `--playwright`. This recovers bot-gated sites automatically instead of waiting for an operator to click "Re-crawl with Browser". Exposed in the API as a checkbox on "Enrich All".
   **Manual content (`--manual-content-path`)** — for a single site behind a hard CAPTCHA wall the crawler cannot clear, the operator captures the page in their own browser (Save Page As .html, or copy the visible text) and supplies it. The flag bypasses Steps 2-3 entirely: `_load_manual_content` loads that file into every record's `_context_text` (HTML converted with the crawler's `_extract_text_from_html`, plain text used as-is), wraps each page as `[Source: <record website_url>]\n<text>` (the same shape a live crawl produces, so Step 4's http(s) evidence gate can accept a `"yes"` — without a real source header the model emits `source_url "not_found"` and every `"yes"` is force-downgraded), sets `source_confidence = "partial"`, and Step 4 runs on it unchanged. Exposed in the API as a per-record "Paste site content" form (`orchestrate_manual_content_recrawl`).
4. **Signal extract (Claude)** — per-record LLM signal extraction + scoring (`enrichment/signal_extractor.py`), `llm_concurrency` workers, checkpointed. Records with fewer than `MIN_CONTEXT_CHARS` of website text skip the LLM call; all signals are set to `not_found` and `enrichment_status = "partial"` to prevent hallucinations from thin context.
5. **Verification (GPT)** — NOT an inline step in `pipeline.py`. Verification runs as a **separate, operator-triggered post-run pass** (`verify_run.py` → `enrichment/verifier.py::run_verification_pass`), invoked from the dashboard (`POST /dashboard/{run_id}/verify`). It operates on a completed run's `enriched_targets.json` and targets only `Needs Verification` records. Two phases per record: (a) **anchor-check** (free) — confirm each `"yes"` signal's `evidence_text` appears verbatim in the page text; any anchor failure skips GPT (compromised evidence). (b) **blind GPT re-extraction** (survivors) — GPT independently re-extracts the unconfirmed gating signals under the **same mention-vs-offering attribution standard as extraction** (a physician bio / educational blog / referral-out / testimonial / historical mention cannot support a `"yes"`, so it never re-blesses evidence the extraction attribution guard downgraded). Results are written as an additive `verification` object (`recommended_action`: promote / hold / disqualify); signals, tier, and score are never overwritten, and a promote still requires an operator override. The pass is idempotent (records with `verification.verified_at` are skipped). Because `_context_text` is stripped from output, the pass **rehydrates page text from the Evidence Vault** (`output/evidence_writer.py::read_record_context_text`) before anchor-check / GPT.
6. **Exclusion check** — hard + configurable rules, tier assignment (`enrichment/exclusion_checker.py`).
7. **Scoring validation** — clamp, validate, enforce invariants (`enrichment/scorer.py`).
8. **Output** — write JSON, CSV, run_log.json (`output/`, atomic writes).

### `--ingest-only` (roster pass, no spend)
`--ingest-only` runs Step 1 → Step 1b (NPI enrichment) → Step 1c (customer
suppression) → Step 1d (consolidation) → the structural pre-filter → Step 8
(output), then exits before any crawl or LLM call (`_finalize_ingest_only`).
Customer-suppressed and structurally-excluded records are both written as
EXCLUDED. **Structural exclusions are decided here, not deferred**: every one of
them (`wrong_specialty`, `outside_geography`, NPI taxonomy rules, and
`no_web_presence`) is decided from the ingested row alone, so no crawl can
change the verdict, and an operator reads a billable count off this roster — a
count that sheds accounts at the next step is the count a client argues with.
Signal-driven exclusions are NOT evaluated; those need a crawl. The canary
REPORTS here rather than halting: an operator ran this to see their list, so an
unmapped website column must produce a loud roster, never no roster.
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
  Weights are **fit-only (`1.0` / `0.0`)**: an additive confidence term put a
  ~36-point floor under any record with one high-confidence signal, so
  "confidently a bad fit" read as ~40. Confidence qualifies fit — through the
  per-signal `SIGNAL_CONFIDENCE_CREDIT` discount, the client-facing
  `confidence_band`, and `fit_confidence_status` — it never adds points of its
  own. For an all-high-confidence record the Bullseye bar is unchanged by this
  (reaching 90 required fit ≥ 90 under the old blend too).

### Rep call brief (`signal_extractor.py::_build_call_brief`)
Every record carries a `call_brief` object. Grounded fields are **derived from the
signals** (no extra LLM call): `top_evidence`, `missing_to_verify`,
`disqualifier_risk`, and `why_contact`. Four fields come from
the LLM: `opening_line`, `likely_objection`, `discovery_question`, and
`hours_of_operation` (office hours stated on the website, or empty string).

`missing_to_verify` mirrors the `not_found` → `"Needs Verification"` caps in
`exclusion_checker._assign_tier`, which fire on **either** `verification_required`
**or** `required_for_bullseye` (not inferred). Both flags must stay in that filter:
`required_for_bullseye` supersedes `verification_required`, so a must-have signal
carrying only that flag would otherwise cap a record at Needs Verification while
showing the rep nothing to verify. A confirmed `"no"` is a known absence (it caps
at Contender) and belongs in `disqualifier_risk`, not here.

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

**Profile-level optional field — `product_context`** (string, ≤700 chars,
validated in `pipeline-api/icp_profiles.py`): client-approved product copy that
lets `sales_angle` and the call brief angle hooks toward the product. Injected
**fenced into the generation section only** (`signal_extractor._PRODUCT_CONTEXT_BLOCK`)
with three rules the block carries itself: it is the ONLY permitted source of
product facts (no added claims, pricing, or regulatory status); every hook still
leads with something observed on the practice's site; and it must never
influence signal evaluation. The model composes, it never sources — product
facts come from this human-approved block, practice facts from the website.
When unset, nothing is injected and hooks stay practice-evidence-only (the
pre-feature behavior). Authored from the client's own materials and approved by
them before a client-facing run; the AI builder may draft it, never ship it.
Product names belong in cartridges, never in engine code (RULE 3).

| Field | Type | Effect |
|-------|------|--------|
| `positive_weight` | number | Desirability weight. Negative = friction (a `"yes"` is bad). |
| `not_found_weight` | number | Score delta when the signal is `not_found` (use negative to penalize an expected-but-absent signal). |
| `no_weight` | number | Score delta when a positive-weight signal is confirmed `"no"` (use negative to penalize a confirmed-absent must-have). Default 0. |
| `verification_required` | bool | When `not_found` (and not inferred), caps a would-be Bullseye at `"Needs Verification"`. |
| `required_for_bullseye` | bool | The must-have definition of Bullseye. When **all** flagged signals are confirmed `"yes"` (not inferred), the record is promoted to Bullseye regardless of the score threshold (evidence floor and every cap still bind); `bullseye_min` remains the gate only for ICPs with no must-haves. When the signal is **not** confirmed `"yes"` and **not** inferred: a confirmed `"no"` caps the tier at `"Contender"`; a `not_found` caps at `"Needs Verification"`. Supersedes `verification_required` (also covers the `not_found` case), so a must-have signal needs only this flag. |
| `required_for_contender` | bool | Qualifier gate, **stricter** than `required_for_bullseye`. When the signal is **not** confirmed `"yes"` and **not** inferred (`not_found` or confirmed `"no"`, no reinforcement), the record is routed to `"Manual Review"` regardless of score or any other confirmed signal — out of the call queue entirely. Where `required_for_bullseye` only *caps* the tier (record stays callable), this *disqualifies* it from every call tier until an operator confirms. Runs **after** reinforcement, so a proxy signal that infers the target suppresses the gate. Sets `tier_cap_reason` (e.g. "Cash pay / self-pay not confirmed (required to qualify)"). Use for a primary qualifier no call should proceed without. |
| `cap_tier` | `"Contender"` \| `"Needs Verification"` | When the signal is `"yes"`, caps the tier at this ceiling regardless of score (e.g. confirmed hospital affiliation → `"Contender"`). |
| `floor_tier` | `"Contender"` \| `"Needs Verification"` | When the signal is `"yes"`, guarantees the record reaches at least this tier, bypassing the low-score Manual Review gate. Use for a confirmed primary qualifier that always warrants a call even on a thin overall score (e.g. confirmed cash-pay → at least Contender). |
| `exclude_if_yes` | bool | When the signal is confirmed `"yes"`, the record is immediately EXCLUDED via the normal exclusion path. The only signal-driven route to `Excluded` (e.g. telehealth-only practice). Default off. |
| `inhibited_by` | string `signal_id` | Used alongside `exclude_if_yes`. When the named signal is also `"yes"`, this exclusion is suppressed — for mutually-exclusive pairs where the companion signal logically invalidates the exclusion. |
| `reinforces` | string `signal_id` | When this signal is `"yes"` and the named target is `not_found`, the target is marked `state_inferred`. Must reference a signal_id in the same profile. |
| `column_label` | string (≤24 chars) | **API presentation only** — surfaces this signal as an at-a-glance column on the operator dashboard (results table + Contact Queue). Signals sharing a label roll up into one column showing the strongest state (yes > inferred > no > not_found). Read from the LIVE ICP so existing runs gain columns immediately; the pipeline engine ignores it (no effect on scoring or output). |

**Reinforcement** lets an observable signal stand in for one rarely printed
verbatim. Example: listed elective/cosmetic procedures (`reinforces` cash pay)
imply cash pay even when "cash pay" never appears on the site. The inferred
target earns partial fit credit and **skips its verification gate** — a clearly
cash-pay practice is not parked in Needs Verification over missing copy.
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
  missing, downgraded by the sourcing enforcement pass; `"attribution_gate"` =
  LLM claimed "yes" but the evidence was a bio / blog / referral-out /
  testimonial / historical mention rather than a service the practice offers
  (attribution guard, prompt v4 — generic, applies to all cartridges). Always
  `""` for `"yes"` and `"no"` signals. Shown in the UI under the NOT FOUND state
  badge so reps can distinguish "we looked and didn't find it" from "we couldn't
  look".

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
1. Start at `Bullseye` when **every** `required_for_bullseye` signal is confirmed
   `"yes"` (direct confirmation only — an inferred must-have never promotes — and
   the record cleared the evidence floor in step 0), or when
   `score >= bullseye_min` (the only Bullseye gate for an ICP that defines no
   must-haves). Otherwise start at `Contender`. Every cap below still pulls a
   promoted record down.
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
   Together with the promotion in step 1, this enforces "Bullseye = all must-haves
   confirmed present" in both directions.
6. A `verification_required` signal that is `not_found` **and not** `state_inferred`
   caps a would-be Bullseye at `Needs Verification`.
7. `cap_tier` constraints only ever pull down; `floor_tier` guarantees only ever
   lift the low-score floor — neither can create a Bullseye on its own; the
   must-have promotion or the score threshold in step 1 are the only entries.

`"Excluded"` is never assigned here — it comes only from an exclusion rule (a
structural/LLM trigger, or a signal flagged `exclude_if_yes` that is confirmed
`"yes"`, both handled in `apply_exclusions`), and the invariant
`target_tier == "Excluded" iff exclusion_status == "EXCLUDED"` is enforced in
`enrichment/scorer.py`. Exported tiers: Bullseye / Needs Verification / Contender
/ Manual Review / Excluded. Analyst overrides in the API use the four call tiers.
**QC sign-off blocks client-package readiness for Bullseye only.** Contenders ship
in client deliverables (CSV + Sales Handoff) unless an analyst rejects them; Needs
Verification / Manual Review / Excluded never block readiness either — operators
audit them ad hoc (`pipeline-api/ui.py::_compute_readiness`).

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
  The checkpoint is **scoped to its inputs and deleted on success**: its first
  line stamps a fingerprint of the config + ICP (by content), the input CSV,
  and the crawl mode (`use_playwright` / `auto_browser_retry` / manual-content
  file identities — re-running the same list with `--playwright` must
  re-extract, not resume from thin HTTP-crawl results), and a checkpoint whose
  fingerprint does not match the current run is discarded rather than reused. Both guards matter because record ids are deterministic
  content hashes and `--output-dir` defaults to a fixed `./output`: without them,
  editing the ICP and re-running the same command restored the previous run's
  signals — scored against the OLD weights, with zero Claude calls — and
  presented it as a fresh run.
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
  clear before giving up. `PIPELINE_BROWSER_PROXY` routes all browser-crawl traffic
  through a proxy (`http://user:pass@gate.provider.com:7000` — the residential-proxy
  vendor shape); bot protection fingerprints the crawler's IP as much as its browser,
  so a residential proxy is the second lever after headful mode when re-crawls must
  run from a server. A malformed value is ignored (crawl proceeds direct). All three
  are read in `extraction/playwright_extractor.py`; the escalation ladder is
  headful → residential proxy → per-record "Paste site content".
- **Browser crawl wall-clock bound**: one browser crawl runs against a single
  deadline (`PLAYWRIGHT_MAX_CRAWL_SECONDS`, 60s, `extraction/playwright_extractor.py`).
  Every step was already bounded individually — navigation timeout, challenge
  budget, per-subpage timeout — but nothing bounded their sum, so one bot-gated
  domain could hold an `io_concurrency` worker for minutes. `_crawl_budget_seconds`
  raises the ceiling when the homepage legitimately needs longer (navigation +
  settle + the FULL challenge budget), so raising `PIPELINE_BROWSER_CHALLENGE_WAIT_MS`
  or `request_timeout_seconds` widens the deadline rather than being silently
  truncated by it — the ceiling governs discretionary subpage depth, never the one
  page that must be fetched. Reaching it is not an error: pages already captured
  are returned. `request_timeout_seconds` is the per-navigation timeout on every
  browser path, including the `recrawl_run.py` post-run pass.

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

<!-- Decision 2026-08-20: Adopted after three numbers in the consolidation
workstream were retracted post-verification. Quarantined from style rules —
this is a correctness contract about numbers that leave the building. -->

## Measurement Provenance

A number's authority comes from where it was produced, not from how carefully it
was computed. Three figures in the consolidation workstream were retracted after
verification, all from the same cause — an analysis script reading intermediate
or superseded state:

| retracted | corrected | cause |
|-----------|-----------|-------|
| 2,415 review pairs | 607 | row-level edges counted as location pairs |
| 13.7% shared surname | 5.0% | script matched `"M.D."` as a surname |
| 174 same-suite pairs | 89 | script read a superseded candidate list |

### RULE M1: Commercial numbers come from pipeline output.
Any number that will be quoted to a client, appear in a deliverable, or justify a
pricing decision must be read from the pipeline's own output — `run_log.json`,
the run manifest, `enriched_targets.json`, or a documented engine counter. Never
from an analysis script written for the occasion. If the engine does not already
emit the number, add the counter to the engine and read it back.

### RULE M2: One-off scripts produce PROVISIONAL numbers.
A number that can only be produced by an ad-hoc script is labelled **PROVISIONAL**
when reported, together with what would have to exist for it to be authoritative
("provisional — becomes authoritative once the engine emits `review_reasons` in
the consolidation block"). Working material is fine; it just has to be labelled.

### RULE M3: A retraction names what consumed the wrong number.
When correcting a figure, state every surface that consumed the old one before it
was caught. The 2,415 reached a dashboard badge, the internal run manifest, and
the CLI console summary. Retracting the number without naming its consumers
leaves the wrong value sitting in three places.

### The consolidation rates, and what may be quoted

> **SUPERSEDED — do not quote until re-measured.** The contact block (phone +
> domain) added a second candidate path to Pass 1 after these runs, and it can
> only ever merge more, never less. Both figures below are therefore floors for
> current engine behaviour, not current behaviour. Re-run both lists through
> `--ingest-only` and read `run_log.json` before any of this reaches a client or
> a price. `diagnose_consolidation.py --compare` reports the delta with the flag
> off and on over the same rows, which is the controlled form (RULE M4).

Measured 2026-08-20 from `--ingest-only` runs, read from `run_log.json`, with the
contact block **absent**:

| list | gross | post-exclusion |
|------|-------|----------------|
| TX, NPPES registry, OBGYN (`RUN-20260820-053114`) | **29.6%** | **30.8%** |
| NorCal, Places scrape, psychiatry (`RUN-20260820-053306`) | 36.1% | 35.8% |

Quote the OBGYN registry row. **Do not quote a source-type delta**: the two lists
differ in source *and* specialty, so the 6.5-point gap is confounded and the
magnitude is unmeasured for OBGYN. The direction is mechanistically supported
(NPPES has no website field, so Pass 2 is inert and domain never contributes to a
Pass 1 merge; a registry row is one provider at one address where a scraper emits
one practice from several listings), but direction is all it supports. The run
that would close it — an OBGYN scrape for one metro through the same path — is
logged in `docs/review-backlog.md`, not run.

### RULE M4: A measurement of a code change is a controlled experiment.
To attribute a delta to a change, hold everything else constant and vary only the
changed function. Two numbers taken from different tree states are not a
before/after — they are two observations with an unknown number of causes.

---

## Open Findings

`docs/review-backlog.md` carries the verified, ranked findings from the latest
adversarial code review — **P1-1 through P2-13 are resolved** (2026-08-17, one
commit per finding with pinning tests); only its P3 debt items (fix
opportunistically, when already editing that file) and previously-deferred items
remain open. `docs/data-boundary-model.md` holds the client/project data-boundary
analysis awaiting business decisions. Consult both before starting remediation
work — do not rediscover them.

`docs/code-review-brief.md` is the one-page orientation for an OUTSIDE reviewer
with no repo context: the rules that look arbitrary but are load-bearing, the
decisions that look like bugs and are not, and what is genuinely worth attacking.
Hand it over before any external review. It exists because three review passes
produced twelve findings of which two were real — the other ten named a module
that does not exist, described behavior the code contradicts, or proposed a fix
a written rule forbids, and every one was disprovable in a single command.

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

  <!-- Decision 2026-06-24: Adopted Verification Gates anti-fabrication policy. Trigger-based, not confidence-based. Quarantined from style/build-freeze rules — this is a truthfulness contract, not a preference. -->

## Verification Gates — Anti-Fabrication Policy

These gates are **trigger-based, not confidence-based**. The gate fires on the category of action or claim, even when the claim sounds obvious, familiar, or highly confident. Memory, prior sessions, summaries, and unstated assumptions are not sources.

### GATE 0 — Destructive Actions
Any action that may delete, overwrite, drop, publish, send, charge, expose, or materially alter data triggers this gate — including deleting/overwriting files, dropping tables, running migrations, modifying production config, sending emails/messages, publishing content, changing permissions, bulk updates, mutating API calls, and commands using `--force`, `--delete`, `--overwrite`, `rm`, `drop`, `truncate`, or `reset`.

Rule: Use the safest path before execution.
1. Prefer dry-run, preview, diff, backup, or staged output.
2. If risk remains, ask for explicit confirmation.
3. Never perform destructive actions silently.

### GATE 1 — File State
Any claim about what a file, repo, dataset, config, spreadsheet, document, or database currently contains triggers this gate — values, rows, columns, formulas, filenames, code, config, structure, schemas, whether something exists/is missing/changed.

Rule: Inspect the relevant source in the current session before making the claim. For large sources, use targeted inspection (search, grep, file tree, line ranges, sampled rows, schema before full data, diffs) rather than blind full reads. If the source can't be inspected:
> Not verified — I could not inspect the source in this session.

Then do not describe its contents as fact.

### GATE 2 — External Behavior
Any claim about how a library, API, platform, tool, product, model, pricing page, marketplace, or external system behaves triggers this gate — limits, syntax, pricing, defaults, auth, permissions, compatibility, supported/deprecated features, version differences, current behavior, whether something can or cannot be done. Time-sensitive or version-like language (latest, current, now supports, as of 2026, v2, 4.6, SDK/API/model version names) always triggers it.

Rule: Check current official documentation or a current primary source when available, and verify at least one of: page date, version number, API version, release-note date, changelog entry, official support article, or version-matched source docs.
> Source checked, but version/date is unclear. (if no clear date/version)
> Not verified against current docs — based on available context only. (if docs can't be checked)

Do not present external behavior as fact unless checked this session.

### GATE 3 — Execution and State Mutation
Any claim that an action succeeded, changed something, ran correctly, passed, failed, exported, uploaded, synced, or fixed an issue triggers this gate — tests passed, build succeeded, file created/updated, export worked, formulas correct, links work, issue fixed, migration/import completed, "this will run."

Rule: Only make the claim if the action was actually performed and verified this session (exit code, test/command output, file-existence check, diff, exported-file inspection, DB query, log review, API response, reopened output file). Do not convert an intended action into a completed result.
> Untested — I have not run or validated this. (if not performed)
> Attempted, but not independently verified. (if attempted, unverified)

### GATE 4 — Source Labeling
For factual claims in a build, sales-facing, client-facing, research, financial, legal, technical, or operational context, the source must be recoverable. A factual claim must be one of:
1. Quoted/cited from a named source inspected this session.
2. Returned by a search, fetch, tool, API call, or DB query this session.
3. Produced by an actual test, command, calculation, or validation this session.
4. Explicitly labeled as inference:
   > Inference from [source/context] — not independently verified.

When a fact isn't in context and can't be retrieved:
> Not in context — the specific source to check would be [source].

Do not bridge gaps with plausible filler. An unsourced factual claim is a defect, not a draft.

### High-Signal Trigger Words
These often hide a verifiable state, source, or execution claim — check whether a gate applies before using them in a factual context: contains, shows, passed, failed, created, updated, deleted, exported, synced, imported, fixed, verified.

### Scope
Applies to: file/code/repo/database state, test and execution results, signal weights and scoring, architecture decisions, API/tool/platform behavior, pricing/limits/compatibility/version behavior, prospect- or client-facing deliverables, and anything shipped, cited, sold, implemented, or relied upon operationally.

Out of scope: casual brainstorming, clearly labeled opinion, creative writing, rough ideation. Gating everything trains both user and model to ignore the gates.

### Enforcement
If a gate is violated, name it ("Gate 1 violation — I described the file from memory without inspecting it"), then correct the answer. The correction is cheap. Silent fabrication is what costs.
