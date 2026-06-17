# Master Practice Registry — Lifecycle

The Master Practice Registry is the platform's cross-run memory: one record per
practice the platform has seen, used to detect what is new or changed in a fresh
market pull. This document describes where it lives, what is in it, and the strict
rules about when it may change.

## Location

`master_practice_registry.json` is a **sibling of the runs directory**:

```
<OUTPUT_RUNS_PATH>/../master_practice_registry.json
<OUTPUT_RUNS_PATH>/<run_id>/...        ← individual runs
```

It is **not** stored inside any single run directory — it is platform-level state
shared across every discovery and enrichment run.

## The one and only writer

The registry is mutated through **exactly one** explicit path:

- `POST /enrichment-runs/{run_id}/update-registry` (API), or
- the equivalent **Update Registry** UI action on a completed enrichment run.

Nothing else writes it. In particular:

- **Discovery does not mutate the registry.** A discovery run only writes
  `updated_registry_preview.json` inside its own run folder — a preview of what
  the registry *would* look like, never the real file.
- **The discovery → enrichment handoff does not mutate the registry.** It writes
  `enrichment_handoff.csv` and creates an ingested run; that's all.
- **Enrichment completion does not mutate the registry.** There is no automatic
  upsert when a run finishes. (An earlier auto-upsert was removed; registry
  updates are explicit-only by design.)

## What an explicit update does

`registry_update.py::update_registry_from_run`:

1. Confirms the run is `run_type == enrichment` and `status == complete`.
2. Loads `enriched_targets.json`.
3. Selects records by `selection_mode` (`bullseye_only` / `clear_only` /
   `all_reviewable`) or explicit record IDs.
4. Applies rejection rules: `failed` always rejected; `EXCLUDED` rejected unless
   `include_excluded`; `needs_review` rejected unless `include_needs_review`;
   records missing minimum identity rejected.
5. Matches each record against existing entries by the standard priority
   (place_id → domain → phone → name+address; NPI supporting only). Ambiguous
   matches (different identifiers → different entries) are rejected and logged as
   `needs_manual_merge`.
6. **Inserts** new entries; **updates** matched entries, appending to
   `change_history` only when a meaningful contact/identity field actually changed.
7. Writes the registry **atomically** (temp file + replace) and writes an
   auditable `registry_update_log.json` into the run folder.
8. Stamps `registry_updated_at` / `registry_update_count` /
   `registry_update_log_path` onto the run's `status.json`.

### Idempotency
Re-running the same update does **not** duplicate `change_history` — history grows
only when a tracked field's value actually changes. Timestamps (`last_seen_at`,
`last_reviewed_at`) and current values (tier, score) refresh each time; that is
expected and is not "history".

### Atomicity / failure safety
The registry is built in memory and written with a single atomic replace. If the
write fails, the temp file is removed and the original registry is left intact and
valid — a crash mid-update never corrupts platform memory.

## Registry record shape (key fields)

Identity / matching: `practice_registry_id`, `google_place_id`, `npi`,
`website_domain`, `phone_digits`, `name_normalized`, `address_normalized`.

Display / contact: `practice_name`, `website_url`, `phone`, `address_full`,
`address_city`, `address_state`, `address_zip`, `specialty`.

Provenance / state: `first_seen_at`, `last_seen_at`, `first_discovery_run_id`,
`last_discovery_run_id`, `last_enrichment_run_id`, `last_reviewed_at`,
`current_tier`, `bullseye_score`, `exclusion_status`, `enrichment_status`,
`source_pipeline_version`, `evidence_path`, `change_history[]`.

## `google_place_id` on the enrichment path

`google_place_id` flows through enrichment end-to-end and is the priority-1
registry match key. `ingestion/outscraper_adapter.py` maps it on ingest,
`enrichment/scorer.py` defaults it so it is always present in
`enriched_targets.json`, and `registry_update.py` reads it and persists it as the
first-priority match key. Registry entries created via an explicit update from
enrichment therefore match by place_id → domain → phone → name+address, the same
priority as discovery. See `pipeline-api/MATCHING_NOTES.md`.

## Developer guardrails

- **No auto-registry mutation** — discovery, handoff, and enrichment completion
  must never write the registry.
- **No NPI-primary matching** — NPI is a supporting identifier only.
- **Ambiguous matches are never auto-merged** — they are logged for manual
  review; there is no merge UI yet.
- **Atomic writes only** — never partial-write the registry file.
