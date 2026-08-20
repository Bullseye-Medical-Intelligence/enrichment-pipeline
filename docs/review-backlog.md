# Review Backlog — Verified Findings

**Status: P1-1 through P2-13 RESOLVED** (2026-08-17, one commit per finding,
each re-verified against the tree before fixing, each with regression tests
pinning the failure scenario — commits `6f2df64`..`99ed5eb`). The P1/P2 items
below are kept for their analysis value. **Still open: item 16 (NPI match rate),
items 22-23, 28, 30 and 31 (consolidation, handoff and override gaps — each
blocked on a schema change, a measurement, or a deliberate trade-off), the P3
debt section, and the "Previously deferred" section.** Items 14-15, 17-21, and
24-27 are resolved; 29 is closed with no action.

Findings are numbered in the order they were found, not by area. Items 1-15 came
from the 2026-07-26 adversarial review, 16-23 from the consolidation build, and
24-30 from the delivery-layer and Sales Handoff work — the last group is what
sits between correct engine output and what a rep or client actually receives.

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
Both are now RESOLVED: item 15 on 2026-08-19 (crawl-mode string folded into
the fingerprint) and item 14 on 2026-08-20 (UnicodeDecodeError added to the
catch tuple in every reader of a hand-editable JSON file — reviews.py,
icp_profiles.py, registry_update.py — with a cp1252 regression test).

### 14. [RESOLVED 2026-08-20] `UnicodeDecodeError` escaped the ReviewsLoadError contract
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

### 17. [RESOLVED 2026-08-20] `review_pairs` counted row-level edges
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

### 18. [RESOLVED 2026-08-20] Manual adapter took `specialty` verbatim
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

### 19. [RESOLVED 2026-08-20] Credentials became phantom providers
`ingestion/manual_adapter.py::_parse_provider_names` comma-splits when no pipe is
present, so `"Jane Smith, MD"` becomes two providers, `["Jane Smith", "MD"]`. The
engine already knows how to handle this — `consolidator._split_credentials` splits
`"Jane Smith, MD, FACOG"` into a name plus credentials against `CREDENTIAL_TOKENS`
— but it runs per-name in `_providers_from_record`, *after* the adapter has already
separated the credential into its own string. Alone in its own string the
credential is `parts[0]`, and the filter only inspects `parts[1:]`, so it survives
as a provider name.

Measured on the 1,200-row NPPES list: **1,025 of 2,263 provider entries (45.3%)
are credential-shaped phantoms, affecting 841 of 892 locations.** The largest
location renders as `"44 providers at 1515 HOLCOMBE BLVD"` where 33 source rows
produced 33 real providers.

Blast radius is the provider count, not the merge: consolidation blocks and scores
on address / phone / domain / name, so merge rates are unaffected. But
`provider_count` is the left-hand side of the roster preview's headline
("1,340 provider entries -> 412 practice locations"), it feeds the naming chain's
never-a-person gate, and it ships to clients as `providers_flat` /
`provider_count` CSV columns. **Fix:** in `_parse_provider_names`, do not
comma-split when the trailing parts are credential tokens — both modules live in
`ingestion/`, so `CREDENTIAL_TOKENS` imports directly. Pipe-separated input is
already unambiguous and stays as-is. Add a test for `"Jane Smith, MD"`.

### 20. [RESOLVED 2026-08-20] A matching suite number scored nothing
`ingestion/consolidator.py` — `units_conflict` only ever *blocks* a merge. A unit
that MATCHES earns no score, so two records at the same street, ZIP **and suite**
score exactly the same 4 (address only) as two unrelated tenants of the same
building. Both land in the review queue as indistinguishable near-matches.

Measured on the 1,200-row TX NPI-registry list, over the 542 address-only review
pairs: **177 (32.7%) are at the same non-empty suite**, 107 (19.7%) have a suite on
one side only, 258 (47.6%) have none. The same-suite clusters are unambiguous —
41 pairs at 6410 Fannin St Ste 360, 15 at Ste 350, 14 at Ste 210, 12 at 6651 Main
St Ste 1020 — one practice per suite, each physician listing a direct line.

This is the finding that blocks the matched_fields review predicate. On a registry
list the phone is present on 100% of rows and is a direct line, so those 177 pairs
present as address-only with *differing* phones. A predicate requiring corroboration
beyond the shared building would auto-separate all of them, and a missing-phone
carve-out would not catch a single one (zero rows lack a phone).

**Status.** Step one is SHIPPED: `review_admission` now carves same-suite pairs
into the review queue instead of auto-separating them, and every ruling captures
the evidence behind it (`review_candidates[].evidence`). The queue is a
measurement instrument, not permanent process — one list, then the rule gets set
from evidence.

Measured after shipping (pairs surviving exclusions, grouped into building
decisions):

| list | review pairs | same_unit | decisions | recoverable rows |
|------|--------------|-----------|-----------|------------------|
| TX registry (run `RUN-20260820-045154`) | 97 | 89 | **28** | 43 |
| NorCal scrape (PROVISIONAL) | 145 | 62 | 50 | 63 |

The TX row is read from that run's `run_log.json` (`consolidation.review_pairs`,
`consolidation.review_reasons`) and its `enriched_targets.json`. The NorCal row is
**PROVISIONAL** — it comes from an analysis script over cached records, and
becomes authoritative once that list is run through the pipeline.

An earlier draft of this table said 50 recoverable rows for TX. That figure grouped
all 97 admitted pairs into decisions, including the 8 unit-gate blocks, which are
mechanical rejects that will never merge. Grouping the 89 same-suite pairs alone
gives 43.

**RESOLVED — `SCORE_UNIT_MATCH = 3`, same-suite carve-out deleted.**

Thirteen decisions were ruled: the top ten by recoverable rows, plus three drawn
from the tail as a control. **All thirteen MERGE.** The pre-committed rule
(8+ of 10 -> scoring term) fired.

**The reasoning, which matters more than the count.** A suite is one leased unit
with one front door, and two competing OBGYN practices do not share one. The
realistic readings of two providers at one suite are a two-provider practice, an
office-share arrangement, or a stale record — and under every one of them a rep
makes ONE call to ONE front desk. So the standard is not "are these the same
legal entity", it is **"would a rep knock once or twice"**. Same street, same ZIP,
same suite means one door. That is the commercial unit being sold, and the one the
cartridge should model.

The control mattered: it was *weaker* evidence than the top ten and still ruled
merge. The institutional decisions had shared exchanges and sequential extensions;
the three control decisions were independent single-physician practices whose only
corroboration was a shared area code. The rule held on the thinner case.

Effect, isolated by varying only `SCORE_UNIT_MATCH` from 0 to 3 and holding every
other input constant (RULE M4):

| list | locations | merged clusters | review pairs |
|------|-----------|-----------------|--------------|
| TX registry | 890 -> **845** (-45) | 143 -> 155 | 118 -> **25** |
| NorCal scrape | 768 -> **726** (-42) | 67 -> 81 | 173 -> **132** |

One case is deliberately NOT merged: a same-suite pair whose two sides carry real
and *different* websites scores 4 + 3 - 3 = 4, under the merge threshold, and is
admitted for review as `corroborated`.

**The reason is mechanical, not a judgement call.** The one-door standard says
merge. The schema says we cannot: a record carries exactly ONE `website_url`, so
merging forces the engine to pick one site and discard the other's evidence. On
an ICP with a second-brand signal — a practice with a separate med-spa brand at
the same suite, where the med-spa is the cash-pay evidence — crawling the wrong
one of the two loses the signal entirely. The schema is the binding constraint,
so the pair stays in review. See item 22 for what would let it auto-merge.

### Authoritative consolidation rates (2026-08-20) — the deliverable

Read from pipeline output only, per RULE M1: each list was run through
`pipeline.py --ingest-only` (no crawl, no LLM spend) and every number below comes
from that run's `run_log.json` and `enriched_targets.json`. The post-exclusion
population is derived by calling the engine's own `check_structural_exclusions`
over engine output, not by reimplementing it. This supersedes every consolidation
figure reported earlier in this workstream.

| | TX registry (`RUN-20260820-053114`) | NorCal scrape (`RUN-20260820-053306`) |
|---|---|---|
| source | NPPES, OBGYN taxonomy, entity type 1 | Apify Google Places, psychiatry |
| **gross** | 1,200 rows -> **845** locations = **29.6%** | 1,137 rows -> **726** locations = **36.1%** |
| **post-exclusion (billable)** | 1,125 -> **779** = **30.8%** | 738 -> **474** = **35.8%** |
| merged clusters | 155 | 81 |
| rows absorbed | 355 | 411 |
| distinct providers | 1,265 (of 1,266 raw entries) | 1,137 (of 1,137) |
| review queue | 25 | 132 |
| queue composition | phone_absent 18, unit_gate_block 7 | corroborated 29, phone_absent 58, unit_gate_block 45 |
| Pass 2 groups | 0 | 26 |
| no street to match on | 1 | 25 |

**What is quotable.** The TX row is a clean OBGYN-registry measurement:
**29.6% gross, 30.8% post-exclusion**. The NorCal row is a real measurement of
that list, but it is **not a like-for-like comparison** with TX.

**The by-source comparison is CONFOUNDED — directional only.** The two lists
differ in *two* variables, not one:

| | source | specialty |
|---|---|---|
| TX | NPPES registry | OBGYN |
| NorCal | Places scrape | psychiatry |

Psychiatry has a materially different clustering profile from OBGYN — heavy solo
practice, therapy groups sharing suites, telehealth-only providers. Some or all
of the 6.5-point gap may be specialty rather than source, and nothing here
separates them.

The *direction* is mechanistically supported and specialty-independent: NPPES
carries no website field at all, so Pass 2 finds zero groups and domain agreement
never contributes to a Pass 1 merge; and a registry row is one provider at one
enumerated address, where a scraper emits the same practice several times from
different listings. The *magnitude* is unmeasured for OBGYN. **Do not quote a
source-type delta for OBGYN.**

**Outstanding measurement (not run):** an OBGYN scrape for a single metro through
the same `--ingest-only` path. That is the one run that isolates source from
specialty and closes the question. It needs a Places scrape we do not currently
have, so it is logged rather than run.

**The queue is a workflow on scraped input and nearly empty on registry input.**
The 25 TX pairs are 18 unknown-phone and 7 mechanical unit-gate rejects — no
judgement calls at all. NorCal's 132 include 29 genuine corroborated conflicts.
If the next real run holds at that shape, close backlog 21 items (b) and (c) and
the rotation repair as won't-do, with their measurements attached, rather than
leaving them open indefinitely.

### 22. One website_url per location blocks a merge the evidence supports [OPEN]
A practice location carries a single `website_url`, so two rows at one suite with
two real and different sites cannot be merged without discarding one site's
evidence. `SCORE_DOMAIN_CONFLICT` currently holds such a pair under the merge
threshold and routes it to review, which is correct **only because** of this
schema limit — the one-door standard would otherwise merge it.

The failure this protects against is live on a cash-pay ICP: a practice with a
separate med-spa brand at the same suite is a Tier 1 signal, and crawling the
wrong one of the two loses either the clinical or the elective evidence.

**Fix (not started):** multiple website URLs per practice location — the crawler
visits each and the extracted evidence is combined before signal extraction. That
is what would let a two-site same-suite pair auto-merge instead of asking. Touches
the record schema (RULE 4: `PIPELINE.md` plus the `enrichment/scorer.py` validator
in the same change), the crawl loop, and the Evidence Vault's per-record layout.
Not started, and not worth starting for this case alone — the driver would be
multi-brand practices in general.

### 23. Pass 1 blocking is single-path, so a street-less row is invisible [OPEN]
`ingestion/consolidator.py::_block_key` returns a key only when a record has BOTH
a normalized zip5 and a street. A record with neither never enters a block, is
never compared to anything, and is emitted as its own practice location — even
when it carries a usable phone and domain that would have scored 6 against a
sibling and merged.

The consequence is commercial, not technical: the collapse figure is a promise
that duplicate providers at one location are consolidated, and this population
sits outside that promise. A duplicate hiding in it is never found.

**Measured (from `run_log.json`, `consolidation.unblocked_count`):**

| list | rows not eligible | share |
|------|-------------------|-------|
| NorCal scrape (`RUN-20260820-053306`) | 25 of 1,137 | **2.2%** |
| TX registry (`RUN-20260820-053114`) | 1 of 1,200 | **0.09%** |

A registry list carries an enumerated address on every row; a scrape does not.

**Disclosed now, not fixed.** The count is surfaced beside the collapse line on
the roster preview, in the run manifest
(`consolidation.rows_not_eligible_for_consolidation`) and in the CLI summary, so
the gap is visible rather than silent.

**Fix (not started):** a secondary blocking pass on normalized phone, run for
records the address block could not key. Same scoring and the same unit gate
afterwards — only the candidate-generation path changes. At 2.2% of a scraped
list it is worth knowing, not worth building yet; the trigger would be a client
list where address quality is materially worse, or a duplicate found in this
population.

## Delivery-layer findings (2026-08-20) [ALL RESOLVED]

Four defects between the engine and what a rep or client actually receives. None
was visible in engine output: every one of them was introduced by a layer
downstream re-deriving, freezing, or dropping something the engine had already
decided correctly.

### 24. [RESOLVED 2026-08-20] The display layer re-derived a tier the engine had set
`record_adapter.displayed_tier` re-applied the low-score Manual Review floor at
render time, a compatibility shim for runs frozen before that threshold existed.
The engine lifts a record to Contender when a `floor_tier` signal is confirmed —
a primary qualifier warrants a call even on a thin score. The shim saw only the
score and put it straight back to Manual Review.

Because `displayed_tier` also gates the Contact Queue, the client CSVs, the
client ZIP, and the run-list counts, **every floor-lifted record under 50 points
was withheld from reps and left out of the client deliverable.** Found on a
Femasys record with confirmed cash pay, a physician owner, and no hospital
committee, showing as Manual Review at score 42.

**Fixed:** the shim is deleted; `displayed_tier` resolves the override overlay
and the legacy `Watchlist` rename only. Re-deriving a tier from one input the
engine weighs among many cannot be made correct — only the engine sees
`floor_tier`, `cap_tier`, must-haves, and source confidence together. Teaching
the shim about `floor_tier` would have kept two copies of the tier ladder in
hand-maintained sync, which is what produced the bug. Runs enriched before the
50-point floor existed now show the tier they were written with.

### 25. [RESOLVED 2026-08-20] Nothing bounded a browser crawl's total wall clock
Navigation timeout, challenge budget, and per-subpage timeout were each bounded;
their sum was not, so one bot-gated domain could hold an `io_concurrency` worker
for minutes while the rest of the batch waited for a slot. The largest hidden
contributor was `_wait_for_real_content`, which tallied `poll_ms` per round and
ignored what else each round cost (the networkidle wait, re-reading page
content) — a nominal 25s budget ran roughly double.

**Fixed:** one deadline per site (`PLAYWRIGHT_MAX_CRAWL_SECONDS`, 60s). The
challenge wait now measures against the monotonic clock, and
`_crawl_budget_seconds` raises the ceiling when the homepage legitimately needs
longer, so raising `PIPELINE_BROWSER_CHALLENGE_WAIT_MS` widens the deadline
rather than being truncated by it. Reaching the deadline is not an error —
captured pages are returned. Also: `request_timeout_seconds` now reaches every
browser path; `recrawl_run.py` had been using the library default instead of the
run's own config.

### 26. [RESOLVED 2026-08-20] A retracted signal left its talk track standing
`apply_signal_overrides` rebuilt a card's signal chips from the merged signals
but read `sales_angle` and the `call_brief` prep lines straight off the frozen
record. An analyst rejecting a false positive left the card still offering an
opener citing the claim they had just rejected.

The staleness dot made it worse: it detected the override and told the operator
to republish, but republishing re-rendered the same frozen text, so the
indicator promised a fix it could not deliver.

**Fixed:** an override that takes a signal from `"yes"` to anything else clears
`sales_angle` and the evidence-composed `call_brief` fields, and drops the
retracted entry from `top_evidence`. `hours_of_operation` survives (a fact about
the practice, not a claim about a signal), as do operator-authored
`extra_sales_angles`. An override that ADDS evidence changes nothing — it can
only make the prose understate. The rule lives at the single chokepoint every
consumer reads through, so dashboard, CSVs, handoff, and client package get it
at once.

**Found while tracing it:** `pdf_report.build_bullseye_cards_html` never applied
the overlay at all. The Bullseye Target Report — the flagship client document —
was handed raw pipeline records, so **a signal an analyst had rejected reached
the client as a confirmed finding with its original evidence quote intact.**
`client_exports._approved_records` now merges and strips the internal marker.

### 27. [RESOLVED 2026-08-20] A re-enrich could not find the record it had sent
A re-enrich runs one record through the pipeline in a scratch dir from a
reconstructed one-row CSV. Step 1d stamps a location's derived `practice_id`
over `record["id"]`, and the scratch CSV carried no street or unit, so identity
fell back to domain or phone and hashed differently. The merge reported an id
mismatch and left the record unchanged.

Fixing only that would have made things worse: the merge replaced the record
wholesale, so a location consolidated from several provider rows came back as
one row — `provider_count` fell to 1, `source_row_ids` lost its members,
`providers` became a placeholder. The id mismatch had been masking that.

**Fixed:** scratch runs spawn with consolidation disabled (the input is already
one row per location, so there is nothing to collapse), both CSV writers carry
address fields so the derived id stays stable regardless, and
`_merge_reenriched_fields` applies only the fields a re-enrich owns. The
allowlist fails safe in the right direction: an unlisted engine field goes stale
rather than being destroyed.

## Sales Handoff audit (2026-08-20) — remaining items

### 28. A floored Contender carries no "elevated by" context [OPEN]
The client handoff renders no tier reason at all, while the internal one does.
A `floor_tier`-lifted Contender therefore arrives with no statement of what
qualified it. Now that finding 24 is fixed these records actually reach the
deliverable, so the gap is live rather than theoretical.

**Blocked on an engine field.** `exclusion_checker._assign_tier` computes
`floor_rank` with a `max()` and discards which signal did the lifting, so the
badge cannot be built from current output. `tier_cap_reason` is the wrong
carrier — it explains why a record is *not* Bullseye, not why it was elevated.

**Fix (not started):** record the lifting signal on the record, then surface it.
Touches the output schema (RULE 4: `PIPELINE.md` plus the `enrichment/scorer.py`
validator in the same change) before any template work.

### 29. Contact-detail fallback on the client card [CLOSED — no action, 2026-08-20]
Audited claim: a missing phone or address renders blank/`N/A` with no directory
fallback. **Not what the code does.** A missing phone renders "No phone listed",
and the card still carries the website link and a Google Maps **Directions**
link built from practice name + city/state/zip
(`sales_export._directions_url`). The one-click fallback already exists.

The residual case is a record with no location context at all, which
deliberately gets no link rather than an ambiguous destination. Surfacing an
NPPES link was declined: NPI is internal provenance, and the client handoff
excludes internal fields by design. Recorded so it is not re-litigated.

### 30. Narrative clearing is all-or-nothing per record [OPEN — accepted trade-off]
Finding 26 clears every angle when any confirmed signal is retracted, even if
four others still stand. The narrative is composed as a set weighing all signals
at once, so one claim cannot be cleanly excised from it. This costs rep value on
records where most evidence survives.

Accepted deliberately: a caveated false claim in a client deliverable is still a
false claim. Recovery paths exist ("+ Add Angle", or the Re-extract Signals pass,
which regenerates properly against the corrected evidence). Revisit only if
operators report the loss is material in practice — the alternative is
per-bullet text matching against signal labels, which fails in both directions.

### 32. [RESOLVED 2026-08-20] One practice behind one front desk shipped as two accounts
Pass 1 blocked only on `(zip5, street)`, so two offices of one group in different
towns shared no key and were **never compared**. Nothing rejected the merge; the
comparison never happened. Found on a real record: one OBGYN group with offices
in two neighbouring towns, one phone, one website, identical provider lists and
identical scores of 42. Both were crawled and both ran a full Claude extraction,
so the only difference between the two cards was the sales angles — two
independent LLM calls disagreeing on the same page text. The practice has four
offices; a full list would have billed four times for one account.

**Fixed:** a second candidate path, the contact block `(phone, registrable
domain)`. Both halves required — phone alone compares everything behind an
answering service, domain alone every clinic in a health system. Together they
score exactly `MERGE_THRESHOLD`, so the path adds merges and **never review
work**: a pair reaching it cannot land in the review band. Umbrella domains are
excluded here even though the address block counts them as merge evidence, since
that exemption rested on street and ZIP having already pinned the location.
Blocks over `MAX_CONTACT_BLOCK` (12) rows are a shared line, skipped and counted.
The unit veto still applies. Off via `consolidation.contact_blocking: false`.

Verified on six shapes: the Atlanta pattern merges; two health-system clinics on
a central line, two rows behind a directory listing, two offices with their own
numbers, and two suites in one building all stay split; a 15-row answering-service
block is skipped and reported.

**Consequences to hold.** The quoted merge rates in `CLAUDE.md` are superseded
and marked so — they are now floors, not current behaviour. And a merged location
carries one address, so the second office's street is no longer on the record
(`source_row_ids` keeps the provenance). Preserving member addresses is a schema
change and is not done.

**Partially closes 23:** a street-less row now has a second way to be reached.
`unblocked_count` still reports the address-path gap unchanged;
`unblocked_rescued_by_contact` reports how many of those the contact path
actually compared.

### 31. A signal override cannot be removed, only re-stated [OPEN]
Found while documenting finding 26. `signal_overrides` is carried forward
verbatim by every review path — `save_review`, `stamp_reenriched`, and the QC
reset, whose docstring says so explicitly — and no post-run pass touches it.
There is no UI control to delete one; the edit form only writes a new
`override_state`.

Consequence, now that an override also withdraws copy: an operator who
overrides a signal and later wants the record back to pipeline behaviour has no
"revert to pipeline" action. Re-extract Signals regenerates the angles, but the
standing override re-clears them unless the fresh extraction happens to agree.
The workaround is to override the signal to the state the pipeline would have
produced, which records an operator decision that was never made.

**Fix (not started):** a delete path on `save_signal_override` plus a clear
control on the signal row, restoring the pipeline state and its original
evidence. Small and self-contained; `original_state` is already captured on
first override, so the pipeline value is available to restore to. Worth doing
before override volume grows.

## Post-ship audit of the contact-block cluster (2026-08-20) [ALL RESOLVED]

A same-day adversarial audit of commits `f64d376`–`cc95e9d`, run by executing
the engine over synthetic pairs and a full `--ingest-only` pass rather than by
re-reading the diffs. Four defects, all fixed in one commit with pinning tests.

### 32. [RESOLVED 2026-08-20] Cross-building merges stitched frankenaddresses
`_merge_cluster` filled the base row's empty fields per-field from any sibling.
Safe while every cluster member shared a building (the address block guaranteed
it); the contact path merges across buildings, where it put building B's suite
on building A's street — and a wrong-town ZIP onto the base street — and shipped
an address that does not exist to the rep's Directions link and the practice_id
hash. **Fixed:** the five address fields fill as one fact — same-building donors
only when the base names its building; one donor's address adopted wholesale
when it does not. The same-building suite fill the unit ruling relies on is
preserved and pinned.

### 33. [RESOLVED 2026-08-20] A missing ZIP silently disarmed the suite veto
The building-scoped veto required strict street+ZIP equality, so same street +
differing suites + one absent ZIP merged — in direct and transitive form — on
exactly the malformed rows most likely to be duplicates. **Fixed:** the veto
(and the cluster guard, same rule) fires on same street unless two PRESENT ZIPs
disagree; two present-and-different ZIPs are positive evidence of two buildings
(one street name, two towns) and lift it.

### 34. [RESOLVED 2026-08-20] An empty state was confidently "outside geography"
`_check_geography` excluded any record whose `address_state` was empty — the
rationale read "Practice is in , outside target geography". A state column that
failed to map became clean-looking screening, invisible below the canary's 90%
line. Pre-existing, but ingest-time exclusions made it fire on every roster.
**Fixed:** absent state is not a confirmed mismatch, the same rule specialty
inference has always documented.

### 35. [RESOLVED 2026-08-20] The contact block's own meter was never persisted
`cross_address_merges` — the number the feature's effect is measured by — plus
`unblocked_rescued_by_contact` and `contact_blocks_skipped_oversized` existed in
the in-memory summary but were dropped by the run-log writer's field copy, so
RULE M1 had no authoritative source. **Fixed:** all three persist in
`run_log.json`'s consolidation block.

Also verified working, first end-to-end executions: `diagnose_consolidation.py`
(both `--compare` and `--domain` modes) and the full `--ingest-only` pass with
ingest-time structural exclusions, contact merge, and the reporting canary.

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
- **[RESOLVED 2026-08-20] `_recompute_counts_from_records` entrenched the
  API-side mirrored threshold** (`record_adapter._LOW_SCORE_MANUAL_REVIEW_THRESHOLD`
  mirroring `enrichment/constants`) into durable status.json. Filed here as a
  drift risk; it drifted before it was fixed — see finding 24. The mirror is
  deleted, not re-synced.
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
