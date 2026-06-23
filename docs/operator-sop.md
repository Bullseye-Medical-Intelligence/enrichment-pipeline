# Bullseye Medical Intelligence — Operator SOP

**Standard Operating Procedure | Internal Use Only**
**Last updated:** 2026-06-23 · Engine build **1.4.0** · Reference cartridge **obgyn-femasys-v12**

---

## What You're Operating

Bullseye is a two-part system:

1. **Operator Dashboard (the web app)** — where you work. You discover prospects, upload lists,
   configure runs, review results, run verification, and export deliverables. Everything below
   happens in the browser. No terminal required.
2. **Enrichment Pipeline (the engine)** — a background process the dashboard triggers for you.
   It crawls practice websites, calls Claude to extract signals, scores each practice against
   your ICP, and tiers it. You never run it by hand.

Your job is entirely in the browser UI.

---

## The Operator Flow at a Glance

```
(optional) Market Radar → Build ICP Profile → Create Project → Upload List
   → Review Roster (no spend) → Enrich All → Recover Blocked Sites
   → Review & QC → (optional) Verify / Re-extract / Rescore
   → Approve → Export / Publish Deliverables
```

---

## Before Your First Run

1. **Get credentials.** Your username/password are set in the server `.env` by whoever runs
   the host. The app remembers your login for 8 hours.
2. **Log in** at the URL your team provided (e.g. `http://your-server:8000/login`). You land on
   the main menu: **ICP Profiles · Projects · Upload & Run · Dashboard** (plus **Market Radar**).
3. If a **System Health** banner appears on the dashboard, read it before running — it flags a
   missing API key, an unwritable output folder, or a missing profile. Green/hidden means healthy.

---

## Part 0 — (Optional) Market Radar: Discover & Triage

Use Market Radar when you want to screen a raw list before committing it to a full run.

1. **Market Radar → Upload**, select an Outscraper CSV.
2. Review the classified results (summary cards + table).
3. Select the records you want — **new**, **changed**, or a manual selection — and **Send to
   Enrichment**. This creates an *ingested* run for you, ready for "Enrich All."

If you already have a clean list, skip this and go straight to Upload & Run.

---

## Part 1 — Build an ICP Profile

An ICP profile defines the signals the pipeline looks for. Build one per product; reuse it for
every run on that product.

1. **ICP Profiles → Build New Profile.**
2. Fill in **Profile Identity** (Profile ID slug, Version, Profile Name) and **Product & Company**
   (company, product type, product name, target specialty, product description, key services,
   practices to exclude). Optionally paste the company and product **URLs** — Claude will read
   them so the draft is grounded in real marketing language.
3. **Generate Hypothesis & Signals** (≈30–45s). Claude drafts a commercial-fit hypothesis,
   synthetic demo accounts (one per tier), and a signal checklist (typically 8–12 signals).
4. **Review carefully.** This is a starting point, not a source of truth — a domain expert must
   approve the signals. For each signal you can edit:
   - **Signal Label** — short UI name.
   - **Prompt Instruction** — the question Claude asks; precise instructions produce precise
     answers.
   - **Weight** — relative importance. Rough guide: most important 25–30, strong positives
     15–20, nice-to-haves 5–10, **friction signals negative** (a "yes" is bad, e.g. `-20`).
     Weights are relative; they don't need to sum to 100.
   - **Must-Have** — a confirmed *absence* blocks Bullseye. Reserve for true deal-breakers.
5. **Score Simulator** (collapsible panel): set hypothetical signal states (Yes / Not Found / No)
   and run a dry simulation to see the resulting tier/score — no LLM, no crawl. Use this to
   validate weights *before* saving.
6. **Save Profile.**

> **Cartridges, weights, caps, and floors are how target behavior is tuned — not code.** If a
> client's targeting changes, it is a profile change, not an engineering ticket.

---

## Part 2 — (Pre-Pilot) Generate a Demo Brief

From the ICP Profiles list, click **Demo Brief** next to an AI-built profile to get a one-page,
three-example (Bullseye / Contender / Excluded) brief. Review it, then **Download HTML** (or
print to PDF) to send to a prospect. The examples are clearly labeled **synthetic** — never
present them as real practices.

---

## Part 3 — Create a Project

A project holds the run configuration for one engagement: geography, exclusion rules, and the
ICP profile to use.

1. **Projects → New Project.**
2. Set **Project Name**, **ICP Profile**, **Target Geography** (state codes), and **Active
   Exclusion Rules**.
3. **Exclusion rules:** always enable the hard structural ones (hospital-owned, health-system
   affiliated, wrong specialty, outside geography, no web presence). Enable specialty-specific
   rules only when relevant.

> Runs **snapshot** their project + ICP at launch. Editing a project later never changes a past
> run.

---

## Part 4 — Prepare the Prospect List

- **Outscraper (default):** export CSV including `name`, `full_address`, `state`, `city`,
  `postal_code`, `phone`, `site`/`website`, `type`.
- **Manual list:** minimum columns `practice_name`, `address_city`, `address_state`,
  `website_url`. Missing columns default to empty without erroring.
- **Size guidance:** 50–200 records ≈ 30–90 min depending on how many have websites. Split runs
  over ~500 or run overnight (single-host cap is ~1,000).

---

## Part 5 — Run Enrichment

1. **Upload & Run** (or **New Run** from the Dashboard). Pick the **Project**, set **CSV Source**,
   upload the file. The upload **validates the CSV before any spend** and shows the run as
   **ingested**.
2. **Review the roster first.** Ingested = loaded and structurally pre-filtered, **no budget
   spent.** Confirm the record count and that obvious mismatches already landed in Excluded.
3. Click **Enrich All.** Before it submits, the button shows an **estimated cost** for the run;
   click again to confirm. Leave **auto browser-retry** on for lists with many modern sites — it
   recovers bot-blocked sites automatically.
4. The pipeline runs in the background: ingest → URL validation → web extraction → **Claude signal
   extraction** (the longest step) → exclusion check → scoring → output. It **checkpoints after
   every record**, so a crash resumes where it stopped.
5. You can leave the page — a toast and a flashing tab title alert you when the run completes,
   even if you navigated away.

> **Note:** GPT verification is **not** part of this automatic run. It is a separate post-run
> action you trigger when needed (Part 7).

---

## Part 6 — Review Results & QC

Open the completed run from the **Dashboard**. You'll see tier stats (Bullseye / Needs
Verification / Contender / Manual Review / Excluded), a filter bar, and the record table.

**Tier meanings & your action**

| Tier | Meaning | Action |
|------|---------|--------|
| **Bullseye** | High score + all must-haves confirmed | Ready for outreach |
| **Needs Verification** | Candidate, a key signal unconfirmed | Verify before committing |
| **Contender** | Solid fit, some signals weak | Worth a call, lower priority |
| **Manual Review** | Nothing confirmable (often thin/blocked site) | Look before queueing |
| **Excluded** | An exclusion rule fired | Off the list |

**Recover blocked sites.** Records whose site was bot-blocked or returned too little text appear
in a dedicated **Site Blocked — Needs Re-crawl** section (not mixed into the scored table). Use
**Retry All with Browser** there, or select records and **Re-crawl with Browser**. For a single
stubborn CAPTCHA-walled site, open the record and use **Paste site content** to supply the page
text yourself. All re-crawls merge back into the **same run**.

**Per-record detail** shows the **call brief** (opening line, likely objection, discovery
question, hours), the **signal checklist** (yes/no/not-found with evidence text and source URL),
and the **score breakdown**. Each confirmed signal has an **Archived snapshot** (Evidence Vault)
link — the exact page text the crawler saw, with the quote highlighted and a capture date. Use
it when a site has since changed or a client questions a claim. Snapshots are internal only.

**Analyst overrides.** Disagree with a classification? Set an **Override** tier and enter a
reason. Overrides flow into the approved export but never change the underlying scores or the
immutable output.

**Confirm Queue & bulk approve.** Open **Confirm Queue** for the analyst sign-off view (Bullseye
+ Contender pending review). Use **Approve High-Confidence** or **Approve All** to clear the queue
in one write. **QC sign-off is required only for Bullseye and Contender** (the client-shipped
tiers); the others never block readiness.

**Contact Queue.** The rep call sheet, sorted by contact priority, with each call brief
pre-loaded — use it to drive a briefing session.

**Cartridge view & Run Economics.** **Cartridge** shows the exact frozen config the run used
(signals, weights, gates, geography). **Run Economics** shows records processed, LLM calls,
tokens, and estimated cost-per-record.

---

## Part 7 — (Optional) Post-Run Passes

From a completed run's header (**Reprocess ▾**), without re-uploading:

| Pass | What it does | Cost |
|------|--------------|------|
| **Verify** (GPT) | Independent second-model check on Needs-Verification records; recommends promote / hold / disqualify without overwriting scores. A promote still needs your override. | LLM |
| **Re-extract Signals** | Re-runs Claude against the frozen ICP using vault-rehydrated page text (no re-crawl). | LLM |
| **Preview Rescore → Apply Rescore** | Re-tiers with current weights. Preview shows tier changes before you commit. | None |
| **Re-check Suppression** | Re-applies the customer suppression list (only if the project has one). | None |
| **Re-crawl Blocked** | Browser re-crawl of all blocked/thin records, merged in place. | Crawl |

---

## Part 8 — Export & Publish Deliverables

From the run header:

| Deliverable | Contents | Use |
|-------------|----------|-----|
| **Client Package (ZIP)** | Bullseye target report (HTML), Sales Handoff (HTML), and Bullseye / Contender / Excluded CSVs | Send to the client |
| **Export Approved** | Analyst-approved, non-excluded records with overrides applied | Rep handoff |
| **Export Excluded** | Excluded records only | QA check |
| **Full CSV / JSON** | Complete output | Analysis / import |
| **Run Manifest** | Provenance summary (scope, ICP version, counts) | Internal record |

- The **Client Package requires every Bullseye and Contender to be reviewed first**, and **strips
  numeric scores** — clients see tier + confidence band only.
- **Publish** the **Sales Handoff** or **Sales Brief** to a stable shareable URL. Re-publishing
  overwrites in place so the link never changes. An **amber dot** on the Sales Handoff button
  means analyst edits post-date the last publish — re-publish to refresh.
- **Before shipping anything, run Check Evidence Links** (Audit ▾). It verifies every cited
  source URL in Bullseye/Contender records still resolves. A 404 in a client brief is a
  credibility failure — fix or override flagged records first. The check never mutates a record.

---

## Common Issues

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Run slow on web extraction | Many complex sites | Expected; check the run log for errors |
| Many `source_confidence = limited` | No/broken site, or bot-gated | Use the Site Blocked section → Retry All with Browser |
| Many records in Site Blocked | Bot protection | Retry All with Browser, or re-run with auto browser-retry on |
| Bullseye flagged for review | Models disagreed / unconfirmed must-have | Review evidence; run **Verify**; approve or override |
| Upload rejected | CSV format | Read the validation message — it names the missing/bad column |
| ICP builder errors | Claude key not set or API down | Tell your system admin to check `.env` |
| New cartridge not reflected | Profile version changed | A profile change loads on the next run; an engine/build change needs an **API restart** |

---

## Data & Compliance

- The pipeline uses **only public-facing data**: practice websites, Google Business profiles, the
  NPI registry, public directories. **Never** patient data, PHI, EMRs, or login-gated systems.
- Output files stay on the Bullseye server. **Never commit output CSV/JSON to git.**
- API keys live only in `.env` — never shared, never emailed.
- Demo-brief example accounts are **synthetic and labeled** — never present them as real.

---

## First-Run Checklist (New Engagement)

- [ ] (Optional) Market Radar: triage the raw list, send selected to enrichment
- [ ] Build ICP profile → review signals/weights → simulate → save
- [ ] Download demo brief (pre-pilot meeting)
- [ ] Create project (geography + exclusions + ICP linked)
- [ ] Pull/prepare the prospect CSV
- [ ] Upload → **review the ingested roster before spending**
- [ ] Enrich All (confirm the cost estimate; auto browser-retry on)
- [ ] Recover Site Blocked records (browser re-crawl / paste content)
- [ ] Review tiers; override misclassifications; (optional) run Verify
- [ ] Confirm Queue → approve Bullseye/Contender
- [ ] Check Evidence Links
- [ ] Export Approved (reps) / Client Package (client); publish Sales Handoff

---

*Bullseye Medical Intelligence — Internal Use Only.*
