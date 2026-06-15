# Practice Matching — Architecture Note (known tech debt)

Status: **documented, not yet refactored.** Do not refactor opportunistically;
treat this as a tracked TODO with a guarding regression test.

## The duplication

Practice-matching normalization and the match-priority logic currently live in
**two API-level places** (plus the engine copy behind the subprocess boundary):

1. `pipeline-api/discovery.py` — `_normalize_domain`, `_normalize_phone`,
   `_normalize_name`, `_normalize_address`, `_name_address_key`,
   `_build_indexes`, `find_match`.
   Used by Market Radar delta (`compute_delta`), `preregister_discovery_rows`,
   and `upsert_from_run`.

2. `pipeline-api/registry_update.py` — the same-named helpers plus
   `_build_indexes` and `match_entry`.
   Used by the explicit registry-update endpoint
   (`POST /enrichment-runs/{run_id}/update-registry`).

3. (For completeness) `discovery/matcher.py` in the repo-root `discovery`
   package — the discovery engine's own copy, reached only via the
   `discovery_cli.py` **subprocess** boundary, never imported by the API.

### Why it is duplicated (not a mistake)

The module name `discovery` is shared by `pipeline-api/discovery.py` and the
repo-root `discovery/` package, so importing matching helpers across that
boundary is fragile (the wrong module can win in `sys.modules`). The project
already duplicates shared constants at the API boundary for the same reason
(e.g. `ALL_KNOWN_EXCLUSION_RULE_NAMES` in `config.py`). Reimplementing the small
matching helpers locally was the deliberate, lower-risk choice.

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

**Any change to normalization or match priority MUST be applied to every
API-level copy in the same change** (`discovery.py` and `registry_update.py`),
and kept consistent with the engine copy in `discovery/matcher.py`. A change to
one copy alone is a bug: discovery and registry update would silently disagree
about whether two practices are "the same".

`tests/test_matching_parity.py` guards this: it asserts the two API-level copies
produce identical normalization output and identical match decisions for a
representative input set. If you intentionally change matching, update both
copies and update that test in the same commit.

## Long-term fix

Extract a single **API-safe** matching utility (e.g.
`pipeline-api/practice_matching.py`) that both `discovery.py` and
`registry_update.py` import. It must NOT import enrichment-pipeline internals and
must NOT import the repo-root `discovery` package (subprocess-only boundary).
Once that exists, delete the duplicated helpers and point the parity test at the
single source.

## Known limitation: `google_place_id` is lost on the enrichment path

When registry update runs from `enriched_targets.json`, `google_place_id` is
**usually absent**: the enrichment pipeline does not preserve it in its output
(it is dropped after ingestion). Consequences:

- Registry entries created/updated via
  `POST /enrichment-runs/{run_id}/update-registry` match and merge by
  domain → phone → name+address only; place_id is `""` unless the matched entry
  already had one from a prior discovery insert.
- This is consistent with the rest of the system today, but it weakens matching
  for records that *did* originate from discovery (where a place_id was known).

**Future work (do not fix now):** for records that originated from discovery,
join the registry update back to the run's `enrichment_handoff.csv` (which
preserves `place_id` per row) or to `source_discovery_run_id` →
`discovery_results.json`, to recover `google_place_id` and strengthen matching.
This is intentionally deferred — it requires a reliable row-to-record join
(handoff row → enriched record id) that does not exist yet, so it is not a
trivial, fully-testable change.
