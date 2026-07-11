# Golden Dataset Labeling SOP — Femasys (FemaSeed)

**Rubric version: `femaseed-rubric-v1`** — record this exact string as `rubric_version` in
every labels.json authored under this SOP. Bump it whenever a per-signal rule below changes;
the preflight refuses a dataset that mixes rubric versions.

How a human analyst assigns the `expected` signal states for a golden case that feeds
`eval_signals.py`. The point of the golden set is a trustworthy ground truth that catches
when a prompt edit or model swap degrades extraction. Garbage labels = a gate that lies.

## Core principle: label as the extraction engine, not as a clinician

You are emulating a strict, verbatim text-matching engine. **Inference is a failure.** Do not
use clinical judgment, the practice's reputation, Google, NPI, or what you "know" about the
practice. Decide each signal **only** from the captured `page.txt`, and only on the terms
below. If it isn't on the page, it doesn't exist.

Three states, never two:
- **`yes`** — a listed term appears **verbatim** in a services/clinical/billing context. Copy
  the exact quote as the evidence anchor.
- **`no`** — the page **explicitly denies** it (e.g. "we do not offer IVF", "insurance required").
- **`not_found`** — the default. Silent, adjacent-but-not-it, or education/blog-only.

> **Referral ≠ denial.** "We refer out for IVF" is **`not_found`**, not `no`. Reserve `no` for an
> explicit statement that the practice does not provide the service. (Both AI drafts of this
> rubric got this wrong; the engine's refer-vs-offer guard makes a referral `not_found`.)

---

## Per-signal rules (anchored to the live cartridge terms)

### 1. `cash_pay_signal` — MUST-HAVE
- **yes** — any of these appears verbatim: `cash pay`, `self pay`, `out of pocket`, `elective`,
  `cosmetic`, `aesthetic`, `med spa`/`medspa`, `membership`, `concierge`, `package pricing`,
  `financing`, `CareCredit`, `hormone optimization`.
  *Note: elective / cosmetic / med-spa / membership / concierge are direct triggers — a practice
  selling elective services has cash-pay capability even if "cash pay" never appears. `financing`
  and `CareCredit` fire BOTH this signal and `patient_financing_visible`.*
- **no** — explicitly states self-pay/cash-pay is unavailable ("insurance required").
- **not_found** — only insurance networks, copays, "pay your bill," or a billing portal; no
  elective/self-pay/financing language.

### 2. `fertility_services` — MUST-HAVE
- **yes** — verbatim: `fertility`, `infertility`, `trying to conceive`, `TTC`, `preconception`,
  `ovulation induction`, `reproductive health`, `fertility evaluation`, `fertility workup`.
- **no** — explicitly states it does not provide fertility care.
- **not_found** — general OB/GYN, prenatal/obstetrics, contraception. **"Family planning" alone
  is contraception → `not_found`** unless pregnancy-seeking language is present.
- **Edge:** a bare **"fertility specialist"** counts toward THIS signal (`yes`), not `rei_on_staff`.

### 3. `iui_listed`
- **yes** — `IUI`, `intrauterine insemination`, `artificial insemination`.
- **no** — explicitly states IUI is not offered.
- **not_found** — "fertility options/treatment" without naming IUI; IUD (different thing);
  ovulation induction without insemination.

### 4. `cycle_monitoring_listed`
- **yes** — `cycle monitoring`, `follicle monitoring`, `follicle tracking`, `monitoring ultrasound`,
  `serial ultrasound`, `ovulation tracking`.
- **no** — explicitly states it is not offered.
- **not_found** — bare `ultrasound` or `monitoring` alone; OB/anatomy ultrasound; menstrual
  "cycle tracking"; ovulation induction without monitoring.

### 5. `patient_financing_visible`
- **yes** — `CareCredit`, `Cherry`, `Sunbit`, `patient financing`, `financing available`,
  `apply for financing`, `payment plan(s)`, `monthly payments`.
- **no** — explicitly states no financing/payment plans.
- **not_found** — "we accept all major credit cards" (card processing, not financing);
  "pay your bill"; accepted-insurance lists.

### 6. `ivf_listed` — NEGATIVE / friction (a `yes` is a bad fit)
- **yes** — `IVF`, `in vitro fertilization`, `embryo transfer`, `egg retrieval`, `ICSI`.
- **no** — explicitly states IVF is NOT offered.
- **not_found** — a **referral** ("we refer for IVF") → `not_found`; IVF named only in
  patient-education/blog copy; IUI-vs-IVF comparison text.

### 7. `rei_on_staff` — NEGATIVE / friction
- **yes** — `reproductive endocrinologist`, `REI`, `board-certified in reproductive endocrinology`,
  `reproductive endocrinology and infertility`.
- **no** — explicitly states no REI on staff / all REI care referred out.
- **not_found** — bios listing only FACOG / MD / DO / OB-GYN. **A bare "fertility specialist"
  with no REI credential → `not_found` here** (it belongs to `fertility_services`).

---

## Site selection — 20 sites that make the gate meaningful

Pick from real captured runs; **never cherry-pick by current model output.**

| Bucket | n | Profile | Purpose |
|---|--:|---|---|
| Slam-dunk fits | 5 | OB/GYN with fertility + cash-pay/financing + IUI, no IVF | Baseline must-have yes-recall (misses = lost revenue) |
| Borderline | 4 | Fertility present, IUI / monitoring / cash-pay unclear | Calibrate the yes/not_found boundary |
| IVF/REI centers | 4 | Full REI clinics offering IVF | Exclusion-recall safety (misses waste rep time) |
| General OB/GYN | 3 | No fertility offering | True negatives across the board |
| Cash-pay, no fertility | 2 | Med-spa / aesthetics / hormone, no fertility | Isolates `cash_pay` from `fertility` |
| "Ghost" sites | 2 | Barebones "Women's Health" page, no services | Proves the model defaults cleanly to `not_found` |

**Denominator rule:** ensure **≥4 true `yes`** for *each* signal across the 20 (watch the rare
ones — `cycle_monitoring_listed`, `patient_financing_visible`). One or two positives makes that
signal's recall number meaningless. Swap a site in if a signal is short.

---

## Ground-truth integrity

- **Two analysts label independently, blind to the model's output.** Never label to match the
  extractor — that is how a golden set silently rots.
- Every `yes` requires a verbatim copy-paste anchor quote; every `no` requires the explicit
  denial/referral quote. Anchors are RECORDED in labels.json (`anchors.<signal_id>`) and
  MACHINE-CHECKED by the preflight: each must appear in `page.txt`, compared lowercase with
  whitespace collapsed (the evaluator's own `normalize_anchor_text` policy) — so reflowed
  whitespace never fails a correct anchor, and a paraphrase always does.
- Disagreements go to an adjudicator; record the resolution.
- A case is `reviewed: true` only when: all 7 labels filled, every `yes`/`no` anchored,
  ambiguities noted, `rubric_version` set to this SOP's stamp, and `page_sha256` set to the
  fingerprint of `page.txt` (`eval_signals.page_fingerprint`). If `page.txt` is ever edited
  afterward, the fingerprint mismatch invalidates the case until it is re-verified.
- **Never edit a label to make the model pass.** If labels and a correctly-behaving model keep
  disagreeing, that's a **cartridge** finding to reconcile (fix the term list), not a label to force.
- Real fixtures stay **local only** (gitignored — RULE 2). Only this SOP and synthetic demos ship.

### Enforced labels.json schema (preflight-checked before any token is spent)

```json
{
  "practice_name": "...", "website_url": "...", "specialty": "...",
  "address_city": "...", "address_state": "...",
  "notes": "labeling ambiguities / adjudication record",
  "reviewed": true,
  "rubric_version": "femaseed-rubric-v1",
  "page_sha256": "<eval_signals.page_fingerprint of page.txt>",
  "anchors": { "<signal_id>": "verbatim on-page quote for every yes and no" },
  "expected": { "<every ICP signal_id>": "yes | no | not_found" }
}
```

`python eval_signals.py --live` / `--check` / `--update-baseline` refuse to run until the
20-case set passes every check (count, key coverage, values, ≥4 yes per signal, metadata,
anchors, nonempty page.txt) — the failure output lists each violation. The two shipped
synthetic demos run under `--dev-dataset` only.

---

## Pass/fail thresholds (reviewed cases only)

| Gate | Floor | Why |
|---|---|---|
| Anchor rate (predicted `yes`) | **100%** | A `yes` without verbatim on-page evidence is a hallucination |
| Must-have yes-recall (`cash_pay`, `fertility`) | **≥ 95%**, ≤1 miss total | A missed must-have downgrades a real target |
| Exclusion-recall (`ivf_listed`, `rei_on_staff`) | **≥ 95%** | A missed IVF/REI sends a rep to pitch an IUI device to an IVF clinic |
| Other yes-recall (`iui`, `cycle`, `financing`) | **≥ 90%** | Needs ≥4 positives each to mean anything |
| Overall state accuracy | **≥ 85%** | Tolerates the occasional `not_found`/`no` slip |
| Yes-precision | **≥ 85%** | Investigate any signal under 80% (hallucination pressure) |

On 20 sites, **read raw miss counts, not just percentages** — one must-have miss can be enough to
block a ship. After the first clean `--live` run, set each floor a few points **below** observed
(`--update-baseline`, then relax) so normal model variance doesn't flap the gate.

---

*Feeds `eval_signals.py`. Run `python eval_signals.py --live --check` before any prompt edit or
`.env` model-snapshot change. Baselines are live-only and production-only:
`--update-baseline` refuses offline mode, refuses `--dev-dataset`, and refuses any dataset
containing an unreviewed draft.*
