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

# Enforce the baseline floors (exit 1 on regression) — use this as your gate:
python eval_signals.py --offline --check

# The real thing — runs Claude on each page.txt (spends tokens):
python eval_signals.py --live --check

# Snapshot current numbers as the baseline (do this from a real --live run):
python eval_signals.py --live --update-baseline
```

Defaults to the Femasys cartridge (`config/clients/obgyn_femasys/`). Point at another with
`--icp <path> --config <path>`.

## What it measures

| Metric | Meaning |
|--------|---------|
| `state_accuracy` | Share of (case, signal) pairs whose `signal_state` matches the label. |
| `yes_recall` | Of signals labeled `yes`, how many the model confirmed. **The business-critical one — a miss is a lost target.** |
| `yes_precision` | Of signals the model called `yes`, how many were truly `yes` (fabrication guard). |
| `anchor_rate` | Of model `yes` signals, how many quote text that appears verbatim in `page.txt`. |

Floors live in `baseline.json`. `--check` fails the run if any floor is breached and names the offending case + signal.

## Adding a real case (the part only your team can do)

1. `mkdir evals/golden/<case_id>/`
2. **`page.txt`** — the site text. Easiest source: copy a real **Evidence Vault** snapshot
   from a completed run (`output/runs/<run>/evidence/<record_id>/page-NN.txt`). Using the
   exact text the crawler saw makes the eval reflect production.
3. **`labels.json`** — practice fields + the `expected` `signal_state` (`yes` / `no` /
   `not_found`) for every signal_id, decided by someone who knows the ICP. This is the
   ground truth; spend the care here.
4. **`recorded_response.json`** (optional) — only needed for `--offline`. After a known-good
   `--live` run you can save the model's signals here to replay deterministically later.

Aim for ~20 cases spanning the tiers: clear Bullseyes, Contenders, an IVF/REI practice, a
thin/insurance-led site, and a couple of edge cases that have burned you.

## The two shipped cases are SYNTHETIC

`cedar_park` and `lone_star` are demo fixtures (not real practices) so the harness runs out
of the box. Replace them with real labeled sites before trusting the numbers.

## Pinning models (do this alongside the eval)

In `.env`, set `CLAUDE_MODEL` / `OPENAI_MODEL` to explicit dated snapshots, not floating
aliases. Upgrade intentionally: change the snapshot, run `python eval_signals.py --live --check`,
and only ship if it stays green. That is the whole point — the eval is what makes a model
upgrade safe.
