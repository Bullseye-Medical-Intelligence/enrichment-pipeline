# BEMI Operator SOP
## Standard Operating Procedure for New Operators
**Bullseye Medical Intelligence | Internal Use Only**

---

## What You're Operating

BEMI is a two-part system:

1. **Pipeline API** — a web app you access in a browser. You upload prospect lists, configure runs, review results, and export deliverables from here.
2. **Enrichment Pipeline** — a background process the web app triggers automatically. It crawls practice websites, calls Claude and GPT to extract signals, scores each practice against your ICP, and tiers them. You never run this manually.

Your job as an operator is entirely in the browser UI. No terminal required.

---

## Before Your First Run

### 1. Get your login credentials
Your username and password are set by whoever manages the `.env` file on the server. Ask them for your credentials if you don't have them.

### 2. Log in
Navigate to the BEMI web app URL your team provided (e.g. `http://your-server:8000`). Enter your username and password. You'll land on the main menu.

### 3. Understand the menu
The main menu has four sections:
- **ICP Profiles** — define what a great prospect looks like for a specific product
- **Projects** — one per client engagement; holds geography and exclusion settings
- **Upload & Run** — where you submit a prospect CSV and kick off enrichment
- **Dashboard** — where you review results after a run completes

---

## Part 1 — Build an ICP Profile

An ICP (Ideal Customer Profile) profile defines the signals the pipeline looks for. You need one profile per product. Create it once; reuse it for every run on that product.

### Step 1: Open the ICP Builder
From the menu, click **ICP Profiles → Build New Profile**.

### Step 2: Fill in Profile Identity
| Field | What to enter | Example |
|-------|---------------|---------|
| **Profile ID** | Lowercase slug, used as the file name | `femaseed-obgyn-v1` |
| **Version** | Leave as `1.0` unless updating an existing profile | `1.0` |
| **Profile Name** | Human-readable label shown in the UI | `FemaSeed OBGYN ICP` |

### Step 3: Fill in Product & Company
| Field | What to enter | Example |
|-------|---------------|---------|
| **Company Name** | The manufacturer/vendor name | `Femasys` |
| **Product Type** | Select from the dropdown | `Medical Device` |
| **Company Website URL** | *(Optional)* The company's main site — Claude will read it | `https://femasys.com` |
| **Product Page URL** | *(Optional)* The specific product page — Claude will read it | `https://femasys.com/femaseed` |
| **Product Name** | The exact product name | `FemaSeed` |
| **Target Specialty** | Medical specialty of the target buyer | `OBGYN` |
| **Product Description** | What the product does, who buys it, deal size, sales cycle | *(see below)* |
| **Key Services / Focus Areas** | Procedures the ideal practice offers (one per line) | `IUD insertion` / `Infertility workup` |
| **Practices to Exclude** | Types of practices that are NOT a fit | `Hospital-employed, academic centers, wrong specialty` |

**Writing a good Product Description:** Answer these four questions in 2–3 sentences:
- What does this product do in plain English?
- Who is the clinical buyer (which physician type, what setting)?
- What problem does it solve for that buyer?
- What is the typical deal size and sales cycle?

> Example: *"FemaSeed is an in-office intrauterine insemination device used by OBGYNs for fertility treatment. It targets independent private practices where the physician performs IUI procedures themselves and is not affiliated with an REI center. Typical deal size is $X; sales cycle is 2–3 months."*

**Providing URLs:** If you paste in the company and product URLs, Claude will crawl the pages before generating the profile. This grounds the hypothesis in your actual marketing language and product claims — use it whenever possible.

### Step 4: Generate
Click **Generate Hypothesis & Signals →**. Wait 30–45 seconds. Claude will:
- Crawl the URLs you provided (if any)
- Write a commercial fit hypothesis (ideal practice profile, fit reasoning, fast-close indicators, common objections)
- Generate 3 synthetic demo account examples (one Bullseye, one Contender, one Excluded)
- Generate a signal checklist (typically 8–12 signals)

### Step 5: Review the output
The Review page shows everything Claude generated. Go through it carefully:

**Commercial Fit Hypothesis** (4 cells, read-only preview)
- Check that the ideal practice profile matches who you actually sell to.
- Check that the fast-close indicators match what your best reps look for.

**Demo Account Examples** (3 cards)
- These are synthetic (not real practices). They show how the scoring logic would classify a Bullseye, Contender, and Excluded account for your product.
- Review the reasoning. If a card's reasoning doesn't feel right, the signals may need tuning.

**Signal Checklist** (editable table)

Each row is one signal. The columns you can edit:
| Column | What it means | When to change it |
|--------|---------------|-------------------|
| **Signal Label** | Short name shown in the UI | Make it clear and specific |
| **Prompt Instruction** | Question Claude asks about each practice | Make it precise — vague instructions produce vague answers |
| **Weight** | How much this signal contributes to the score | Higher = more important to the deal |
| **Must-Have** | If checked, a confirmed *absence* of this signal prevents Bullseye | Use for true deal-breakers (e.g. cash pay required) |

**Rules of thumb for signal weights:**
- The most important signal for your ICP: 25–30 points
- Strong positive signals: 15–20 points
- Nice-to-have signals: 5–10 points
- Friction signals (where a "yes" is bad): negative weight (e.g. `-20` for "REI on staff")
- Weights don't have to sum to 100 — they're relative

**Add, remove, or reorder signals** using the +/× controls in the table. The order matters: list the most important signals first.

### Step 6: Save the profile
When the checklist looks right, click **Save Profile**. The profile is saved as a JSON file on the server. You'll be taken to the ICP Profiles list.

---

## Part 2 — Generate a Prospect Demo Brief

Before a client commits to a pilot, show them a one-page demo brief: three synthetic example practices, one per tier, with scoring reasoning. This is the pre-pilot sales deliverable.

### From the ICP Profiles list:
- Click **Demo Brief** next to a profile that has demo accounts (profiles built with the AI builder always have them).
- Review the HTML version in-browser.
- Click **↓ Download HTML** to get a self-contained HTML file you can send to a prospect (print to PDF from your browser if a PDF is preferred).

The brief shows:
- Your ICP description and commercial fit hypothesis
- A Bullseye example: what an ideal practice looks like and why it scores high
- A Contender example: a near-fit that needs a conversation before committing
- An Excluded example: the type of practice BEMI would surface and immediately flag out

> **Note:** All three example accounts are AI-generated and labeled as synthetic. They are not real practices.

---

## Part 3 — Create a Project

A project holds the run configuration for one client engagement: geography, active exclusion rules, and which ICP profile to use.

### Step 1: Go to Projects → New Project

Fill in:
| Field | What to enter |
|-------|---------------|
| **Project Name** | Client name + engagement descriptor | `Femasys Texas Pilot` |
| **ICP Profile** | Select the profile you just built | `femaseed-obgyn-v1` |
| **Target Geography** | State codes (comma-separated) | `TX, FL, GA` |
| **Active Exclusion Rules** | Check all that apply — see below | ✓ Hospital-owned, ✓ REI on staff |

**Exclusion rules explained:**
| Rule | What it does |
|------|--------------|
| Hospital-owned | Excludes practices employed by or owned by a hospital system |
| Health system affiliated | Excludes practices formally affiliated with a health system |
| Wrong specialty | Excludes practices that don't match the target specialty |
| Outside geography | Excludes practices not in the target states |
| Practice closed | Excludes practices with no web presence or closed indicators |
| Academic medical center | Excludes teaching hospitals and academic practices |
| REI on staff | Excludes practices with a reproductive endocrinologist on staff |
| No web presence | Excludes practices where URL validation and extraction both fail |

When in doubt, enable the hard exclusions (hospital-owned, health system affiliated, wrong specialty, outside geography) for every run. Enable specialty-specific ones (like REI on staff) only when relevant to the product.

---

## Part 4 — Prepare Your Prospect List

### Sourcing from Outscraper (default)
1. Search for your target specialty + geography in Outscraper.
2. Export as CSV.
3. Make sure the export includes: `name`, `full_address`, `state`, `city`, `postal_code`, `phone`, `site` (or `website`), `type`.
4. Save the file somewhere you can upload it.

### Using a manual list
If you have a pre-built CSV from another source (CRM export, analyst list):
- It must have at minimum: `practice_name`, `address_city`, `address_state`, `website_url`.
- Missing columns default to empty — the pipeline won't error.

**File size guidance:** Runs of 50–200 records take 30–90 minutes depending on how many practices have websites. Runs over 500 records should be split or run overnight.

---

## Part 5 — Run Enrichment

### Step 1: Upload & Run
From the menu click **Upload & Run** (or **New Run** from the Dashboard).

Fill in:
| Field | What to enter |
|-------|---------------|
| **Project** | Select the project you created |
| **CSV Source** | `Outscraper export` or `Manual (Bullseye canonical format)` |
| **Outscraper CSV File** | Upload your prospect list (max 10,000 rows / 50 MB) |

### Step 2: Review the roster, then Enrich All
Uploading loads and validates the list without spending any crawl or LLM budget — the run shows as **ingested**, with structural exclusions (wrong specialty, outside geography) already applied. Review the roster: confirm the record count looks right and the obvious mismatches landed in Excluded.

When the roster looks right, click **Enrich All**. The optional **auto browser retry** checkbox re-crawls bot-blocked sites with a headless browser automatically — leave it on for lists with many modern practice websites. The pipeline then runs in the background through 8 steps:
1. Ingest
2. URL validation
3. Web extraction
4. Signal extraction (Claude) ← longest step
5. Bullseye verification (GPT) ← only for high-scoring records
6. Exclusion check
7. Scoring validation
8. Output generation

Do not close the tab — you can check progress from the Dashboard if you do.

### Step 3: Wait for completion
A typical run of 50 records takes 20–40 minutes. The pipeline checkpoints after every record, so if it crashes you can resume from where it stopped.

---

## Part 6 — Review Results

### Opening a run
From **Dashboard**, click on the completed run. You'll see a summary: total records, tier breakdown (Bullseye / Needs Verification / Contender / Manual Review / Excluded), errors.

### Tier meanings
| Tier | What it means | Your action |
|------|---------------|-------------|
| **Bullseye** | High score + all must-have signals confirmed | Ready for rep outreach |
| **Needs Verification** | Scored as a candidate but a key signal is unconfirmed | Call to verify before committing |
| **Contender** | Solid fit but one or more signals are weak or missing | Warm — worth a call, but not the top priority |
| **Manual Review** | No signal could be confirmed (often a thin or blocked website) | An operator must look before it enters any call queue |
| **Excluded** | Hard or soft exclusion rule fired | Remove from outreach list |

Records whose website was bot-blocked or returned too little text appear in the dedicated **Site Blocked — Needs Re-crawl** section below the main table. Use **Retry All with Browser** there to recover them — many come back as scored Contenders or Bullseyes.

### Reviewing individual records
Click any record to open its full detail view. You'll see:
- **Call brief** — opening line, likely objection, discovery question, and hours of operation for the rep
- **Signal checklist** — what Claude found (or didn't) for each signal, with evidence text and source URL
- **Score breakdown** — fit score, confidence score, and composite Bullseye score

### Cartridge view
Click **Cartridge** in the run header to see a read-only view of the exact configuration the run used: the client identity, every ICP signal with its weight and must-have flag, the exclusion gates and tier caps, and the geography. "No geography restriction" and "Not configured for this ICP" are normal states for some clients, not errors. Use this when you need to confirm which weights or rules produced a result without opening config files.

### Run economics
Completed runs show a **Run Economics** line above the results: records processed, LLM calls, token totals, and an estimated cost with cost-per-record. The figure is an estimate at the rates noted next to it; older runs that predate token tracking say "cost data not captured for this run".

### Evidence Vault: Archived snapshots
Each confirmed signal shows an **Archived snapshot** link next to its evidence. It opens the page text exactly as the crawler captured it, with the capture date, a content fingerprint, and the evidence quote highlighted. Use it when a practice's website has changed since the run, or when a client questions where a claim came from — the snapshot is the proof. Snapshots are internal only; they are never sent to clients.

### Analyst overrides
If you disagree with how a record was classified:
1. Open the record.
2. Use the **Override** dropdown to set it to Bullseye / Needs Verification / Contender / Excluded.
3. Enter a reason. The override is saved immediately.

Overrides appear in the approved export. They do not change the underlying scores.

### Records flagged "needs_review"
These are Bullseye-tier records where Claude and GPT disagreed. An analyst must look at these before they go to the rep. Check the signal checklist and the `internal_notes` field to see where the models differed. Then either approve with an override or move to Contender.

### Contact Queue
Click **Contact Queue** from the run view. This shows the callable records sorted by contact priority, with the call brief pre-loaded. Use this view during a rep briefing session.

---

## Part 7 — Export Deliverables

From the run detail page, click the **Export** dropdown:

| Export | What it contains | When to use |
|--------|-----------------|-------------|
| **Download JSON** | Full schema with all signals, scores, briefs | For dashboard import or data analysis |
| **Download CSV** | Flat export — all records, signals omitted | For quick review in Excel |
| **Export Approved** | Only analyst-approved records, with overrides applied | Deliver to client or hand to rep team |
| **Export Excluded** | Only excluded records | QA check — confirm the right practices are out |
| **Client Package** | ZIP of 5 client-safe files: Bullseye Target Report (HTML), Sales Handoff (HTML), and Bullseye / Contender / Excluded CSVs | Send to the client contact |

**For a rep handoff:** use **Export Approved**. This contains only records that have been reviewed and approved, with the override classification and call brief for each.

**For a client check-in:** use **Client Package**. It requires every Bullseye and Contender record to be reviewed first; numeric scores are stripped — clients see tier and confidence band only.

**Before shipping any deliverable:** click **Check Evidence Links** on the run. It verifies every evidence link in Bullseye and Contender records still resolves and flags dead links and suspicious redirects. A 404 in a client brief is a credibility failure — fix or override flagged records before exporting. The check never changes any record; you decide what to do with the flags.

---

## Common Issues & What to Do

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Run stuck on Step 3 for >20 min | Many practices with complex websites | Wait — web extraction is slow. Check the run log for errors. |
| Many records with `source_confidence = "limited"` | Practices have no website or URL is broken | Expected for some lists. These records still get scored from what's available. |
| Bullseye records flagged `needs_review` | Claude and GPT disagreed on signals | Review manually. Check `internal_notes` on each record. |
| ICP builder returns an error | Claude API key not configured, or the API is down | Contact your system admin to check the `.env` configuration. |
| Run fails to start | CSV format issue | The upload validates the CSV before any spend — check the error message for which column is missing or malformed. |
| Many records in Site Blocked section | Practice websites use bot protection | Click **Retry All with Browser** in that section, or re-run Enrich All with the auto browser retry checkbox on. |

---

## Data & Compliance Reminders

- The pipeline uses **only public-facing data**: practice websites, Google Business profiles, NPI registry, public directories.
- It never accesses patient data, PHI, EMRs, or login-gated systems.
- All output files stay on the BEMI server. Never commit output CSVs or JSONs to the git repository.
- API keys live only in `.env` — never shared, never emailed.
- The synthetic demo accounts in the demo brief are labeled clearly and are not real practices. Do not present them as real.

---

## Quick-Reference Checklist: First Run on a New Engagement

- [ ] Build ICP profile (ICP Builder → review → save)
- [ ] Download the demo brief HTML (for the pre-pilot prospect meeting)
- [ ] Create a project (geography + exclusion rules + ICP profile linked)
- [ ] Pull Outscraper CSV for target specialty + geography
- [ ] Upload CSV — review the ingested roster before spending budget
- [ ] Click Enrich All
- [ ] Review results: Bullseye → Needs Verification → Contender → Manual Review
- [ ] Re-crawl any Site Blocked records with the browser retry
- [ ] Override any misclassified records
- [ ] Export Approved CSV for the rep team

---

*Bullseye Medical Intelligence | Internal Use Only*
*Last updated: June 2026*
