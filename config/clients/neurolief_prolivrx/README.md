# Neurolief / Proliv Rx — ICP Cartridge

Client config for the Bullseye chassis. Two ICP profiles for one product.

## Product

Proliv Rx (Neurolief) is an FDA PMA Class III at-home brain neuromodulation
device (eCOT-AS) approved January 2026 as an adjunctive treatment for adults
with MDD who did not adequately respond to at least one antidepressant.
Physician-directed, home-delivered, with adherence and PHQ-9 tracking handled
by Neurolief. Prescribers may be MD, DO, PA, or NP.

## Why cash-pay is the spine of this ICP

TMS is the rare interventional-psychiatry treatment where insurance is the
default. Proliv Rx is brand new and PMA, with no established commercial
coverage policy as of mid-2026. So it is effectively a cash-pay product even
though it treats the same TRD population TMS does.

The ideal target therefore has BOTH:
1. The TRD patient pipeline that running TMS generates (the qualifier), and
2. A demonstrated ability to collect cash directly from patients without an
   insurer in the loop (the wedge).

A practice that only bills TMS through insurance has the patients but not the
payment muscle. A practice running TMS plus cash-pay lines (IV ketamine,
self-pay packages, concierge membership) has both. That second practice is the
Bullseye.

## Profiles

### 1. Cash-Pay Interventional Psychiatry (PRIMARY)
`icp_checklist_cashpay_psychiatry.json`

Independent, physician-owned psychiatry / interventional-psychiatry practices
offering TMS and a cash-pay service line. Standard practice-website sourcing.
This is the high-volume profile and the default.

Signal logic: TMS = qualifier (must-have). Cash-pay/out-of-network service
line = the wedge (must-have, the signal no competitor scores). Concierge/
membership = bonus. Hospital/health-system employment = friction.

### 2. Correctional Mental Health Program (SECONDARY)
`icp_checklist_corrections.json`

State DOCs, large jail systems, and the correctional-healthcare contractors
that run their mental health services, where a robust mental health program is
publicly documented. Proliv Rx fits a population that cannot access clinic TMS.

IMPORTANT: this profile sources from government and public-record signals
(DOC program pages, court-ordered settlements like Coleman v. Newsom,
contractor awards, procurement records) — NOT practice-website scraping. The
buyer is a DOC or a contractor (Centurion, Wexford, YesCare, VitalCore), not a
practice. Longer government procurement cycle. Build out the source-layer
adapter before running this at volume.

## Field schema note

These checklists use only verified-live signal fields:
`signal_id`, `signal_label`, `prompt_instruction`, `positive_weight`,
`no_weight`, `required_for_bullseye`, and `note`.

Fields documented in the product brief but NOT confirmed live by audit
(`cap_tier`, `not_found_weight`, `verification_required`, `reinforces`) are
deliberately omitted to avoid shipping phantom config. Where `cap_tier`
behavior was wanted (hospital affiliation capping at Contender), it is
expressed as a heavy negative `positive_weight` instead, with a note. If a
field audit confirms `cap_tier` is live, convert S-NLF-008 to use it.

## state_mandate_status

Does not apply. Proliv Rx is cash-pay nationwide. Read cash-pay readiness from
the service-line signals. Same pattern as the Angel Aligner elective cartridge.

## Open items before first run

1. Confirm the "TMS + cash-pay psychiatry" framing with Neurolief. The literal
   "primary care clinic with TMS" target is thin — TMS lives overwhelmingly in
   psychiatry and health systems, and health systems are a weak Bullseye fit.
   This cartridge targets the defensible reality (independent cash-pay
   interventional psychiatry), not literal primary care.
2. Verify the engine handles a cartridge with no state_mandate_status field
   cleanly (the Angel work should have proven this).
3. Reimbursement will change. When a major payer issues a Proliv Rx coverage
   policy, the cash-pay weighting (S-NLF-002) should be revisited.
   Build so that weighting can be tuned down without re-architecting.
4. Corrections profile needs a public-record source adapter before it can run.
