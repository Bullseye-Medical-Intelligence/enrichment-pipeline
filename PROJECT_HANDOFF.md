# Bullseye Medical Intelligence — Project Handoff
*As of 2026-06-21*

---

## What Bullseye Is

An AI-powered commercial intelligence platform. Clients upload a list of prospects; Bullseye's pipeline crawls each practice's website, runs Claude to extract ICP signals, scores each practice, and tiers them (Bullseye / Contender / Needs Verification / Excluded). Output is a sales-ready brief telling reps exactly who to call, why, and how.

The core insight: a sales rep with limited dials needs to know *confidence of a fast commercial close*, not just a lead score. Bullseye answers that with a tier + confidence band backed by evidence quotes.

---

## System Architecture

```
bullseyemedical.ai (Hostinger static site)  ← Marketing / intelligence blog
BEMI Pipeline API (FastAPI, pipeline-api/)  ← operator UI, job management
        │  subprocess + shared /output/runs/
BEMI Enrichment Pipeline (pipeline.py)      ← all scoring/signal logic lives here
```

Two repos (marketing site + this one); the enrichment pipeline + API share this repo. API never imports pipeline internals — subprocess only.

---

## Changes — 2026-06-21 session

- **(a)** Removed the deprecated/unused demo React/Vite dashboard ("bemi" repo) from the root docs. The production operator UI is the server-rendered FastAPI UI in `pipeline-api/`.
- **(b)** Consolidated the run-results page header into dropdown menus: **Reprocess ▾** (Re-crawl Blocked Sites, Preview Rescore, Apply Rescore, Re-extract Signals, Re-check Suppression, Re-run), **Export ▾** (Full CSV, Run Manifest), **Audit ▾** (Cartridge, Check Evidence Links), plus standalone Update Registry and ← All Runs. New bulk multi-select bottom bar: **Re-enrich (N)** | **Re-crawl with Browser (N)** | **Review All ▾** (Accept / Reject / Reset, opens upward) | **Clear**. New route `POST /dashboard/{run_id}/bulk-review` sets QC status on selected records — writes `reviews.json` only.
- **(c)** New generic ICP signal flag **`required_for_contender`** (bool, default false): when the flagged signal is not confirmed "yes" and not inferred via reinforcement, the record is routed to Manual Review regardless of score — stricter than `required_for_bullseye`, which only caps the tier.
- **(d)** Merged the two Femasys cartridges into one national **`obgyn-femasys-v11`** (9 signals): adopts the Michigan service-context prompt fixes; S-ICP-006 now matches "FemVue or FemaSeed"; cash-pay S-ICP-007 is a weight-20 `required_for_contender` must-have with elective service line S-ICP-008 as its reinforcing proxy; `target_geography` cleared (all states). The contraception signal (S-ICP-011) was later dropped as not a fertility-intent signal.
- **(e)** Deleted concept clients (cartridges + seeds + tests): Michigan Femasys variant (`obgyn_femasys_mi`), Angel Aligner (`ortho_angel_v1`), Neurolief (`neurolief_prolivrx`), and Right at Home (`right_at_home_south_oc`). Ormco (`ormco-spark`) was kept.
- **(f)** Dashboard fix: the "+N pts" badge now renders only for confirmed "yes" signals, not for inferred `not_found` signals.
- **(g)** Brief publishing: Hostinger FTP-21 fallback enabled via `.env` (`HOSTINGER_ALLOW_FTP_FALLBACK=1`) — config only.

---

## The Enrichment Pipeline (8 Steps)

1. **Ingest** — normalize CSV, dedup, NPI enrichment (opt-in), customer suppression (opt-in), structural pre-filter (wrong specialty / outside geography → Excluded before any spend)
2. **URL validate** — reachability check
3. **Web extract** — crawl homepage + ranked subpages; auto browser-retry for bot-blocked sites; manual content paste option
4. **Signal extract (Claude)** — per-record LLM extraction against ICP signals; thin-context records skip LLM and get `not_found` on all signals
5. **Verification (GPT)** — NOT inline. A separate operator-triggered post-run pass (`verify_run.py` → `enrichment/verifier.py`) over a completed run, targeting only `Needs Verification` records. Anchor-check (free) then blind GPT re-extraction; results are additive (`verification` object), never overwrite tier/score/signals. Does not run during the main `pipeline.py` flow.
6. **Exclusion check** — hard rules + ICP `exclude_if_yes` signals → Excluded
7. **Scoring validation** — clamp, enforce tier invariants, apply tier caps/floors
8. **Output** — `enriched_targets.json`, CSV, `run_log.json` (atomic writes)

Key config knobs: `io_concurrency` (default 6; operators may raise it for throughput), `llm_concurrency` (default 3), checkpoint/resume on Step 4. (`verify_near_miss_band` is a retained no-op — not consumed by the current verification design.)

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

## Clients (2026-06-21)

| Client | Product | Status |
|--------|---------|--------|
| Femasys | FemaSeed (intratubal insemination) | **Active** — national ICP v11 cartridge; Atlanta brief is the next deliverable |
| Ormco | Orthodontic (TBD) | **Pending** — sales rep expected to have direction by Tuesday |

Only two client cartridges remain in the repo: `obgyn_femasys` and `ormco-spark`.
Angel Aligner (`ortho_angel_v1`), Neurolief (`neurolief_prolivrx`), and Right at Home
(`right_at_home_south_oc`) — along with the Michigan Femasys variant
(`obgyn_femasys_mi`) — were removed this session as concept/demo cartridges.

### Femasys Context
FemaSeed: ITI via balloon catheter, ~24% pregnancy rate vs ~7% IUI. Target: OB/GYN practices doing IUI. FemVue (fallopian tube assessment via ultrasound) is another Femasys product — practices that have it are warm leads.

ICP v11: 9 signals (`obgyn-femasys-v11`). This is a national merge — the former
national cartridge and the Michigan variant were combined into one, adopting the
Michigan service-context prompt fixes (a named service, a dedicated service/fertility
page, a "we treat/offer" statement, or a provider specialty now count, not only
verbatim phrases; editorial/blog mentions still excluded). The geography limit was
removed in run_config (`target_geography: []` = all states). Cash-pay (S-ICP-007) is
now a **`required_for_contender`** must-have at weight 20: a practice with no confirmed
cash-pay/self-pay capability is held in Manual Review until an operator confirms it —
unless the elective/aesthetic service line (S-ICP-008) reinforces it as a cash-pay
proxy. The FemVue signal (S-ICP-006) now also matches FemaSeed (the strongest warm
lead — the practice already uses the product being sold). S-ICP-010 (in-house
ultrasound) remains reserved, pending clinical confirmation.

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
- Bot-blocking is the primary operational friction; `io_concurrency` defaults to 6 (operators may raise it for throughput)
- Browser retry (Playwright/Chromium) available for bot-gated sites

---

## Immediate Next Steps

1. **Femasys** — pull 20-30 Atlanta OBGYNs and run against the national v11 cartridge
2. **Ormco** — sales rep direction expected Tuesday; propose hypothesis ICP once product is clearer
3. **Atlanta brief format** — CEO-facing Market Report layout (cover page, tier summary, top highlights) still needs to be built before external delivery. This is the one open design item.

---

## Key File Locations

| What | Where |
|------|-------|
| Pipeline entry point | `pipeline.py` |
| Scoring + signals | `enrichment/signal_extractor.py`, `enrichment/scorer.py` |
| Scoring constants | `enrichment/constants.py` |
| Exclusion / tier logic | `enrichment/exclusion_checker.py` |
| Market Radar / discovery (engine) | repo-root `discovery/` package + `discovery_cli.py` |
| Market Radar / discovery (API) | `pipeline-api/discovery_runs.py`, `pipeline-api/registry_update.py`, `pipeline-api/practice_matching.py` |
| Registry update | `pipeline-api/registry_update.py` |
| API routes | `pipeline-api/ui.py`, `pipeline-api/main.py` |
| Brief publisher (SFTP) | `pipeline-api/brief_publisher.py` |
| Femasys ICP | `config/clients/obgyn_femasys/icp_checklist.json` |
| Femasys run config | `config/clients/obgyn_femasys/run_config.json` |
| ICP simulator | `simulate_icp.py` |
| Website upload | `upload_site.py`, `update_site_logos.py` |
| Tests | `tests/` (756+ deterministic tests, no API calls) |
