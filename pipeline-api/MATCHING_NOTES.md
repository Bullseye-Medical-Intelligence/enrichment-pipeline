# Practice Matching — Architecture Note

Status: **API-side duplication resolved.** There is now one API-side source of
truth: `pipeline-api/practice_matching.py`. One copy remains in the engine, by
necessity (see below).

## The single source of truth (API side)

`pipeline-api/practice_matching.py` owns all API-side normalization + match logic:
`normalize_domain`, `normalize_phone`, `normalize_name`, `normalize_address`,
`name_address_key`, `build_match_indexes`, `match_candidates`, `find_match`
(first-priority-wins, for discovery delta) and `match_with_ambiguity`
(ambiguity-aware, for registry update). It imports nothing from the enrichment
pipeline and nothing from the repo-root `discovery` package — it is pure, I/O-free,
and config-free.

The one API consumer is `pipeline-api/registry_update.py`, which imports the helpers
under their existing private names (`_normalize_domain`, `_build_indexes`,
`match_entry = match_with_ambiguity`, …). Used by
`POST /enrichment-runs/{run_id}/update-registry`.

(The legacy `pipeline-api/discovery.py` shim was deleted after the shared utility
was extracted. The live discovery flow runs via `discovery_runs.py` →
`discovery_cli.py` → the repo-root `discovery` package.)

## The remaining copy: the engine

`discovery/matcher.py` in the repo-root `discovery` package is the discovery
**engine's** own copy, reached only via the `discovery_cli.py` **subprocess**
boundary — the API never imports it. This copy cannot be merged with
`practice_matching.py` without crossing that boundary (which is forbidden), so it
stays separate **by necessity**. It must be kept behaviorally consistent with
`practice_matching.py`, and `tests/test_matching_parity.py` now guards that
consistency directly: new tests compare the engine copy's `normalize_*` functions
against `practice_matching.py`, so the engine copy is no longer relying on
hand-sync alone.

### Why the API/engine split exists

The subprocess boundary between the API and the repo-root `discovery` package is
intentional and permanent (same isolation model as enrichment). The module name
`discovery` was also shared with the now-deleted `pipeline-api/discovery.py`, which
made direct imports fragile. Both reasons remain: one API-side util + one
engine-side copy.

## Invariants that MUST hold across all copies

1. **Match priority is fixed and identical everywhere:**
   `google_place_id` → normalized website domain → normalized phone (last 10
   digits, only when ≥ 10) → normalized practice name + normalized address.
2. **NPI is a supporting identifier only** — it is recorded on entries but is
   **never** a match key, and must never become one.
3. **Normalization rules are identical** across copies: domain strips
   scheme/`www.`/port and lowercases; phone keeps the last 10 digits; name
   lowercases, strips punctuation, collapses whitespace; address concatenates
   available parts and strips punctuation.

## The rule for future changes

**Change API-side matching in exactly one place: `practice_matching.py`.** The only
manual-sync obligation that remains is keeping the **engine copy**
(`discovery/matcher.py`) behaviorally consistent — if you change the priority or
normalization, mirror it there too.

`tests/test_matching_parity.py` guards this: it asserts (by identity) that
`registry_update.py` uses the `practice_matching` functions, covers the matcher's
behavior + the fixed priority directly, and now also compares the engine copy's
`normalize_*` functions (`discovery/matcher.py`) against `practice_matching.py`.
If you intentionally change matching, update `practice_matching.py`, the engine
copy, and this test in the same commit.

## Status

**Complete.** The shared `practice_matching.py` utility exists; `registry_update.py`
imports from it; the legacy `discovery.py` shim has been deleted. The engine copy
(`discovery/matcher.py`) stays separate by necessity (subprocess boundary).

## `google_place_id` on the enrichment path

`google_place_id` is preserved end-to-end through enrichment and is the
priority-1 match key. `ingestion/outscraper_adapter.py` maps it on ingest,
`enrichment/scorer.py` defaults it so it is always present in
`enriched_targets.json`, and `registry_update.py` reads it
(`rec.get("google_place_id")`) and persists it as the first-priority match key.

- Registry entries created/updated via
  `POST /enrichment-runs/{run_id}/update-registry` match and merge by
  place_id → domain → phone → name+address — the same priority as discovery.
- `tests/test_lifecycle.py` asserts place_id survives the discovery → enrichment
  handoff and matches on registry update.
