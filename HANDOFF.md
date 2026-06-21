# HANDOFF — Signal Override + Signal Filtering build

**Read this before any task.** It captures the real state of a 6-prompt build
(signal-override feature + signal-filtering) on the BEMI `pipeline-api`. Values
below were pulled by running git and reading the files, not from memory.

> **Note (2026-06-21):** This handoff predates later work. Since it was written,
> the run-results header was consolidated into dropdowns (Reprocess / Export /
> Audit) with a new bulk "Review All" QC action, the `required_for_contender`
> signal gate was added, the two Femasys cartridges were merged into one national
> `obgyn-femasys-v11`, the Angel Aligner / Neurolief / Right at Home concept
> clients were removed, and the dashboard "+N pts" badge was fixed to show only on
> confirmed signals. See `PROJECT_HANDOFF.md` → "Changes — 2026-06-21 session".

---

## 1. Repo + branch state

- **Active branch (checked out):** `main` (local). Per the repo's CLAUDE.md, local
  `main` is the working copy that tracks the remote dev branch
  `origin/claude/pensive-cray-MaWmt`. `git status` reports:
  `On branch main / Your branch is up to date with 'origin/claude/pensive-cray-MaWmt' / nothing to commit, working tree clean`.
- **Working tree:** clean. Nothing uncommitted.
- **HEAD commit (where all this work sits):** `69dcec2` — "Prompt 6: add
  client-side signal filtering to the analyst dashboard".
- **In sync?** YES. Both `origin/main` and `origin/claude/pensive-cray-MaWmt`
  point at `69dcec2` (verified via `git log --oneline -1` on each remote ref).
  Local `main` == both remotes.

`git log --oneline` (top of history — the 6 prompts of this build + the two
pre-build tasks from the same session):

```
69dcec2 Prompt 6: add client-side signal filtering to the analyst dashboard
e9b9e83 Prompt 5: propagate signal overrides into client-facing outputs
4f7b2f1 Add signal-override edit UI to the analyst dashboard (template only)   # Prompt 4b
71ceab6 Render signal overrides on the analyst dashboard (display only)        # Prompt 4a
8e4d8f4 Add signal-override API route (POST .../signal-override)               # Prompt 3
706776e Add signal-override data layer to the reviews.json overlay             # Prompt 2
14004a9 Strengthen Femasys FemaSeed ICP to v10: cash-pay and fertility phrase expansion
1805d1c Fix day mode text visibility in Sales Handoff template
```
(Prompt 1 was a read-only audit — no commit.)

### ⚠️ FLAG: direct push to `main`
- **Yes, we pushed directly to `origin/main`.** It was **intended and explicitly
  authorized by the human** (Rajiv) via an AskUserQuestion at the end of Prompt 6.
  The auto-mode classifier had blocked an earlier chained `git push ... main:main`;
  the human then chose "Push to origin/main", and `git push origin main:main`
  succeeded (`e9b9e83..69dcec2 main -> main`).
- **What is on `main` now vs the feature branch:** they are **identical** — both at
  `69dcec2`. There is no divergence and nothing on `main` that is missing from the
  feature branch.
- **Review status:** these commits were **not** PR-reviewed (no PR was opened — the
  task instructions say never open a PR unless asked). If a PR review of this work is
  desired, it has not happened. The push pattern used throughout was
  `git push origin main:claude/pensive-cray-MaWmt` (+ the one authorized
  `main:main`).
- **Other local branches (stale, ignore unless asked):** `claude/pensive-cray-MaWmt`
  (local copy, "behind 201"), `femasys-cashpay-signal`, and a worktree branch
  `worktree-agent-...`. The live work is on local `main` / the two remote refs above.

---

## 2. What was built, feature by feature

### Architecture (the non-negotiable spine — applies to BOTH features)
- Overrides are an **overlay in `reviews.json`**, never written to
  `enriched_targets.json`. `enriched_targets.json` is pipeline-owned and immutable
  from the API side (RULE in `pipeline-api/CLAUDE.md`).
- **Scores and tiers are never recomputed.** `bullseye_score`, `fit_signal_score`,
  `confidence_score`, `target_tier` flow through untouched. An override changes a
  signal's *displayed state/evidence/source only*. Re-scoring after an override is a
  **separate, deliberate operator action** (the existing "Apply Rescore" button →
  `rescore_run.py`), intentionally NOT wired to overrides.
- The merge is a pure function: `apply_signal_overrides(record, review)` reads
  `review["signal_overrides"]` itself and returns a NEW record dict (originals not
  mutated). A record whose review has no `signal_overrides` is returned unchanged
  (regression-safe).
- **Client/internal boundary:** the merged signal carries `is_override=True` for the
  internal dashboard badge. This marker — and any "override" wording — must NEVER
  reach a client-facing surface. Client output strips it (see `_strip_is_override`).

### FEATURE A — Signal Override (Prompts 1–5)

**`pipeline-api/schema.py`**
- `VALID_SIGNAL_STATES: frozenset[str] = frozenset({"yes", "no", "not_found"})` (line 161)
- `class SignalOverride(BaseModel)` (line 164) — Pydantic v2 model. Fields:
  `signal_id: str`, `override_state: str`, `source_url: str`,
  `override_note: str = ""`, `override_by: str = ""`, `override_at: str = ""`.
  Validators: `_signal_id_non_empty` (signal_id required), `_state_is_valid`
  (override_state ∈ VALID_SIGNAL_STATES), `_source_url_non_empty` (source_url
  required). Imports `from pydantic import BaseModel, field_validator`.

**`pipeline-api/reviews.py`** (docstring updated: "never WRITES enriched_targets.json",
may READ it read-only). New module constant `ENRICHED_TARGETS_FILENAME = "enriched_targets.json"`;
added `import record_adapter` and `SignalOverride` to schema import.
- `default_review() -> dict` (line 28) — now includes `"signal_overrides": {}`.
- `get_signal_overrides(run_id: str, record_id: str, run_directory: Path) -> dict`
  (line 198) — the signal_overrides map for one record, keyed by signal_id; `{}` when none.
- `save_signal_override(run_id: str, record_id: str, override: SignalOverride, run_directory: Path) -> dict`
  (line 209) — persists one override atomically; captures `original_state` from
  `enriched_targets.json` on first override of a signal, preserves it on re-override;
  stamps `override_at` server-side; returns the updated review entry.
- `apply_signal_overrides(record: dict, review: dict) -> dict` (line 257) — pure merge.
  For each overridden signal: sets `signal_state`, `evidence_text`
  (`override_note` or the literal `"Operator-verified"` when blank), `source_url`,
  and `is_override=True`. No-op when `review` has no `signal_overrides`.
- `_read_original_signal_state(record_id: str, signal_id: str, run_directory: Path) -> str`
  (line 290) — read-only lookup of a signal's current state from
  `enriched_targets.json`; `""` when not found; never writes.
- `save_review()` and `bulk_approve()` were updated to PRESERVE `signal_overrides`
  (and `extra_sales_angles`) so a normal review save doesn't clobber overrides.

**`pipeline-api/ui.py`**
- Import: `from schema import ReviewEdit, SignalOverride`.
- `_load_merged_records(run_id, status) -> list[dict]` (line 1172) — at **line 1196**
  calls `record = reviews.apply_signal_overrides(record, review)` before the record
  spread, so the dashboard renders overridden state. This is the Prompt 4a wiring.
- `_find_record_in_run(run_directory: Path, record_id: str) -> dict | None` (line 3055)
  — read-only record lookup used by the route.
- Route `POST /api/ui/reviews/{run_id}/{record_id}/signal-override` →
  `async def save_signal_override(run_id, record_id, override: SignalOverride, username=Depends(auth.require_session))`
  (decorator line 3074). 404 if run dir missing, 404 if record missing, 422 if the
  signal_id is not on the record. **`override_by` is overwritten from the session**
  (`override.model_copy(update={"override_by": username})`) — never trusted from the
  body. Returns `{"ok": True, "signal_override": <entry>}`.
- `_brief_stale(run_id, run_directory, brief_type) -> bool` (line 3276) — extended in
  Prompt 5 to also return True when any signal override's `override_at` is newer than
  the last publish (calls `brief_publisher.newest_signal_override_at`).

**`pipeline-api/templates/results.html`** (internal dashboard; Prompts 4a/4b)
- Override **badge** in both FOUND and NOT-FOUND signal groups:
  `{% if sig.is_override %}<span class="signal-polarity-tag" ...>Override</span>{% endif %}`.
- Jinja macro `signal_override_ui(rid, run_id, sig)` (top of content block) — inline
  edit form (State select defaulted to current state, required Source URL, optional
  Note, Save/Cancel, inline error). Element IDs keyed `sigedit-...-{rid}__{signal_id}`.
- JS `toggleSignalEdit(rid, sigId)` and `submitSignalOverride(runId, rid, sigId)` —
  POST body is `{signal_id, override_state, source_url, override_note}` (NO
  `override_by`); empty source_url is guarded client-side; `window.location.reload()`
  on success; inline error on failure.

**`pipeline-api/sales_export.py`** (Prompt 5 — client + internal handoff feed)
- `_strip_is_override(record: dict) -> dict` (line 369) — returns a shallow copy with
  `is_override` removed from every signal. Client boundary enforcement.
- In `_build_handoff_run(...)` (line ~353): each record is merged then stripped before
  building the Account: `merged = _strip_is_override(reviews.apply_signal_overrides(rec, review))`,
  then `_record_to_account(merged, tier_str, review)`.

**`pipeline-api/exports.py`** (Prompt 5 — client CSVs)
- In `_build_csv(...)` at line ~194: `merged = reviews.apply_signal_overrides(rec, review)`
  before building the row. Signals are a list and are excluded from CSV columns, so
  `is_override` physically cannot become a CSV header (belt-and-suspenders).
  `_HIDDEN_SCORE_COLUMNS` behavior unchanged.

**`pipeline-api/brief_publisher.py`** (Prompt 5 — staleness)
- `newest_signal_override_at(all_reviews: dict) -> str | None` (line 108) — newest
  `override_at` across all signal overrides in a reviews map, or None. Guards against
  `signal_overrides` being None.

### FEATURE B — Signal Filtering (Prompt 6) — internal dashboard ONLY
Client-side only. No backend file changed, no route added.

**`pipeline-api/templates/results.html`** (the ONLY file changed for this feature)
- In the `record_rows` macro: computes `_yes_sig_ns.ids` (signal_ids that fired
  `yes` OR `state_inferred`, mirroring the FOUND grouping) and stamps
  `data-signals-yes="{{ _yes_sig_ns.ids | join(' ') }}"` on each `.record-row <tr>`.
- Filter bar (`id="signal-filter-bar"`) rendered above `#results-table`, guarded by
  `{% if not is_ingested %}` and only when ≥1 signal fired yes. One `.sig-filter-btn`
  per distinct yes-signal (button text = `signal_label`, `data-sig-id` = signal_id),
  plus an `#sig-filter-all` button (class includes `active` server-side) and a
  `#sig-filter-count` span. A small template-local `<style>` block styles
  `.sig-filter-btn` / `.active` (NOT in style.css — kept inside results.html).
- Inline JS `window.toggleSignalFilter(btn)` + closure helpers: AND combine logic
  (a row shows only if it has ALL selected signals), "Showing X of Y practices" live
  count, "All" resets, collapses open detail rows on hide. No fetch, no localStorage.
  Operates on `#results-table .record-row` only.

**Grouping decision (and why):** `#results-table` is a flat, column-sorted table —
tier is a badge column, not a section header — so there are no tier sub-sections to
hide. The filter applies to `#results-table` only (where signals are the meaningful
lens). Blocked / Excluded / Rejected records live in separate tables with no/thin
signals and are intentionally untouched. The signal filter and the existing
stat-block filter are independent controls (consistent with the existing
`filterRecords` / `filterByQC` / `filterByStatBlock` in `static/app.js`, which each
independently call `_applyFilter`).

---

## 3. Test state

- **Full suite command:** `python -m pytest tests/ -q` (run from repo root
  `/home/user/enrichment-pipeline`). Deterministic — no network, no LLM, no subprocess.
- **Total: 1038 passing** as of `69dcec2`.
- **Lint:** `python -m pyflakes <touched .py files>` (templates are not linted).

**Test files added for this work:**
| File | Count | Covers |
|------|-------|--------|
| `tests/test_signal_overrides.py` | 15 | data layer (save/get/apply/original_state) |
| `tests/test_signal_override_route.py` | 10 | POST route (auth, 404/422, override_by from session) |
| `tests/test_signal_override_render.py` | 7 | dashboard render of overrides (4a) |
| `tests/test_signal_override_editui.py` | 9 | edit UI markup + JS (4b) |
| `tests/test_signal_override_exports.py` | 10 | client/internal outputs (5) |
| `tests/test_signal_filter.py` | 11 | filter bar + data attrs + JS (6) |
| `tests/test_icp_femasys_v9.py` | 18 | ICP v11 (updated earlier in session) |

**Load-bearing guard tests — DO NOT BREAK these (they encode the contract):**
- **Client boundary:**
  - `test_signal_override_exports.py::test_is_override_absent_from_handoff_account`
    (no `is_override` / "override" wording in the Account).
  - `test_signal_override_exports.py::test_csv_no_is_override_column_in_approved_export`
    and `::test_csv_excluded_export_no_override_marker`.
  - `test_signal_override_editui.py::test_edit_ui_absent_from_client_templates`
  - `test_signal_filter.py::test_filter_absent_from_client_templates`
- **No rescore:** `test_signal_override_exports.py::test_no_rescore_when_overlay_applied`,
  `test_signal_override_render.py::test_tier_and_signal_overlay_coexist`.
- **enriched_targets.json untouched (sha256 checks):**
  `test_signal_override_render.py::test_enriched_targets_untouched_by_render`,
  `test_signal_override_exports.py::test_enriched_targets_untouched_by_handoff_build`.
- **No-override regression (byte/deep-equal passthrough):**
  `test_signal_override_render.py::test_no_overrides_signals_unchanged`,
  `::test_zero_overrides_full_passthrough`,
  `test_signal_filter.py::test_no_filter_bar_when_no_yes_signals`,
  `::test_initial_render_hides_no_rows`.

---

## 4. Data contracts the next session must respect

### `reviews.json` shape (per record_id key)
```json
{
  "<record_id>": {
    "analyst_note": "", "override_tier": null, "override_reason": null,
    "qc_status": "pending", "reviewed_by": null, "reviewed_at": null,
    "extra_sales_angles": [],
    "signal_overrides": {
      "<signal_id>": {
        "signal_id": "<signal_id>",
        "override_state": "yes|no|not_found",
        "source_url": "https://...",          // required
        "override_note": "",                   // optional
        "override_by": "<session username>",   // server-set, never from body
        "override_at": "<ISO 8601 UTC>",       // server-stamped each save
        "original_state": "yes|no|not_found|"  // captured once, on first override
      }
    }
  }
}
```
- `original_state` is captured **once** from `enriched_targets.json` on the first
  override of that signal and **preserved** on re-override. Read-only reference.
- `default_review()` always includes `"signal_overrides": {}`.

### `SignalOverride` (schema.py)
Fields + validators as in §2. `override_state` ∈ {yes,no,not_found}; `signal_id` and
`source_url` non-empty. `override_by`/`override_at` default empty and are filled
server-side.

### Override route
- `POST /api/ui/reviews/{run_id}/{record_id}/signal-override`
- Auth: session cookie (`Depends(auth.require_session)`).
- Body: `SignalOverride` JSON — client sends `{signal_id, override_state, source_url, override_note}`.
- Responses: 404 (run/record missing), 422 (signal_id not on record), 200
  `{"ok": true, "signal_override": <entry>}`.

### Outputs: live vs static, client vs internal
| Output | Live/Static | Audience | Override behavior |
|--------|-------------|----------|-------------------|
| Dashboard `results.html` | live | internal | shows merged state + Override badge + filter bar |
| Internal Sales Handoff (`pdf_report.build_sales_handoff_html` via `build_sales_handoff`) | live | internal | reflects overrides; MAY show marker |
| Client Sales Handoff (`_build_client_handoff_html` → `render_handoff(client_facing=True)`) | live | **client** | reflects overrides; marker STRIPPED via `_strip_is_override` |
| Approved/Excluded/Bullseye/Contender CSVs (`exports.py`) | live | **client** | merged; no marker column; `_HIDDEN_SCORE_COLUMNS` stripped |
| Sales Brief (`build_sales_brief`) | live | client | methodology brief; no marker |
| Client package ZIP (`client_exports.py`) | live | **client** | 5 sanitized files; no marker, no scores |
- There are **no static rendered files** for these — all live-render from
  `enriched_targets.json` + `reviews.json` on each request, so an override shows up on
  the next render/publish automatically. `_brief_stale` drives the amber "re-publish"
  dot when an override post-dates the last publish.

---

## 5. Open / unfinished items (honest + specific)

- **Prompt 6 (signal filtering): DONE.** Built, tested (11 tests), committed
  (`69dcec2`), pushed to both remotes. It was the last thing in progress and is fully
  landed. Not behind a flag.
- **Direct-to-main push: RESOLVED.** Human authorized it; `origin/main == origin/
  claude/pensive-cray-MaWmt == 69dcec2`. No divergence.
- **UNVERIFIED — human has NOT personally eyeballed the client/internal boundary
  render.** The boundary is covered by automated tests (see §3 guards), and Prompt 5's
  validation step described a side-by-side, but Rajiv stated he would read the
  boundary tests himself and that confirmation is **not recorded as done**. Next
  session: if asked, render the same overridden record through the internal handoff
  (`build_sales_handoff`, marker allowed) and the client handoff
  (`_build_client_handoff_html`, marker must be absent) and show them side by side.
- **UNVERIFIED / known nuance — `apply_signal_overrides` sets `evidence_text` to the
  literal `"Operator-verified"` when `override_note` is blank** (reviews.py:282).
  Prompt 5's spec said an overridden signal with no evidence should show the
  *source_url only, never a fabricated quote*. The current string is `"Operator-
  verified"` (contains no "override" token, so boundary tests pass and it does not
  surface in client `confirmed_signals`, which are built from labels only). But it is
  technically a synthesized evidence string. Confirm with Rajiv whether
  `"Operator-verified"` is acceptable as internal evidence text or should be `""` for
  client paths. Low risk; not currently leaking to client surfaces.
- **Deliberate non-goals (do NOT "fix" these):**
  - Tier/score is NOT recomputed after an override — re-scoring is the separate
    "Apply Rescore" action. This is intended.
  - Filter combine logic is **AND only** (no AND/OR toggle) — deliberately not
    overbuilt for v1. An OR toggle is a possible future enhancement, not a bug.
  - Filtering is client-side only (no persistence across reload, no server filter
    route) — intended for v1.
  - Filtering applies to `#results-table` only, not the blocked/excluded/rejected
    tables — intended.

---

## 6. Guardrails that kept this build clean (keep them)

- **Scoped-prompt discipline.** Each prompt declared an explicit `SCOPE — TOUCH ONLY`
  list and a `DO NOT TOUCH` list, did a read-first audit before editing, and shipped a
  per-prompt regression guard test. Replicate this for any follow-up.
- **Pinned model, no silent fallback.** The build ran on a pinned model
  (`claude-opus-4-8`) so behavior didn't drift mid-build. Do not let the model
  silently downgrade between prompts of a multi-part build. (Model identity goes in
  chat replies only — never in commits/PRs/code, per session rules.)
- **Protected files / layers — feature work must NOT touch these** (they own scoring/
  tiering/signals; changing them is out of scope for override/filter work):
  - `enrichment/scorer.py`, `enrichment/exclusion_checker.py`,
    `enrichment/signal_extractor.py`, `enrichment/constants.py`
  - `config/clients/*` ICP configs (except the deliberate ICP-versioning task)
  - `enriched_targets.json` — never written by the API/overlay
  - `handoff_renderer/renderer.py` + its templates — the renderer receives an Account
    whose signals are already merged/stripped; do not change the renderer for this work.
  - `static/style.css` — kept untouched; override/filter styles live inline or in a
    template-local `<style>` block.
- **API never imports pipeline internals** (subprocess only) and **never re-scores**
  (`pipeline-api/CLAUDE.md` RULES 1–3). The overlay pattern (`reviews.json`) is the
  only sanctioned way to layer operator edits on immutable pipeline output.
- **Read project docs first.** Every session starts by reading root `CLAUDE.md`,
  `PIPELINE.md`, and `pipeline-api/CLAUDE.md`. They are canonical; if code conflicts
  with them, fix the code.

---

### Quick resume checklist for the next session
1. `git status` (expect clean, on `main`, up to date with
   `origin/claude/pensive-cray-MaWmt`).
2. `python -m pytest tests/ -q` (expect **1038 passed**).
3. Read root `CLAUDE.md` + `pipeline-api/CLAUDE.md` before touching anything.
4. If asked to verify the client boundary, do the side-by-side handoff render in §5.
5. Develop on `claude/pensive-cray-MaWmt`; do not push to `main` without explicit
   human authorization.
