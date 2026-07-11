# Signal-Extraction Evals (Golden Dataset)

The `tests/` suite proves the **code** is correct (scoring, tiering, exclusions) and is
deterministic by rule — no API calls. It cannot tell you when a **prompt change or a
vendor model update** makes the LLM start missing real signals. This harness covers that
gap: it runs labeled sites through the real extractor and measures extraction quality.

Run via `eval_signals.py` at the repo root. It is **opt-in** and **not part of `pytest`/CI**.

## Quick start

```bash
# Deterministic self-test — replays saved responses, no API, no spend:
python eval_signals.py --offline

# Enforce the baseline floors on the shipped SYNTHETIC demo set (exit 1 on
# regression). --dev-dataset is required: the demo set is 2 cases, not a
# production dataset, and the gate refuses to pretend otherwise:
python eval_signals.py --offline --check --dev-dataset

# The real thing — runs Claude on each page.txt (spends tokens). Requires a
# production-complete golden set (see "Dataset preflight" below):
python eval_signals.py --live --check

# Snapshot current numbers as the baseline. Live-only, production-only:
python eval_signals.py --live --update-baseline
```

Defaults to the Femasys cartridge (`config/clients/obgyn_femasys/`). Point at another with
`--icp <path> --config <path>`.

## Review status is authoritative

Only cases with `"reviewed": true` contribute to metrics. Drafts (scaffolded or
hand-started) are excluded from every aggregate, never spend live tokens, and are
reported as excluded-unreviewed; the report shows discovered / reviewed /
excluded-unreviewed / evaluated counts. `--check` fails while any draft exists —
even one that cannot execute. `--update-baseline` refuses unreviewed cases,
refuses `--offline` (a replay measures the recording, not the current extractor),
and refuses `--dev-dataset` (a demo set must never produce a production baseline).

## Dataset preflight (before any tokens are spent)

Gating modes (`--live`, `--check`, `--update-baseline`) validate the golden set
first and refuse to run — before any API call — unless it meets the production
contract from `LABELING_SOP.md`:

- exactly **20 reviewed cases**, no unreviewed drafts in the directory;
- every case's `expected` keys **exactly match** the current ICP's signal IDs
  (no missing, duplicate, unknown, or obsolete IDs), values only
  `yes` / `no` / `not_found`;
- **≥ 4 labeled-yes per signal** across the set (recall is meaningless below that);
- labeling metadata: one consistent `rubric_version`, and a `page_sha256` that
  still matches `page.txt` (catches text edited after labeling);
- every human-labeled `yes`/`no` carries its verbatim quote in `anchors`, and the
  quote appears in `page.txt` — compared lowercase with whitespace collapsed, the
  same normalization the evaluator's `anchor_rate` uses (`normalize_anchor_text`);
- `page.txt` nonempty.

`--dev-dataset` is the explicit non-production mode: it keeps the schema checks
(key match, value domain, nonempty page) and relaxes count, coverage, metadata,
and anchors. It prints a loud banner and is never valid with `--update-baseline`.

## What it measures

| Metric | Meaning |
|--------|---------|
| `state_accuracy` | Share of (case, signal) pairs whose `signal_state` matches the label. |
| `yes_recall` | Of signals labeled `yes`, how many the model confirmed (overall). **A miss is a lost target.** |
| `must_have_recall` | yes-recall over the must-have signals (`required_for_bullseye`). Gated. |
| `exclusion_recall` | yes-recall over the negative signals (`positive_weight < 0`, e.g. ivf/rei). A miss sends a rep to a disqualified account. Gated. |
| `other_recall` | yes-recall over the remaining positive signals. Gated. |
| `yes_precision` | Of signals the model called `yes`, how many were truly `yes` (fabrication guard). Gated. |
| `anchor_rate` | Of model `yes` signals, how many quote text that appears verbatim in `page.txt`. Gated at 100%. |

Groups are derived from the cartridge's own flags (no hardcoded signal IDs). Floors live in
`baseline.json` and mirror `LABELING_SOP.md`; `--check` fails the run if any floor is breached and
names the offending case + signal. A group with no labeled `yes` examples in the set is skipped (n/a).

## Adding a real case (the part only your team can do)

1. `mkdir evals/golden/<case_id>/` — or scaffold drafts in bulk with
   `python eval_signals.py --scaffold-from-run output/runs/<run>` (born `reviewed: false`,
   with `expected`/`anchors` prefilled from the run's own extraction as a DRAFT to verify,
   never as ground truth).
2. **`page.txt`** — the site text. Easiest source: copy a real **Evidence Vault** snapshot
   from a completed run (`output/runs/<run>/evidence/<record_id>/page-NN.txt`). Using the
   exact text the crawler saw makes the eval reflect production.
3. **`labels.json`** — practice fields, labeling metadata, and the ground truth:
   - `expected` — `yes` / `no` / `not_found` for **every** signal_id in the ICP;
   - `anchors` — the verbatim on-page quote for every `yes` and `no`;
   - `rubric_version` — the SOP version stamp the labels were authored under;
   - `page_sha256` — the fingerprint of `page.txt` (`eval_signals.page_fingerprint`);
   - `reviewed: true` — only after the LABELING_SOP.md review criteria are met.
4. **`recorded_response.json`** (optional) — only needed for `--offline`. After a known-good
   `--live` run you can save the model's signals here to replay deterministically later.

The production gate needs exactly 20 cases spanning the SOP's site-selection table, with
≥ 4 labeled-yes per signal. `--check` will list exactly what is missing.

## The two shipped cases are SYNTHETIC

`cedar_park` and `lone_star` are demo fixtures (not real practices) so the harness runs out
of the box. Replace them with real labeled sites before trusting the numbers.

## Pinning models (do this alongside the eval)

In `.env`, set `CLAUDE_MODEL` / `OPENAI_MODEL` to explicit dated snapshots, not floating
aliases. Upgrade intentionally: change the snapshot, run `python eval_signals.py --live --check`,
and only ship if it stays green. That is the whole point — the eval is what makes a model
upgrade safe.
