# Discovery / Market Radar — Architecture

This document describes how Discovery (Market Radar) fits into the platform, the
component boundaries, and the rules that keep discovery, enrichment, and the
registry cleanly separated.

## The four-stage mental model

```
Discovery   →   Enrichment   →   Dashboard   →   Registry
(finds)         (scores)         (reviews)        (remembers)
```

- **Discovery finds candidates.** It compares an uploaded Outscraper CSV against
  the Master Practice Registry and classifies each row. No scoring, no crawl, no
  LLM, no budget.
- **Enrichment scores candidates.** The existing 8-step pipeline crawls each
  practice's public website footprint and scores it against the ICP.
- **The dashboard reviews candidates.** Operators QC enriched records, override
  tiers, and export client deliverables.
- **The registry remembers candidates.** The Master Practice Registry is the
  platform's cross-run memory of every practice it has seen.

Each stage hands off explicitly; nothing chains automatically into spend or into
registry mutation.

## Run types

Every run directory under `<OUTPUT_RUNS_PATH>/<run_id>/` carries a `run_type` in
its `status.json`:

- `discovery` — a Market Radar comparison run. Produces `discovery_results.json`,
  `discovery_results.csv`, `discovery_run_log.json`, `updated_registry_preview.json`.
- `enrichment` — a normal pipeline run (default; runs created before this field
  existed are treated as `enrichment`). Produces `enriched_targets.json`.

The enrichment dashboard run list (`runs.list_runs`) **excludes** `discovery`
runs so the two surfaces never mix. Discovery runs are listed separately on the
Market Radar landing page.

## Component boundaries

```
BEMI dashboard (React, demo)
        │ HTTP
pipeline-api (FastAPI, ./pipeline-api/)
        │ subprocess + shared /output/runs/
enrichment pipeline (this repo's CLI: pipeline.py, enrichment/, discovery/ package)
```

- **pipeline-api invokes the engine through a subprocess + filesystem boundary
  only.** It never imports enrichment-pipeline internals (no importing
  `enrichment/…`, no importing the repo-root `discovery` package). Enrichment runs
  via `pipeline.py` as a subprocess; discovery runs via `discovery_cli.py` as a
  subprocess (the same pattern as `simulate_icp.py`).
- **No re-scoring or re-implementing engine logic in the API.** All scoring,
  signal, tier, and exclusion logic lives in the engine.

### Why discovery has a subprocess CLI

The discovery engine lives in the repo-root `discovery` package. The API spawns
`discovery_cli.py` (cwd = repo root) so the engine runs in a clean process. This
keeps the API/engine boundary identical to enrichment and sidesteps a module-name
collision: a `discovery` module name conflict existed with the now-deleted
`pipeline-api/discovery.py` legacy shim.

## The persistent discovery-runs flow (current)

API endpoints (`pipeline-api/discovery_runs.py`):

- `POST /discovery-runs` — upload CSV → create run dir → run `discovery_cli.py` →
  write the four artifacts + `status.json` (run_type `discovery`).
- `GET /discovery-runs/{run_id}` — status + summary.
- `GET /discovery-runs/{run_id}/results` — full classified record list.
- `POST /discovery-runs/{run_id}/send-to-enrichment` — see "Handoff" below.

UI (`pipeline-api/ui.py`, server-rendered): `/discovery` landing,
`/discovery/runs/{id}` results, `/discovery/runs/{id}/send`.

> The older `pipeline-api/discovery.py` (in-memory `compute_delta` flow) has
> been deleted. The persistent discovery flow described above replaced it entirely.
> See `pipeline-api/MATCHING_NOTES.md`.

## Matching

Discovery and registry update use the **same** match priority (highest to lowest):

1. `google_place_id` (a.k.a. `place_id`)
2. normalized website domain
3. normalized phone (last 10 digits, only when ≥ 10)
4. normalized practice name + normalized address

**NPI is a supporting identifier only — never a primary/first-class match key.**
It is stored on registry entries but is not used to decide whether two practices
are the same.

If different identifiers point to **different** existing entries, the match is
**ambiguous**: registry update rejects it and logs it as `needs_manual_merge`
rather than guessing. (There is no merge UI yet — by design.)

> The API-side normalization + priority logic lives in one place:
> `pipeline-api/practice_matching.py`. `registry_update.py` imports from it
> directly and cannot drift. A separate engine-side copy in `discovery/matcher.py`
> exists across the subprocess boundary (which prevents sharing).
> `tests/test_matching_parity.py` guards both: it asserts `registry_update.py`
> uses the `practice_matching` functions by identity, and it now also compares the
> engine copy's `normalize_*` functions against `practice_matching.py` so the
> engine copy is no longer unguarded.
> See `pipeline-api/MATCHING_NOTES.md`.

## Handoff: discovery → enrichment

`POST /discovery-runs/{run_id}/send-to-enrichment` (and the equivalent UI action):

1. Selects NEW/CHANGED records (POSSIBLE_DUPLICATE only via explicit IDs; KNOWN
   and INSUFFICIENT_DATA are never sent).
2. Writes `enrichment_handoff.csv` into the discovery run folder (Outscraper
   columns the pipeline already ingests + traceability columns:
   `discovery_run_id`, `discovery_status`, `discovery_reason`,
   `matched_existing_record_id`, `changed_fields`).
3. Creates a normal enrichment run through the existing runner
   (`orchestrate_ingest`) — **ingest-first**: the run lands in `ingested` status.
4. Stamps `source_discovery_run_id` / `source_discovery_selection_count` /
   `source_discovery_selection_mode` onto the new run's `status.json`.

**The handoff creates an ingested enrichment run; it does not auto-spend.** The
operator triggers **Enrich All** separately from the run page when ready. The
handoff never mutates the registry and never pre-registers rows.

## The Master Practice Registry

- **Location:** `master_practice_registry.json` is a **sibling of the runs
  directory** (`<OUTPUT_RUNS_PATH>/../master_practice_registry.json`), not inside
  any single run. It is platform-level memory shared across all runs.
- **Discovery never mutates it.** A discovery run only writes a *preview*
  (`updated_registry_preview.json`) inside its own run folder.
- **Enrichment completion never mutates it.** There is no automatic upsert.
- **The only writer is the explicit registry-update action** — see
  [`registry_lifecycle.md`](registry_lifecycle.md).

## Config validation (two layers)

- **API preflight validation** runs before a subprocess is spawned
  (`projects.validate_project_config` + `icp_profiles.get_icp_profile`). It
  rejects bad config early, before any budget is committed.
- **CLI validation is the final authority before spend** — `pipeline.py` calls
  `enrichment/config_validator.py` at the start of every run, after loading the
  frozen config/ICP snapshots and before any crawl or LLM call.
- A signal `source_type` of `static_lookup` (or any other `source_type`) **fails
  loudly** in both layers — it is not implemented, and there is no silent
  fallback.

## `google_place_id` on the enrichment path

`google_place_id` is preserved **end-to-end** through enrichment and is the
priority-1 registry match key. `ingestion/outscraper_adapter.py` maps it on
ingest, `enrichment/scorer.py` defaults it so it is always present in
`enriched_targets.json`, and `registry_update.py` reads it
(`rec.get("google_place_id")`) and persists it as the first-priority match key.
A registry update run from an enrichment run therefore matches/merges by
place_id → domain → phone → name+address, the same priority as discovery.
`tests/test_lifecycle.py` asserts place_id survives the handoff and matches.
See `pipeline-api/MATCHING_NOTES.md`.

## Developer guardrails

- **No direct imports across the enrichment boundary.** pipeline-api talks to the
  engine via subprocess + the shared `/output/runs/` filesystem only.
- **No auto-spend from discovery.** Handoff is ingest-first; Enrich All is a
  separate operator action.
- **No auto-registry mutation.** The registry changes only via the explicit
  update action — never on discovery, handoff, or enrichment completion.
- **No NPI-primary matching.** NPI is supporting only.
- **No silent `static_lookup` fallback.** Unimplemented `source_type` values fail
  validation, loudly, in both the API and the CLI.
