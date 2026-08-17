# Bullseye — Client & Project Data-Boundary Model

**Scope:** Bullseye Medical Intelligence, `enrichment-pipeline` repository.
**Method:** Read-only architecture analysis (2026-07). Six parallel investigations of
the actual codebase, each claim traced to a specific file and line.

**Status (2026-08-17): Section H decided — fix-only implemented.** The registry
safety fix is live: the shared registry is identity + provenance only
(commercial fields never written, stripped from legacy entries on touch, and
removable wholesale via `pipeline-api/prune_registry.py`, which takes the
Phase 0 backup itself), the C-2 `include_excluded` bypass is closed, and
shared-ICP edits warn (C-3). The ClientPracticeRelationship capability upgrade
(Section D's right-hand box, Phases 1–3/5) was deliberately **not** built — see
the decisions recorded in Section H.

---

## Framing — read this before the rest

**This is not a multi-tenant access-control problem.** Bullseye is a single internal
tool where every operator already has equal access to everything, and nothing in this
design changes that. Conflating *"prevent Client A's commercial read on a practice
from silently becoming Client B's default belief about it"* (a data-modeling and
provenance problem) with *"prevent Operator X from seeing Client Y's data"* (an
access-control problem) is exactly the kind of scope creep that turns a lean internal
tool into SaaS infrastructure nobody asked for.

This document designs for the first problem only. Section G states plainly what it
does *not* license Bullseye to claim.

---

## A. Current-state data flow

One node in this system is genuinely global. Everything else already lives inside a
project or a run.

```
Project config (client_name: free text, icp_profile_id → shared store)
        │
        ▼
Run snapshot (project config + ICP frozen at run start — immutable)
        │
        ▼
8-step pipeline (crawl → extract → score → tier)
        │
        ├──▶ enriched_targets.json   (scores, tiers, signals, sales angles)  ─┐
        ├──▶ evidence/<record>/       (run-scoped, never reused)             ├─ all run-scoped,
        └──▶ reviews.json             (QC overlay, run-scoped)               │  confirmed clean
                │                                                            │
                ├──▶ Client deliverables (CSV / ZIP / published brief) ──────┘
                │      ✓ confirmed clean — reads only this one run
                │
                └──▶ "Update Registry" (explicit operator action only)
                            │
                            ▼
              ╔═══════════════════════════════════════════════╗
              ║  master_practice_registry.json                ║
              ║  ONE FILE — SHARED BY EVERY CLIENT             ║
              ║  identity fields  +  current_tier,             ║
              ║  bullseye_score, exclusion_status,             ║
              ║  enrichment_status — NO client field           ║
              ╚═══════════════════════════════════════════════╝
                            ▲
                            │ read-only match
                            │ (place_id / domain / phone / name+address)
                            │
              Market Radar upload (any project, any client)
                    classification only exposes label + opaque id
                    — never tier/score, confirmed by code trace
                            │
                            ▼
              "Send to Enrichment" — always starts fresh,
               zero inheritance from the registry (confirmed)
```

---

## B. Data-classification matrix

**Key:** `IDENTITY` globally reusable public fact · `COMMERCIAL` client-specific
intelligence · `PROJECT` project configuration · `RUN` run-specific immutable
artifact · `AUDIT` operator metadata · `AMBIGUOUS` requires a policy decision.

| Field / artifact | Today | Class | Note |
|---|---|---|---|
| `client_name` | free-text string, no uniqueness check (`projects.py`) | AMBIGUOUS | No `client_id` exists anywhere in the repo — confirmed by exhaustive grep. The one missing key everything else hangs off. |
| `project_id` | stable, slug-validated | PROJECT | Already the right shape. |
| Practice `id`, name, address, phone, website, `google_place_id`, NPI fields | ingested once, deterministic `id` hash | IDENTITY | Objectively true regardless of which client's ICP is applied. |
| `icp_profile_id` + profile file | flat store, zero ownership enforcement | AMBIGUOUS | Sharing across unrelated clients is fully unrestricted today — no guard exists. The seed profiles carry a decorative `"client"` string no code reads. |
| `bullseye_score`, `fit_signal_score`, `confidence_score`, `target_tier` | computed per-ICP, correctly run-scoped in output | COMMERCIAL | Correct in `enriched_targets.json`. Leaks into the global registry — see C-1. |
| Evidence Vault (page snapshots) | strictly `<run>/evidence/<record>/`, no reuse | RUN | Confirmed clean by exhaustive negative search. |
| Operator review (`reviews.json`) | analyst_note, override_tier, qc_status, signal_overrides | AUDIT | Correctly run-scoped; no global store exists. |
| Suppression / customer status | re-derived from a project-owned CSV on every run | COMMERCIAL | No durable cross-run memory exists at all — a gap, not a leak. |
| Sales angle, call brief | correctly run-scoped, never read by exports/registry | COMMERCIAL | Confirmed clean. |
| `exclusion_status` / reason / gate | reason+gate never reach the registry; bare status can via a dormant path | COMMERCIAL | See C-2 — gated behind an unused flag with no UI button. |
| Published briefs / deliverables | per-run; remote path already scoped by `client_slug` | RUN | Confirmed clean — an existing correct pattern worth extending. |
| Registry entries — identity fields | name, address, phone, domain, place_id, npi + `change_history` | IDENTITY | Correctly global. Keep as the one shared identity index. |
| Registry entries — `current_tier`, `bullseye_score`, `exclusion_status`, `enrichment_status` | stored on the same shared entry, last-writer-wins, no history | AMBIGUOUS | **The central finding.** Must move out of the shared entry — see C-1. |
| Run status (`status.json`) | client/ICP metadata denormalized, 3 independent copies per run | RUN | Never cross-validated — fragile, not yet harmful. |
| Exported artifacts (CSV, ZIP, manifest) | built strictly from one `(run_id, run_directory)` | RUN | Confirmed clean — zero registry reads in any export path. |
| Duplicate-resolution decisions | no merge feature exists — ambiguous matches flagged and skipped only | AMBIGUOUS | Constraint for whoever builds the merge UI. |
| Competitor interpretation (`competitive_brands`) | ICP-profile-level field | COMMERCIAL | Inherits the unrestricted-sharing risk of all ICP fields. |

---

## C. Contamination scenarios, ranked by confirmed severity

### 1. [RESOLVED 2026-08-17 — fields removed] Registry tier/score cross-client clobber
Any two clients whose runs resolve to the same practice — matched purely on
`google_place_id` / domain / phone / name+address, properties of the practice, not
the engagement — silently overwrite each other's `current_tier`, `bullseye_score`,
`exclusion_status`, and `enrichment_status` on every "Update Registry" click. No
client-attribution field exists to even detect it, and unlike identity fields (which
get `change_history` entries), these four have **no audit trail at all**.

Currently invisible: no registry-browsing UI exists, and the one reader (Market
Radar) happens not to surface these fields. The protection is *"this code doesn't
read those keys"*, not *"this code cannot reach them"* — a landmine that detonates
with the first registry browser, outcome-correlation view, or "we already know this
practice" panel.

### 2. [RESOLVED 2026-08-17 — bypass closed, API returns 400] Suppressed/excluded records could reach the registry via an unbuttoned path
Requires `include_excluded=True`, which the only UI form hardcodes to `False`;
reachable only via the raw JSON API. Even then only the bare EXCLUDED flag crosses —
never the reason text. Worth closing outright; no legitimate current use exists.

### 3. [MITIGATED 2026-08-17 — warn on save/import] ICP profile sharing was fully unrestricted, with no warning
Nothing prevents, or even logs, two projects with different `client_name` values
referencing the same `icp_profile_id`. An operator adjusting one signal weight for a
new engagement can silently re-score every other project pointed at that file.

### 4. [STRUCTURAL — FORWARD-LOOKING] Duplicate resolution has no client-boundary awareness because it doesn't exist yet
Ambiguous registry matches are flagged and skipped (verified byte-for-byte-unchanged
by test). Constraint for the future merge UI: keep *"these are the same practice"*
(identity, correctly global) structurally separate from any commercial
interpretation.

### 5. [CONFIRMED CLEAN] Evidence, exports, deliverables, discovery→enrichment handoff
Evidence Vault strictly run-scoped, no caching. Exports read only the one
`(run_id, run_directory)`. Published briefs namespace by `client_slug`. "Send to
Enrichment" starts at zero — no inheritance of prior tier/score/registry state.
Named so effort isn't spent fixing what already works.

---

## D. Proposed entity model

One new entity. One new field on an existing table. Everything else keeps its shape —
the registry keeps doing its original job (identity matching) unchanged.

```
┌─────────────────┐
│ Client   [NEW]   │
│ client_id  (pk)  │
│ display_name     │
└────────┬─────────┘
         │ owns
         ▼
┌─────────────────────────┐        ┌──────────────────────────────────┐
│ Project                  │        │ ICPProfile                        │
│ project_id       (pk)    │──────▶ │ icp_profile_id            (pk)    │
│ client_id   (fk, NEW)    │  refs  │ version                            │
│ icp_profile_id   (fk)    │        │ owning_client_id (fk, NEW,        │
│ suppression csv (owned)  │        │   nullable — null = shared)       │
└────────┬─────────────────┘        └────────────────────────────────────┘
         │ produces
         ▼
┌─────────────────────────┐
│ Run                      │
│ run_id           (pk)    │
│ project_id       (fk)    │
│ frozen config + ICP snap │
└────────┬─────────────────┘
         │ writes (unchanged — already correctly run-scoped)
         ▼
  Evidence · QC (reviews.json) · Client Deliverables


        ┌───────────────────────────────────────────┐        ┌──────────────────────────────────┐
        │ ClientPracticeRelationship  [NEW — the fix] │        │ PracticeIdentity  [registry,      │
        │ client_id               (fk)                │──────▶ │  stripped of commercial fields]   │
        │ practice_registry_id    (fk)                │  refs  │ practice_registry_id      (pk)    │
        │ current_tier                                │        │ google_place_id                    │
        │ bullseye_score                               │        │ website_domain / phone_digits      │
        │ customer_status                              │        │ name / address (normalized)        │
        │ sales_angle summary                          │        │ npi · specialty                    │
        │ last_run_id             (fk)                 │        │ change_history                     │
        │ outcome_data  — reserved, not built           │        └──────────────────────────────────┘
        └───────────────────────────────────────────┘         Stays the ONE global, shared table —
        Derived, rebuildable index — replayable from          by design. A practice's address
        each run's own immutable output. Not a new             doesn't belong to any client.
        source of truth.
```

### The one honest fork in this design
The *safety fix* alone is smaller than the diagram: delete `current_tier`,
`bullseye_score`, `exclusion_status`, and `enrichment_status` from the registry entry
and store nothing in their place — that closes the leak completely on its own.
`ClientPracticeRelationship` is the *capability upgrade* bundled alongside: durable,
correctly-scoped memory of a client's read on a practice across runs, which doesn't
exist in any form today. These are separable — Section H asks which one.

---

## E. Migration plan

Mostly additive; the one place data cannot be safely recovered is named plainly.

- **Phase 0 — Backup.** Snapshot `master_practice_registry.json` and every run's
  `status.json`. *(reversible, zero risk)*
- **Phase 1 — Introduce `client_id` with a human in the loop.** `client_name` is
  free text; an automated slugify pass would merge/split clients on a typo. List
  every distinct value; an operator maps each to a canonical `client_id` by hand.
  `client_name` stays as display cache. *(manual review, backward compatible)*
- **Phase 2 — Backfill `client_id` onto existing runs** via `project_id → client_id`,
  written additively to each run's `status.json`. *(idempotent)*
- **Phase 3 — Rebuild ClientPracticeRelationship from the source of truth.** The
  registry's cached tier/score cannot be attributed to a client after the fact — no
  attribution field, no history. Ignore the corrupted cache; rebuild by walking every
  run's own `enriched_targets.json`, which knows its client via `status.json`.
  Nothing is lost — the real data was never corrupted, only the shared cache of it.
- **Phase 4 — Prune the registry to identity-only.** Matching logic unchanged.
  *(reversible via Phase 0)*
- **Phase 5 — ICP ownership, opt-in.** Nullable `owning_client_id`; existing profiles
  stay shared; prompt to fork only when editing a profile referenced by more than one
  live client. *(warn, don't block)*
- **Unresolvable records** get an explicit `client_id: "unassigned-legacy"` bucket —
  visible and flagged, never silently inferred.

---

## F. Application paths that become scope-aware

- **Registry update** — split the write: identity → PracticeIdentity (unchanged);
  tier/score/etc. → ClientPracticeRelationship keyed by the run's client. Remove the
  `include_excluded` bypass entirely.
- **Market Radar** — matching unchanged; additionally check
  ClientPracticeRelationship for `(this client, matched practice)` so "known"
  distinguishes *known to the platform* from *known to you*.
- **Suppression** — mechanism unchanged; also write resolved customer_status into
  ClientPracticeRelationship so it survives across runs.
- **Enrichment (run creation)** — `_prepare_run` resolves and validates client_id.
- **No change:** QC/reviews, exports/published reports, re-crawl paths (all already
  correctly scoped). **Duplicate resolution** — not built; constraint noted in C-4.

---

## G. Product claims

**With fix-only implemented (2026-08-17), may claim:** no client's
scoring/tier/exclusion read on a practice can be silently overwritten by
another client's engagement — because the shared registry stores no such
read at all; a client's commercial intelligence lives only in its own runs'
immutable output. **May NOT claim (fix-only):** per-client tracking across
runs, Market Radar "known to you", or per-client ICP ownership — those are
the unbuilt capability upgrade below.

**May claim after the full upgrade's implementation:** per-client tracking of scoring/tier/exclusion/
suppression/sales intelligence that cannot be silently overwritten by another
client's engagement; practice identity maintained once without exposing which other
clients evaluated it; Market Radar distinguishing "new to the platform" vs "new to
your engagement"; ICP models owned per client by default with sharing as a visible
choice.

**May NOT claim:** multi-tenant access control or "operators cannot see other
clients' data" (false — explicitly out of scope); compliance-grade isolation
(SOC2/HIPAA-style — this is data-modeling hygiene, no auth change); accurate
per-client pre-migration registry history (honestly unrecoverable; rebuilt from run
history, unresolved items flagged); cross-client evidence deduplication (correctly
absent — re-crawling per engagement is the right call).

---

## H. Business-policy decisions — DECIDED 2026-08-17 (operator: Rajiv)

1. **Who owns the one-time `client_name → client_id` reconciliation?**
   **Moot until an upgrade is scheduled.** Fix-only (decision 3) never runs
   Phase 1, so no reconciliation happens and no owner is needed. If the
   capability upgrade is ever green-lit, name the owner then — do not start
   Phase 1 without one.
2. **Shared-ICP edits: block or warn?** **Warn** (as recommended).
   Implemented: saving or importing over a profile referenced by 2+ live
   projects logs and surfaces a banner naming those projects
   (`projects.projects_referencing_icp`, `ui._shared_icp_warning`). Past runs
   are safe either way — they keep their frozen ICP snapshot.
3. **Fix-only, or fix + ClientPracticeRelationship?** **Fix-only.**
   Implemented: the four commercial fields are gone from the registry write
   path, stripped from legacy entries on touch, and prunable wholesale via
   `pipeline-api/prune_registry.py` (which writes the Phase 0 backup first;
   `--preview` supported; idempotent). The upgrade remains available later;
   nothing here forecloses it. Rationale: closing the confirmed leak is small
   and reversible, while the upgrade builds migration-heavy per-client memory
   before a second client exists to need it.
4. **May two Bullseye clients pursue the same practice simultaneously?**
   **Yes — allowed.** No tooling constraint; engagement overlap is a
   sales-ops/contract matter. Constraint for future features (registry
   browser, merge UI, outcome views): they must not assume exclusivity, and
   nothing in the product warns on overlap today.
5. **Should suppression/customer status become durable across runs?**
   **Deferred.** Suppression stays re-derived from the project-owned CSV per
   run — correct and client-scoped by construction. Durable status belongs
   inside ClientPracticeRelationship if that is ever built; a standalone
   store would recreate the shared-file mistake this fix removes.

---

## Confirmation

This document originally described analysis and a proposal with nothing
implemented. As of 2026-08-17 the Section H decisions above are made and the
fix-only scope is implemented (registry identity-only + C-2 bypass closed +
shared-ICP warning); the Section D capability upgrade and migration Phases 1-3/5
remain unbuilt by decision.
