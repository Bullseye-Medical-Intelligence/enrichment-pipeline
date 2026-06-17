# Ormco Spark Cartridge Contract — Reference Spec

**Status:** Built from runbook (ormco-spark-cartridge-v1 runbook). The original
`ormco-spark-cartridge-v1.md` contract document was not available during build.
This file documents what was built and the design decisions made from the runbook.

---

## Client

**Ormco / Spark Aligners** — Orthodontic bracket and aligner system.
Target audience: Independent orthodontic specialists currently using Damon bracket
system or open-ecosystem digital workflows.

## Tier Map

| Tier | Criteria |
|------|----------|
| **Bullseye** | damon_system_listed=yes AND invisalign_exclusive=no AND non-DSO-locked AND ≥2 scored sophistication signals (open scanner + CBCT) |
| **Contender** | Invisalign-led displacement candidate (invisalign_exclusive=yes caps at Contender) OR Invisalign Platinum-or-higher (S-OS-019=yes caps at Contender) OR airway/MARPE niche (floor_tier=Contender when either fires) |
| **Existing Account / Retention** | spark_already_listed=yes — operator routes out of acquisition queue; no score cap, no exclusion |
| **Excluded** | Any Section 6 gate (see below) |

No new tiers. Engine produces Bullseye / Contender / Excluded / Needs Verification / Manual Review.

---

## Section 3 — Signal Fields

### Competitive Incumbency Layer (scored)

| Signal ID | Field | Weight | Notes |
|-----------|-------|--------|-------|
| S-OS-001 | damon_system_listed | +40 | required_for_bullseye |
| S-OS-002 | invisalign_provider_tier (any level) | 0 | contextual / rep-prep; captures named tier (Bronze→Diamond Plus) from text + image alt/title/filename; feeds S-OS-019 |
| S-OS-003 | invisalign_exclusive | -20 | friction + cap_tier: Contender + floor_tier: Contender (pins displacement candidates exactly at Contender) |
| S-OS-004 | spark_already_listed | 0 | routing flag only, no score/tier effect |
| S-OS-005 | custom_bracket_competitor_listed | -5 | minor friction |
| S-OS-006 | competitor_aligner_listed (non-Invisalign) | 0 | contextual |
| S-OS-019 | invisalign_platinum_or_higher | -15 | friction + cap_tier: Contender; yes only for Platinum/Platinum Plus/Elite/Diamond/Diamond Plus/Top 1% |

### Scanner Signals (competitive context, scored)

| Signal ID | Field | Weight | Notes |
|-----------|-------|--------|-------|
| S-OS-007 | itero_scanner_listed | -15 | friction; inhibited_by S-OS-008 |
| S-OS-008 | open_scanner_listed (non-iTero) | +25 | 3Shape/Medit/other |

### Fit / Sophistication Layer

| Signal ID | Field | Weight | Notes |
|-----------|-------|--------|-------|
| S-OS-009 | cbct_listed | +25 | scored |
| S-OS-010 | digital_workflow_listed | 0 | contextual |
| S-OS-011 | intraoral_scanner_listed (any brand) | 0 | contextual |
| S-OS-012 | indirect_bonding_listed | 0 | contextual |
| S-OS-013 | marpe_listed | 0 | floor_tier: Contender |
| S-OS-014 | airway_focus_listed | 0 | floor_tier: Contender |
| S-OS-015 | complex_treatment_types_listed | 0 | contextual |

### Display / Rep-Prep (not scored, informs call_brief)

| Signal ID | Field | Weight | Notes |
|-----------|-------|--------|-------|
| S-OS-016 | provider_education_program_listed | 0 | CE/residency flag for rep |

Note: `hours_of_operation` lives natively in `call_brief.hours_of_operation`
(LLM-extracted) and does not need a signal entry.

---

## Section 6 — Exclusion Gates

### Hard Gates (exclude_if_yes, score capped at 40)

| Signal ID | Gate | Notes |
|-----------|------|-------|
| S-OS-017 | dso_competitor_locked | DSO with exclusive rival aligner contract |
| S-OS-018 | gp_or_pediatric_only | No orthodontic services (orthodontist_only=true default) |

### Chassis-Inherited Gates (active_exclusion_rules)

- `hospital_owned`
- `health_system_affiliated`
- `practice_closed`
- `no_web_presence`

### Routing Flag (NOT a gate)

- `spark_already_listed` (S-OS-004) — weight=0, no cap, no exclude_if_yes.
  Routes to Existing Account / Retention as an operator action, never auto-excluded.

### Section 5 — State Mandate

Not applicable for orthodontics. No mandate scoring weight in this cartridge.
All signals are practice-behavior based; no state-regulatory signals included.

---

## Scoring Math

max_positive = 40 (damon) + 25 (open_scanner) + 25 (cbct) = **90**

| Scenario | Achieved | Fit | Bullseye Score | Tier |
|----------|----------|-----|----------------|------|
| damon + open_scanner + cbct (all high) | 90 | 100% | ~96 | Bullseye |
| damon + cbct only | 65 | 72% | ~78 | Contender |
| damon + open_scanner only | 65 | 72% | ~78 | Contender |
| damon only | 40 | 44% | ~60 | Contender |
| damon + open_scanner + cbct + invisalign_platinum=yes | 75 | 83% | ~86 | Contender (S-OS-019 cap) |
| invisalign_exclusive=yes (any score) | any | any | any | Contender (cap) |
| marpe=yes OR airway=yes | — | — | — | ≥ Contender (floor) |
| dso_competitor_locked=yes | — | — | capped 40 | Excluded |

bullseye_min_score = 85 (lowered from 90 on 2026-06-17 to restore headroom after
the invisalign presence penalty was retired; see changelog). max_positive is
unchanged at 90 — S-OS-002 (now weight 0) and S-OS-019 (-15) are non-positive and
do not enter max_positive. S-OS-019 friction lands a max-sophistication Platinum+
practice in the displacement lane and the cap_tier holds it at Contender.

> **Engine note (floor_tier):** the `floor_tier` guarantees on S-OS-003 / S-OS-013 /
> S-OS-014 depend on `signal_extractor` carrying the flag onto enriched signals.
> That carry-through was added to the engine (it previously copied only `cap_tier`),
> so these floors now fire in live runs, matching the simulator. Verified by the 6
> synthetic cases and `tests/test_pipeline.py::test_floor_tier_carried_on_all_signal_paths`.

---

## Sales Angle Templates

Templates encoded in signal `note` fields (the only injection point available
in the current engine). They appear in the LLM system prompt as bracketed
annotations and are gated by the signal integrity check (cleared when
`top_evidence` is empty — no confirmed yes signals).

Three integrity-gated templates:

1. **Cross-sell** (fires when damon_system_listed=yes):
   "You're already seeing Damon outcomes — Spark gives you bracket-to-aligner
   continuity without changing your workflow or stocking a second brand."

2. **Displacement** (fires when invisalign_exclusive=yes):
   "Your Invisalign contract doesn't own your aligner chair. Spark's per-case
   fee structure lets you A/B test without renegotiating."

3. **Niche** (fires when marpe_listed=yes or airway_focus_listed=yes):
   "MARPE cases are your hardest bracket workflow — Spark's precision series
   is built for that load." / "Airway-focused ortho is exactly the case type
   where digital-first brackets pay off fastest."

---

## Format Conflicts / Deviations

The following contract fields required decomposition or reinterpretation because
the engine only supports scalar yes/no/not_found signals:

| Contract Field | Type | Resolution |
|----------------|------|------------|
| aligner_brands_listed | array | Decomposed: invisalign_provider_tier (S-OS-002, contextual) + invisalign_platinum_or_higher (S-OS-019, friction), spark_already_listed (S-OS-004), competitor_aligner_listed (S-OS-006) |
| scanner_brand | enum (iTero/3Shape/Medit/other/not_found) | Decomposed: itero_scanner_listed (S-OS-007, friction), open_scanner_listed (S-OS-008, positive) |
| treatment_types_listed | array | Single presence signal: complex_treatment_types_listed (S-OS-015) |
| provider_education_program | array | Single presence signal: provider_education_program_listed (S-OS-016) |
| hours_of_operation | display field | Native call_brief.hours_of_operation (LLM-extracted) — no signal needed |
| sales_angle templates | injection slot | Encoded in signal note fields — no template injection slot exists in engine |

No chassis schema changes. No engine changes. All fields expressed within
existing cartridge format.

---

## Run Command

```bash
python pipeline.py --input data/ormco_input.csv --source outscraper \
  --config config/clients/ormco-spark/run_config.json \
  --icp    config/clients/ormco-spark/icp_checklist.json
```

## Dependency Note

This cartridge is built. It does not source Austin. The asset bank is empty for
ortho, so a real brief requires pre-ingesting ZIP codes 78664 and 78746 first.
Build → validate → ingest → brief. Do not run a live brief off an empty pool.

## Changelog

2026-06-17: invisalign_listed presence penalty (-10) retired as non-discriminating. Replaced with S-OS-019 invisalign_platinum_or_higher friction (-15, cap Contender) + S-OS-002 repurposed to weight-0 tier capture. bullseye_min 90->85 to restore headroom. iTero unchanged. Badge confirm = text + alt + manual locator; vision OCR deferred.
2026-06-17d: Enabled auto_browser_retry in run_config (was relying on unchecked UI default). Root cause of 100% Manual Review: requests crawler returns <150 chars on JS-rendered/Cloudflare ortho sites, LLM skipped, all signals no_context. Browser-retry forces Chromium render on thin records. Prerequisite: Chromium installed locally via playwright.
