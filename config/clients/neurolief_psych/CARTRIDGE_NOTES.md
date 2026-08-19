# Neurolief Cartridge — v1 DRAFT Notes

**Status: DRAFT — not run-ready until the review call.** Built 2026-08 from the
client onboarding form, which came back partially filled. Everything the client
stated is honored; everything marked ⚠ below is operator judgment that needs
client confirmation.

## What the client told us (form answers, verbatim or near)

- **Specialties:** Psychiatry, Primary Care, Neurology.
- **Setting:** "All (independent/small group/large group/hospital-affiliated)."
- **Decision-maker:** "Depends — can be physician driven but not always; larger
  practices could have a business manager or clinical director."
- **Geography:** "Northern CA to start."
- **Must-haves:** "Interventional psych therapies / Cash pay / Commercial
  Insurance accepted." Primary qualifier: "Interventional psych therapies."
- **Disqualifiers:** declined — "too early to rule out certain models."
- **Red flags:** declined — "we need to be open as this is a prescription
  device. Depression is co-morbid."
- **Competitors:** "Any TMS companies, Spravato, ECT or VNS would help — they
  are readily used to interventional products and might be open to a
  discussion about Neurolief." (Presence HELPS — scored as positives.)

## Operator judgment calls (⚠ = confirm on review call)

1. ⚠ **Commercial insurance softened to `verification_required`, not a
   Bullseye must-have.** The client listed it as a must-have, but interventional
   practices routinely accept commercial insurance without listing payers, and
   the client's "be open" guidance argues against double-gating payment. As
   configured: interventional + cash-pay confirmed but insurance unstated →
   Needs Verification (call to confirm), not blocked. Alternative if the client
   insists: add `required_for_bullseye: true` to the insurance signal.
2. ⚠ **No negative signals and no hard disqualifiers** — per the client's
   explicit refusal. Consequence: nothing caps a tier and only the engine's
   built-in gates exclude. Revisit after the sample batch: telehealth-only
   practices are the likeliest first candidate if reps find them unworkable
   (note: a home-use prescription device may make telehealth-only viable —
   client to decide).
3. ⚠ **Geography is state-level (CA).** "Northern CA" cannot be expressed in
   `target_geography` (2-letter state codes) — scope the Outscraper export to
   Northern-CA metros at list time; the engine will then only exclude
   out-of-state strays.
4. **`target_specialty` is a deliberately broad comma-token list** (the engine
   matches any token). With three target specialties, narrow tokens would
   structurally exclude generically-labeled practices ("Doctor", "Medical
   Clinic", "Mental Health Clinic"). The broad list means `wrong_specialty`
   pre-excludes only confirmed OTHER specialties (dentist, chiropractor, …);
   generic labels pass through and the interventional-psych gate does the real
   qualification.
5. ⚠ **Engine hard exclusions still apply:** `practice_closed` and
   `academic_medical_center` fire whenever the LLM detects them, and are not
   configurable per engagement. The client accepts hospital-*affiliated*
   practices (so `hospital_owned` / `health_system_affiliated` are NOT
   activated), but a full academic medical center will still be excluded.
   Confirm that boundary is acceptable.
6. **Ketamine reinforces cash-pay** — infusion therapy is predominantly
   patient-pay, the classic rarely-printed cash-pay proxy. A ketamine practice
   with no cash-pay copy gets cash-pay inferred (partial credit, no
   verification gate) rather than parked in Needs Verification.
7. **`bullseye_min_score: 80`** (reference default). The client skipped the
   strictness question — calibrate after the sample batch.

## Still missing from the form (blocking full confidence, not a sample run)

- Section 1 entirely: product description, price point, purchase model,
  evidence links. (Needed for the sales-angle quality, not for scoring.)
- The ranked signals table (section 4) — draft signals were derived from
  sections 3 and 6 instead.
- Dream accounts and near-misses (section 7) — nothing to calibrate against or
  seed evals with. **Ask again on the review call; highest-value missing input.**
- Customer/do-not-call CSV (section 8) — no `suppression_list_path` set.
- Contact strategy specifics (section 9) — drafted from the section-2 answer.

## Scoring shape (simulated through the real engine, `simulate_icp.py`, bullseye_min 80)

| Scenario (confirmed "yes" at high confidence) | Tier | Score |
|---|---|---|
| Interventional only | Contender (floored) | 58 |
| Interventional + cash-pay | Contender | 68 |
| All three must-haves, no modalities | Contender | 74 |
| Must-haves + insurance + TMS + Spravato | **Bullseye** | 88 |
| Everything except insurance (not_found) | Needs Verification | 89 |
| Interventional + TMS + ketamine (cash not printed) | Contender — cash INFERRED via ketamine | 73 |
| No interventional confirmed (any other combo) | **Manual Review** | — |
| All seven | **Bullseye** | 96 |

Reading: the three stated must-haves alone make a Contender, not a Bullseye —
Bullseye additionally requires named-modality depth (a site saying only
"interventional psychiatry" generically is thinner evidence than one naming
TMS/Spravato/ketamine, and in practice the terms co-occur). If the client wants
must-haves-alone to reach Bullseye, drop `bullseye_min_score` to ~72 or raise
the must-have weights — review-call item.

## Suggested first run

Ingest-only pass over a Northern-CA psychiatry-weighted Outscraper export,
roster review, then a ~25-record sample enrichment before the full batch.

---

## product_context (added 2026-08-19 — PENDING CLIENT APPROVAL)

Sourced from prolivrx.com (home, /how-it-works/, /clinical-evidence-safety/,
/patients/, /about-us/) on 2026-08-19. Every sentence is verbatim-or-close from
the site; nothing inferred. Injected into the extraction prompt's generation
section only (sales_angle / call_brief) with fencing rules — it can never
influence signal states or evidence.

Deliberate omissions:
- **Payment/coverage** — the site states only "Coverage varies. Your doctor or
  care team can help you understand your options." Hooks must not assert how
  the device is paid for; the cash-pay signal remains about the PRACTICE's
  billing model.
- **"FDA-approved" is the site's own wording** — regulatory phrasing is the
  client's to own; get it approved as written or corrected by them.

Naming correction: **Neurolief is the manufacturer (the client company);
Proliv Rx is the product** ("Our flagship therapy, Proliv Rx" — about-us page).
Earlier repo artifacts had these inverted (sample_run.py demo fixed 2026-08-19);
check the live dashboard project's client/product fields match this orientation.
