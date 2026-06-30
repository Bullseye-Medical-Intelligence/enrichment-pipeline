# Operator Workflow — Market Radar → Enrichment → Registry

This is the step-by-step operator workflow for taking a fresh market pull all the
way to an updated Master Practice Registry. It is written for a non-developer
operator. Nothing here spends crawl/LLM budget until **Step 6**.

## Quick mental model

- **Discovery finds** new/changed practices.
- **Enrichment scores** the ones you send.
- **The dashboard reviews** the scored results.
- **The registry remembers** what you explicitly approve.

Budget is only spent at "Enrich All" (Step 6). The registry only changes when you
explicitly click "Update Registry" (Step 8).

## Steps

### 1. Run / export Outscraper
Pull your target market from Outscraper (e.g. metro-Atlanta OBGYNs) and export
the CSV. Same format you'd use for a normal pipeline run.

### 2. Upload the CSV to Market Radar
In the dashboard, open **Market Radar** (top nav) → **New Discovery Run** →
choose your Outscraper CSV → **Run Discovery**. This is free: no crawl, no LLM.

### 3. Review the classification
The results page shows summary cards and a table classifying every row:

- **NEW** — not in the registry; a fresh candidate.
- **CHANGED** — matches a known practice, but a meaningful field changed (e.g.
  new website). The "Changed Fields" column shows what moved.
- **KNOWN** — already in the registry, unchanged. Not actionable.
- **POSSIBLE_DUPLICATE** — looks like another row in this same upload. Review
  before sending.
- **INSUFFICIENT_DATA** — too little identity info to place. Not actionable.

### 4. Send selected / new / changed to enrichment
Pick a **Project** (which carries the ICP) and enter your name, then choose:

- **Send NEW** — all new practices.
- **Send NEW + CHANGED** — new plus changed.
- **Send Selected** — only the rows you checked. This is the only way to send a
  POSSIBLE_DUPLICATE (deliberately). KNOWN and INSUFFICIENT_DATA can never be
  sent in bulk.

This creates a normal enrichment run **but does not start enrichment yet**.

### 5. Open the ingested enrichment run
You're redirected to the enrichment run page. Its status is **ingested** — the
roster is loaded and reviewable, but no budget has been spent. The page links
back to its source discovery run for traceability.

### 6. Trigger Enrich All
Review the loaded roster, then click **Enrich All**. **This is the step that
spends crawl + LLM budget.** The pipeline crawls each practice's public website
footprint and scores it against the project's ICP.

### 7. Review enriched results
When the run completes, QC the results: tiers (Bullseye / Needs Verification /
Contender / Manual Review / Excluded), confidence bands, evidence, and call
briefs. Override tiers where your judgment differs. GPT verification is a separate,
operator-triggered pass: run **Verify** from the completed run's header to get a
second-model check on Needs Verification records. It does not run automatically.

### 8. Explicitly update the Master Practice Registry
On the completed run page, click **Update Registry**. This is an explicit,
operator-only action — the registry is **never** updated automatically. The
conservative default adds *reviewable* records (CLEAR, not failed, not
needs-review); EXCLUDED and needs-review records are excluded. You'll see a
summary of inserted / updated / rejected / needs-manual-merge, plus an audit-log
path. Re-running is safe (idempotent): unchanged records won't duplicate history.

### 9. Export / report
Export the client deliverables (client package, CSVs, briefs) from the run page
as usual.

## What this workflow will not do for you

- It will **not** spend budget at upload, classification, or handoff — only at
  Enrich All.
- It will **not** change the registry until you click Update Registry.
- It will **not** auto-promote a record because GPT agreed — tiers still require
  your QC sign-off for client-shipped tiers.
- It will **not** send KNOWN or INSUFFICIENT_DATA practices into enrichment.
