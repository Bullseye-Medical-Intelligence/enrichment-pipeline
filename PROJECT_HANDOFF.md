# Bullseye Medical Intelligence — Project Handoff
*As of 2026-06-16*

---

## What Bullseye Is

An AI-powered commercial intelligence platform. Clients upload a list of prospects; Bullseye's pipeline crawls each practice's website, runs Claude to extract ICP signals, scores each practice, and tiers them (Bullseye / Contender / Needs Verification / Excluded). Output is a sales-ready brief telling reps exactly who to call, why, and how.

The core insight: a sales rep with limited dials needs to know *confidence of a fast commercial close*, not just a lead score. Bullseye answers that with a tier + confidence band backed by evidence quotes.

---

## System Architecture

```
bullseyemedical.ai (Hostinger static site)
        │  Marketing / intelligence blog
BEMI Dashboard (React, demo only)
        │  HTTP
BEMI Pipeline API (FastAPI, pipeline-api/)  ← operator UI, job management
        │  subprocess + shared /output/runs/
BEMI Enrichment Pipeline (pipeline.py)      ← all scoring/signal logic lives here
```

Three repos, but enrichment pipeline + API share one repo. API never imports pipeline internals — subprocess only.

---

## The Enrichment Pipeline (8 Steps)

1. **Ingest** — normalize CSV, dedup, NPI enrichment (opt-in), customer suppression (opt-in), structural pre-filter (wrong specialty / outside geography → Excluded before any spend)
2. **URL validate** — reachability check
3. **Web extract** — crawl homepage + ranked subpages; auto browser-retry for bot-blocked sites; manual content paste option
4. **Signal extract (Claude)** — per-record LLM extraction against ICP signals; thin-context records skip LLM and get `not_found` on all signals
5. **Verification (GPT)** — opt-in; near-miss records always verified; Bullseye records only if low-confidence signals present; thin-context always skipped
6. **Exclusion check** — hard rules + ICP `exclude_if_yes` signals → Excluded
7. **Scoring validation** — clamp, enforce tier invariants, apply tier caps/floors
8. **Output** — `enriched_targets.json`, CSV, `run_log.json` (atomic writes)

Key config knobs: `io_concurrency` (default 10), `llm_concurrency` (default 3), `verify_near_miss_band` (default 0, recommend 10 for production), checkpoint/resume on Step 4.

---

## Market Radar / Discovery Workflow

Built 2026-06-15. Sits upstream of enrichment. Full operator flow:

1. Upload Outscraper CSV → `/discovery`
2. Delta detection against `master_practice_registry.json` — classifies each row: **NEW / CHANGED / KNOWN / POSSIBLE_DUPLICATE / INSUFFICIENT_DATA**
3. Operator selects records (NEW, NEW+CHANGED, or individual rows) → sends to enrichment as ingest-first run (no LLM spend yet)
4. Operator triggers enrichment from the existing dashboard
5. Explicit "Update Registry" action at `/dashboard/{id}/registry-update` — conservative upsert, idempotent, logs change_history

**Registry matching priority:** `google_place_id` → domain → phone → name+address. Ambiguous matches are rejected and flagged `needs_manual_merge` (no merge UI yet).

---

## ICP Signal System

Signals defined per client in `config/clients/{slug}/icp_checklist.json`. Engine is fully generic — no hardcoded specialty/client logic anywhere.

Key signal flags:
- `required_for_bullseye` — must-have gate; not confirmed = cap at Needs Verification or Contender
- `cap_tier` / `floor_tier` — ceiling/floor overrides when signal is yes
- `exclude_if_yes` — confirmed yes → Excluded
- `reinforces` — one signal inferring another (e.g. listed elective procedures → cash pay inferred)
- `verification_required` — not_found caps a Bullseye at Needs Verification

Scoring: `fit_signal_score` = share of achievable positive weight actually captured, confidence-weighted. `bullseye_score` = blend of fit + confidence. Score answers: *how confident should a rep be in a fast commercial close?*

---

## Clients (2026-06-16)

| Client | Product | Status |
|--------|---------|--------|
| Femasys | FemaSeed (intratubal insemination) | **On hold** — waiting for VP to return ICP v8 validation |
| Ormco | Orthodontic (TBD) | **Pending** — sales rep expected to have direction by Tuesday |
| Angel Aligner | Aligners | **Unknown** — Rajiv reached out, no update |
| Neurolief | Neuromodulation | **Dormant** — focused on VA channel, out of scope |

### Femasys Context
FemaSeed: ITI via balloon catheter, ~24% pregnancy rate vs ~7% IUI. Target: OB/GYN practices doing IUI. FemVue (fallopian tube assessment via ultrasound) is another Femasys product — practices that have it are warm leads.

ICP v8: 11 signals, cash-pay proxy via reinforcement (elective procedures → cash pay). S-ICP-010 (in-house ultrasound) likely to become `required_for_bullseye` — awaiting clinical team confirmation.

Atlanta brief is the immediate deliverable: 20-30 metro Atlanta OBGYNs → CEO-facing "Market Report" format (cover page, tier summary, top highlights). Not the operator QC layout.

---

## Open Strategic Question: ICP Proposal Model

Should Bullseye propose a hypothesis ICP for new clients and let them tweak it, rather than waiting for clients to define it from scratch?

**Answer: Yes — propose and tweak is the right model.** Clients react better to concrete proposals than blank slates. Waiting for clients to generate ICPs produces vague or stalled conversations (seen in Ormco meeting). Lead with a 7-10 signal hypothesis grounded in their product, explain the reasoning, let them correct it. That's not assuming — that's doing the work they hired us to do.

---

## Website (bullseyemedical.ai)

Static site on Hostinger shared hosting. FTP via port 21 (SFTP port 22 blocked from both cloud and local).

**Logo replacement scripts (run locally):**
- `upload_site.py` — upload specific files
- `update_site_logos.py` — batch: download all HTML, replace old fan-geometry SVG logos with new bullseye ring mark (`/assets/bullseye-mark-ink.svg`), re-upload

After any logo script update: `git pull && python update_site_logos.py`

**Intelligence blog** (`/intelligence/`) — geo-focused market analysis pages, same Hostinger host.

---

## Real Run History

- 2 enrichment runs completed on ~20 Femasys records each
- 1 Bullseye found — practice with existing FemVue relationship (existing Femasys customer)
- Early runs used hypothesized weights before validated client ICP
- Bot-blocking is the primary operational friction; all configs at `io_concurrency: 10`
- Browser retry (Playwright/Chromium) available for bot-gated sites

---

## Immediate Next Steps

1. **Femasys** — wait for VP to return ICP v8; then pull 20-30 Atlanta OBGYNs and run
2. **Ormco** — sales rep direction expected Tuesday; propose hypothesis ICP once product is clearer
3. **Atlanta brief format** — CEO-facing Market Report layout needs to be built before external delivery
4. **FemVue placeholder signal** — add at weight 0 to Femasys ICP, ready to activate
5. **S-ICP-010 ultrasound gate** — awaiting Femasys clinical confirmation

**Pending decisions (do not act without Rajiv's input):**
- S-ICP-010 → `required_for_bullseye`
- FemVue signal weight
- `verify_near_miss_band` for Atlanta run
- CEO brief format design

---

## Key File Locations

| What | Where |
|------|-------|
| Pipeline entry point | `pipeline.py` |
| Scoring + signals | `enrichment/signal_extractor.py`, `enrichment/scorer.py` |
| Scoring constants | `enrichment/constants.py` |
| Exclusion / tier logic | `enrichment/exclusion_checker.py` |
| Market Radar / discovery | `pipeline-api/discovery.py`, `pipeline-api/discovery_runs.py` |
| Registry update | `pipeline-api/registry_update.py` |
| API routes | `pipeline-api/ui.py`, `pipeline-api/main.py` |
| Brief publisher (SFTP) | `pipeline-api/brief_publisher.py` |
| Femasys ICP | `config/clients/obgyn_femasys/icp_checklist.json` |
| Femasys run config | `config/clients/obgyn_femasys/run_config.json` |
| ICP simulator | `simulate_icp.py` |
| Website upload | `upload_site.py`, `update_site_logos.py` |
| Tests | `tests/` (756+ deterministic tests, no API calls) |
