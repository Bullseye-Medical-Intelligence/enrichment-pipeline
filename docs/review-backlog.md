# Review Backlog — Verified Findings

**Status: P1-1 through P2-13 RESOLVED** (2026-08-17, one commit per finding,
each re-verified against the tree before fixing, each with regression tests
pinning the failure scenario — commits `6f2df64`..`99ed5eb`). The P1/P2 items
below are kept for their analysis value. **Still open: items 14-15 (tenth-lens
findings that arrived after the resolution pass), the P3 debt section, and the
"Previously deferred" section.**

Resolution notes that refine the original findings:
- **P1-5** was the flagged product call: viewing surfaces (results page,
  Contact Queue, confirm queue, compare) now degrade per damaged entry with a
  visible banner (`reviews.get_reviews_lenient`); write paths and
  client-shipping paths stay fail-closed on `get_reviews`.
- **P2-11** added `output/atomic_write.py::guarded_staged_write` (per-pass
  `mkstemp` staging) and all five CLIs route through it.
- **P2-8** moved the usage increment into `runs.add_llm_usage` under the run
  lock; both merge monitors book scratch-run usage from `run_log.json`.

Originally found by a ten-lens adversarial code review of the branch diff
`origin/main...claude/keen-sagan-hhpl3y` (commits `57c7ebd`, `4fb3ec7`, `83a9bf3`,
`b41a3ec`), 2026-07-26. Every item below was verified against the actual tree —
either traced by the reviewing agent with quoted code, or independently re-checked.
Nothing here is speculative. Items marked **(regression)** were introduced by those
four commits; the rest are pre-existing issues those commits exposed or contradict.

All ten lenses completed (the language-pitfalls lens on retry — its two findings
are items 14 and 15).

Sibling document: `docs/data-boundary-model.md` (client/project data-boundary
architecture — separate decision track).

---

## P1 — wrong data or lost work, reachable in normal operation [ALL RESOLVED]

### 1. Internal Sales Handoff silently lost analyst notes (regression)
`pipeline-api/reports/pdf_report.py` — the analyst-note leak fix (`4fb3ec7`)
stripped `analyst_note` from **both** report-context builders, but
`_prepare_sales_record` feeds `build_sales_handoff_html` → the **internal**
Sales Handoff (`sales_export.py` docstring: "Internal-only … analyst notes";
operator download route), whose template still references `rec.analyst_note`
(`sales_handoff.html:667-681`). Jinja default-undefined renders it empty, and the
"no review" fallback at line 681 misfires for records whose only review content is
a note. The client-facing removal (`_prepare_record` → bullseye cards / executive
report) was correct and stays. **Fix:** restore `analyst_note` in
`_prepare_sales_record` only; add a regression test asserting the internal handoff
contains the note while the client report does not.

### 2. Evidence Vault desyncs from stored signals when a re-crawl pass aborts (regression)
`recrawl_run.py` — vault snapshots are destructively replaced **per record
mid-loop** (`write_record_evidence` after each successful extraction), but the
signals write is a single guarded write at the end. Both new abort paths —
fail-closed `apply_exclusions` and a `ConcurrentRunChange` refusal — discard all
record updates *after* earlier records' vaults were already replaced. Result: old
signals whose `evidence_text` quotes the old pages, paired with new vault text →
the verifier's anchor-check falsely flags "compromised evidence" and the evidence
viewer's quote highlighting breaks. **Fix:** stage vault writes (e.g. to a temp
dir) and commit them only after `guarded_replace` succeeds.

### 3. A refused pass loses its entire Claude spend from cost reporting (regression)
`reextract_run.py` + `pipeline-api/ui.py` — when `guarded_replace` refuses
(concurrent merge landed mid-pass), the CLI `sys.exit`s **before printing its
stats line**, and the route calls `add_llm_usage` only on returncode 0. A refused
500-record re-extract burns ~500 real Claude calls recorded nowhere — the exact
undercount `add_llm_usage` was built to eliminate, reintroduced on the failure
path the same batch introduced. **Fix:** print the usage summary before exiting on
refusal (or write it to a sidecar), and have the route book spend on refusal too.

### 4. Run deletion races post-run CLI passes (regression-adjacent)
`pipeline-api/runs.py` + `ui.py` — the new delete guard checks
`refresh_status.json` and drains `.run.lock`, but post-run passes hold
**`.postrun.lock`** (never checked) and write no refresh entries; their `.run.lock`
hold lasts only the final milliseconds. Deleting mid-pass: `rmtree` races the
CLI's tmp write / `O_CREAT` lock-file recreation → `ENOTEMPTY` (an `OSError` the
routes' `except ValueError` misses → 500, half-deleted directory) or the CLI dies
on an uncaught `FileNotFoundError` after full LLM spend. Related: `locking.file_lock`'s
`mkdir(parents=True)` can resurrect a ghost run directory containing only lock
files. **Fix:** delete probes `.postrun.lock` non-blocking (one probe covers every
pass); map `FileNotFoundError` in `run_state_lock` to a clean refusal; drop
`parents=True` for per-run locks; catch `locking.LockTimeout` in both delete routes
(it is a `RuntimeError`, so today it escapes `except ValueError` — one busy run
aborts a bulk delete and surfaces a full-page 503).

### 5. One malformed review entry now blocks every read surface for the run (regression, deliberate-call)
`pipeline-api/reviews.py` — the new per-entry validation raises `ReviewsLoadError`
on any non-dict entry, and every read caller (results page, Contact Queue, confirm
queue, compare, CSV exports, client package, brief publishing) lets it propagate →
409 for the whole run until the file is hand-repaired. Previously these pages
rendered and only the damaged entry showed empty. Write-path fail-closed is
correct; read paths are a product call: keep hard-fail (visible damage) or degrade
per-entry with a visible warning. Decide deliberately — currently one `null` entry
takes down the client package.

## P2 — wrong numbers on screens, contract mismatches [ALL RESOLVED]

### 6. Two count writers, two semantics — completion vs refresh
`pipeline-api/runner.py::_read_completion_counts` still counts **raw
`target_tier`** while `refresh_run_counts` (new) counts override-aware
`effective_tier`. A fresh run's counts silently flip the first time any
refresh-triggering action runs. **Fix:** route completion counts through
`_recompute_counts_from_records`.

### 7. Blocked records inflate `manual_review_count` on the run list
`_recompute_counts_from_records` counts blocked records (`source_confidence`
limited/failed → tier "Manual Review") into `manual_review_count`, while the
results page diverts them to a separate "Site Blocked" bucket
(`ui._calculate_stats`). Run list vs results page disagree by the size of the
blocked set — pre-existing, but the new code's docs claim the disagreement is
eliminated. **Fix:** subtract blocked records in the shared count helper (they are
identifiable from `source_confidence`), or count them in a dedicated field.

### 8. Cost capture still missing from the paths that spend most
`runner.add_llm_usage` is wired only into the re-extract route. Batch re-enrich,
"Retry All with Browser", per-record re-crawl, and manual-content merges all make
fresh Claude calls whose usage never reaches the run totals — and each scratch
run's `run_log.json` already carries the exact fields at the merge choke points
(`_monitor_batch_reenrich`, `_merge_recrawled_record`). Also: `add_llm_usage`'s
read-modify-write is unlocked (its "serialized by the job lock" comment is false —
the route calls it after `.postrun.lock` is released). **Fix:** accumulate usage
inside the merge monitors from `scratch_dir/run_log.json`; make the increment
atomic under `run_lock` inside `runs.py`.

### 9. Checkpoint hardening left three edges (regression-adjacent)
`pipeline.py` — (a) `_load_step4_checkpoint` calls `read_text()` before any
guard: a run killed mid-append inside a multibyte UTF-8 char makes resume crash
with `UnicodeDecodeError` on every retry (docstring claims corrupted tails are
skipped) — catch it and treat as corrupted-tail; (b) `_write_step4_checkpoint`
appends blindly, so a still-running older process can append old-ICP records under
a newer run's re-stamped fingerprint header (shared `./output` default) — a
run-scoped checkpoint path is the deeper fix; (c) an unstamped pre-upgrade
checkpoint is discarded with a message blaming "config, ICP, or input file
changed" — misattributed, and the one-time cross-version loss of resume is
unannounced.

### 10. `reextract` pass has no per-record error handling
`reextract_run.py::run_reextract_pass` — one Anthropic 429/overloaded error from
any worker re-raises out of `future.result()`, aborting the whole pass after real
spend, writing nothing (and per finding 3, reporting nothing). Pipeline Step 4 has
`_extract_with_retry`; the re-extract pass has no equivalent. **Fix:** per-record
catch + mark-failed like Step 4, so one bad record doesn't discard 299 good ones.

### 11. Fixed tmp filenames allow concurrent-pass payload swap
All five CLIs stage to fixed paths (`enriched_targets.tmp` for verify/recrawl,
`.json.tmp` for rescore/reextract/suppress), written **outside** the lock. Two
concurrent passes sharing a name (e.g. API-triggered verify + operator-run CLI)
can interleave: pass A installs pass B's bytes under A's fingerprint check.
Related: `guarded_replace` leaks the tmp file when the lock acquisition times out
(unlink happens only on the fingerprint-mismatch branch). **Fix:**
`tempfile.mkstemp(dir=run_dir)` per pass; unlink in a `finally`.

### 12. `load_refresh_status` can still 500
`runner.py` — a non-string `started_at` (hand-edit / partial write) raises
`TypeError` from `datetime.fromisoformat`, which the `except ValueError` misses;
GET refresh-status and both delete routes 500. **Fix:** catch `(ValueError,
TypeError)`.

### 13. Preflight session-key message overclaims
`preflight.py` — the error text says "login cannot issue session cookies without
it", but `auth.py` raises only on an **empty** key; with a placeholder like
`changeme` login works. A dev on placeholder values sees an auto-expanded red
error asserting a working subsystem is broken. Also: the placeholder blocklist is
hand-coupled to `.env.example` text with no test tying them together — rewording
the example silently reverts the check. **Fix:** accurate message per branch
(empty vs placeholder); add a test iterating `.env.example` values asserting
`_is_configured` is False for each.

## P2 — tenth-lens findings, found after the resolution pass

These two arrived from the review's final (retried) lens after the P1/P2
resolution pass above ran; both re-verified as still present at the merge.
Item 15 RESOLVED 2026-08-19 (crawl-mode string folded into the fingerprint,
pinning test in TestStep4Checkpoint). Item 14 remains OPEN.

### 14. `UnicodeDecodeError` escapes the ReviewsLoadError contract (regression-adjacent)
`pipeline-api/reviews.py` — `get_reviews` converts damage to `ReviewsLoadError` via
`except (json.JSONDecodeError, OSError)`, but invalid UTF-8 bytes in reviews.json
raise `UnicodeDecodeError` during the stream decode — verified empirically: it is
neither a `JSONDecodeError` nor an `OSError`, so it escapes raw. Every downstream
`except ReviewsLoadError` (including the new fail-soft `_reviews_for_counts` /
`refresh_run_counts`) misses it: a batch re-enrich whose merge already persisted
then crashes in counting — `_monitor_batch_reenrich`'s blanket handler logs "run
left untouched" (false), marks every merged record failed, and the operator
re-runs, double-spending Claude. Plausible trigger: the ReviewsLoadError recovery
message itself tells operators to hand-repair the JSON — a Windows editor saving
cp1252 produces exactly these bytes. **Fix:** add `UnicodeDecodeError` (or
`ValueError`) to the catch tuple in `get_reviews`; add a test with invalid-UTF-8
bytes.

### 15. [RESOLVED 2026-08-19] Checkpoint fingerprint omits crawl-mode inputs (regression-adjacent)
`pipeline.py::_checkpoint_fingerprint(input_file, config_path, icp_path)` — the
fingerprint scopes the Step 4 checkpoint to config + ICP + input CSV but not to
`use_playwright`, `auto_browser_retry`, or `manual_content_path`, which change the
page text signals are extracted from. The natural operator flow — a thin
HTTP-crawl run is killed, then re-run with `--playwright` "to do it properly" —
matches the fingerprint, restores the thin-crawl records from checkpoint, discards
the fresh Chromium crawl for them, and reports the browser run as having found
nothing new, with zero Claude calls. **Fix:** fold the three crawl-mode values
into the fingerprint hash; test that adding `--playwright` invalidates a
checkpoint written without it.

## P2 — measured findings from the consolidation build (2026-08-20) [OPEN]

Both were found by measuring the practice-location consolidation work against a
real 1,137-row list, not by reading code. Numbers below are from that run and are
reproducible; neither item is started.

### 16. NPI enrichment matches 2.8% of a place-scraped list (visible, not started)
`ingestion/npi_lookup.py` — measured 32 of 1,137 rows matched (2.8%) on an Apify
Google Places list. Step 1b is therefore close to a no-op on the source type the
pipeline actually ingests most often, and every downstream consumer of NPI data
inherits that: taxonomy-based structural exclusions almost never fire pre-crawl,
`npi_practice_name` is nearly absent from the consolidation naming chain, and
provider-level customer suppression can only match the 2.8%. The operator is not
told any of this — the run reports NPI enrichment as having run, not as having
found nothing.

Cause is structural, not a bug. The fast path keys on an NPI number in the input
row; Apify and Outscraper exports carry none, so it never fires. The fallback
searches NPPES by `organization_name`, but only 20.2% of rows on that list have an
organization-shaped `practice_name` — the rest are individual physician names
("Kernick Nancy", "Temporini Humberto D MD"), which an organization-name search
cannot match by construction.

**Fix (not started):** add an individual-provider lookup — NPPES entity type 1
searched by parsed `first_name` / `last_name` + `state`, used when the practice
name parses as a person. Verify against measured lift before adopting; a name+state
search returns multiple candidates and needs an address tiebreak to stay safe.
**Interim, cheap, and separate:** make NPI enrichment skippable by source type, or
report the match rate in the run manifest so a 2.8% run is visibly a 2.8% run.

### 17. `review_pairs` counts row-level edges, so the badge contradicts the page
`ingestion/consolidator.py::consolidate_records` — the `review_pairs` counter
increments once per surviving Pass 1 review *edge*. Because a cluster absorbs many
input rows, several edges resolve to the same pair of merged `practice_id`s.
Measured on the same list: 2,415 edges versus 607 distinct location pairs, a 4x
overstatement.

The operator dashboard's review route (`pipeline-api/ui.py::consolidation_review_queue`)
already dedupes by sorted `(left_id, right_id)`, so the results-page badge reads
2,415 from the snapshotted status field while the page it links to renders 607.
The same inflated number reaches the internal run manifest
(`client_exports.py` → `review_queue_pairs`) and the CLI's console summary.
**Fix:** count distinct `practice_id` pairs in the engine, where the count is
produced, so every consumer inherits the corrected number without recomputing it.

### 18. Manual adapter takes `specialty` verbatim, so a registry list self-excludes
`ingestion/manual_adapter.py:118` —
`(row.get("specialty") or "").strip() or infer_specialty("", practice_name)`.
The column value is used as-is; `infer_specialty` runs only as a fallback and is
handed `""` as the type argument, so it can never resolve a type/taxonomy string.
Both scraped-source adapters do the opposite: `outscraper_adapter.py:338` and
`apify_places_adapter.py:67` both call `infer_specialty(type_raw, practice_name)`.

Consequence, verified this session: an NPI-registry-derived CSV carries real NUCC
taxonomy descriptions ("Obstetrics & Gynecology"). `_specialty_matches
("Obstetrics & Gynecology", "OBGYN")` returns `False`, so `wrong_specialty` fires
on every row and the entire list is structurally excluded before any crawl. The
same string through `infer_specialty` returns `"OBGYN"` and matches. This is a
plausible operator flow — buy or export an NPI-derived list, upload it as a manual
CSV — and it fails silently as a fully-excluded run, which reads like a bad list
rather than a bad mapping. **Fix:** route the column through `infer_specialty` as
the type argument (`infer_specialty(row.get("specialty") or "", practice_name)`),
matching the other two adapters; add a test with a taxonomy-description CSV.

## P3 — technical debt (grouped; fix opportunistically) [OPEN]

- **Post-run route boilerplate:** the five trigger routes each repeat cmd build,
  LockTimeout→409, returncode→500, count-refresh, and a 4×-copied
  parse-last-JSON-line loop (which also swallows parse failures unlogged — RULE 7;
  and `_json.loads("20")` on a bare-number line would break `.get`). One
  parameterized helper collapses ~60 duplicated lines. Also: local
  `import json as _json` aliases in six routes despite the top-level import; the
  1.0s job-lock timeout is an inline magic number.
- **Five-CLI load/write copy-paste:** fingerprint-load + unwrap + tmp-write +
  guarded-replace (plus the identical `except ConcurrentRunChange: sys.exit`
  wrapper) is pasted five times with drift already visible (tmp suffixes,
  `record_count` refresh, `default=str` vs `ensure_ascii`). A shared
  `load_run_records`/`write_run_records` pair in `output/` was the natural home.
  A stale comment in `rescore_run.py` still says "os.replace()".
- **`refresh_run_counts` re-inlines `_reviews_for_counts`** defined 13 lines above
  (two log strings for one policy).
- **`DEFAULT_RECRAWL_MAX_PAGES = 20` literal** duplicates
  `web_extractor.MAX_CRAWL_PAGES` instead of importing it; the test pins the
  literal, so drift would pass green.
- **`save_review` recounts the whole run on every QC click** — a full
  `enriched_targets.json` parse + locked status rewrite per save, though QC
  accept/reject cannot change counts. Skip unless `override_tier` changed (or
  adjust the two affected buckets ±1). Same shape: `trigger_resuppress` refreshes
  counts before reading its own stats that prove a no-op.
- **`_load_step4_checkpoint` reads the whole file before the fingerprint check** —
  a mismatched multi-MB checkpoint is fully read just to be discarded; readline
  the stamp first. The init/load/clear trio could fold init into load.
- **Duplicated lock implementation is unpinned:** `output/atomic_write.py`
  re-implements `pipeline-api/locking.py`'s flock pattern (forced by the
  no-cross-import rule), but nothing asserts the two `.run.lock` filenames stay
  identical — the guard silently dies if either renames. Add a test importing
  both. `ConcurrentRunChange` also covers two distinct conditions (busy vs
  changed); rename or split deliberately. Missing docstrings on the four
  platform-lock helpers.
- **`_recompute_counts_from_records` entrenches the API-side mirrored threshold**
  (`record_adapter._LOW_SCORE_MANUAL_REVIEW_THRESHOLD` mirrors
  `enrichment/constants`) into durable status.json — documented as deliberate in
  `pipeline-api/CLAUDE.md`, but the mirror is the drift risk RULE 3 warns about.
- **Test overlap:** the effective-tier mapping is pinned four times across
  `test_runner.py`/`test_run_counts.py`; the checkpoint OSError swallows
  (`_init`/`_clear`) log nothing.

## Previously deferred (documented, unchanged)

- Admission check scans the whole run archive under the global lock
  (`pipeline-api/CLAUDE.md` → Known Performance Debt, with fix triggers).
- Dashboard stats one filesystem entry per record (same section).
- GPT verification spend absent from cost figures (needs an operator-maintained
  GPT rate; do NOT fold into Claude-priced fields).
- Client/project data-boundary decisions — see `docs/data-boundary-model.md`.
