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

Both API consumers import from it (no local copies remain):

1. `pipeline-api/registry_update.py` — imports the helpers under their existing
   private names (`_normalize_domain`, `_build_indexes`, `match_entry =
   match_with_ambiguity`, …). Used by `POST /enrichment-runs/{run_id}/update-registry`.

2. `pipeline-api/discovery.py` — **legacy / non-runtime.** Nothing imports it at
   runtime (the live discovery flow is `discovery_runs.py` → `discovery_cli.py` →
   the repo-root `discovery` package). It now imports the shared helpers too, and
   its `find_match` is a thin shim onto `practice_matching.find_match`. The file is
   retained only because `tests/test_matching_parity.py` loads it by path to prove
   it still delegates to the shared util. Its `compute_delta` /
   `preregister_discovery_rows` / `upsert_from_run` helpers are dead code retained
   for reference. Do not add new runtime behavior here.

## The remaining copy: the engine

`discovery/matcher.py` in the repo-root `discovery` package is the discovery
**engine's** own copy, reached only via the `discovery_cli.py` **subprocess**
boundary — the API never imports it. This copy cannot be merged with
`practice_matching.py` without crossing that boundary (which is forbidden), so it
stays separate **by necessity**. It must be kept behaviorally consistent with
`practice_matching.py` by hand.

### Why the API/engine split exists

The module name `discovery` is shared by `pipeline-api/discovery.py` and the
repo-root `discovery/` package, so importing across that boundary is fragile (the
wrong module can win in `sys.modules`) — and crossing the subprocess boundary is
disallowed regardless. Hence one API-side util + one engine-side copy.

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

**Change API-side matching in exactly one place: `practice_matching.py`.** Both
`discovery.py` and `registry_update.py` import from it, so they cannot drift. The
only manual-sync obligation that remains is keeping the **engine copy**
(`discovery/matcher.py`) behaviorally consistent — if you change the priority or
normalization, mirror it there too.

`tests/test_matching_parity.py` guards this: it asserts (by identity) that both
`discovery.py` and `registry_update.py` use the `practice_matching` functions, and
covers the matcher's behavior + the fixed priority directly. If you intentionally
change matching, update `practice_matching.py` and this test in the same commit
(and mirror the engine copy).

## Status of the long-term fix

**Done (API side):** the shared `practice_matching.py` utility now exists and both
API consumers import it; the duplicated local helpers were removed. It imports no
enrichment internals and not the repo-root `discovery` package.

**Remaining:** the engine copy (`discovery/matcher.py`) stays separate by
necessity (subprocess boundary). `discovery.py` is now a thin legacy shim and
becomes deletable once `tests/test_matching_parity.py` no longer needs to load it
by path to prove delegation.

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
