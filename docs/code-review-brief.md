# Code Review Brief — read before reviewing this repo

For an outside reviewer with no prior context. One page. Orientation, not
scripture: the last section names what you should actively challenge.

**What this is:** a Python CLI (`pipeline.py`) that turns a raw prospect list
into scored, tiered, evidence-backed account intelligence, plus a thin FastAPI
operator UI (`pipeline-api/`) that spawns it as a subprocess. Deliverables go to
paying clients, so a wrong record is a commercial problem, not a bug report.

---

## 1. Verify before you claim it

Three review passes have produced twelve findings. Two were real. The other ten
named a module that does not exist, described behavior the code contradicts, or
proposed a fix that a written rule forbids. All ten were disprovable in one
command.

```bash
python3 -m pytest tests/ -q          # 1,755 tests, deterministic, no network
grep -rn "<symbol>" --include=*.py . # does it exist / who consumes it
```

Before asserting a file duplicates another, open both. Before asserting
something is unused, grep for it. Before proposing an abstraction, check
`CLAUDE.md` and `pipeline-api/CLAUDE.md` for a rule against it. A confident
wrong finding costs more to refute than a hedged right one costs to confirm.

Say "I could not verify X" rather than inferring it. Unverified claims presented
as fact are the single most expensive thing a review can produce here.

## 2. Rules that look arbitrary and are load-bearing

**The API never imports the engine.** Communication is subprocess plus shared
files, deliberately. "Extract a shared module" is the most common proposal and
is always rejected. Where logic genuinely must exist twice, it is a declared
parity twin guarded by a test (`tests/test_matching_parity.py`).

**The engine owns tiering.** `pipeline-api` must never re-derive a tier from a
score or signals. It resolves the analyst override overlay and nothing else. A
display-layer copy of one engine rule silently withheld qualified accounts from
client deliverables for weeks — that is what this rule now exists to prevent.

**Filesystem JSON is the state store.** No database, no cache, no task queue, no
in-memory state surviving a restart. Proposals to "just keep an index in memory"
break crash consistency, which is the property the design buys.

**No hardcoded client, product, or specialty logic.** OBGYN, cash-pay, and
product names are cartridge config. A function branching on a specialty name is
a bug regardless of how clean it looks.

**Evidence is anchored.** A `"yes"` signal carries a verbatim quote and a source
URL, archived at capture. Anything that would let generated text stand in for
observed text is a defect, however useful it seems.

## 3. Decisions that look like bugs and are not

| Looks wrong | Why it is that way |
|---|---|
| `discovery/matcher.py` duplicates `pipeline-api/practice_matching.py` | Deliberate parity twin; the no-cross-import rule forces it. Guarded by `test_matching_parity.py`. Note `ingestion/practice_normalizer.py` is **not** part of this pair — it is intentionally stricter and must not be merged into either |
| The suite veto only fires within one building | A suite number means "which door in this building". Comparing suites across two towns vetoed exactly the multi-office practices that should merge |
| `count_active_runs()` scans the whole run archive under a lock | Real debt, documented, with a stated trigger. The full scan is load-bearing: a stuck run older than the display page must still count toward the cap |
| LLM rates are hardcoded in `llm_pricing.py` | Manually maintained by contract. `LAST_VERIFIED` reaches the UI as `cost_summary.rates_as_of` and renders as "estimate — rates as of {date}" beside every figure, so staleness is visible rather than silent. Display-only; nothing bills or throttles on it |
| `client_exports.py` writes no files | Everything is `io.BytesIO` streamed to the client. There is no export artifact bloat and no pruning needed |
| Inferred signals are absent from CSV evidence columns | An inferred signal has no verbatim text; quoting one would invent evidence |

`docs/review-backlog.md` is the authoritative list of known-and-accepted debt,
each item with a trigger. **Check it before reporting anything as new debt** —
most "findings" are already there, below threshold, on purpose.

## 4. What to actually attack

The code has been reviewed for internal consistency. It has **not** been
independently reviewed for whether its decisions are correct. That is where an
outside reviewer is worth far more than another consistency pass.

- **Run it, don't read it.** The last four real defects were found by executing
  the engine over constructed inputs, not by reading diffs. A reviewer who
  writes a harness will out-find one who reads.
- **The scoring model.** Fit-only scoring, the 50-point evidence floor, the
  confidence discount — inherited as given and never independently challenged.
- **The suite ruling.** Settled by thirteen sampled decisions, all merges, on a
  sample chosen from a list chosen by the same person. Small, and possibly
  biased. Worth re-deriving.
- **The merge thresholds.** `MERGE_THRESHOLD = 6` on an additive score. Is the
  additivity right? Is 6 right? Nobody has argued the alternative.
- **Untested-in-production surfaces.** Every verification to date is synthetic
  or fixture-based. No real client package, no real crawl, no live deployment
  has been inspected. Assume anything depending on real-world data shape is
  unverified.
- **Failure modes nobody has provoked.** Partial writes, concurrent operators,
  a truncated `enriched_targets.json`, a cartridge with contradictory flags.

## 5. Caveat on this document

Written by the assistant that implemented much of the recent work, so it encodes
that understanding — including its blind spots. Treat sections 2 and 3 as "here
is the reasoning, now judge it," not as settled fact. If a decision listed as
deliberate looks wrong to you after reading the code, say so. Several entries in
section 3 exist because an earlier version of that reasoning turned out to be
wrong and was corrected only when challenged.
