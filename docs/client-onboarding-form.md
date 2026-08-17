# Bullseye Client Onboarding Form — Cartridge Intake

**Purpose:** this form collects everything Bullseye needs to build your
targeting cartridge — the set of signals, weights, and disqualifiers we score
every practice against. Your answers translate directly into the screening
engine; the more concrete they are, the sharper your shortlist.

**Who fills it in:** whoever knows your best customers best — typically a
sales leader or founder. Expect 45–60 minutes. Short answers are fine;
specific beats polished.

**What happens next:** we draft the cartridge from your answers, walk it back
to you signal-by-signal on a review call, run a small sample batch, and
calibrate together before the full run.

---

## The one rule that makes this work

> **Every signal must be something a stranger could confirm from the
> practice's public website in five minutes.**

Our engine reads public practice websites only — never patient data, claims,
or anything behind a login. A signal like "physicians who are early adopters"
can't be scored; "practice lists elective or cosmetic procedures on its
services page" can. Throughout this form, when you name a quality you want,
also tell us **what it looks like on a website**. We'll help translate on the
review call — but your first attempt at the translation is the most valuable
input in this document.

| Instead of… | Say… |
|---|---|
| "Progressive, entrepreneurial physicians" | "Lists elective/cosmetic procedures patients pay for out of pocket" |
| "Practices that value efficiency" | "Offers online scheduling / same-week appointments" |
| "Decision-maker owns the practice" | "Independent private practice — no hospital or health-system branding" |

---

## 1. Your product

- **Product name and one-sentence description:**
- **What does it replace or improve for the practice?**
- **How does the practice pay for it, and how do patients pay for the
  procedure it enables?** (cash-pay / insurance / hybrid — this often becomes
  the single most important signal)
- **Approximate price point / purchase model** (capital purchase,
  per-procedure, subscription):
- **Links:** product page, clinical evidence, anything a skeptical physician
  would ask for:

## 2. Your buyer

- **Medical specialty (or specialties) you sell to:**
- **Practice setting that fits best:** independent / small group / large
  group / hospital-affiliated — and which of these are *acceptable* vs *ideal*:
- **Who actually says yes?** (physician-owner, practice manager, clinical
  director, treatment coordinator…) And who champions it day-to-day?
- **Geography for this engagement:** states or metros, plus any regulatory or
  reimbursement differences by state that change how attractive a market is:

## 3. Must-haves — the gates

Think about calls that went nowhere because one fact ruled the practice out
from the start.

- **List up to five things that MUST be true before a rep should spend a
  dial.** For each, describe what it looks like on the practice's website.
- **Of those, which single one is the primary qualifier** — the fact that, if
  we can't confirm it, the practice shouldn't even sit in the call queue
  until a human checks?

## 4. Positive signals — what predicts a fast close

List every observable practice behavior or service that makes a close more
likely. For each, mark its importance:

| Signal (what it looks like on the website) | Importance (critical / strong / nice-to-have) | Why it predicts adoption |
|---|---|---|
|  |  |  |
|  |  |  |
|  |  |  |
|  |  |  |

- **Proxy evidence:** some facts are true but rarely printed (cash-pay is the
  classic — practices take cash but never say "cash pay"). For any must-have
  or critical signal above, what *indirect* evidence implies it? (e.g.
  "listing cosmetic procedures implies cash-pay capability")

## 5. Deal-breakers and red flags

- **Hard disqualifiers** — facts that mean *never call*, no matter what else
  is true (e.g. hospital-owned, telehealth-only, a competing program
  in-house). For each: what does it look like on the website?
- **Red flags** — bad but not fatal; the practice stays on the list but drops
  in priority. Which are they, and how bad is each?

## 6. Competitive landscape

- **Which competing products or brands matter?** For each: if a practice
  website mentions it, does that *disqualify* the practice, *lower* its
  priority, or actually *help* (proves budget and category awareness)?

## 7. Real examples — this calibrates everything

- **Five dream accounts:** real practices (name + website) that are perfect
  fits — current customers or ones you wish were. One line each on *why*.
- **Three near-misses:** practices that *look* like fits on paper but turned
  out wrong — and what a rep only learned later. These teach the engine what
  "looks good but isn't" looks like, which is where most wasted dials come
  from.

## 8. Existing customers and do-not-call

We exclude your current customers and any do-not-contact accounts *before*
any research spend, and they appear in your deliverables as excluded rather
than silently missing.

- **Attach a CSV** with one row per account: practice name, city, state, zip,
  and website if you have it. Column headers can be whatever your CRM
  exports — we'll map them.
- **Anyone else we should keep off the list?** (open deals, partners,
  accounts under contract dispute…)

## 9. The call itself

- **Who should the rep ask for**, in order of preference? (role names, not
  people — e.g. "treatment coordinator first; the physician-owner only if
  the coordinator is enthusiastic")
- **What context makes the door open?** The one line about the practice that,
  if a rep knew it, would earn thirty more seconds.

## 10. Calibration

- **Shortlist strictness:** would you rather have a *shorter list you can
  trust blind*, or a *broader list where some accounts need a confirming
  call first*? (There's no wrong answer — it sets how strict our top tier is.)
- **Anything we didn't ask** that your best rep knows about spotting a great
  account?

---

*Bullseye Medical Intelligence | leads@bullseyemedical.ai*
*Your answers stay within the engagement. We only ever use public-facing
practice information for research.*

---

## Appendix — Operator use only (remove before sending)

How answers map into the cartridge. Every mapping below is config, never
engine code (CLAUDE.md RULE 3).

| Form section | Cartridge / config destination |
|---|---|
| 1. Product (payment model) | Usually the top-weight signal; cash-pay products → a `cash_pay`-style signal with `required_for_bullseye` + `floor_tier` (see `config/clients/obgyn_femasys/icp_checklist.json` for the reference shape) |
| 2. Specialty | `run_config.target_specialty` + `wrong_specialty` structural exclusion; taxonomy codes → `taxonomy_exclusion_rules` if applicable |
| 2. Geography | `run_config.target_geography` (2-letter state codes) + `outside_geography` |
| 2. Setting (independent vs hospital) | `hospital_owned` / `health_system_affiliated` in `active_exclusion_rules`, or a `cap_tier` signal if soft |
| 3. Must-haves | Signals with `required_for_bullseye: true` (cap) — the primary qualifier gets `required_for_contender: true` (routes to Manual Review until confirmed) and usually `floor_tier` when confirmed |
| 4. Importance ranks | `positive_weight` tiers — critical ≈ 35–50, strong ≈ 15–25, nice-to-have ≈ 5–10; consider `not_found_weight` / `no_weight` on expected-but-absent must-haves |
| 4. Proxy evidence | `reinforces` on the proxy signal, pointing at the target signal's `signal_id` |
| 5. Hard disqualifiers | `exclude_if_yes: true` signals (with `inhibited_by` for mutually-exclusive pairs), or structural rules in `active_exclusion_rules` |
| 5. Red flags | Negative `positive_weight` (friction) and/or `cap_tier` |
| 6. Competitors | `competitive_brands` on the profile; `competitor_conflict` exclusion only if the client said "disqualify" |
| 7. Dream accounts / near-misses | `demo_accounts` on the profile; prime candidates for the eval golden dataset (`evals/`) and the first sample batch |
| 8. Customer CSV | `suppression_list_path` in run_config (Step 1c) |
| 9. Who to ask for | `contact_strategy` on the profile (feeds `key_contact` in the call brief) |
| 10. Strictness | `bullseye_min_score` and how liberally to apply `verification_required` |

Suggested workflow: paste sections 1–7 into the ICP builder as the product
brief, let it draft signals, then hand-tune weights and flags against this
table before the client review call. Validate weight choices with the Score
Simulator before saving the profile.
